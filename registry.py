"""Council CLI registry.

Loads built-in CLI definitions from `default_clis.toml`, then overlays any user-local
definitions from the platform config dir (Windows: %APPDATA%\\Council\\clis.toml, etc).
The runtime sees a single merged list, indexed by name.

Built-in entries can be DISABLED (not redefined) by the user setting
`disabled = true` for that name in their local file. Adding the same name with
`disabled = false` plus a new command override-replaces the built-in entry.

User overrides win on name collision. We never silently drop entries — invalid ones
raise `RegistryError` at load time. Same fail-fast policy applies to options_schema.
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


# ---- Option schema (v0.3) --------------------------------------------------

_OPTION_TYPES = ("enum", "bool", "number", "string")


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """One configurable option for a CLI.

    `argv` is the token list spliced into the spawn argv when the option is set;
    every "{value}" placeholder is replaced by the chosen value's string form.
    Argv tokens are NEVER shell-parsed.
    """

    name: str
    type: str  # one of _OPTION_TYPES
    argv: tuple[str, ...]
    default: object = None
    choices: tuple[object, ...] = ()
    min: float | None = None
    max: float | None = None
    description: str = ""

    def render_argv(self, value: object) -> tuple[str, ...]:
        """Substitute {value} in the argv template with the chosen value."""
        out: list[str] = []
        for tok in self.argv:
            if tok == "{value}":
                out.append(str(value))
            elif "{value}" in tok:
                out.append(tok.replace("{value}", str(value)))
            else:
                out.append(tok)
        return tuple(out)

    def coerce_value(self, raw: object) -> object:
        """Coerce a user-supplied value to the option's declared type and validate it."""
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                if raw.lower() in ("true", "1", "yes", "on"):
                    return True
                if raw.lower() in ("false", "0", "no", "off"):
                    return False
            raise RegistryError(f"option '{self.name}': bad bool {raw!r}")
        if self.type == "number":
            try:
                num = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise RegistryError(f"option '{self.name}': bad number {raw!r}") from exc
            if self.min is not None and num < self.min:
                raise RegistryError(f"option '{self.name}': {num} < min {self.min}")
            if self.max is not None and num > self.max:
                raise RegistryError(f"option '{self.name}': {num} > max {self.max}")
            # If declared as integer-looking (no fractional in default/choices), prefer int
            return int(num) if num.is_integer() else num
        if self.type == "enum":
            if raw not in self.choices:
                raise RegistryError(
                    f"option '{self.name}': {raw!r} not in choices {list(self.choices)}"
                )
            return raw
        if self.type == "string":
            return str(raw)
        raise RegistryError(f"option '{self.name}': unknown type {self.type!r}")


@dataclass(frozen=True, slots=True)
class CLIEntry:
    """One registered CLI.

    Session persistence (v0.4):
    - `resume_command`: alternate token list used when Council has a saved session_id
      for this CLI in the current conversation. Same {options}/{prompt} placeholders
      apply, plus {session_id} substitutes the captured id.
    - `session_id_pattern`: regex with one capture group; Council parses the CLI's
      stdout after the run completes and stores the captured group as the session id
      for the next turn.

    If a CLI does not support resumption, leave both fields empty/None — every turn
    starts fresh.
    """

    name: str
    command: tuple[str, ...]
    invocation_mode: str  # "stdin" | "argv" | "file"
    headless_supported: bool = True
    experimental: bool = False
    description: str = ""
    homepage: str = ""
    disabled: bool = False
    env: dict[str, str] = field(default_factory=dict)
    options_schema: tuple[OptionSpec, ...] = ()
    resume_command: tuple[str, ...] = ()
    session_id_pattern: str = ""

    @property
    def executable(self) -> str:
        return self.command[0]

    @property
    def supports_resume(self) -> bool:
        return bool(self.resume_command) and bool(self.session_id_pattern)

    def is_available(self) -> bool:
        """True if the CLI binary is on PATH right now."""
        if self.disabled or not self.headless_supported:
            return False
        return shutil.which(self.executable) is not None

    def option(self, name: str) -> OptionSpec | None:
        for spec in self.options_schema:
            if spec.name == name:
                return spec
        return None


# ---- Loading ---------------------------------------------------------------


