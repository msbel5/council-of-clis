"""Unit tests for spawn.apply_options — exact-argv output per option type."""

from __future__ import annotations

import pytest

from registry import CLIEntry, OptionSpec, RegistryError
from spawn import apply_options, build_spawn_spec

# ---- Helpers ---------------------------------------------------------------


def _entry(*opts: OptionSpec, command: tuple[str, ...] = ("foo",)) -> CLIEntry:
    return CLIEntry(
        name="foo",
        command=command,
        invocation_mode="argv",
        options_schema=opts,
    )


# ---- apply_options ---------------------------------------------------------


def test_no_options_returns_base_command() -> None:
    entry = _entry()
    assert apply_options(entry, {}) == ("foo",)


def test_enum_option_substitutes_value() -> None:
    entry = _entry(
        OptionSpec(
            name="model",
            type="enum",
            argv=("--model", "{value}"),
            choices=("a", "b"),
            default="a",
        ),
    )
    assert apply_options(entry, {"model": "b"}) == ("foo", "--model", "b")


def test_enum_option_invalid_value_raises() -> None:
    entry = _entry(
        OptionSpec(
            name="model",
            type="enum",
            argv=("--model", "{value}"),
            choices=("a", "b"),
        ),
    )
    with pytest.raises(RegistryError, match="not in choices"):
        apply_options(entry, {"model": "c"})


def test_bool_true_includes_argv() -> None:
    entry = _entry(
        OptionSpec(name="yolo", type="bool", argv=("--yolo",), default=False),
    )
    assert apply_options(entry, {"yolo": True}) == ("foo", "--yolo")


def test_bool_false_skips_argv() -> None:
    entry = _entry(
        OptionSpec(name="yolo", type="bool", argv=("--yolo",), default=False),
    )
    assert apply_options(entry, {"yolo": False}) == ("foo",)


def test_bool_string_truthy_coerced() -> None:
    entry = _entry(
        OptionSpec(name="yolo", type="bool", argv=("--yolo",)),
    )
    assert apply_options(entry, {"yolo": "true"}) == ("foo", "--yolo")
    assert apply_options(entry, {"yolo": "false"}) == ("foo",)


def test_number_option_passes_value() -> None:
    entry = _entry(
        OptionSpec(
            name="max_turns",
            type="number",
            argv=("--max-turns", "{value}"),
            min=1,
            max=20,
            default=3,
        ),
    )
    assert apply_options(entry, {"max_turns": 5}) == ("foo", "--max-turns", "5")


def test_number_option_out_of_range_raises() -> None:
    entry = _entry(
        OptionSpec(
            name="max_turns",
            type="number",
            argv=("--max-turns", "{value}"),
            min=1,
            max=20,
        ),
    )
    with pytest.raises(RegistryError, match=">"):
        apply_options(entry, {"max_turns": 100})


def test_template_inside_token_substituted() -> None:
    """argv token can have {value} interpolated, not just be the placeholder."""
    entry = _entry(
        OptionSpec(
            name="reasoning",
            type="enum",
            argv=("--config", 'model_reasoning_effort="{value}"'),
            choices=("low", "high"),
            default="low",
        ),
    )
    assert apply_options(entry, {"reasoning": "high"}) == (
        "foo",
        "--config",
        'model_reasoning_effort="high"',
    )


def test_unknown_option_silently_ignored() -> None:
    entry = _entry(
        OptionSpec(name="model", type="enum", argv=("--model", "{value}"), choices=("a",)),
    )
    # Stale UI selection shouldn't crash the spawn.
    assert apply_options(entry, {"model": "a", "ghost_opt": "x"}) == ("foo", "--model", "a")


def test_empty_string_value_skipped() -> None:
    entry = _entry(
        OptionSpec(name="model", type="enum", argv=("--model", "{value}"), choices=("a",)),
    )
    assert apply_options(entry, {"model": ""}) == ("foo",)


def test_none_value_skipped() -> None:
    entry = _entry(
        OptionSpec(name="model", type="enum", argv=("--model", "{value}"), choices=("a",)),
    )
    assert apply_options(entry, {"model": None}) == ("foo",)


def test_multiple_options_in_declared_order() -> None:
    """apply_options walks options_schema in order; order matters for diffability."""
    entry = _entry(
        OptionSpec(name="model", type="enum", argv=("--model", "{value}"),
                   choices=("a", "b"), default="a"),
        OptionSpec(name="sandbox", type="enum", argv=("--sandbox", "{value}"),
                   choices=("ro", "rw"), default="ro"),
    )
    assert apply_options(entry, {"sandbox": "rw", "model": "b"}) == (
        "foo",
        "--model",
        "b",
        "--sandbox",
        "rw",
    )


# ---- build_spawn_spec ------------------------------------------------------


def test_build_spawn_spec_threads_options(tmp_path: object) -> None:
    from pathlib import Path

    entry = _entry(
        OptionSpec(name="model", type="enum", argv=("--model", "{value}"),
                   choices=("a", "b"), default="a"),
        command=("codex", "exec", "-"),
    )
    spec = build_spawn_spec(
        entry,
        "hello",
        cwd=Path(tmp_path) if isinstance(tmp_path, str) else tmp_path,
        options={"model": "b"},
    )
    assert spec.argv == ("codex", "exec", "-", "--model", "b")
    assert spec.cli_name == "foo"
    assert spec.invocation_mode == "argv"
