"""Project-folder trust model.

When a conversation specifies `project_dir`, every CLI subprocess is spawned with
`cwd=project_dir`. That means the CLI can read (and depending on its own sandbox
settings, write/execute) anything under that folder.

We require **explicit user trust per folder** before spawning into it. The trust list
is stored in the platform config dir (alongside `clis.toml`). System roots like `/`,
`C:\\Windows`, `/etc` are blocked unconditionally — no trust override.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import platformdirs

USER_CONFIG_DIR = Path(platformdirs.user_config_dir("Council", appauthor=False))
TRUST_STORE = USER_CONFIG_DIR / "trusted_folders.json"


# System paths that can NEVER be a project_dir, regardless of user trust.
# `EXACT` blocks only the literal path. `DESCENDANTS` blocks the path and everything under it.
# (Root `/` and `C:/` go in EXACT — every other path is under root, so we can't blanket-block.)
_FORBIDDEN_EXACT_POSIX = (Path("/"),)
_FORBIDDEN_DESCENDANTS_POSIX = (
    Path("/etc"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/boot"),
    Path("/proc"),
    Path("/sys"),
    Path("/var/log"),
    Path("/Library"),
    Path("/System"),
)
_FORBIDDEN_EXACT_WIN = (Path("C:/"), Path("C:\\"))
_FORBIDDEN_DESCENDANTS_WIN = (
    Path("C:/Windows"),
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
    Path("C:/ProgramData"),
)


class TrustError(ValueError):
    """Raised when a project_dir cannot be trusted (forbidden or unresolved)."""


@dataclass(frozen=True, slots=True)
class TrustDecision:
    """Result of checking a candidate project_dir."""

    canonical: Path
    is_trusted: bool
    reason: str  # "trusted" | "forbidden:<path>" | "needs-approval" | "not-a-directory"


def _forbidden_exact() -> tuple[Path, ...]:
    if sys.platform.startswith("win"):
        return _FORBIDDEN_EXACT_WIN
    return _FORBIDDEN_EXACT_POSIX


def _forbidden_descendants() -> tuple[Path, ...]:
    if sys.platform.startswith("win"):
        return _FORBIDDEN_DESCENDANTS_WIN
    return _FORBIDDEN_DESCENDANTS_POSIX


def _is_under(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` or any descendant. Case-insensitive on Windows."""
    try:
        child_resolved = child.resolve(strict=False)
        parent_resolved = parent.resolve(strict=False)
    except OSError:
        return False
    if sys.platform.startswith("win"):
        c = str(child_resolved).lower()
        p = str(parent_resolved).lower()
        return c == p or c.startswith(p + os.sep.lower()) or c.startswith(p + "/")
    return child_resolved == parent_resolved or parent_resolved in child_resolved.parents


def canonicalize(raw: str | Path) -> Path:
    """Resolve to absolute, follow symlinks. Raises TrustError for non-existent dirs."""
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TrustError(f"cannot resolve {raw!r}: {exc}") from exc
    if not resolved.is_dir():
        raise TrustError(f"{resolved} is not a directory")
    return resolved


def is_forbidden(canonical: Path) -> str | None:
    """Return the forbidden parent that contains `canonical`, or None if safe.

    EXACT roots (like `/`, `C:\\`) only match if the path is literally that path.
    DESCENDANT roots (like `/etc`, `C:\\Windows`) match the path or any descendant.
    """
    try:
        resolved = canonical.resolve(strict=False)
    except OSError:
        resolved = canonical
    for forbidden in _forbidden_exact():
        try:
            if resolved == forbidden.resolve(strict=False):
                return str(forbidden)
        except OSError:
            continue
    for forbidden in _forbidden_descendants():
        if _is_under(canonical, forbidden):
            return str(forbidden)
    return None


# ---- Trust store -----------------------------------------------------------


def _load_trust_store() -> dict[str, dict[str, object]]:
    if not TRUST_STORE.exists():
        return {}
    try:
        data = json.loads(TRUST_STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Trusted folders schema: {"<canonical_path>": {"trusted_at": <unix_ts>, "note": "..."}}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _save_trust_store(store: dict[str, dict[str, object]]) -> None:
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TRUST_STORE.write_text(
        json.dumps(store, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def list_trusted() -> list[str]:
    return sorted(_load_trust_store().keys())


def is_trusted(canonical: Path) -> bool:
    return str(canonical) in _load_trust_store()


def trust_folder(canonical: Path, *, note: str = "") -> None:
    """Add a folder to the trust store. Forbidden roots are refused."""
    forbidden = is_forbidden(canonical)
    if forbidden is not None:
        raise TrustError(f"refusing to trust forbidden path under {forbidden}: {canonical}")
    store = _load_trust_store()
    store[str(canonical)] = {"trusted_at": int(time.time()), "note": note}
    _save_trust_store(store)


def untrust_folder(canonical: Path) -> bool:
    """Remove a folder from the trust store. Returns True if it was present."""
    store = _load_trust_store()
    if str(canonical) in store:
        del store[str(canonical)]
        _save_trust_store(store)
        return True
    return False


# ---- Main check -----------------------------------------------------------


def check(project_dir: str | Path | None) -> TrustDecision:
    """Evaluate a candidate project_dir.

    - None or "" → fall through to server cwd (always trusted).
    - Forbidden under system roots → refused outright.
    - Already in trust store → trusted.
    - Otherwise → needs explicit approval before spawn.
    """
    if not project_dir:
        return TrustDecision(canonical=Path.cwd(), is_trusted=True, reason="trusted")
    try:
        canonical = canonicalize(project_dir)
    except TrustError as exc:
        return TrustDecision(
            canonical=Path(str(project_dir)),
            is_trusted=False,
            reason=f"not-a-directory: {exc}",
        )
    forbidden = is_forbidden(canonical)
    if forbidden is not None:
        return TrustDecision(canonical=canonical, is_trusted=False, reason=f"forbidden:{forbidden}")
    if is_trusted(canonical):
        return TrustDecision(canonical=canonical, is_trusted=True, reason="trusted")
    return TrustDecision(canonical=canonical, is_trusted=False, reason="needs-approval")