def _parse_option(raw: object, cli_name: str, source: str) -> OptionSpec:
    if not isinstance(raw, dict):
        raise RegistryError(
            f"{source}: cli '{cli_name}' option entry must be a table, "
            f"got {type(raw).__name__}"
        )

    name = raw.get("name")
    typ = raw.get("type")
    argv = raw.get("argv")

    if not isinstance(name, str) or not name:
        raise RegistryError(f"{source}: cli '{cli_name}' has an option missing `name`")
    if typ not in _OPTION_TYPES:
        raise RegistryError(
            f"{source}: cli '{cli_name}' option '{name}' has invalid `type` {typ!r}; "
            f"must be one of {_OPTION_TYPES}"
        )
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise RegistryError(
            f"{source}: cli '{cli_name}' option '{name}' has invalid `argv` — "
            "must be a non-empty list of strings"
        )

    choices_raw = raw.get("choices", [])
    if not isinstance(choices_raw, list):
        raise RegistryError(
            f"{source}: cli '{cli_name}' option '{name}' `choices` must be a list"
        )
    choices: tuple[object, ...] = tuple(choices_raw)

    if typ == "enum" and len(choices) == 0:
        raise RegistryError(
            f"{source}: cli '{cli_name}' option '{name}' is enum but has no `choices`"
        )

    default = raw.get("default")
    if typ == "enum" and default is not None and default not in choices:
        raise RegistryError(
            f"{source}: cli '{cli_name}' option '{name}' default {default!r} "
            f"not in choices {list(choices)}"
        )

    min_v = raw.get("min")
    max_v = raw.get("max")
    if typ != "number" and (min_v is not None or max_v is not None):
        raise RegistryError(
            f"{source}: cli '{cli_name}' option '{name}' has min/max but type is {typ}, not number"
        )

    return OptionSpec(
        name=name,
        type=typ,
        argv=tuple(argv),
        default=default,
        choices=choices,
        min=float(min_v) if isinstance(min_v, (int, float)) else None,
        max=float(max_v) if isinstance(max_v, (int, float)) else None,
        description=str(raw.get("description", "")),
    )


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

    opts_raw = raw.get("options_schema") or []
    if not isinstance(opts_raw, list):
        raise RegistryError(
            f"{source}: cli '{name}' has invalid `options_schema` — must be a list of tables"
        )
    parsed_opts: list[OptionSpec] = []
    seen_names: set[str] = set()
    for entry in opts_raw:
        spec = _parse_option(entry, name, source)
        if spec.name in seen_names:
            raise RegistryError(
                f"{source}: cli '{name}' has duplicate option name {spec.name!r}"
            )
        seen_names.add(spec.name)
        parsed_opts.append(spec)

    resume_raw = raw.get("resume_command") or []
    if resume_raw and (
        not isinstance(resume_raw, list)
        or not all(isinstance(x, str) for x in resume_raw)
    ):
        raise RegistryError(
            f"{source}: cli '{name}' has invalid `resume_command` — "
            "must be a list of strings or omitted"
        )
    session_pattern_raw = raw.get("session_id_pattern", "")
    if session_pattern_raw and not isinstance(session_pattern_raw, str):
        raise RegistryError(
            f"{source}: cli '{name}' has invalid `session_id_pattern` — must be a string"
        )
    # Validate the regex compiles AND has exactly one capture group.
    # Codex bot v0.4 P2: a pattern like `(label):\s*(id)` would silently let
    # `extract_session_id` save the wrong group, corrupting resume on next turn.
    if session_pattern_raw:
        import re as _re
        try:
            _compiled = _re.compile(session_pattern_raw)
        except _re.error as exc:
            raise RegistryError(
                f"{source}: cli '{name}' session_id_pattern is not a valid regex: {exc}"
            ) from exc
        if _compiled.groups != 1:
            raise RegistryError(
                f"{source}: cli '{name}' session_id_pattern must have exactly one "
                f"capture group (the session id); found {_compiled.groups}. Use "
                "non-capturing groups `(?:...)` for grouping you don't want captured."
            )
    if bool(resume_raw) != bool(session_pattern_raw):
        raise RegistryError(
            f"{source}: cli '{name}' must declare BOTH resume_command and "
            "session_id_pattern, or NEITHER"
        )

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
        options_schema=tuple(parsed_opts),
        resume_command=tuple(resume_raw) if resume_raw else (),
        session_id_pattern=session_pattern_raw,
    )


def _load_toml(path: Path) -> list[CLIEntry]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise RegistryError(f"{path}: TOML parse error: {exc}") from exc
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
