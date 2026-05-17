"""Unit tests for spawn.extract_session_id.

Defends against two failure modes flagged in v0.4 Codex review (P2):

1. **Model-text poisoning**: if a model says "Session ID: deadbeef" in its
   answer, we must NOT capture that as the session id. The CLI's real footer
   comes LAST in stdout, so we scan the tail and prefer the rightmost match.
2. **Pathological large output**: tail-scan is bounded to 4 KB so a multi-MB
   transcript doesn't make regex evaluation pathological.

Real CLI patterns (codex/claude/gemini) place their session marker in a
trailer line printed by the CLI runtime — not the model's reply — so the tail
heuristic is safe in practice.
"""

from __future__ import annotations

from registry import CLIEntry
from spawn import extract_session_id


def _entry(pattern: str) -> CLIEntry:
    return CLIEntry(
        name="fake",
        command=("noop",),
        invocation_mode="argv",
        resume_command=("noop", "--resume", "{session_id}"),
        session_id_pattern=pattern,
    )


def test_no_pattern_returns_none() -> None:
    entry = CLIEntry(name="x", command=("noop",), invocation_mode="argv")
    assert extract_session_id(entry, "session_id: abc12345") is None


def test_no_match_returns_none() -> None:
    entry = _entry(r"session_id:\s*([A-Za-z0-9]+)")
    assert extract_session_id(entry, "no session here") is None


def test_simple_capture() -> None:
    entry = _entry(r"session_id:\s*([A-Za-z0-9]+)")
    assert extract_session_id(entry, "ok\nsession_id: abc123XYZ") == "abc123XYZ"


def test_prefers_last_match_not_first() -> None:
    """If the model's prose mentions a session id format, the CLI's real footer
    appears AFTER. We must capture the last one.

    This is the exact Codex P2 scenario: model talks about session_id in its
    answer, then the CLI runtime emits its own footer below.
    """
    entry = _entry(r"session_id:\s*([A-Za-z0-9]+)")
    stdout = (
        "Here is some advice:\n"
        "  You can resume a session with `myctl --session_id: bogusFromModel`\n"
        "  See docs for more.\n"
        "session_id: REAL_FOOTER_id_xyz\n"  # ← CLI's actual footer
    )
    assert extract_session_id(entry, stdout) == "REAL_FOOTER_id_xyz"


def test_tail_scan_ignores_far_history() -> None:
    """A long transcript with a session-id-looking string near the top should
    not poison capture if it's beyond the 4 KB tail window AND there's a real
    match in the tail.
    """
    entry = _entry(r"session_id:\s*([A-Za-z0-9]+)")
    head = "session_id: POISONED_far_above\n" + ("filler\n" * 1000)
    tail = "session_id: TAIL_REAL_id\n"
    assert extract_session_id(entry, head + tail) == "TAIL_REAL_id"


def test_pattern_without_capture_group_returns_none() -> None:
    """Defensive: registry validates this at load time, but extract_session_id
    should not crash even if a no-group pattern slips through.
    """
    entry = _entry(r"session_id:\s*[A-Za-z0-9]+")
    # Pattern has no capture group → re.findall returns full matches (strings).
    # The function should still return *something* sane rather than crash.
    # We accept either the matched string or None; what matters is no exception.
    result = extract_session_id(entry, "session_id: xyz")
    assert result in (None, "session_id: xyz")


def test_codex_style_uuid_capture() -> None:
    """Real codex pattern matches uuid-ish hex strings with 8+ chars + dashes."""
    pattern = r"[Ss]ession\s*(?:id|ID|Id)\s*[:=]\s*([0-9a-fA-F-]{8,})"
    entry = _entry(pattern)
    stdout = (
        "User asked about sessions.\n"
        "Model: To resume, use `codex exec resume <session-id>`.\n"
        "...\n"
        "Session ID: 9f8c1ab2-1234-5678-90ab-cdef01234567\n"
    )
    captured = extract_session_id(entry, stdout)
    assert captured == "9f8c1ab2-1234-5678-90ab-cdef01234567"


def test_claude_style_capture() -> None:
    """Claude documents `conversation_id: <uuid>` style trailers."""
    pattern = r"(?:[Ss]ession[_\s]?[Ii][Dd]|conversation[_\s]?id)\s*[:=]\s*([0-9a-fA-F-]{8,})"
    entry = _entry(pattern)
    stdout = "model output\nconversation_id: abcd1234-ef56-7890-abcd-ef0123456789\n"
    assert extract_session_id(entry, stdout) == "abcd1234-ef56-7890-abcd-ef0123456789"


def test_empty_stdout_returns_none() -> None:
    entry = _entry(r"session_id:\s*([A-Za-z0-9]+)")
    assert extract_session_id(entry, "") is None
