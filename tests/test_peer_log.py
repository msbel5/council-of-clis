"""Unit tests for peer_log module.

Covers:
- write → read → tail roundtrip
- DSU marker stripping in `PeerLogEntry.from_response` (no recursion)
- `latest_per_cli` exclude/skip semantics
- Graceful behavior on malformed input
"""

from __future__ import annotations

from pathlib import Path

from peer_log import PeerLogEntry, append_entries, latest_per_cli, read_tail


def _e(turn: int, cli: str, text: str, **kw) -> PeerLogEntry:
    return PeerLogEntry.from_response(turn=turn, cli=cli, mode="parallel", text=text, **kw)


def test_write_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "peer_log.jsonl"
    entries = [_e(1, "a", "hello a"), _e(1, "b", "hello b")]
    append_entries(path, entries)
    back = read_tail(path)
    assert len(back) == 2
    assert back[0].cli == "a"
    assert back[0].text == "hello a"
    assert back[1].cli == "b"


def test_read_tail_missing_file(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "nope.jsonl") == []


def test_read_tail_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "peer_log.jsonl"
    path.write_text(
        '{"valid": true}\n'  # missing required fields → schema error
        + "not json at all\n"
        + '{"ts": 1.0, "turn": 1, "cli": "a", "mode": "parallel", "text": "ok"}\n'
        + "\n"
        + '{"ts": "bad type"}\n',
        encoding="utf-8",
    )
    entries = read_tail(path)
    # Only the well-formed entry survives.
    assert len(entries) == 1
    assert entries[0].cli == "a"
    assert entries[0].text == "ok"


def test_read_tail_returns_last_n(tmp_path: Path) -> None:
    path = tmp_path / "peer_log.jsonl"
    entries = [_e(t, "a", f"t{t}") for t in range(1, 11)]
    append_entries(path, entries)
    last_three = read_tail(path, n=3)
    assert [e.text for e in last_three] == ["t8", "t9", "t10"]


def test_dsu_marker_stripped_no_recursion(tmp_path: Path) -> None:
    """If a CLI quotes the previous turn's DSU block in its response, the
    persisted text must NOT contain that block — otherwise the NEXT turn's
    DSU read would include a nested marker block."""
    polluted = (
        "Here is what I think.\n"
        "<!-- COUNCIL_DSU_START -->\n"
        "## Council standup — turn 1\n"
        "- **peer**: stuff\n"
        "<!-- COUNCIL_DSU_END -->\n"
        "And here is my real answer."
    )
    entry = _e(2, "a", polluted)
    assert "COUNCIL_DSU_START" not in entry.text
    assert "COUNCIL_DSU_END" not in entry.text
    assert "Here is what I think." in entry.text
    assert "And here is my real answer." in entry.text


def test_latest_per_cli_excludes_self(tmp_path: Path) -> None:
    path = tmp_path / "peer_log.jsonl"
    append_entries(
        path,
        [
            _e(1, "a", "a-t1"),
            _e(1, "b", "b-t1"),
            _e(2, "a", "a-t2"),
            _e(2, "b", "b-t2"),
        ],
    )
    # From a's perspective: only b's latest
    from_a = latest_per_cli(path, exclude="a")
    assert set(from_a.keys()) == {"b"}
    assert from_a["b"].text == "b-t2"
    # From b's perspective: only a's latest
    from_b = latest_per_cli(path, exclude="b")
    assert set(from_b.keys()) == {"a"}
    assert from_b["a"].text == "a-t2"


def test_latest_per_cli_skips_empty_and_error(tmp_path: Path) -> None:
    path = tmp_path / "peer_log.jsonl"
    append_entries(
        path,
        [
            _e(1, "a", "real"),
            _e(1, "b", ""),  # empty
            _e(1, "c", "boom", error=True),
        ],
    )
    out = latest_per_cli(path)
    assert "a" in out
    assert "b" not in out  # empty filtered
    assert "c" not in out  # error filtered


def test_latest_per_cli_does_not_fall_back_to_stale_after_failure(
    tmp_path: Path,
) -> None:
    """Codex bot v0.5 P2 follow-up: if a CLI's LATEST entry is empty/error,
    it must be omitted entirely — NOT replaced by an older successful turn.

    Otherwise the next DSU would silently advertise stale "successful" data
    after the CLI's most recent run failed. The correct semantic is "what
    was this CLI's most recent response?" and the honest answer is "nothing
    useful" if the most recent run failed.
    """
    path = tmp_path / "peer_log.jsonl"
    append_entries(
        path,
        [
            # Turn 1: CLI a succeeds, CLI b succeeds
            _e(1, "a", "a-t1 success"),
            _e(1, "b", "b-t1 success"),
            # Turn 2: CLI a fails (empty), CLI b succeeds again
            _e(2, "a", ""),
            _e(2, "b", "b-t2 success"),
        ],
    )
    out = latest_per_cli(path)
    # CLI a's LATEST is empty → must be omitted, NOT replaced by t1 success
    assert "a" not in out, (
        f"stale fallback: turn-2 fake-a failed but turn-1 leaked: {out}"
    )
    # CLI b's LATEST is fine → included with t2 content
    assert "b" in out
    assert out["b"].text == "b-t2 success"
    assert out["b"].turn == 2


def test_latest_per_cli_drops_when_only_entry_is_error(tmp_path: Path) -> None:
    """Single-turn case: if the CLI's only entry is an error, omit it."""
    path = tmp_path / "peer_log.jsonl"
    append_entries(path, [_e(1, "a", "stderr fragment", error=True)])
    assert "a" not in latest_per_cli(path)


def test_latest_per_cli_unfiltered_modes_keep_latest(tmp_path: Path) -> None:
    """When skip_empty/skip_error are False, the latest entry stays even if
    empty or errored — the filter is independent of the latest-resolution.
    """
    path = tmp_path / "peer_log.jsonl"
    append_entries(
        path,
        [
            _e(1, "a", "old success"),
            _e(2, "a", "", error=True),
        ],
    )
    out_strict = latest_per_cli(path, skip_empty=True, skip_error=True)
    assert "a" not in out_strict
    out_lax = latest_per_cli(path, skip_empty=False, skip_error=False)
    assert out_lax["a"].turn == 2
    assert out_lax["a"].error is True


def test_empty_flag_set_for_whitespace_only(tmp_path: Path) -> None:
    entry = _e(1, "x", "   \n  \t  \n")
    assert entry.empty
    assert entry.text == ""


def test_text_length_recorded(tmp_path: Path) -> None:
    entry = _e(1, "x", "hello world")
    assert entry.len == len("hello world")
