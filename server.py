"""Council — Multi-CLI orchestration server.

Single FastAPI + uvicorn process. Spawns codex / claude / copilot / gemini subprocesses
in parallel, streams their stdout line-by-line over WebSocket to the browser UI.

Run (Windows PowerShell):
    cd <path-to-council-repo>
    uv venv && .\\.venv\\Scripts\\Activate.ps1
    uv pip install fastapi "uvicorn[standard]" pydantic platformdirs
    python server.py

Run (macOS/Linux):
    cd <path-to-council-repo>
    uv venv && source .venv/bin/activate
    uv pip install fastapi "uvicorn[standard]" pydantic platformdirs
    python server.py

Then open http://localhost:8765
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import trust
from modes import MODES, ModeResult
from registry import CLIEntry, ensure_user_config_seeded, load_registry
from spawn import build_spawn_spec, extract_session_id
from spawn import spawn as spawn_subprocess

# ---- Paths ------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PROMPTS_DIR = ROOT / "prompts"
CONVERSATIONS = ROOT / "conversations"
STATUS_FILE = PROMPTS_DIR / "status.md"
STATUS_EXAMPLE = PROMPTS_DIR / "status.example.md"

for d in (STATIC, PROMPTS_DIR, CONVERSATIONS):
    d.mkdir(parents=True, exist_ok=True)

# First-run bootstrap: if user has no local status.md, seed from the example.
# status.md is gitignored — your private project context lives here.
if not STATUS_FILE.exists() and STATUS_EXAMPLE.exists():
    STATUS_FILE.write_text(STATUS_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


# ---- Validation helpers -----------------------------------------------------

# conv_id is user-influenced (via WebSocket path + REST API). Validate hard against
# any path-separator surprises before joining into CONVERSATIONS / conv_id.
_CONV_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_conv_id(conv_id: str) -> str:
    """Return conv_id if safe, else raise HTTPException."""
    if not conv_id or not _CONV_ID_PATTERN.match(conv_id) or len(conv_id) > 64:
        raise HTTPException(
            status_code=400,
            detail="invalid conversation id (expected [A-Za-z0-9_-]{1,64})",
        )
    return conv_id


# ---- CLI registry -----------------------------------------------------------
# Loaded from default_clis.toml (package) + <platform-config>/Council/clis.toml (user).
# See registry.py.

REGISTRY: dict[str, CLIEntry] = load_registry()
ensure_user_config_seeded()


# ---- Status injection -------------------------------------------------------


def load_status() -> str:
    """Read the current project status block, prepended to every prompt.

    Edit `prompts/status.md` to update. Always re-read (no cache) so the user
    can hot-edit during a session.
    """
    if STATUS_FILE.exists():
        return STATUS_FILE.read_text(encoding="utf-8").strip()
    return ""


def build_full_prompt(prompt: str, include_status: bool = True) -> str:
    """Compose final prompt: status block + user prompt."""
    if not include_status:
        return prompt
    status = load_status()
    if not status:
        return prompt
    return (
        "<!-- ALCYONE STATUS (injected by Council) -->\n"
        + status
        + "\n<!-- END STATUS -->\n\n"
        + prompt
    )


# ---- Conversation persistence ----------------------------------------------


class Conversation:
    """One conversation = one directory under conversations/.

    v0.4: per-CLI session ids (for `--resume`) live in `cli_sessions.json` so future
    turns of the same Council conversation continue each CLI's transcript on its end.
    """

    def __init__(self, conv_id: str) -> None:
        self.id = conv_id
        self.dir = CONVERSATIONS / conv_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "responses").mkdir(exist_ok=True)
        self.log_path = self.dir / "events.jsonl"
        self.sessions_path = self.dir / "cli_sessions.json"
        # Serializes concurrent set_cli_session() in parallel modes — Codex bot
        # P2 v0.4: two CLIs finishing at the same moment would each read the
        # store, modify in-memory, and write — the loser's write overwrote the
        # winner's session id. Single lock per Conversation keeps the
        # load → modify → write atomic from the event loop's perspective.
        self._sessions_lock = asyncio.Lock()

    def write_event(self, event: dict[str, Any]) -> None:
        event["ts"] = time.time()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_prompt(self, prompt: str) -> None:
        (self.dir / "prompt.md").write_text(prompt, encoding="utf-8")

    def write_response(self, cli: str, content: str) -> None:
        (self.dir / "responses" / f"{cli}.md").write_text(content, encoding="utf-8")

    def load_cli_sessions(self) -> dict[str, str]:
        """Per-CLI session ids saved from prior turns of this conversation."""
        if not self.sessions_path.exists():
            return {}
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    async def set_cli_session(self, cli: str, session_id: str) -> None:
        """Persist a CLI's session id for future turns.

        Async + lock-guarded so parallel CLIs that finish at the same moment
        can't lose one another's session ids in a read-modify-write race
        (Codex bot v0.4 P2). One Conversation instance = one lock, so each
        write reads the latest on-disk state under exclusion.
        """
        async with self._sessions_lock:
            store = self.load_cli_sessions()
            store[cli] = session_id
            self.sessions_path.write_text(
                json.dumps(store, indent=2, sort_keys=True), encoding="utf-8"
            )

    @classmethod
    def new(cls) -> Conversation:
        cid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        return cls.get_or_create(cid)

    # Per-process registry of Conversation instances keyed by conv_id.
    # Codex bot v0.4 P3: a fresh `Conversation(conv_id)` per WebSocket means two
    # tabs (or a reconnect overlapping the old connection) on the same id get
    # independent `_sessions_lock`s, defeating the asyncio.Lock that fixed the
    # earlier write race. Routing all accesses through `get_or_create` ensures
    # a single instance — and a single lock — per conv_id within this server
    # process. Multi-process write contention would still need file locks; out
    # of scope for v0.4 (Council runs one server process per user).
    _INSTANCES: dict[str, Conversation] = {}

    @classmethod
    def get_or_create(cls, conv_id: str) -> Conversation:
        existing = cls._INSTANCES.get(conv_id)
        if existing is not None:
            return existing
        inst = cls(conv_id)
        cls._INSTANCES[conv_id] = inst
        return inst


# ---- Subprocess streaming --------------------------------------------------


async def stream_cli(
    cli: str,
    prompt: str,
    ws: WebSocket,
    conv: Conversation,
    label: str = "",
    *,
    cwd: Path | None = None,
    options: dict[str, object] | None = None,
    session_id: str | None = None,
) -> str:
    """Spawn one CLI subprocess, stream its stdout/stderr line-by-line over WS.

    Sends JSON envelopes: {"cli": "codex", "kind": "stdout"|"stderr"|"done"|"error",
                           "data": "...", "label": "<round/phase tag>"}.
    Saves the full response to conversations/<id>/responses/<cli>__<label>.md.
    Returns the captured stdout text so consensus/debate modes can chain it.

    `cwd` is the project folder the conversation is bound to (trusted at routing time).
    None means use Council's own working directory.

    `prompt` is passed through verbatim — status injection happens in the dispatcher
    (`run_cli_wrapped` in `ws_stream`) so this function never double-injects.
    """
    entry = REGISTRY.get(cli)
    if entry is None:
        await ws.send_json({"cli": cli, "kind": "error", "data": "unknown CLI", "label": label})
        return ""
    if not entry.is_available():
        reason = "disabled or non-headless" if entry.disabled or not entry.headless_supported \
                 else "CLI not installed locally"
        await ws.send_json({"cli": cli, "kind": "error", "data": reason, "label": label})
        return ""

    try:
        spec = build_spawn_spec(
            entry, prompt, cwd=cwd or ROOT, options=options, session_id=session_id
        )
    except Exception as exc:
        await ws.send_json(
            {"cli": cli, "kind": "error", "data": f"option error: {exc}", "label": label}
        )
        return ""
    conv.write_event(
        {
            "kind": "cli_start",
            "cli": cli,
            "cmd": " ".join(spec.argv),
            "mode": spec.invocation_mode,
            "cwd": str(spec.cwd),
            "options": dict(options or {}),
            "resumed": session_id is not None,
            "session_id": session_id,
        }
    )
    proc = await spawn_subprocess(spec)
    if spec.invocation_mode == "stdin" and proc.stdin is not None:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

    captured: list[str] = []

    async def pump(stream: asyncio.StreamReader, kind: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            try:
                text = line.decode("utf-8", errors="replace")
            except Exception:
                text = repr(line)
            if kind == "stdout":
                captured.append(text)
            try:
                await ws.send_json(
                    {"cli": cli, "kind": kind, "data": text, "label": label}
                )
            except Exception:
                # Browser disconnected mid-stream; let the process finish but stop sending.
                return

    full_response = ""
    try:
        assert proc.stdout is not None and proc.stderr is not None
        await asyncio.gather(
            pump(proc.stdout, "stdout"),
            pump(proc.stderr, "stderr"),
        )
        rc = await proc.wait()
        full_response = "".join(captured)
        # Save with label suffix so multi-round modes don't overwrite
        suffix = f"__{label}" if label else ""
        (conv.dir / "responses" / f"{cli}{suffix}.md").write_text(
            full_response, encoding="utf-8"
        )
        # Extract session id from output if the CLI supports resumption.
        captured_session_id = extract_session_id(entry, full_response)
        if captured_session_id:
            await conv.set_cli_session(cli, captured_session_id)
        conv.write_event(
            {
                "kind": "cli_done",
                "cli": cli,
                "label": label,
                "rc": rc,
                "captured_session_id": captured_session_id,
            }
        )
        with suppress(Exception):
            await ws.send_json(
                {"cli": cli, "kind": "done", "data": f"exit={rc}", "label": label}
            )
    except Exception as e:
        conv.write_event({"kind": "cli_error", "cli": cli, "label": label, "err": str(e)})
        with suppress(Exception):
            await ws.send_json(
                {"cli": cli, "kind": "error", "data": str(e), "label": label}
            )
    return full_response


# ---- API models -------------------------------------------------------------


class SendRequest(BaseModel):
    """Payload schema for /ws/{conv_id} action='send' messages.

    Kept as documentation of the wire contract used by static/app.js. Not currently
    consumed via FastAPI body parsing because the WebSocket payload is parsed manually.
    """

    prompt: str = Field(min_length=1)
    clis: list[str] = Field(default_factory=lambda: ["codex", "claude"])
    mode: str = "parallel"
    include_status: bool = True
    project_dir: str = ""


# ---- FastAPI app ------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    print(f"[Council] static={STATIC} prompts={PROMPTS_DIR} conversations={CONVERSATIONS}")
    print(f"[Council] CLIs: { {n: e.is_available() for n, e in REGISTRY.items()} }")
    yield


app = FastAPI(title="Council", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/clis")
async def list_clis() -> dict[str, Any]:
    return {
        "clis": {
            entry.name: {
                "available": entry.is_available(),
                "command": entry.executable,
                "experimental": entry.experimental,
                "description": entry.description,
                "homepage": entry.homepage,
                "disabled": entry.disabled,
                "options_schema": [
                    {
                        "name": opt.name,
                        "type": opt.type,
                        "default": opt.default,
                        "choices": list(opt.choices),
                        "min": opt.min,
                        "max": opt.max,
                        "description": opt.description,
                    }
                    for opt in entry.options_schema
                ],
            }
            for entry in REGISTRY.values()
        }
    }


@app.get("/api/trust")
async def trust_list() -> dict[str, list[str]]:
    return {"trusted": trust.list_trusted()}


class TrustRequest(BaseModel):
    project_dir: str = Field(min_length=1)
    note: str = ""


@app.post("/api/trust/check")
async def trust_check(payload: TrustRequest) -> dict[str, Any]:
    decision = trust.check(payload.project_dir)
    return {
        "canonical": str(decision.canonical),
        "trusted": decision.is_trusted,
        "reason": decision.reason,
    }


@app.post("/api/trust/approve")
async def trust_approve(payload: TrustRequest) -> dict[str, Any]:
    decision = trust.check(payload.project_dir)
    if decision.reason.startswith("forbidden"):
        raise HTTPException(status_code=400, detail=decision.reason)
    if decision.reason.startswith("not-a-directory"):
        raise HTTPException(status_code=400, detail=decision.reason)
    trust.trust_folder(decision.canonical, note=payload.note)
    return {"canonical": str(decision.canonical), "trusted": True}


@app.post("/api/trust/revoke")
async def trust_revoke(payload: TrustRequest) -> dict[str, bool]:
    canonical = trust.canonicalize(payload.project_dir)
    removed = trust.untrust_folder(canonical)
    return {"removed": removed}


@app.get("/api/status")
async def get_status() -> dict[str, str]:
    return {"status": load_status()}


@app.post("/api/status")
async def set_status(payload: dict[str, str]) -> dict[str, str]:
    new = payload.get("status", "")
    STATUS_FILE.write_text(new, encoding="utf-8")
    return {"ok": "saved", "bytes": str(len(new))}


@app.post("/api/conversations")
async def new_conversation() -> dict[str, str]:
    conv = Conversation.new()
    return {"id": conv.id}


@app.get("/api/conversations")
async def list_conversations() -> dict[str, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    for d in sorted(CONVERSATIONS.iterdir(), reverse=True):
        if d.is_dir():
            prompt_path = d / "prompt.md"
            items.append(
                {
                    "id": d.name,
                    "has_prompt": prompt_path.exists(),
                    "responses": [
                        p.stem for p in (d / "responses").iterdir() if p.is_file()
                    ]
                    if (d / "responses").exists()
                    else [],
                }
            )
    return {"conversations": items[:50]}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str) -> dict[str, Any]:
    conv_id = _validate_conv_id(conv_id)
    d = CONVERSATIONS / conv_id
    if not d.exists():
        raise HTTPException(status_code=404, detail="not found")
    manifest: dict[str, Any] = {}
    mfile = d / "manifest.json"
    if mfile.exists():
        try:
            manifest = json.loads(mfile.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    prompt = (d / "prompt.md").read_text(encoding="utf-8") if (d / "prompt.md").exists() else ""
    responses = {}
    rdir = d / "responses"
    if rdir.exists():
        for p in rdir.iterdir():
            if p.is_file():
                responses[p.stem] = p.read_text(encoding="utf-8")
    return {"id": conv_id, "prompt": prompt, "responses": responses, "manifest": manifest}


@app.post("/api/fs/pick-folder")
async def pick_folder(payload: dict[str, str] | None = None) -> dict[str, Any]:
    """Spawn the folder-picker helper in a short-lived subprocess.

    Codex review explicitly required keeping tkinter out of the FastAPI request
    thread (event-loop block + platform fragility), so we shell out to a helper.
    """
    helper = ROOT / "scripts" / "folder_picker_helper.py"
    if not helper.exists():
        raise HTTPException(status_code=500, detail="folder picker helper missing")
    initial = (payload or {}).get("initial_dir", "") or os.path.expanduser("~")
    title = (payload or {}).get("title", "") or "Choose project folder"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(helper),
        initial,
        title,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    out = stdout_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        try:
            err_payload = json.loads(out) if out else {}
        except json.JSONDecodeError:
            err_payload = {}
        stderr_text = stderr_b.decode("utf-8", errors="replace")
        reason = err_payload.get("error") or stderr_text or "picker failed"
        raise HTTPException(status_code=503, detail=str(reason))
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"picker returned non-JSON: {exc}") from exc
    return data


@app.get("/api/modes")
async def list_modes() -> dict[str, list[str]]:
    return {"modes": list(MODES.keys())}


@app.websocket("/ws/{conv_id}")
async def ws_stream(ws: WebSocket, conv_id: str) -> None:
    # Validate before path-join. Reject obvious traversal at the boundary.
    if not _CONV_ID_PATTERN.match(conv_id) or len(conv_id) > 64:
        await ws.close(code=1008, reason="invalid conv_id")
        return
    await ws.accept()
    # Use the per-process registry so two WebSocket connections on the same
    # conv_id share one Conversation instance (and its session lock).
    conv = Conversation.get_or_create(conv_id)
    try:
        while True:
            msg = await ws.receive_json()
            # Expected: {"action": "send", "prompt": "...", "clis": [...],
            #            "mode": "parallel|debate|cascade|moa|router|consensus",
            #            "include_status": true}
            if msg.get("action") != "send":
                await ws.send_json({"cli": "*", "kind": "error", "data": "unknown action"})
                continue
            prompt = msg.get("prompt", "").strip()
            clis = msg.get("clis") or ["codex", "claude"]
            mode_name = msg.get("mode", "parallel")
            include_status = bool(msg.get("include_status", True))
            project_dir_raw = msg.get("project_dir") or ""
            cli_options_raw = msg.get("cli_options") or {}
            cli_options: dict[str, dict[str, object]] = {}
            if isinstance(cli_options_raw, dict):
                for cli_name, opts in cli_options_raw.items():
                    if isinstance(cli_name, str) and isinstance(opts, dict):
                        cli_options[cli_name] = {
                            str(k): v for k, v in opts.items() if isinstance(k, str)
                        }
            if not prompt:
                await ws.send_json({"cli": "*", "kind": "error", "data": "empty prompt"})
                continue
            # Trust check on project_dir before any spawn.
            decision = trust.check(project_dir_raw or None)
            if not decision.is_trusted:
                await ws.send_json(
                    {
                        "cli": "*",
                        "kind": "trust_required",
                        "data": str(decision.canonical),
                        "reason": decision.reason,
                    }
                )
                continue
            conv_cwd = decision.canonical
            mode_fn = MODES.get(mode_name)
            if mode_fn is None:
                await ws.send_json(
                    {"cli": "*", "kind": "error", "data": f"unknown mode '{mode_name}'"}
                )
                continue
            conv.write_prompt(prompt)
            conv.write_event(
                {
                    "kind": "send",
                    "prompt_len": len(prompt),
                    "clis": clis,
                    "mode": mode_name,
                    "cwd": str(conv_cwd),
                }
            )
            # Persist per-conversation manifest (project_dir, selected_clis, mode, options).
            (conv.dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "project_dir": str(conv_cwd),
                        "selected_clis": clis,
                        "mode": mode_name,
                        "include_status": include_status,
                        "cli_options": cli_options,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            await ws.send_json(
                {
                    "cli": "*",
                    "kind": "info",
                    "data": f"mode={mode_name} → CLIs {clis} (cwd={conv_cwd})",
                }
            )

            # Snapshot per-CLI session ids saved from prior turns. We read once here
            # (not per spawn) so all CLIs in this send agree on what they're resuming.
            cli_sessions = conv.load_cli_sessions()

            # Wrap stream_cli so modes get a single, fully-prepared prompt + cwd + options
            # + session_id. SOLE OWNER of status injection — stream_cli never re-injects.
            # Bind via default args to avoid B023 (loop-variable closure).
            async def run_cli_wrapped(
                cli: str,
                sub_prompt: str,
                ws_in: WebSocket,
                conv_in: Conversation,
                label: str = "",
                *,
                _inject: bool = include_status,
                _cwd: Path = conv_cwd,
                _opts: dict[str, dict[str, object]] = cli_options,
                _sessions: dict[str, str] = cli_sessions,
            ) -> str:
                if label in ("", "r1") and _inject:
                    final_prompt = build_full_prompt(sub_prompt, include_status=True)
                else:
                    final_prompt = sub_prompt
                return await stream_cli(
                    cli,
                    final_prompt,
                    ws_in,
                    conv_in,
                    label,
                    cwd=_cwd,
                    options=_opts.get(cli),
                    session_id=_sessions.get(cli),
                )

            try:
                result: ModeResult = await mode_fn(
                    prompt, clis, ws, conv, run_cli_wrapped
                )
                conv.write_event(
                    {
                        "kind": "mode_done",
                        "mode": result["mode"],
                        "rounds": result["rounds"],
                        "final_len": len(result["final_text"]),
                    }
                )
                # Save final summary if mode produced one
                if result["final_text"]:
                    (conv.dir / "final.md").write_text(
                        result["final_text"], encoding="utf-8"
                    )
                summary = (
                    f"mode={result['mode']} rounds={result['rounds']} "
                    f"final={len(result['final_text'])}c"
                )
                await ws.send_json(
                    {"cli": "*", "kind": "batch_done", "data": summary}
                )
            except Exception as e:
                conv.write_event({"kind": "mode_error", "mode": mode_name, "err": str(e)})
                await ws.send_json(
                    {"cli": "*", "kind": "error", "data": f"mode failed: {e}"}
                )
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False, log_level="info")
