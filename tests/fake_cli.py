"""Fake CLI used by black-box end-to-end tests.

Reads prompt from stdin OR positional argv (last token), echoes it back deterministically,
and emits a fake "session_id: <uuid>" trailer so session-capture tests can verify the
parser. Behavior is controlled by env vars set per-test:

    FAKE_CLI_NAME     — label printed in output (default: "fake")
    FAKE_CLI_SLEEP    — seconds to sleep before responding (default: 0)
    FAKE_CLI_SID      — explicit session_id to print (default: a generated uuid)
    FAKE_CLI_FAIL     — if "1", exit 1 with stderr "fake failure"

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

    # Stream deterministic output. Newlines flush so Council's line-by-line pump sees
    # multiple stdout events instead of one big chunk.
    # NOTE: prompt is echoed in full (no truncation) so e2e tests can verify what
    # Council actually passed in — including injected DSU blocks, status, etc.
    print(f"[{name}] received:")
    for line in prompt.splitlines():
        print(f"[{name}] | {line}")
    print(f"[{name}] reply: ok")
    print(f"session_id: {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
