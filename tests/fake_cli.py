"""Fake CLI used by black-box end-to-end tests.

Reads prompt from stdin OR positional argv (last token), echoes it back deterministically,
and emits a fake "session_id: <uuid>" trailer so session-capture tests can verify the
parser. Behavior is controlled by env vars set per-test:

    FAKE_CLI_NAME       — label printed in output (default: "fake")
    FAKE_CLI_SLEEP      — seconds to sleep before responding (default: 0)
    FAKE_CLI_SID        — explicit session_id to print (default: a generated uuid)
    FAKE_CLI_FAIL       — if "1", exit 1 with stderr "fake failure"
    FAKE_CLI_ECHO_FULL  — if "1", echo every line of the incoming prompt (for DSU
                          tests that need to see what Council passed in). Default
                          is OFF — emits one summary line — to avoid exploding
                          stdout events for multi-round modes (debate, consensus)
                          where prompts grow quadratically.

This script is invoked as a normal subprocess by Council under test, so it exercises
the same code paths real CLIs hit.
"""

from __future__ import annotations

import os
import sys
import time
import uuid


def main() -> int:
    if os.environ.get("FAKE_CLI_FAIL") == "1":
        print("fake failure", file=sys.stderr)
        return 1

    name = os.environ.get("FAKE_CLI_NAME", "fake")
    sleep_s = float(os.environ.get("FAKE_CLI_SLEEP", "0") or "0")
    sid = os.environ.get("FAKE_CLI_SID") or uuid.uuid4().hex[:12]

    # Read prompt: try stdin first, then argv[1] if present.
    prompt = ""
    if not sys.stdin.isatty():
        try:
            prompt = sys.stdin.read()
        except Exception:
            prompt = ""
    if not prompt and len(sys.argv) > 1:
        prompt = sys.argv[-1]

    if sleep_s > 0:
        time.sleep(sleep_s)

    # Stream deterministic output. Default behavior is a SHORT echo (one summary
    # line) so multi-round modes (debate/consensus) don't drown the WebSocket in
    # echo events. Tests that need to see the full prompt (DSU verification) set
    # FAKE_CLI_ECHO_FULL=1 to opt in.
    echo_full = os.environ.get("FAKE_CLI_ECHO_FULL") == "1"
    if echo_full:
        lines = prompt.splitlines()
        print(f"[{name}] received: {len(lines)} lines")
        for line in lines[:200]:
            print(f"[{name}] | {line}")
        if len(lines) > 200:
            print(f"[{name}] | ... (truncated {len(lines) - 200} more lines)")
    else:
        print(f"[{name}] received: {prompt.strip()[:200]}")
    print(f"[{name}] reply: ok")
    print(f"session_id: {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
