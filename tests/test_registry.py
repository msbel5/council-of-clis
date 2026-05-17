"""Unit tests for the TOML CLI registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from registry import CLIEntry, RegistryError, _parse_one, load_registry


def test_parse_minimal_entry() -> None:
    entry = _parse_one(
        {"name": "x", "command": ["x", "--print"], "invocation_mode": "argv"}, "test"
    )
    assert entry.name == "x"
    assert entry.command == ("x", "--print")
    assert entry.invocation_mode == "argv"
    assert not entry.experimental
    assert not entry.disabled


def test_parse_full_entry() -> None:
    entry = _parse_one(
        {
            "name": "grok",
            "command": ["grok", "-p"],
            "invocation_mode": "argv",
            "headless_supported": True,
            "experimental": True,
            "description": "xAI Grok Build",
            "homepage": "https://x.ai/cli",
            "disabled": False,
            "env": {"GROK_API_KEY": "from-keyring"},
        },
        "test",
    )
    assert entry.experimental
    assert entry.env == {"GROK_API_KEY": "from-keyring"}


def test_parse_missing_name() -> None:
    with pytest.raises(RegistryError, match="missing `name`"):
        _parse_one({"command": ["x"]}, "test")


def test_parse_invalid_command() -> None:
    with pytest.raises(RegistryError, match="invalid `command`"):
        _parse_one({"name": "x", "command": "not-a-list"}, "test")


def test_parse_empty_command() -> None:
    with pytest.raises(RegistryError, match="invalid `command`"):
        _parse_one({"name": "x", "command": []}, "test")


def test_parse_invalid_mode() -> None:
    with pytest.raises(RegistryError, match="invalid `invocation_mode`"):
        _parse_one(
            {"name": "x", "command": ["x"], "invocation_mode": "shell"}, "test"
        )


def test_parse_invalid_env() -> None:
    with pytest.raises(RegistryError, match="invalid `env`"):
        _parse_one({"name": "x", "command": ["x"], "env": {1: "v"}}, "test")


def test_load_defaults_yields_codex_claude_copilot_gemini() -> None:
    """Package defaults must include the 4 main CLIs."""
    registry = load_registry(user_config_path=Path("/nonexistent"))
    assert "codex" in registry
    assert "claude" in registry
    assert "copilot" in registry
    assert "gemini" in registry
    assert registry["codex"].invocation_mode == "stdin"
    assert registry["claude"].invocation_mode == "argv"


def test_load_defaults_marks_grok_and_vibe_experimental() -> None:
    """v0.3-candidate CLIs should be flagged experimental."""
    registry = load_registry(user_config_path=Path("/nonexistent"))
    assert registry["grok"].experimental
    assert registry["vibe"].experimental


def test_user_override_replaces_default(tmp_path: Path) -> None:
    """A user file with the same `name` overrides the default."""
    user_file = tmp_path / "clis.toml"
    user_file.write_text(
        '[[cli]]\nname = "codex"\ncommand = ["my-custom-codex", "--flag"]\n'
        'invocation_mode = "stdin"\n',
        encoding="utf-8",
    )
    registry = load_registry(user_config_path=user_file)
    assert registry["codex"].command == ("my-custom-codex", "--flag")


def test_user_adds_new_entry(tmp_path: Path) -> None:
    """A user file can add a new CLI not in defaults."""
    user_file = tmp_path / "clis.toml"
    user_file.write_text(
        '[[cli]]\nname = "myllm"\ncommand = ["myllm", "-q"]\ninvocation_mode = "argv"\n',
        encoding="utf-8",
    )
    registry = load_registry(user_config_path=user_file)
    assert "myllm" in registry
    assert registry["myllm"].command == ("myllm", "-q")


def test_cli_entry_executable_is_first_arg() -> None:
    entry = CLIEntry(
        name="x",
        command=("foo", "bar"),
        invocation_mode="argv",
    )
    assert entry.executable == "foo"


def test_disabled_entry_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled entry is not_available even if the binary exists on PATH."""
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/foo")
    entry = CLIEntry(
        name="x", command=("foo",), invocation_mode="argv", disabled=True
    )
    assert not entry.is_available()


def test_non_headless_entry_not_available() -> None:
    entry = CLIEntry(
        name="x",
        command=("foo",),
        invocation_mode="argv",
        headless_supported=False,
    )
    assert not entry.is_available()


# ---- v0.4 session_id_pattern validation -----------------------------------


def test_session_pattern_one_capture_group_ok() -> None:
    """The canonical case: exactly one capture group."""
    entry = _parse_one(
        {
            "name": "x",
            "command": ["x"],
            "invocation_mode": "argv",
            "resume_command": ["x", "--resume", "{session_id}"],
            "session_id_pattern": r"sid=([0-9a-f]+)",
        },
        "test",
    )
    assert entry.session_id_pattern == r"sid=([0-9a-f]+)"


def test_session_pattern_zero_capture_groups_rejected() -> None:
    """A pattern with no capture group can't yield an id — fail at load."""
    with pytest.raises(RegistryError, match="exactly one capture group"):
        _parse_one(
            {
                "name": "x",
                "command": ["x"],
                "invocation_mode": "argv",
                "resume_command": ["x", "--resume", "{session_id}"],
                "session_id_pattern": r"sid=[0-9a-f]+",  # no group
            },
            "test",
        )


def test_session_pattern_two_capture_groups_rejected() -> None:
    """Codex bot P2: `(label):(value)` would silently save the wrong group."""
    with pytest.raises(RegistryError, match="exactly one capture group"):
        _parse_one(
            {
                "name": "x",
                "command": ["x"],
                "invocation_mode": "argv",
                "resume_command": ["x", "--resume", "{session_id}"],
                "session_id_pattern": r"(session_id):\s*([0-9a-f]+)",
            },
            "test",
        )


def test_session_pattern_non_capturing_group_is_one_group() -> None:
    """Non-capturing groups `(?:...)` don't count — pattern is still 1 capture group."""
    entry = _parse_one(
        {
            "name": "x",
            "command": ["x"],
            "invocation_mode": "argv",
            "resume_command": ["x", "--resume", "{session_id}"],
            "session_id_pattern": r"(?:[Ss]ession[_\s]?[Ii][Dd])\s*[:=]\s*([0-9a-fA-F-]{8,})",
        },
        "test",
    )
    assert "?:" in entry.session_id_pattern


def test_resume_without_pattern_rejected() -> None:
    """Both-or-neither rule: resume_command without session_id_pattern fails."""
    with pytest.raises(RegistryError, match="BOTH resume_command and"):
        _parse_one(
            {
                "name": "x",
                "command": ["x"],
                "invocation_mode": "argv",
                "resume_command": ["x", "--resume", "{session_id}"],
                # no session_id_pattern
            },
            "test",
        )


def test_pattern_without_resume_rejected() -> None:
    """Mirror: session_id_pattern without resume_command fails."""
    with pytest.raises(RegistryError, match="BOTH resume_command and"):
        _parse_one(
            {
                "name": "x",
                "command": ["x"],
                "invocation_mode": "argv",
                "session_id_pattern": r"sid=([0-9a-f]+)",
                # no resume_command
            },
            "test",
        )
