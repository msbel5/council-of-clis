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
    """Argv-mode fake CLI so prompts land in cli_start.cmd for inspection."""
    return CLIEntry(
        name=name,
        command=(PYTHON, str(FAKE_HELPER), "{prompt}"),
        invocation_mode="argv",
        headless_supported=True,
        env={"FAKE_CLI_NAME": name},
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
    Turn 2: each CLI's argv must contain the OTHER's sentinel but NOT its own.
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

    starts = _read_cli_starts(conv_id)
    # Two CLIs × two turns = 4 cli_start events. Verify turn-2 events have DSU
    # markers and contain peer's name but not own.
    assert len(starts) == 4, f"expected 4 cli_starts, got {len(starts)}: {starts}"
    # Turn-2 events are the LAST 2 (order: a-r1, b-r1, a-r2, b-r2).
    t2_a = starts[2]
    t2_b = starts[3]
    cmd_a = " ".join(t2_a["cmd"]) if isinstance(t2_a["cmd"], list) else str(t2_a["cmd"])
    cmd_b = " ".join(t2_b["cmd"]) if isinstance(t2_b["cmd"], list) else str(t2_b["cmd"])

    # Both turn-2 prompts must carry the DSU marker
    assert "COUNCIL_DSU_START" in cmd_a
    assert "COUNCIL_DSU_START" in cmd_b
    # fake-a's prompt mentions fake-b's name (as the peer header) but not its
    # own `**fake-a**` peer header (since self is excluded).
    assert "**fake-b**" in cmd_a, f"fake-a's DSU block missing fake-b: {cmd_a[:500]}"
    assert "**fake-a**" not in cmd_a, f"fake-a's DSU block leaked own entry: {cmd_a[:500]}"
    assert "**fake-a**" in cmd_b
    assert "**fake-b**" not in cmd_b


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

    starts = _read_cli_starts(conv_id)
    # 2 CLIs × 3 turns = 6 cli_starts. Last 2 are turn 3.
    assert len(starts) == 6
    for s in starts[-2:]:
        cmd = " ".join(s["cmd"]) if isinstance(s["cmd"], list) else str(s["cmd"])
        assert "COUNCIL_DSU_START" not in cmd, (
            f"turn-3 had DSU markers after one-shot should have cleared: {cmd[:300]}"
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
        assert "received: hi" in e["text"], (
            f"peer log entry missing fake_cli sentinel: {e}"
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
        env={"FAKE_CLI_NAME": "fake-b", "FAKE_CLI_FAIL": "1"},
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

    starts = _read_cli_starts(conv_id)
    t2 = starts[2]  # fake-a's turn 2 cli_start
    cmd = " ".join(t2["cmd"]) if isinstance(t2["cmd"], list) else str(t2["cmd"])
    # fake-b failed → its entry has error=true → omitted from DSU block.
    # fake-a's DSU block should be EMPTY (no peers to report on), so no marker.
    # (build_dsu_block returns empty string when no peers have non-empty content,
    # so no marker block is added.)
    assert "**fake-b**" not in cmd, (
        f"fake-b should be omitted from DSU after its failure: {cmd[:500]}"
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

    starts = _read_cli_starts(conv_id)
    raw_cmd = starts[2]["cmd"]
    cmd_t2_a = " ".join(raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)
    # 8 tokens × 4 chars/token = ~32 chars per peer in the DSU block.
    # The full 800-char echo cannot fit. We assert the X-run got truncated
    # (no string of 100+ X's appears).
    assert "X" * 100 not in cmd_t2_a, (
        f"DSU block didn't truncate to budget: {cmd_t2_a[:500]}"
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
