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
    """Return the entry's base argv with option-specified extras appended.

    Only options that appear in `entry.options_schema` are honored. Unknown option
    names are ignored (logged as a warning by the caller, not raised here, so a stale
    UI selection doesn't crash a spawn). For each known option:

    - bool: include the argv if value is truthy, skip if falsy
    - enum / number / string: substitute {value} placeholder(s) in the argv tokens

    All values are coerced + validated via `OptionSpec.coerce_value` (raises
    RegistryError on bad input).
    """
    extras: list[str] = []
    for opt in entry.options_schema:
        if opt.name not in options:
            continue
        raw = options[opt.name]
        if raw is None or (isinstance(raw, str) and not raw):
            continue
        coerced = opt.coerce_value(raw)
        if opt.type == "bool":
            if coerced:
                extras.extend(opt.argv)
        else:
            extras.extend(opt.render_argv(coerced))
    return tuple([*entry.command, *extras])


def build_spawn_spec(
    entry: CLIEntry,
    prompt: str,
    *,
    cwd: Path,
    options: Mapping[str, object] | None = None,
    extra_env: dict[str, str] | None = None,
) -> SpawnSpec:
    """Compose a SpawnSpec from a registry entry, a prompt, options, and a cwd.

    For `argv` mode, the prompt is appended as the final argv element at spawn time
    (in `spawn()` below, not here — keeps the spec inspectable).
    For `stdin` mode, the prompt is piped to stdin by the caller.
    """
    argv = apply_options(entry, options or {})
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


async def spawn(spec: SpawnSpec) -> asyncio.subprocess.Process:
    """Actually launch the subprocess.

    Returns the running process. Caller is responsible for reading stdout/stderr,
    handling stdin (for stdin mode), and waiting/cancelling.
    """
    if spec.invocation_mode == "stdin":
        return await asyncio.create_subprocess_exec(
            *spec.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spec.env,
            cwd=str(spec.cwd),
        )
    if spec.invocation_mode == "argv":
        return await asyncio.create_subprocess_exec(
            *spec.argv,
            spec.prompt,
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


__all__ = [
    "OptionSpec",
    "RegistryError",
    "SpawnSpec",
    "apply_options",
    "build_spawn_spec",
    "spawn",
]
