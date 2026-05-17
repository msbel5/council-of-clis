"""Unit tests for orchestration modes.

We use a `_FakeRunCLI` that returns canned responses without spawning subprocesses, and a
`_FakeWS` that records send_json calls. This lets us assert mode behavior deterministically.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# modes.py lives at the repo root; we add the parent dir to sys.path in conftest.py if needed.
from modes import (
    MODES,
    ModeResult,
    mode_cascade,
    mode_consensus,
    mode_debate,
    mode_moa,
    mode_parallel,
    mode_router,
    pack_for_aggregation,
    pack_for_critique,
    pack_for_revision,
)

# ---- Fakes ----------------------------------------------------------------


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


def _make_run_cli(responses: dict[str, list[str]]) -> Any:
    """Return a fake run_cli that returns canned responses in order per cli."""
    counters: dict[str, int] = dict.fromkeys(responses, 0)

    async def fake(cli: str, prompt: str, ws: Any, conv: Any, label: str = "") -> str:
        idx = counters.get(cli, 0)
        bucket = responses.get(cli, [])
        if idx < len(bucket):
            counters[cli] = idx + 1
            return bucket[idx]
        return f"[no canned response for {cli} #{idx}]"

    return fake


# ---- Tests ----------------------------------------------------------------


def test_modes_registry_has_all_six() -> None:
    assert set(MODES.keys()) == {
        "parallel",
        "debate",
        "cascade",
        "moa",
        "router",
        "consensus",
    }


@pytest.mark.asyncio
async def test_parallel_calls_all_clis_once() -> None:
    ws = _FakeWS()
    run = _make_run_cli({"codex": ["A"], "claude": ["B"]})
    result: ModeResult = await mode_parallel("prompt", ["codex", "claude"], ws, None, run)
    assert result["mode"] == "parallel"
    assert result["rounds"] == 1
    assert result["per_cli_history"]["codex"] == ["A"]
    assert result["per_cli_history"]["claude"] == ["B"]


@pytest.mark.asyncio
async def test_debate_runs_two_rounds() -> None:
    ws = _FakeWS()
    run = _make_run_cli({
        "codex": ["r1-codex", "r2-codex"],
        "claude": ["r1-claude", "r2-claude"],
    })
    result = await mode_debate("prompt", ["codex", "claude"], ws, None, run, max_rounds=2)
    assert result["mode"] == "debate"
    assert result["rounds"] == 2
    assert len(result["per_cli_history"]["codex"]) == 2
    assert len(result["per_cli_history"]["claude"]) == 2
    # Final is first CLI's last round
    assert result["final_text"] == "r2-codex"


@pytest.mark.asyncio
async def test_cascade_invokes_four_stages() -> None:
    ws = _FakeWS()
    run = _make_run_cli({
        "codex": ["draft", "revised"],
        "claude": ["critique"],
        "gemini": ["validation"],
    })
    result = await mode_cascade(
        "prompt", ["codex", "claude", "gemini"], ws, None, run
    )
    assert result["mode"] == "cascade"
    assert result["final_text"] == "revised"
    # Drafter wrote 2 (draft + revise), critic wrote 1, validator wrote 1
    assert len(result["per_cli_history"]["codex"]) == 2
    assert len(result["per_cli_history"]["claude"]) == 1


@pytest.mark.asyncio
async def test_moa_aggregator_synthesizes() -> None:
    ws = _FakeWS()
    run = _make_run_cli({
        "codex": ["aggregated final"],
        "claude": ["proposal 1", "proposal 1 refined"],
        "gemini": ["proposal 2", "proposal 2 refined"],
    })
    result = await mode_moa(
        "prompt", ["codex", "claude", "gemini"], ws, None, run, proposer_rounds=2
    )
    assert result["mode"] == "moa"
    assert result["final_text"] == "aggregated final"


@pytest.mark.asyncio
async def test_router_routes_based_on_classification() -> None:
    ws = _FakeWS()
    run = _make_run_cli({
        "codex": ["reasoning here\nROUTE: CODE", "codex answer to code task"],
        "claude": ["claude answer"],
    })
    result = await mode_router("prompt", ["codex", "claude"], ws, None, run)
    assert result["mode"] == "router"
    # codex is preferred for CODE; second call returns the answer
    assert result["final_text"] == "codex answer to code task"


@pytest.mark.asyncio
async def test_consensus_stops_on_unanimous_vote() -> None:
    ws = _FakeWS()
    run = _make_run_cli({
        "codex": ["answer A\nVOTE: AGREE_WITH=codex", "answer A revised"],
        "claude": ["answer B\nVOTE: AGREE_WITH=codex", "claude agrees"],
    })
    # After round 1, both vote AGREE_WITH=codex → consensus reached, no round 2 needed
    result = await mode_consensus(
        "prompt", ["codex", "claude"], ws, None, run, max_rounds=3
    )
    assert result["mode"] == "consensus"
    # If consensus is reached after round 1, rounds = 1
    assert result["rounds"] == 1


def test_pack_for_revision_includes_others() -> None:
    text = pack_for_revision(
        "the task",
        "my prior answer",
        {"claude": "claude's answer", "gemini": "gemini's answer"},
    )
    assert "the task" in text
    assert "my prior answer" in text
    assert "claude's answer" in text
    assert "gemini's answer" in text
    assert "VOTE:" in text


def test_pack_for_critique_demands_verdict() -> None:
    text = pack_for_critique("the task", "draft text", "codex")
    assert "VERDICT: APPROVE" in text
    assert "draft text" in text


def test_pack_for_aggregation_lists_all_proposals() -> None:
    text = pack_for_aggregation(
        "the task",
        {"claude": "C says X", "gemini": "G says Y"},
    )
    assert "C says X" in text
    assert "G says Y" in text
    assert "Synthesize ONE final answer" in text


def test_phase_messages_are_sent() -> None:
    """Phase markers should appear in WS output during multi-round modes."""

    async def run() -> _FakeWS:
        ws = _FakeWS()
        run_cli = _make_run_cli({
            "codex": ["r1", "r2"],
            "claude": ["r1", "r2"],
        })
        await mode_debate("prompt", ["codex", "claude"], ws, None, run_cli, max_rounds=2)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(run())
    phases = [m for m in ws.sent if m.get("kind") == "phase"]
    assert len(phases) >= 2
    assert any("round 1/2" in p["data"] for p in phases)
    assert any("round 2/2" in p["data"] for p in phases)
