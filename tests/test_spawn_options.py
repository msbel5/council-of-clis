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


# ---- {options} / {prompt} placeholder regression tests (Codex bot P1 fix) ----


def test_options_placeholder_spliced_in_position() -> None:
    """Options replace {options} token, preserving everything after."""
    entry = CLIEntry(
        name="gemini",
        command=("gemini", "{options}", "-p", "{prompt}"),
        invocation_mode="argv",
        options_schema=(
            OptionSpec(name="model", type="enum", argv=("--model", "{value}"),
                       choices=("a",), default="a"),
        ),
    )
    # With options
    assert apply_options(entry, {"model": "a"}) == (
        "gemini", "--model", "a", "-p", "{prompt}",
    )
    # Without options
    assert apply_options(entry, {}) == ("gemini", "-p", "{prompt}")


def test_no_options_placeholder_appends_at_end() -> None:
    """Legacy CLIs without {options} still get extras appended at end."""
    entry = CLIEntry(
        name="foo",
        command=("foo",),
        invocation_mode="argv",
        options_schema=(
            OptionSpec(name="m", type="enum", argv=("--m", "{value}"),
                       choices=("a",), default="a"),
        ),
    )
    assert apply_options(entry, {"m": "a"}) == ("foo", "--m", "a")


def test_spawn_prompt_placeholder_substituted() -> None:
    """spawn._resolve_argv_with_prompt substitutes {prompt} → actual prompt."""
    from spawn import _resolve_argv_with_prompt

    argv = ("gemini", "--model", "x", "-p", "{prompt}")
    assert _resolve_argv_with_prompt(argv, "hi") == (
        "gemini", "--model", "x", "-p", "hi",
    )


def test_spawn_prompt_appended_when_no_placeholder() -> None:
    """Legacy: no {prompt} in argv → prompt appended at end."""
    from spawn import _resolve_argv_with_prompt

    argv = ("foo", "--flag")
    assert _resolve_argv_with_prompt(argv, "hi") == ("foo", "--flag", "hi")


def test_schema_defaults_applied_when_user_omits_option() -> None:
    """Codex bot P1 #2: defaults must apply on first-send, not require popover open."""
    entry = CLIEntry(
        name="codex",
        command=("codex", "exec", "-", "{options}"),
        invocation_mode="stdin",
        options_schema=(
            OptionSpec(name="model", type="enum", argv=("--model", "{value}"),
                       choices=("gpt-5.4", "gpt-5.5"), default="gpt-5.4"),
            OptionSpec(name="sandbox", type="enum", argv=("--sandbox", "{value}"),
                       choices=("read-only", "workspace-write"), default="read-only"),
        ),
    )
    # User never opened the popover — options dict is empty
    result = apply_options(entry, {})
    # But defaults should still apply
    assert "--sandbox" in result
    assert "read-only" in result
    assert "--model" in result
    assert "gpt-5.4" in result


def test_user_override_takes_priority_over_default() -> None:
    entry = CLIEntry(
        name="codex",
        command=("codex", "{options}"),
        invocation_mode="argv",
        options_schema=(
            OptionSpec(name="model", type="enum", argv=("--model", "{value}"),
                       choices=("a", "b"), default="a"),
        ),
    )
    assert apply_options(entry, {"model": "b"}) == ("codex", "--model", "b")
    assert apply_options(entry, {}) == ("codex", "--model", "a")  # default kicks in


def test_bool_default_false_skipped() -> None:
    entry = CLIEntry(
        name="x",
        command=("x", "{options}"),
        invocation_mode="argv",
        options_schema=(
            OptionSpec(name="yolo", type="bool", argv=("--yolo",), default=False),
        ),
    )
    # bool default False → flag not included
    assert apply_options(entry, {}) == ("x",)


def test_bool_default_true_included() -> None:
    entry = CLIEntry(
        name="x",
        command=("x", "{options}"),
        invocation_mode="argv",
        options_schema=(
            OptionSpec(name="verbose", type="bool", argv=("-v",), default=True),
        ),
    )
    assert apply_options(entry, {}) == ("x", "-v")


def test_gemini_p_flag_gets_prompt_not_options() -> None:
    """Regression for Codex bot P1: `gemini -p` must consume the prompt, not --model."""
    from spawn import _resolve_argv_with_prompt

    entry = CLIEntry(
        name="gemini",
        command=("gemini", "{options}", "-p", "{prompt}"),
        invocation_mode="argv",
        options_schema=(
            OptionSpec(name="model", type="enum", argv=("--model", "{value}"),
                       choices=("gemini-2.5-pro",), default="gemini-2.5-pro"),
        ),
    )
    after_options = apply_options(entry, {"model": "gemini-2.5-pro"})
    assert after_options == ("gemini", "--model", "gemini-2.5-pro", "-p", "{prompt}")
    after_prompt = _resolve_argv_with_prompt(after_options, "what is 2+2")
    # The token immediately after -p must be the user prompt, NOT --model.
    p_idx = after_prompt.index("-p")
    assert after_prompt[p_idx + 1] == "what is 2+2"
