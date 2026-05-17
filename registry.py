"""Council CLI registry.

Loads built-in CLI definitions from `default_clis.toml`, then overlays any user-local
definitions from the platform config dir (Windows: %APPDATA%\\Council\\clis.toml, etc).
The runtime sees a single merged list, indexed by name.

Built-in entries can be DISABLED (not redefined) by the user setting
`disabled = true` for that name in their local file. Adding the same name with
`disabled = false` plus a new command override-replaces the built-in entry.

User overrides win on name collision. We never silently drop entries — invalid ones
raise `RegistryError` at load time.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

PACKAGE_ROOT = Path(__file__).resolve().parent
PACKAGE_DEFAULTS = PACKAGE_ROOT / "default_clis.toml"
USER_CONFIG_DIR = Path(platformdirs.user_config_dir("Council", appauthor=False))
USER_CONFIG_FILE = USER_CONFIG_DIR / "clis.toml"


class RegistryError(ValueError):
    """Raised when a registry entry is missing required fields or has bad shape."""


@dataclass(frozen=True, slots=True)
class CLIEntry:
    """One registered CLI."""

    name: str
    command: tuple[str, ...]
    invocation_mode: str  # "stdin" | "argv" | "file"
    headless_supported: bool = True
    experimental: bool = False
    description: str = ""
    homepage: str = ""
    disabled: bool = False
    env: dict[str, str] = field(default_factory=dict)

    @property
    def executable(self) -> str:
        return self.command[0]

    def is_available(self) -> bool:
        """True if the CLI binary is on PATH right now."""
        if self.disabled or not self.headless_supported:
            return False
        return shutil.which(self.executable) is not None


# ---- Loading ---------------------------------------------------------------


def _parse_one(raw: dict[str, object], source: str) -> CLIEntry:
    name = raw.get("name")
    cmd = raw.get("command")
    mode = raw.get("invocation_mode", "stdin")

    if not isinstance(name, str) or not name:
        raise RegistryError(f"{source}: a [[cli]] entry is missing `name`")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise RegistryError(
            f"{source}: cli '{name}' has invalid `command` — must be a non-empty list of strings"
        )
    if mode not in ("stdin", "argv", "file"):
        raise RegistryError(
            f"{source}: cli '{name}' has invalid `invocation_mode` {mode!r}; "
            "must be one of stdin|argv|file"
        )

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()
    ):
        raise RegistryError(f"{source}: cli '{name}' has invalid `env` — must be str→str map")

    return CLIEntry(
        name=name,
        command=tuple(cmd),
        invocation_mode=mode,
        headless_supported=bool(raw.get("headless_supported", True)),
        experimental=bool(raw.get("experimental", False)),
        description=str(raw.get("description", "")),
        homepage=str(raw.get("homepage", "")),
        disabled=bool(raw.get("disabled", False)),
        env={str(k): str(v) for k, v in env_raw.items()},
    )


def _load_toml(path: Path) -> list[CLIEntry]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        data = tomllib.load(f)
    raw_entries = data.get("cli", [])
    if not isinstance(raw_entries, list):
        raise RegistryError(f"{path}: top-level [[cli]] must be an array")
    return [_parse_one(r, str(path)) for r in raw_entries if isinstance(r, dict)]


def load_registry(
    *,
    package_defaults_path: Path | None = None,
    user_config_path: Path | None = None,
) -> dict[str, CLIEntry]:
    """Load defaults + user overrides; user wins on name collision.

    Returns dict keyed by CLI name. Order is preserved (defaults first, then user-only entries).
    """
    defaults_path = package_defaults_path or PACKAGE_DEFAULTS
    user_path = user_config_path or USER_CONFIG_FILE

    by_name: dict[str, CLIEntry] = {}
    for entry in _load_toml(defaults_path):
        by_name[entry.name] = entry
    for entry in _load_toml(user_path):
        by_name[entry.name] = entry  # user override
    return by_name


def ensure_user_config_seeded() -> Path:
    """If user config dir is missing, create it with an empty placeholder file.

    Returns the user config file path (which may not exist yet on first call).
    """
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not USER_CONFIG_FILE.exists():
        USER_CONFIG_FILE.write_text(
            "# Council — local CLI overrides.\n"
            "# Add [[cli]] entries here to add or override built-in CLIs.\n"
            "# Schema: see default_clis.toml in the council-of-clis package.\n",
            encoding="utf-8",
        )
    return USER_CONFIG_FILE
