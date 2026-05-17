"""Unit tests for dsu.build_dsu_block + truncation + marker semantics."""

from __future__ import annotations

import time

from dsu import MARK_END, MARK_START, build_dsu_block, strip_dsu_markers
from peer_log import PeerLogEntry


def _peer(cli: str, text: str, turn: int = 1) -> PeerLogEntry:
    return PeerLogEntry(
        ts=time.time(), turn=turn, cli=cli, mode="parallel", text=text, len=len(text)
    )


def test_empty_peers_returns_empty_string() -> None:
    assert build_dsu_block(turn=1, peers={}) == ""


def test_basic_block_has_markers_and_peer_lines() -> None:
    peers = {
        "codex": _peer("codex", "hello from codex"),
        "claude": _peer("claude", "claude says hi"),
    }
    block = build_dsu_block(turn=2, peers=peers, budget_tokens=64)
    assert MARK_START in block
    assert MARK_END in block
    assert "Council standup — turn 2" in block
    assert "**codex**" in block
    assert "**claude**" in block
    assert "QUOTED NOTES" in block  # injection-defense framing


def test_block_sorted_by_cli_name_for_determinism() -> None:
    """Two snapshots with the same peers must produce byte-identical output."""
    peers = {
        "zeta": _peer("zeta", "z"),
        "alpha": _peer("alpha", "a"),
        "mu": _peer("mu", "m"),
    }
    block = build_dsu_block(turn=1, peers=peers)
    pos_a = block.find("**alpha**")
    pos_m = block.find("**mu**")
    pos_z = block.find("**zeta**")
    assert 0 < pos_a < pos_m < pos_z


def test_truncation_respects_budget() -> None:
    """budget_tokens=8 → max ~32 chars per peer."""
    long_text = "X" * 500
    peers = {"a": _peer("a", long_text)}
    block = build_dsu_block(turn=1, peers=peers, budget_tokens=8)
    # The block should not contain the full 500-char run
    assert "X" * 100 not in block
    # But should contain SOME of the truncated content and an ellipsis marker
    assert "X" in block
    assert "…" in block


def test_zero_budget_returns_empty_block() -> None:
    """Edge: budget=0 → all peers truncate to "", block becomes empty."""
    peers = {"a": _peer("a", "stuff")}
    block = build_dsu_block(turn=1, peers=peers, budget_tokens=0)
    # All peers truncated to empty → block returns "" (no marker block).
    assert block == ""


def test_block_skips_peer_with_empty_truncated_text() -> None:
    """If truncation produces empty text for one peer, it gets skipped."""
    peers = {
        "a": _peer("a", ""),
        "b": _peer("b", "actual content"),
    }
    block = build_dsu_block(turn=1, peers=peers, budget_tokens=64)
    assert "**b**" in block
    # "a" had empty text → no line for it
    assert "**a**" not in block


def test_strip_markers_removes_block() -> None:
    polluted = (
        "before\n"
        + MARK_START
        + "\n## block\n- peer says X\n"
        + MARK_END
        + "\nafter"
    )
    cleaned = strip_dsu_markers(polluted)
    assert MARK_START not in cleaned
    assert MARK_END not in cleaned
    assert "before" in cleaned
    assert "after" in cleaned


def test_strip_markers_handles_multiple_blocks() -> None:
    polluted = MARK_START + " A " + MARK_END + " mid " + MARK_START + " B " + MARK_END
    cleaned = strip_dsu_markers(polluted)
    assert "mid" in cleaned
    assert MARK_START not in cleaned


def test_truncation_breaks_on_word_boundary_when_possible() -> None:
    """If the budget cap lands mid-word, prefer the previous space."""
    text = "alpha bravo charlie delta echo foxtrot golf"
    peers = {"a": _peer("a", text)}
    block = build_dsu_block(turn=1, peers=peers, budget_tokens=4)  # ~16 chars
    # Find the peer line
    line = next(line for line in block.splitlines() if "**a**" in line)
    # Either we truncated on a space (no mid-word cut) or budget was too tight
    # (text up to ~16 chars). Verify we don't end mid-word like "char…".
    assert line.endswith("…")
    # Common acceptable truncations: "alpha bravo…", "alpha…" etc.
    # Must NOT end with "alph…" or "alpha b…" (mid-word).
    payload = line.split(":", 1)[1].strip() if ":" in line else line
    payload_no_dots = payload.rstrip("…").rstrip()
    if " " in payload_no_dots:
        # Last word should be complete
        last_word = payload_no_dots.rsplit(" ", 1)[-1]
        assert last_word in text.split(), (
            f"truncation cut mid-word: last word {last_word!r} not in source"
        )
