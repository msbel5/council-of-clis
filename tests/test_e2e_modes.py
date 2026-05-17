"""End-to-end black-box tests for all 6 orchestration modes.

These tests:
- swap REGISTRY with fake CLIs backed by `tests/fake_cli.py`
- talk to the real FastAPI app via TestClient WebSocket
- assert event sequences arrive (stdout/done/batch_done, plus phase events for
  multi-round modes)
- verify session_id capture + resume on a second turn (parallel mode only — same
  flow applies to other modes)

If you add a new mode, add a case here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from registry import CLIEntry, OptionSpec

FAKE_HELPER = Path(__file__).resolve().parent / "fake_cli.py"
PYTHON = sys.executable


def _fake_entry(name: str = "fake-a") -> CLIEntry:
    """Build a CLIEntry that shells out to fake_cli.py.

    invocation_mode=argv so the prompt arrives as the last argv element (mirrors
    the way claude/gemini/copilot work in production). Uses sys.executable to
    survive CI runners that don't alias `python` to `python3`.
    """
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
    """Swap REGISTRY with two fake CLIs and isolate conversations dir."""
    fakes = {
        "fake-a": _fake_entry("fake-a"),
        "fake-b": _fake_entry("fake-b"),
        "fake-c": _fake_entry("fake-c"),
    }
    monkeypatch.setattr(server, "REGISTRY", fakes)
    yield fakes


@pytest.fixture
def client(fake_registry):
    with TestClient(server.app) as c:
        yield c


def _collect_until(ws, kind: str, cli: str = "*", limit: int = 400):
    """Read WS messages until we see {cli, kind} or hit the limit.

    Bumped to 400 from 200 because consensus mode in CI under load can produce
    enough phase + chunk events across 3 rounds × 3 CLIs that the original
    cap was hit before batch_done arrived.
    """
    events = []
    for _ in range(limit):
        msg = ws.receive_json()
        events.append(msg)
        if msg.get("cli") == cli and msg.get("kind") == kind:
            return events
    raise AssertionError(
        f"timeout waiting for cli={cli} kind={kind}; got {len(events)} events"
    )


def _send(ws, **kwargs):
    payload = {
        "action": "send",
        "include_status": False,
        "project_dir": "",
        **kwargs,
    }
    ws.send_json(payload)


# ---- Per-mode tests --------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected_phases",
    [
        ("parallel", []),
        ("debate", ["DEBATE round 1/2", "DEBATE round 2/2"]),
        ("cascade", ["CASCADE step 1/4", "CASCADE step 2/4", "CASCADE step 3/4"]),
        ("moa", ["MoA proposer round 1/2", "MoA proposer round 2/2", "MoA aggregation"]),
        ("router", ["ROUTER classifying", "ROUTER →"]),
        ("consensus", ["CONSENSUS round 1/3"]),
    ],
)
def test_mode_end_to_end(client, mode, expected_phases):
    """Each mode produces stdout chunks, a per-CLI done, and a batch_done summary."""
    # Create conversation
    r = client.post("/api/conversations")
    assert r.status_code == 200
    conv_id = r.json()["id"]

    clis = ["fake-a", "fake-b"]
    if mode == "cascade":
        clis = ["fake-a", "fake-b", "fake-c"]  # cascade needs drafter+critic+validator
    if mode in ("moa", "router", "consensus"):
        clis = ["fake-a", "fake-b", "fake-c"]

    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="hello council", clis=clis, mode=mode)
        events = _collect_until(ws, kind="batch_done", cli="*")

    # All modes must emit at least one stdout event and a batch_done.
    stdout_events = [e for e in events if e.get("kind") == "stdout"]
    assert stdout_events, f"mode={mode} produced no stdout events"
    batch_done = [e for e in events if e.get("kind") == "batch_done" and e.get("cli") == "*"]
    assert batch_done, f"mode={mode} did not emit batch_done"
    assert mode in batch_done[-1]["data"], f"batch_done summary missing mode name: {batch_done[-1]}"

    # Multi-round modes must emit phase events with the expected substrings.
    if expected_phases:
        phase_events = [e for e in events if e.get("kind") == "phase"]
        phase_blob = " | ".join(p.get("data", "") for p in phase_events)
        for needle in expected_phases:
            assert needle in phase_blob, (
                f"mode={mode} missing phase {needle!r} in phase events: {phase_blob}"
            )

    # CLI-level done events: at least one per selected CLI participated.
    done_per_cli = {e["cli"] for e in events if e.get("kind") == "done"}
    assert done_per_cli.intersection(clis), (
        f"mode={mode} no done events for any of {clis}; got {done_per_cli}"
    )


# ---- Session persistence ---------------------------------------------------


def test_session_id_captured_and_reused(client, monkeypatch):
    """First turn → CLI prints session_id, Council saves it.
    Second turn → Council prepends resume_command with that session_id.
    """
    sid = "abc12345xyz"
    monkeypatch.setenv("FAKE_CLI_SID", sid)

    r = client.post("/api/conversations")
    conv_id = r.json()["id"]

    # Turn 1
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="first turn", clis=["fake-a"], mode="parallel")
        _collect_until(ws, kind="batch_done", cli="*")

    # Sessions file should now hold the captured id.
    sessions_file = server.CONVERSATIONS / conv_id / "cli_sessions.json"
    assert sessions_file.exists(), "sessions file not written"
    import json as _json
    data = _json.loads(sessions_file.read_text(encoding="utf-8"))
    assert data.get("fake-a") == sid, f"expected sid {sid}, got {data}"

    # Turn 2 — observe the resume_command in cli_start event.
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="second turn", clis=["fake-a"], mode="parallel")
        _collect_until(ws, kind="batch_done", cli="*")

    # Read events log to verify the second cli_start used resume mode.
    log_path = server.CONVERSATIONS / conv_id / "events.jsonl"
    log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    cli_starts = [_json.loads(line) for line in log_lines if '"cli_start"' in line]
    # There should be at least 2 cli_start events; the second one should be resumed.
    assert len(cli_starts) >= 2, f"expected >=2 cli_start events, got {len(cli_starts)}"
    assert cli_starts[1].get("resumed") is True, (
        f"second turn was not resumed: {cli_starts[1]}"
    )
    assert cli_starts[1].get("session_id") == sid


def test_failed_cli_emits_error_event(client, monkeypatch):
    """When the fake CLI exits non-zero, Council emits a per-CLI error event."""
    monkeypatch.setenv("FAKE_CLI_FAIL", "1")
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="will fail", clis=["fake-a"], mode="parallel")
        events = _collect_until(ws, kind="batch_done", cli="*")
    # rc=1 still produces a "done" event (with exit=1 in data), but stderr arrives too.
    stderr_events = [e for e in events if e.get("kind") == "stderr"]
    done = [e for e in events if e.get("kind") == "done"]
    assert stderr_events, f"expected stderr events on failure, got: {events}"
    assert any("exit=1" in e.get("data", "") for e in done), "expected exit=1 in done event"


def test_unknown_cli_emits_error(client):
    """Sending to an unregistered CLI name emits an error, not a crash."""
    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(ws, prompt="hi", clis=["does-not-exist"], mode="parallel")
        events = _collect_until(ws, kind="batch_done", cli="*")
    errs = [e for e in events if e.get("kind") == "error" and e.get("cli") == "does-not-exist"]
    assert errs and "unknown" in errs[0]["data"].lower()


def test_options_threaded_to_cli(client, monkeypatch):
    """Per-CLI options sent in the WS payload reach the spawned subprocess."""
    fake = _fake_entry("fake-a")
    new_entry = CLIEntry(
        name=fake.name,
        command=(PYTHON, str(FAKE_HELPER), "{options}", "{prompt}"),
        invocation_mode=fake.invocation_mode,
        env=dict(fake.env),
        options_schema=(
            OptionSpec(
                name="label",
                type="enum",
                argv=("--label", "{value}"),
                choices=("alpha", "beta"),
                default="alpha",
            ),
        ),
    )
    monkeypatch.setattr(server, "REGISTRY", {fake.name: new_entry})

    r = client.post("/api/conversations")
    conv_id = r.json()["id"]
    with client.websocket_connect(f"/ws/{conv_id}") as ws:
        _send(
            ws,
            prompt="hi",
            clis=["fake-a"],
            mode="parallel",
            cli_options={"fake-a": {"label": "beta"}},
        )
        _collect_until(ws, kind="batch_done", cli="*")

    # The fake CLI doesn't actually USE --label, but the cli_start event records the
    # full argv so we can verify the splice happened.
    import json as _json
    log_path = server.CONVERSATIONS / conv_id / "events.jsonl"
    log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    starts = [_json.loads(line) for line in log_lines if '"cli_start"' in line]
    assert starts and "--label" in starts[0]["cmd"]
    assert "beta" in starts[0]["cmd"]
