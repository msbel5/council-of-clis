"""Council — Multi-CLI orchestration server.

Single FastAPI + uvicorn process. Spawns codex / claude / copilot / gemini subprocesses
in parallel, streams their stdout line-by-line over WebSocket to the browser UI.

Run:
    cd C:\\Users\\msbel\\alcyone-project\\council
    uv venv && .\\.venv\\Scripts\\Activate.ps1
    uv pip install fastapi "uvicorn[standard]" pydantic
    python server.py

Then open http://localhost:8765
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from modes import MODES, ModeResult


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


# ---- CLI registry -----------------------------------------------------------
# Each entry: name -> (command_template, available_check, prompt_via_stdin)
# command_template: argv list using "{prompt_file}" placeholder if file mode,
#                   or argv list to pipe stdin if stdin mode.
# prompt_via_stdin: True → pipe prompt to stdin; False → read from file argument

# INVOCATION RULES (verified via Codex CLI research, 2026-05-17):
#   codex exec -                  ← `-` is explicit stdin sentinel (Windows hang fix)
#   claude --print "PROMPT"       ← prompt as argv; stdin is optional extra context
#   copilot -sp "PROMPT"          ← new standalone Copilot CLI (NOT `gh copilot suggest`)
#   gemini -p "PROMPT"            ← or pipe stdin

CLIS: dict[str, dict[str, Any]] = {
    "codex": {
        # Use `-` to explicitly read prompt from stdin (avoids Windows non-interactive hang)
        "command": [
            "codex",
            "exec",
            "-",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            "gpt-5.4",
            "--config",
            'model_reasoning_effort="medium"',
        ],
        "mode": "stdin",
        "available": lambda: shutil.which("codex") is not None,
    },
    "claude": {
        # Claude Code CLI: prompt as argv with --print flag, stdin reserved for context
        "command": ["claude", "--print"],
        "mode": "argv",
        "available": lambda: shutil.which("claude") is not None,
    },
    "copilot": {
        # NEW standalone GitHub Copilot CLI (not `gh copilot suggest` which is shell-only)
        # Install: gh extension install github/gh-copilot OR npm i -g @github/copilot-cli
        "command": ["copilot", "-sp"],
        "mode": "argv",
        "available": lambda: shutil.which("copilot") is not None,
    },
    "gemini": {
        # Google's gemini-cli — npm install -g @google/gemini-cli
        "command": ["gemini", "-p"],
        "mode": "argv",
        "available": lambda: shutil.which("gemini") is not None,
    },
}


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
    """One conversation = one directory under conversations/."""

    def __init__(self, conv_id: str) -> None:
        self.id = conv_id
        self.dir = CONVERSATIONS / conv_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "responses").mkdir(exist_ok=True)
        self.log_path = self.dir / "events.jsonl"

    def write_event(self, event: dict[str, Any]) -> None:
        event["ts"] = time.time()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_prompt(self, prompt: str) -> None:
        (self.dir / "prompt.md").write_text(prompt, encoding="utf-8")

    def write_response(self, cli: str, content: str) -> None:
        (self.dir / "responses" / f"{cli}.md").write_text(content, encoding="utf-8")

    @classmethod
    def new(cls) -> Conversation:
        cid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        return cls(cid)


# ---- Subprocess streaming --------------------------------------------------


async def stream_cli(
    cli: str,
    prompt: str,
    ws: WebSocket,
    conv: Conversation,
    label: str = "",
) -> str:
    """Spawn one CLI subprocess, stream its stdout/stderr line-by-line over WS.

    Sends JSON envelopes: {"cli": "codex", "kind": "stdout"|"stderr"|"done"|"error",
                           "data": "...", "label": "<round/phase tag>"}.
    Saves the full response to conversations/<id>/responses/<cli>__<label>.md.
    Returns the captured stdout text so consensus/debate modes can chain it.
    """
    spec = CLIS.get(cli)
    if spec is None:
        await ws.send_json({"cli": cli, "kind": "error", "data": "unknown CLI", "label": label})
        return ""
    if not spec["available"]():
        await ws.send_json(
            {"cli": cli, "kind": "error", "data": "CLI not installed locally", "label": label}
        )
        return ""

    cmd = list(spec["command"])
    full_prompt = build_full_prompt(prompt)
    mode = spec.get("mode", "stdin")
    conv.write_event({"kind": "cli_start", "cli": cli, "cmd": " ".join(cmd), "mode": mode})

    if mode == "stdin":
        # Pipe prompt to stdin (codex exec -, gemini bare)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "TERM": "dumb"},
        )
        if proc.stdin is not None:
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
    else:
        # Pass prompt as positional argv (claude --print, copilot -sp, gemini -p)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            full_prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "TERM": "dumb"},
        )

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
        conv.write_event({"kind": "cli_done", "cli": cli, "label": label, "rc": rc})
        try:
            await ws.send_json(
                {"cli": cli, "kind": "done", "data": f"exit={rc}", "label": label}
            )
        except Exception:
            pass
    except Exception as e:
        conv.write_event({"kind": "cli_error", "cli": cli, "label": label, "err": str(e)})
        try:
            await ws.send_json(
                {"cli": cli, "kind": "error", "data": str(e), "label": label}
            )
        except Exception:
            pass
    return full_response


# ---- API models -------------------------------------------------------------


class SendRequest(BaseModel):
    prompt: str = Field(min_length=1)
    clis: list[str] = Field(default_factory=lambda: ["codex", "claude"])
    include_status: bool = True


# ---- FastAPI app ------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    print(f"[Council] static={STATIC} prompts={PROMPTS_DIR} conversations={CONVERSATIONS}")
    print(f"[Council] CLIs: { {k: v['available']() for k, v in CLIS.items()} }")
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
            name: {"available": spec["available"](), "command": spec["command"][0]}
            for name, spec in CLIS.items()
        }
    }


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
    d = CONVERSATIONS / conv_id
    if not d.exists():
        raise HTTPException(status_code=404, detail="not found")
    prompt = (d / "prompt.md").read_text(encoding="utf-8") if (d / "prompt.md").exists() else ""
    responses = {}
    rdir = d / "responses"
    if rdir.exists():
        for p in rdir.iterdir():
            if p.is_file():
                responses[p.stem] = p.read_text(encoding="utf-8")
    return {"id": conv_id, "prompt": prompt, "responses": responses}


@app.get("/api/modes")
async def list_modes() -> dict[str, list[str]]:
    return {"modes": list(MODES.keys())}


@app.websocket("/ws/{conv_id}")
async def ws_stream(ws: WebSocket, conv_id: str) -> None:
    await ws.accept()
    conv = Conversation(conv_id)
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
            if not prompt:
                await ws.send_json({"cli": "*", "kind": "error", "data": "empty prompt"})
                continue
            mode_fn = MODES.get(mode_name)
            if mode_fn is None:
                await ws.send_json(
                    {"cli": "*", "kind": "error", "data": f"unknown mode '{mode_name}'"}
                )
                continue
            conv.write_prompt(prompt)
            conv.write_event(
                {"kind": "send", "prompt_len": len(prompt), "clis": clis, "mode": mode_name}
            )
            await ws.send_json(
                {
                    "cli": "*",
                    "kind": "info",
                    "data": f"mode={mode_name} → CLIs {clis}",
                }
            )

            # Wrap stream_cli so modes get the captured text + status injection.
            async def run_cli_wrapped(
                cli: str, sub_prompt: str, ws_in: WebSocket, conv_in: Conversation, label: str = ""
            ) -> str:
                # Re-inject status when the mode hands a packaged prompt? Only on round 1.
                # For internal rounds (label != "r1" and != ""), DO NOT re-inject — packaged
                # prompts already include the original task.
                if label in ("", "r1") and include_status:
                    final_prompt = build_full_prompt(sub_prompt, include_status=True)
                else:
                    final_prompt = sub_prompt
                return await stream_cli(cli, final_prompt, ws_in, conv_in, label)

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
                await ws.send_json(
                    {
                        "cli": "*",
                        "kind": "batch_done",
                        "data": f"mode={result['mode']} rounds={result['rounds']} final={len(result['final_text'])}c",
                    }
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
