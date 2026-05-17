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
