"""Spawn a CLI subprocess with the right cwd, env, and invocation mode.

One choke-point so the rest of server.py never builds spawn arguments itself.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from registry import CLIEntry


@dataclass(frozen=True, slots=True)
class SpawnSpec:
    """Fully resolved spawn parameters — no I/O performed yet."""

    cli_name: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    invocation_mode: str  # "stdin" | "argv" | "file"
    prompt: str


def build_spawn_spec(
    entry: CLIEntry,
    prompt: str,
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> SpawnSpec:
    """Compose a SpawnSpec from a registry entry, a prompt, and a cwd.

    For `argv` mode, the prompt is appended as the final argv element at spawn time
    (not here, so the spec stays inspectable).
    For `stdin` mode, the prompt is piped to stdin.
    For `file` mode, the prompt is written to a temp file and the path appended (future use).
    """
    env = {**os.environ, "TERM": "dumb"}
    for k, v in entry.env.items():
        env[k] = v
    if extra_env:
        env.update(extra_env)
    return SpawnSpec(
        cli_name=entry.name,
        argv=entry.command,
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
        # Reserved for v0.3 — write prompt to a temp file, append path to argv.
        raise NotImplementedError("file invocation mode is reserved for v0.3")
    raise ValueError(f"unknown invocation_mode: {spec.invocation_mode}")
