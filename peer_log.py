"""Per-conversation peer activity log.

The peer log is the structured source-of-truth for what each CLI in a Council
conversation produced on each turn. It is written ONCE per `mode_fn` return
(server.py), recording each CLI's FINAL per-send response — not intermediate
r1/r2/critique fragments. The /dsu trigger reads the tail of this file to
build the cross-CLI peer brief injected into the next prompt.

Format: JSON Lines, one entry per CLI per turn.

    {"ts": 1747000000.0, "turn": 3, "cli": "codex", "mode": "parallel",
     "label": "", "text": "...", "len": 1234, "empty": false, "error": false}

`text` is stripped of any COUNCIL_DSU_* markers (no recursion if a peer
quoted the previous turn's DSU block in its response).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

# COUNCIL_DSU_START ... COUNCIL_DSU_END — stripped before persisting peer text
# so the next turn's DSU read doesn't quote a nested DSU block (recursion bloat).
# The marker constants themselves live in `dsu.py`; we use a robust pattern
# here that matches both whether or not the inner content has been rewrapped.
_DSU_BLOCK_RE = re.compile(
    r"<!--\s*COUNCIL_DSU_START.*?COUNCIL_DSU_END\s*-->",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class PeerLogEntry:
    """One CLI's finalized response on one turn of a Council conversation."""

    ts: float
    turn: int
    cli: str
    mode: str
    text: str
    label: str = ""
    len: int = 0
    empty: bool = False
    error: bool = False

    @classmethod
    def from_response(
        cls,
        *,
        turn: int,
        cli: str,
        mode: str,
        text: str,
        label: str = "",
        error: bool = False,
    ) -> PeerLogEntry:
        """Build an entry from a raw response, stripping nested DSU blocks.

        - `empty=True` when the response is whitespace-only (so DSU readers can
          skip it without losing position in the log).
        - `error=True` when the spawn failed; the text may be a stderr capture.
        """
        stripped_text = _DSU_BLOCK_RE.sub("", text).strip()
        return cls(
            ts=time.time(),
            turn=turn,
            cli=cli,
            mode=mode,
            label=label,
            text=stripped_text,
            len=len(stripped_text),
            empty=(not stripped_text),
            error=error,
        )

    def to_jsonl(self) -> str:
        """Serialize to a single JSON line (no trailing newline)."""
        return json.dumps(asdict(self), ensure_ascii=False)


def append_entries(path: Path, entries: Iterable[PeerLogEntry]) -> None:
    """Append entries to the peer log. Creates parents if missing.

    Single open() per call so a burst of writes hits the disk atomically per
    line on POSIX (one syscall per `f.write(line)`). Python's text mode write
    of a single line is the smallest atomic unit we can rely on without
    fcntl/msvcrt; this is fine because each entry stands alone — partial
    appends only corrupt the entry being written, never older ones.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry.to_jsonl() + "\n")


def read_tail(path: Path, n: int = 50) -> list[PeerLogEntry]:
    """Return the last N entries from the log.

    Returns an empty list if the file is missing. Malformed lines (corrupted
    by an interrupted write or a manual edit) are skipped — we never crash on
    a bad peer log, just lose visibility of the bad line.

    For typical Council conversations (≤100 turns × ≤4 CLIs = ≤400 entries)
    we read the whole file. This is cheap and avoids reverse-seek
    complications. If logs ever grow >1 MB, swap to a chunked reverse reader.
    """
    if not path.exists():
        return []
    out: list[PeerLogEntry] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                out.append(PeerLogEntry(**data))
            except TypeError:
                # Schema drift between versions — skip rather than crash.
                continue
    except OSError:
        return []
    return out[-n:] if n > 0 else out


def latest_per_cli(
    path: Path,
    *,
    exclude: str = "",
    skip_empty: bool = True,
    skip_error: bool = True,
) -> dict[str, PeerLogEntry]:
    """Last entry per CLI (by ts), keyed by cli name.

    - `exclude`: drop this CLI from the result (the receiver, who already has
      its own history via --resume; we never inject its own answer back).
    - `skip_empty`: drop entries with no text content.
    - `skip_error`: drop entries that come from failed runs (their text is
      typically a stderr fragment, useless as peer context).

    Iteration is forward through the tail; later entries overwrite earlier
    ones for the same CLI, so the final dict holds the most recent.
    """
    out: dict[str, PeerLogEntry] = {}
    for entry in read_tail(path, n=0):
        if entry.cli == exclude:
            continue
        if skip_empty and entry.empty:
            continue
        if skip_error and entry.error:
            continue
        out[entry.cli] = entry
    return out


__all__ = [
    "PeerLogEntry",
    "append_entries",
    "latest_per_cli",
    "read_tail",
]
