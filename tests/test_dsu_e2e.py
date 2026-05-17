"""End-to-end black-box tests for v0.5 DSU peer awareness.

What's verified:

- `peer_sync_mode` starts as "off"; `/dsu_load` is a no-op until enabled
- When enabled, `/dsu_load` arms a one-shot flag; the NEXT send injects a
  peer brief into each receiving CLI's prompt
- The brief excludes the receiver's own previous response (because --resume
  already carries it)
- One-shot semantics: the FOLLOWING send (no re-arm) has plain prompts
- Empty / failed CLIs from previous turn are not advertised in the brief
- `peer_log.jsonl` is written after every successful `mode_fn` return

Strategy: reuse the v0.4 fake CLI subprocess harness; assert on
`events.jsonl` (cli_start.cmd carries the full argv for argv-mode CLIs, so
the prompt — including any injected DSU block — is visible there).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from registry import CLIEntry

FAKE_HELPER = Path(__file__).resolve().parent / "fake_cli.py"
PYTHON = sys.executable


def _fake_entry(name: str = "fake-a") -> CLIEntry:
    """Argv-mode fake CLI with FULL prompt echo so DSU assertions can inspect
    what Council actually passed to each CLI.
    """
    return CLIEntry(
        name=name,
        command=(PYTHON, str(FAKE_HELPER), "{prompt}"),
        invocation_mode="argv",
        headless_supported=True,
        env={"FAKE_CLI_NAME": name, "FAKE_CLI_ECHO_FULL": "1"},
        resume_command=(PYTHON, str(FAKE_HELPER), "--resume", "{session_id}", "{prompt}"),
        session_id_pattern=r"session_id:\s*([A-Za-z0-9]+)",
    )


@pytest.fixture
def fake_registry(monkeypatch):
    fakes = {
        "fake-a": _fake_entry("fake-a"),
        "fake-b": _fake_entry("fake-b"),
    }
    monkeypatch.setattr(server, "REGISTRY", fakes)
    # Reset the Conversation singleton registry between tests so DSU flags
    # and disk state from one test don't leak.
    monkeypatch.setattr(server.Conversation, "_INSTANCES", {})
    yield fakes


@pytest.fixture
def client(fake_registry):
    with TestClient(server.app) as c:
        yield c


def _collect_until(ws, kind: str, cli: str = "*", limit: int = 200) -> list[dict]:
    events = []
    for _ in range(limit):
        msg = ws.receive_json()
        events.append(msg)
        if msg.get("cli") == cli and msg.get("kind") == kind:
            return events
    raise AssertionError(f"timeout waiting for cli={cli} kind={kind}; got {len(events)}")


def _send(ws, **kwargs) -> None:
    payload = {
        "action": "send",
        "include_status": False,
        "project_dir": "",
        "mode": "parallel",
        **kwargs,
    }
    ws.send_json(payload)


def _read_cli_starts(conv_id: str) -> list[dict]:
    """Pull all cli_start events from events.jsonl for inspection."""
    log_path = server.CONVERSATIONS / conv_id / "events.jsonl"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    starts = []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("kind") == "cli_start":
            starts.append(ev)
    return starts


def _read_response(conv_id: str, cli: str, *, label: str | None = None) -> str:
    """Read what the fake CLI captured as its stdout.

    Parallel mode passes `label="r1"` for symmetry with multi-round modes, so
    the file lands at `responses/{cli}__r1.md`. By default we pick the most-
    recently-modified file matching `{cli}*.md` so the caller doesn't have to
    know the mode's internal labeling convention.

    Pass an explicit `label` to read a specific labeled file, e.g.
    `_read_response(cid, "fake-a", label="draft")` for cascade's drafter call.

    fake_cli echoes the incoming prompt verbatim with `[name] | <line>` per
    line, so DSU blocks and full prompt structure are visible in the returned
    text.
    """
    dir_path = server.CONVERSATIONS / conv_id / "responses"
    if not dir_path.exists():
        return ""
    if label is not None:
        path = dir_path / f"{cli}__{label}.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""
    matches = sorted(
        dir_path.glob(f"{cli}*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return ""
    return matches[0].read_text(encoding="utf-8")


# ---- Default state --------------------------------------------------------


def test_dsu_off_by_default(client) -> None:
    """Fresh conversation has peer_sync_mode='off'; /dsu_load returns dsu_skipped."""
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "dsu_load"})
        msg = ws.receive_json()
        assert msg["cli"] == "*"
        assert msg["kind"] == "dsu_skipped"
        assert "off" in msg["data"].lower()


def test_set_peer_sync_updates_manifest(client) -> None:
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 32})
        msg = ws.receive_json()
        assert msg["kind"] == "peer_sync_updated"
        data = json.loads(msg["data"])
        assert data["peer_sync_mode"] == "dsu"
        assert data["peer_budget_tokens"] == 32


# ---- Inject + exclusion ---------------------------------------------------


def test_dsu_inject_on_next_send_excludes_self(client) -> None:
    """Two CLIs, dsu enabled. Turn 1 produces distinct sentinels. /dsu_load.
    Turn 2: each CLI's received prompt must contain the OTHER's sentinel but
    NOT its own (fake_cli echoes the prompt verbatim with `[name] | <line>`).
    """
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]

    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        # Enable DSU
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 64})
        ws.receive_json()  # peer_sync_updated
        # Turn 1
        _send(ws, prompt="round one prompt", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")
        # Arm DSU
        ws.send_json({"action": "dsu_load"})
        armed = ws.receive_json()
        assert armed["kind"] == "dsu_armed"
        # Turn 2
        _send(ws, prompt="round two prompt", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")

    # responses/fake-a.md holds fake-a's TURN-2 output (latest overwrite).
    # fake_cli echoes the full incoming prompt, so the DSU block is visible.
    resp_a = _read_response(conv_id, "fake-a")
    resp_b = _read_response(conv_id, "fake-b")

    # Diagnostic dump on failure (CI logs)
    if not resp_a or "COUNCIL_DSU_START" not in resp_a:
        conv_dir = server.CONVERSATIONS / conv_id
        responses_dir = conv_dir / "responses"
        files_in_responses = (
            list(responses_dir.iterdir()) if responses_dir.exists() else []
        )
        events_text = (
            (conv_dir / "events.jsonl").read_text(encoding="utf-8")
            if (conv_dir / "events.jsonl").exists()
            else "(no events.jsonl)"
        )
        peer_log_text = (
            (conv_dir / "peer_log.jsonl").read_text(encoding="utf-8")
            if (conv_dir / "peer_log.jsonl").exists()
            else "(no peer_log.jsonl)"
        )
        manifest_text = (
            (conv_dir / "manifest.json").read_text(encoding="utf-8")
            if (conv_dir / "manifest.json").exists()
            else "(no manifest.json)"
        )
        raise AssertionError(
            f"fake-a turn-2 no DSU marker. resp_a={resp_a!r}\n"
            f"responses files: {files_in_responses}\n"
            f"manifest: {manifest_text}\n"
            f"peer_log:\n{peer_log_text}\n"
            f"events:\n{events_text}"
        )

    # Both turn-2 prompts must carry the DSU marker
    assert "COUNCIL_DSU_START" in resp_a, f"fake-a turn-2 no DSU marker: {resp_a[:500]}"
    assert "COUNCIL_DSU_START" in resp_b, f"fake-b turn-2 no DSU marker: {resp_b[:500]}"
    # fake-a's DSU block contains fake-b's peer header but NOT its own
    assert "**fake-b**" in resp_a, f"fake-a's DSU missing fake-b: {resp_a[:500]}"
    assert "**fake-a**" not in resp_a, (
        f"fake-a's DSU leaked own entry: {resp_a[:500]}"
    )
    assert "**fake-a**" in resp_b
    assert "**fake-b**" not in resp_b


def test_dsu_one_shot_clears_after_send(client) -> None:
    """After the DSU-armed send completes, the NEXT send (no re-arm) is plain."""
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 64})
        ws.receive_json()
        # Turn 1
        _send(ws, prompt="turn one", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")
        # Arm + turn 2 (consumes the arm)
        ws.send_json({"action": "dsu_load"})
        ws.receive_json()
        _send(ws, prompt="turn two with dsu", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")
        # Turn 3 — no re-arm → must be plain
        _send(ws, prompt="turn three plain", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")

    # The response file is overwritten each turn → contains TURN-3 content.
    resp_a = _read_response(conv_id, "fake-a")
    resp_b = _read_response(conv_id, "fake-b")
    assert "COUNCIL_DSU_START" not in resp_a, (
        f"turn-3 fake-a had DSU markers; one-shot should have cleared: {resp_a[:400]}"
    )
    assert "COUNCIL_DSU_START" not in resp_b, (
        f"turn-3 fake-b had DSU markers: {resp_b[:400]}"
    )


# ---- Storage --------------------------------------------------------------


def test_peer_log_written_per_turn(client) -> None:
    """After every send, peer_log.jsonl gains one entry per participating CLI."""
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="hi", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")

    log_path = server.CONVERSATIONS / conv_id / "peer_log.jsonl"
    assert log_path.exists(), "peer_log.jsonl not written"
    entries = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    clis_recorded = {e["cli"] for e in entries}
    assert clis_recorded == {"fake-a", "fake-b"}, entries
    for e in entries:
        assert e["turn"] == 1
        assert e["mode"] == "parallel"
        # fake_cli echoes the prompt with "[name] | <line>" per line, so the
        # raw "hi" appears with the pipe prefix in the captured stdout text.
        assert "| hi" in e["text"], (
            f"peer log entry missing fake_cli echo of 'hi': {e}"
        )


def test_peer_log_increments_turn(client) -> None:
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        for _ in range(3):
            _send(ws, prompt="x", clis=["fake-a"])
            _collect_until(ws, kind="batch_done", cli="*")

    log_path = server.CONVERSATIONS / conv_id / "peer_log.jsonl"
    entries = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    turns = [e["turn"] for e in entries]
    assert turns == [1, 2, 3], turns


# ---- Failure path ---------------------------------------------------------


def test_dsu_skips_failed_cli(client, monkeypatch) -> None:
    """A CLI that exited with error on the previous turn must NOT appear in
    the next turn's DSU block (its 'text' would be a stderr fragment, useless
    as peer context).
    """
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]

    # Make fake-b fail by setting FAKE_CLI_FAIL=1 only for it.
    fail_entry = CLIEntry(
        name="fake-b",
        command=(PYTHON, str(FAKE_HELPER), "{prompt}"),
        invocation_mode="argv",
        env={"FAKE_CLI_NAME": "fake-b", "FAKE_CLI_FAIL": "1", "FAKE_CLI_ECHO_FULL": "1"},
        resume_command=(PYTHON, str(FAKE_HELPER), "--resume", "{session_id}", "{prompt}"),
        session_id_pattern=r"session_id:\s*([A-Za-z0-9]+)",
    )
    monkeypatch.setattr(
        server,
        "REGISTRY",
        {"fake-a": _fake_entry("fake-a"), "fake-b": fail_entry},
    )

    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 64})
        ws.receive_json()
        _send(ws, prompt="turn one", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")
        ws.send_json({"action": "dsu_load"})
        ws.receive_json()
        _send(ws, prompt="turn two", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")

    resp_a = _read_response(conv_id, "fake-a")
    # fake-b failed → its entry has error=true → omitted from DSU block.
    # fake-a's DSU block should be EMPTY (no peers to report on), so no marker.
    assert "**fake-b**" not in resp_a, (
        f"fake-b should be omitted from DSU after its failure: {resp_a[:500]}"
    )


# ---- Budget --------------------------------------------------------------


def test_dsu_budget_truncates_peer_text(client) -> None:
    """A tiny budget_tokens cap must clamp peer-text length. We can verify
    by sending a long prompt on turn 1 (which fake_cli echoes back), then
    checking that turn-2's DSU block doesn't contain the full echo.
    """
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    long_payload = "X" * 800  # 800 chars in the prompt → ~800 chars in the echo
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 8})
        ws.receive_json()  # peer_sync_updated
        _send(ws, prompt=long_payload, clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")
        ws.send_json({"action": "dsu_load"})
        ws.receive_json()
        _send(ws, prompt="next turn", clis=["fake-a", "fake-b"])
        _collect_until(ws, kind="batch_done", cli="*")

    resp_a = _read_response(conv_id, "fake-a")
    # 8 tokens × 4 chars/token = ~32 chars per peer in the DSU block.
    # The full 800-char echo cannot fit. We assert the X-run got truncated.
    # fake_cli echoes the prompt with `[name] | ` prefix per line, so a long X
    # run from turn-1's echo would show up if not truncated.
    assert "X" * 100 not in resp_a, (
        f"DSU block didn't truncate to budget: {resp_a[:500]}"
    )


# ---- DSU armed event ------------------------------------------------------


def test_dsu_armed_event_emitted(client) -> None:
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 64})
        ws.receive_json()
        ws.send_json({"action": "dsu_load"})
        msg = ws.receive_json()
        assert msg["kind"] == "dsu_armed"
        assert "queued" in msg["data"].lower() or "DSU" in msg["data"]


# ---- Codex bot v0.5 P2 #2: rc tracking + peer_log error flag ---------------


def test_peer_log_marks_nonzero_rc_as_error(client, monkeypatch) -> None:
    """A CLI that emits stdout then exits non-zero must land in peer_log with
    error=true. Pre-fix the rc was only in events.jsonl; peer_log only flagged
    the synthetic `[error: ...]` strings from asyncio.gather exceptions.
    """
    # fake_cli with FAKE_CLI_FAIL=1 prints to stderr and exits 1 — it does NOT
    # print to stdout, so this catches the basic case. The richer "stdout +
    # nonzero exit" case isn't easy to simulate with fake_cli without rewriting
    # it; the rc-based check covers both.
    fail_entry = CLIEntry(
        name="fake-a",
        command=(PYTHON, str(FAKE_HELPER), "{prompt}"),
        invocation_mode="argv",
        env={"FAKE_CLI_NAME": "fake-a", "FAKE_CLI_FAIL": "1"},
        resume_command=(PYTHON, str(FAKE_HELPER), "--resume", "{session_id}", "{prompt}"),
        session_id_pattern=r"session_id:\s*([A-Za-z0-9]+)",
    )
    monkeypatch.setattr(server, "REGISTRY", {"fake-a": fail_entry})

    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="will fail", clis=["fake-a"])
        _collect_until(ws, kind="batch_done", cli="*")

    log_path = server.CONVERSATIONS / conv_id / "peer_log.jsonl"
    assert log_path.exists()
    entries = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    assert len(entries) == 1
    # The CLI exited rc=1 — must be flagged regardless of text content.
    assert entries[0]["error"] is True, (
        f"non-zero rc CLI should be error=true: {entries[0]}"
    )


# ---- Codex bot v0.5 P2 #3: DSU injects on first-call across all modes ------


def test_dsu_injects_on_first_call_in_cascade_mode(client, monkeypatch) -> None:
    """Cascade mode's first call to the drafter uses label='draft' (not
    'r1'/''). Pre-fix the DSU block was skipped because of hardcoded label
    list. Now we track first-call per CLI via `_dsu_emitted_to`.
    """
    # Cascade needs 3 CLIs (drafter, critic, validator). Add fake-c.
    monkeypatch.setitem(server.REGISTRY, "fake-c", _fake_entry("fake-c"))
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]

    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        ws.send_json({"action": "set_peer_sync", "mode": "dsu", "budget_tokens": 64})
        ws.receive_json()
        _send(ws, prompt="turn one", clis=["fake-a", "fake-b", "fake-c"], mode="cascade")
        _collect_until(ws, kind="batch_done", cli="*")
        ws.send_json({"action": "dsu_load"})
        ws.receive_json()
        _send(ws, prompt="turn two", clis=["fake-a", "fake-b", "fake-c"], mode="cascade")
        _collect_until(ws, kind="batch_done", cli="*")

    # Cascade calls fake-a twice (drafter then reviser). Pre-fix the DSU was
    # gated by label in ("", "r1"), so neither call got DSU. Post-fix the
    # FIRST call per CLI gets it regardless of mode-specific label.
    resp_a_draft = _read_response(conv_id, "fake-a", label="draft")
    resp_a_revise = _read_response(conv_id, "fake-a", label="revise")
    # The drafter call (fake-a's first call) MUST have DSU.
    assert "COUNCIL_DSU_START" in resp_a_draft, (
        f"DSU missing in cascade's first call to fake-a (label='draft'): "
        f"{resp_a_draft[:500]}"
    )
    # The reviser call (fake-a's second call) should NOT — within-mode peer
    # packing already conveys peer info on subsequent rounds.
    assert "COUNCIL_DSU_START" not in resp_a_revise, (
        f"DSU should be on FIRST call only, not reviser: {resp_a_revise[:500]}"
    )
