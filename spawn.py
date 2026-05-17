"""Spawn a CLI subprocess with the right cwd, env, and invocation mode.

One choke-point so the rest of server.py never builds spawn arguments itself.
v0.3 adds `apply_options` that splices per-CLI options into the argv as token lists.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from registry import CLIEntry, OptionSpec, RegistryError


@dataclass(frozen=True, slots=True)
class SpawnSpec:
    """Fully resolved spawn parameters — no I/O performed yet."""

    cli_name: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    invocation_mode: str  # "stdin" | "argv" | "file"
    prompt: str


def apply_options(entry: CLIEntry, options: Mapping[str, object]) -> tuple[str, ...]:
    """Return the entry's command with option-specified extras spliced in.

    Splice rule (Codex bot P1 fix):
    - If the command contains a literal "{options}" token, the extras list replaces
      that token (preserving everything that comes after, like "{prompt}" or a
      prompt-taking flag pair).
    - If no "{options}" placeholder, extras append at the end (legacy behavior; OK
      for stdin-mode CLIs and for argv-mode CLIs that don't have a flag whose value
      IS the prompt).

    This matters because some argv-mode CLIs use a flag like ``gemini -p <prompt>``
    or ``vibe --prompt <prompt>`` where the prompt is consumed as the flag's value.
    Appending option tokens after such a flag steals the slot meant for the prompt.

    Only options that appear in `entry.options_schema` are honored. Unknown option
    names are ignored so a stale UI selection doesn't crash a spawn. Per type:

    - bool   → include the argv tokens if value is truthy, skip if falsy
    - enum / number / string → substitute {value} placeholder(s) in the argv tokens

    Raises `RegistryError` on bad input (validated via `OptionSpec.coerce_value`).
    """
    extras: list[str] = []
    for opt in entry.options_schema:
        # Apply user-set value if present, else fall back to schema default.
        # This guarantees safety-critical defaults (e.g. codex --sandbox read-only)
        # are applied even on a first send when the user never opened the popover.
        # (Codex bot review P1 fix.)
        if opt.name in options:
            raw = options[opt.name]
            if raw is None or (isinstance(raw, str) and not raw):
                continue
        elif opt.default is not None:
            raw = opt.default
        else:
            continue
        coerced = opt.coerce_value(raw)
        if opt.type == "bool":
            if coerced:
                extras.extend(opt.argv)
        else:
            extras.extend(opt.render_argv(coerced))

    if "{options}" in entry.command:
        result: list[str] = []
        for tok in entry.command:
            if tok == "{options}":
                result.extend(extras)
            else:
                result.append(tok)
        return tuple(result)
    return tuple([*entry.command, *extras])


def _select_command(
    entry: CLIEntry, session_id: str | None
) -> tuple[str, ...]:
    """Return resume_command (with {session_id} substituted) if we have a session id
    and the entry supports resume; else the fresh command.
    """
    if session_id and entry.supports_resume:
        return tuple(
            session_id if tok == "{session_id}" else tok
            for tok in entry.resume_command
        )
    # Strip any stray {session_id} from the fresh command (defensive).
    return tuple(tok for tok in entry.command if tok != "{session_id}")


def build_spawn_spec(
    entry: CLIEntry,
    prompt: str,
    *,
    cwd: Path,
    options: Mapping[str, object] | None = None,
    extra_env: dict[str, str] | None = None,
    session_id: str | None = None,
) -> SpawnSpec:
    """Compose a SpawnSpec from a registry entry, a prompt, options, cwd, and an
    optional saved session_id (continues a CLI's prior turn when supported).

    For `argv` mode, the prompt is substituted into {prompt} or appended.
    For `stdin` mode, the prompt is piped to stdin by the caller.
    """
    # apply_options operates on a "base command" which may be the resume variant
    # when we have a session id — so swap entry.command for that variant briefly.
    base_argv = _select_command(entry, session_id)
    # Build an ephemeral CLIEntry-like for apply_options without dataclass mutation
    extras_entry = CLIEntry(
        name=entry.name,
        command=base_argv,
        invocation_mode=entry.invocation_mode,
        headless_supported=entry.headless_supported,
        experimental=entry.experimental,
        description=entry.description,
        homepage=entry.homepage,
        disabled=entry.disabled,
        env=entry.env,
        options_schema=entry.options_schema,
        resume_command=entry.resume_command,
        session_id_pattern=entry.session_id_pattern,
    )
    argv = apply_options(extras_entry, options or {})
    env = {**os.environ, "TERM": "dumb"}
    for k, v in entry.env.items():
        env[k] = v
    if extra_env:
        env.update(extra_env)
    return SpawnSpec(
        cli_name=entry.name,
        argv=argv,
        cwd=cwd,
        env=env,
        invocation_mode=entry.invocation_mode,
        prompt=prompt,
    )


def _resolve_argv_with_prompt(argv: tuple[str, ...], prompt: str) -> tuple[str, ...]:
    """Substitute the literal "{prompt}" token in argv with the actual prompt.

    If no "{prompt}" token is present, the prompt is appended at the end (legacy
    behavior — keeps backwards compat with CLIs declared before placeholders existed).
    """
    if "{prompt}" in argv:
        return tuple(prompt if t == "{prompt}" else t for t in argv)
    return (*argv, prompt)


async def spawn(spec: SpawnSpec) -> asyncio.subprocess.Process:
    """Actually launch the subprocess.

    Returns the running process. Caller is responsible for reading stdout/stderr,
    handling stdin (for stdin mode), and waiting/cancelling.
    """
    if spec.invocation_mode == "stdin":
        # stdin mode: prompt goes through PIPE, never the argv. Any "{prompt}"
        # placeholder still in the argv would be passed as a literal — strip it.
        clean_argv = tuple(t for t in spec.argv if t != "{prompt}")
        return await asyncio.create_subprocess_exec(
            *clean_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spec.env,
            cwd=str(spec.cwd),
        )
    if spec.invocation_mode == "argv":
        resolved = _resolve_argv_with_prompt(spec.argv, spec.prompt)
        return await asyncio.create_subprocess_exec(
            *resolved,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spec.env,
            cwd=str(spec.cwd),
        )
    if spec.invocation_mode == "file":
        # Reserved for v0.4 — write prompt to a temp file, append path to argv.
        raise NotImplementedError("file invocation mode is reserved for v0.4")
    raise ValueError(f"unknown invocation_mode: {spec.invocation_mode}")


def extract_session_id(entry: CLIEntry, captured_stdout: str) -> str | None:
    """Parse the CLI's stdout for a session id using its declared pattern.

    Returns None if the CLI doesn't declare a pattern, or if no match was found.
    The pattern must have exactly one capture group — the session id itself.
    """
    if not entry.session_id_pattern:
        return None
    import re

    match = re.search(entry.session_id_pattern, captured_stdout)
    if match is None:
        return None
    try:
        return match.group(1)
    except IndexError:
        # Pattern compiled but has no capture group — treat as no match.
        return None


__all__ = [
    "OptionSpec",
    "RegistryError",
    "SpawnSpec",
    "apply_options",
    "build_spawn_spec",
    "extract_session_id",
    "spawn",
]
