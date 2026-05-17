"""Conversation.set_cli_session must be safe under concurrent writers.

Defends against the v0.4 Codex bot P2 finding: parallel mode runs multiple CLI
tasks concurrently, and each task calls set_cli_session when it captures its
own session id. Without serialization the load → modify → write sequence
races and earlier writes get clobbered.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import server


@pytest.fixture
def isolated_conv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server.Conversation:
    """A Conversation whose disk state lives in tmp_path."""
    monkeypatch.setattr(server, "CONVERSATIONS", tmp_path)
    conv_id = "lock-test-1"
    return server.Conversation(conv_id)


@pytest.mark.asyncio
async def test_concurrent_set_cli_session_preserves_all_writers(
    isolated_conv: server.Conversation,
) -> None:
    """N CLIs writing their session id simultaneously must all land on disk.

    Pre-fix (sync `set_cli_session`): two tasks each call `load_cli_sessions`
    on the same empty store, write their own key in-memory, then both write
    to disk — the second write wins. This test would see only one key in the
    final file.

    Post-fix (async + asyncio.Lock): each call awaits the lock, reads the
    latest disk state, adds its key, writes; the next caller reads the
    just-written state.
    """
    n_clis = 8
    tasks = [
        asyncio.create_task(isolated_conv.set_cli_session(f"cli-{i}", f"sid-{i}"))
        for i in range(n_clis)
    ]
    await asyncio.gather(*tasks)

    on_disk = json.loads(isolated_conv.sessions_path.read_text(encoding="utf-8"))
    assert len(on_disk) == n_clis, f"expected {n_clis} keys, got {len(on_disk)}: {on_disk}"
    for i in range(n_clis):
        assert on_disk[f"cli-{i}"] == f"sid-{i}"


@pytest.mark.asyncio
async def test_repeated_writes_to_same_cli_keep_latest(
    isolated_conv: server.Conversation,
) -> None:
    """Same-CLI repeated writes (e.g. multi-round modes that hit the same CLI
    several times) must converge on the last-written value, not lose entries.
    """
    await isolated_conv.set_cli_session("codex", "first")
    await isolated_conv.set_cli_session("codex", "second")
    await isolated_conv.set_cli_session("claude", "claude-1")
    await isolated_conv.set_cli_session("codex", "third")

    on_disk = json.loads(isolated_conv.sessions_path.read_text(encoding="utf-8"))
    assert on_disk == {"codex": "third", "claude": "claude-1"}


@pytest.mark.asyncio
async def test_mixed_concurrent_and_sequential(
    isolated_conv: server.Conversation,
) -> None:
    """Realistic mode: a batch of parallel CLIs writes, then a second batch
    writes (next turn). Both batches must end up reflected.
    """
    # Turn 1: 3 parallel CLIs
    await asyncio.gather(
        isolated_conv.set_cli_session("a", "a1"),
        isolated_conv.set_cli_session("b", "b1"),
        isolated_conv.set_cli_session("c", "c1"),
    )
    # Turn 2: 2 of them resume with new session ids + a new one
    await asyncio.gather(
        isolated_conv.set_cli_session("a", "a2"),
        isolated_conv.set_cli_session("c", "c2"),
        isolated_conv.set_cli_session("d", "d1"),
    )

    on_disk = json.loads(isolated_conv.sessions_path.read_text(encoding="utf-8"))
    assert on_disk == {"a": "a2", "b": "b1", "c": "c2", "d": "d1"}
