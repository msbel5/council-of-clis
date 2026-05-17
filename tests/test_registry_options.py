"""Unit tests for options_schema parsing/validation in registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from registry import OptionSpec, RegistryError, _parse_option, load_registry


def _parse(option_dict: dict) -> OptionSpec:
    return _parse_option(option_dict, "codex", "test")


def test_parse_valid_enum() -> None:
    spec = _parse(
        {"name": "model", "type": "enum", "choices": ["a", "b"], "default": "a",
         "argv": ["--model", "{value}"]}
    )
    assert spec.type == "enum"
    assert spec.choices == ("a", "b")
    assert spec.default == "a"


def test_parse_bool_no_choices() -> None:
    spec = _parse({"name": "yolo", "type": "bool", "default": False, "argv": ["--yolo"]})
    assert spec.type == "bool"


def test_parse_number_with_range() -> None:
    spec = _parse(
        {"name": "n", "type": "number", "default": 3, "min": 1, "max": 10,
         "argv": ["-n", "{value}"]}
    )
    assert spec.min == 1.0
    assert spec.max == 10.0


def test_invalid_type_raises() -> None:
    with pytest.raises(RegistryError, match="invalid `type`"):
        _parse({"name": "x", "type": "magic", "argv": ["--x"]})


def test_missing_name_raises() -> None:
    with pytest.raises(RegistryError, match="missing `name`"):
        _parse({"type": "string", "argv": ["--x"]})


def test_missing_argv_raises() -> None:
    with pytest.raises(RegistryError, match="invalid `argv`"):
        _parse({"name": "x", "type": "string"})


def test_empty_argv_raises() -> None:
    with pytest.raises(RegistryError, match="invalid `argv`"):
        _parse({"name": "x", "type": "string", "argv": []})


def test_argv_non_string_raises() -> None:
    with pytest.raises(RegistryError, match="invalid `argv`"):
        _parse({"name": "x", "type": "string", "argv": ["--x", 5]})


def test_enum_without_choices_raises() -> None:
    with pytest.raises(RegistryError, match="no `choices`"):
        _parse({"name": "x", "type": "enum", "choices": [], "argv": ["--x", "{value}"]})


def test_enum_default_not_in_choices_raises() -> None:
    with pytest.raises(RegistryError, match="not in choices"):
        _parse(
            {"name": "x", "type": "enum", "choices": ["a", "b"], "default": "c",
             "argv": ["--x", "{value}"]}
        )


def test_min_max_on_non_number_raises() -> None:
    with pytest.raises(RegistryError, match="min/max but type"):
        _parse({"name": "x", "type": "string", "min": 0, "argv": ["--x"]})


def test_default_cli_registry_loads_with_options() -> None:
    """default_clis.toml ships with options_schema for codex/claude/gemini."""
    registry = load_registry(user_config_path=Path("/nonexistent"))
    assert registry["codex"].options_schema  # not empty
    codex_opts = {opt.name for opt in registry["codex"].options_schema}
    assert codex_opts == {"model", "reasoning", "sandbox"}

    claude_opts = {opt.name for opt in registry["claude"].options_schema}
    assert claude_opts == {"model", "output_format"}

    gemini_opts = {opt.name for opt in registry["gemini"].options_schema}
    assert gemini_opts == {"model", "yolo"}


def test_duplicate_option_names_raises(tmp_path: Path) -> None:
    user_file = tmp_path / "clis.toml"
    user_file.write_text(
        '''
[[cli]]
name = "dup"
command = ["dup"]
invocation_mode = "argv"

[[cli.options_schema]]
name = "x"
type = "string"
argv = ["--x", "{value}"]

[[cli.options_schema]]
name = "x"
type = "string"
argv = ["--x2", "{value}"]
''',
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="duplicate option name"):
        load_registry(user_config_path=user_file)


def test_render_argv_substitutes_placeholder() -> None:
    spec = OptionSpec(
        name="reasoning",
        type="enum",
        argv=("--config", 'model_reasoning_effort="{value}"'),
        choices=("low", "high"),
    )
    assert spec.render_argv("high") == (
        "--config",
        'model_reasoning_effort="high"',
    )


def test_coerce_bool_string_truthy() -> None:
    spec = OptionSpec(name="b", type="bool", argv=("--b",))
    assert spec.coerce_value("true") is True
    assert spec.coerce_value("1") is True
    assert spec.coerce_value("FALSE") is False
    assert spec.coerce_value(False) is False


def test_coerce_number_out_of_range() -> None:
    spec = OptionSpec(name="n", type="number", argv=("-n", "{value}"), min=0, max=10)
    with pytest.raises(RegistryError):
        spec.coerce_value(100)
    with pytest.raises(RegistryError):
        spec.coerce_value(-5)
