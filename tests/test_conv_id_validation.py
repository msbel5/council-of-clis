"""Tests for conv_id path-traversal validation in server.py.

These verify the security fix Codex flagged: user-influenced conv_id values must be
constrained to a safe character set before any filesystem path-join.
"""

from __future__ import annotations

import re

import pytest

from server import _CONV_ID_PATTERN, _validate_conv_id


def test_valid_conv_id_allowed() -> None:
    """Standard timestamp-uuid format used by Conversation.new() passes."""
    assert _validate_conv_id("1779042931-795a3d") == "1779042931-795a3d"


def test_alphanumeric_only_allowed() -> None:
    assert _validate_conv_id("abc123") == "abc123"


def test_underscores_and_hyphens_allowed() -> None:
    assert _validate_conv_id("a_b-c-1") == "a_b-c-1"


def test_empty_rejected() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _validate_conv_id("")


@pytest.mark.parametrize(
    "bad_id",
    [
        "../etc/passwd",            # traversal
        "..\\windows\\system32",    # windows traversal
        "abc/def",                  # path separator
        "abc\\def",                 # windows separator
        "abc def",                  # space
        "abc.def",                  # dot (could enable .. games)
        "abc;rm -rf /",             # shell metachar
        "abc\nrm",                  # newline
        "abc\x00",                  # null byte
        "a" * 65,                   # too long
    ],
)
def test_invalid_conv_id_rejected(bad_id: str) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _validate_conv_id(bad_id)


def test_pattern_anchors_full_string() -> None:
    """Pattern must match the ENTIRE string, not a substring."""
    assert _CONV_ID_PATTERN.match("abc/def") is None
    assert _CONV_ID_PATTERN.match("abc-def") is not None
    # Verify no surprises with regex special chars
    assert isinstance(_CONV_ID_PATTERN, re.Pattern)
