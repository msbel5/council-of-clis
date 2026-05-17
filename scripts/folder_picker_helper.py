"""Standalone folder-picker helper.

Spawned as a short-lived subprocess by `/api/fs/pick-folder` so the GUI event loop
never lives inside the FastAPI request thread. Codex review explicitly flagged this.

Exit codes + stdout JSON:
    0  {"path": "<absolute path>"}      user picked a folder
    0  {"cancelled": true}              user dismissed the dialog
    2  {"error": "<reason>"}            cannot open dialog (no DISPLAY, missing tk,
                                         etc.) — server returns 503

Usage:
    python folder_picker_helper.py [initial_dir] [title]
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress


def main() -> int:
    initial_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")
    title = sys.argv[2] if len(sys.argv) > 2 else "Choose project folder"

    # Headless detection on Linux/POSIX — no DISPLAY usually means no GUI session.
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if sys.platform.startswith("linux") and not has_display:
        print(json.dumps({"error": "no DISPLAY/WAYLAND_DISPLAY — headless environment"}))
        return 2

    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        print(json.dumps({"error": f"tkinter not available: {exc}"}))
        return 2

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(json.dumps({"error": f"cannot init Tk: {exc}"}))
        return 2

    try:
        root.withdraw()
        with suppress(tk.TclError):
            # Some Linux WMs don't support -topmost; ignore.
            root.attributes("-topmost", True)
        picked = filedialog.askdirectory(
            initialdir=initial_dir,
            title=title,
            mustexist=True,
        )
    finally:
        with suppress(Exception):
            root.destroy()

    if not picked:
        print(json.dumps({"cancelled": True}))
        return 0
    print(json.dumps({"path": os.path.abspath(picked)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
