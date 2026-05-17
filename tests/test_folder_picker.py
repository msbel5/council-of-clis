"""Tests for the folder-picker helper subprocess.

We can't realistically pop a Tk dialog in CI; instead we test the headless code paths
(no DISPLAY → structured error) and the wire format of all 3 exit shapes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent.parent / "scripts" / "folder_picker_helper.py"


def _run_helper(
    env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> tuple[int, str, str]:
    """Run the helper script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(HELPER), *(args or [])]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=10,
        env=env or {**os.environ, "DISPLAY": "", "WAYLAND_DISPLAY": ""},
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def test_helper_exists() -> None:
    assert HELPER.exists(), f"helper script not found at {HELPER}"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="headless DISPLAY check is Linux-specific",
)
def test_no_display_returns_error_json() -> None:
    """On Linux with DISPLAY blanked, helper must exit 2 with structured error JSON."""
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    rc, out, _err = _run_helper(env=env)
    assert rc == 2, f"expected exit 2, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "error" in payload
    assert "DISPLAY" in payload["error"] or "WAYLAND" in payload["error"]


def test_helper_output_is_json_only() -> None:
    """Whatever the exit code, stdout is parseable JSON (or empty)."""
    rc, out, _ = _run_helper()
    # rc in (0, 2) — either way, stdout must be a JSON object or empty
    if out:
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        assert any(k in parsed for k in ("path", "cancelled", "error"))
    assert rc in (0, 2)


def test_helper_accepts_initial_dir_arg(tmp_path: Path) -> None:
    """Helper must accept a positional initial_dir argument without crashing the parser."""
    rc, out, _ = _run_helper(args=[str(tmp_path)])
    assert rc in (0, 2)
    if out:
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
