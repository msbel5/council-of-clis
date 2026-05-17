# Architecture

## Layer diagram

```
┌───────────────────────────────────────────────────────────────────┐
│  Browser (static/index.html + app.js)                             │
│  - prompt textarea, CLI checkboxes, mode dropdown                 │
│  - 4 panes streaming stdout + phase messages                      │
│  - status editor dialog                                           │
└─────────────┬─────────────────────────────────────────────────────┘
              │ WebSocket /ws/<conv_id>
              ▼
┌───────────────────────────────────────────────────────────────────┐
│  server.py (FastAPI + uvicorn)                                    │
│  ├─ ws_stream():       receives {action, prompt, clis, mode}      │
│  ├─ build_full_prompt(): prepends prompts/status.md               │
│  ├─ stream_cli():      spawns one subprocess, pumps stdout/stderr │
│  │                     line-by-line to WS, captures full stdout   │
│  └─ MODES dispatch:    delegates to modes.<mode_fn>               │
└─────────────┬─────────────────────────────────────────────────────┘
              │ run_cli_wrapped(cli, prompt, ws, conv, label) → str
              ▼
┌───────────────────────────────────────────────────────────────────┐
│  modes.py                                                         │
│  - parallel, debate, cascade, moa, router, consensus              │
│  - prompt packers for inter-round messages                        │
│  - VOTE: parser for consensus convergence                         │
└─────────────┬─────────────────────────────────────────────────────┘
              │ asyncio.create_subprocess_exec
              ▼
┌───────────────────────────────────────────────────────────────────┐
│  External CLI subprocesses (in parallel)                          │
│  codex exec -  │  claude --print  │  copilot -sp  │  gemini -p   │
└───────────────────────────────────────────────────────────────────┘
```

## Module contracts

### server.py

| Function | Returns | Notes |
|---|---|---|
| `load_status()` | str | Re-reads `prompts/status.md` every call (no cache) |
| `build_full_prompt(prompt, include_status)` | str | Prepends `<!-- ALCYONE STATUS -->` block |
| `stream_cli(cli, prompt, ws, conv, label)` | str (captured stdout) | Returns text so modes can chain |
| `ws_stream(ws, conv_id)` | None | One WS per conversation; dispatches mode |

### modes.py

Every mode coroutine has the signature:

```python
async def mode_<name>(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
    **kwargs,
) -> ModeResult: ...
```

Where `RunCLI` is the wired version of `stream_cli` returning captured text.

`ModeResult` is a `TypedDict`:

```python
class ModeResult(TypedDict):
    mode: str
    rounds: int
    final_text: str
    per_cli_history: dict[str, list[str]]
```

Modes emit phase messages via `await ws.send_json({"cli": "*", "kind": "phase", "data": "..."})`.

### Prompt packers

| Packer | When | What it produces |
|---|---|---|
| `pack_for_revision(orig, self_answer, others)` | Debate R2+, MoA R2+, Consensus R2+ | "Your previous answer / others' answers / refine or stand_alone, end with VOTE:" |
| `pack_for_critique(orig, draft, drafter_cli)` | Cascade step 2, validator | "Critique this draft / VERDICT: APPROVE/REVISE/REJECT" |
| `pack_for_revision_after_critique(orig, my_draft, critique, critic)` | Cascade step 3 | "Address the critique / STATUS: REVISED or UNCHANGED" |
| `pack_for_aggregation(orig, proposals)` | MoA final | "Synthesize one best answer / cite [from <cli>] inline" |

These never include the full chat history — only the structured shorthand. Codex review
confirms this is better than raw paste for both quality and token efficiency.

## WebSocket message envelopes

```json
{ "cli": "codex", "kind": "stdout", "data": "...", "label": "r1" }
{ "cli": "codex", "kind": "stderr", "data": "...", "label": "r1" }
{ "cli": "codex", "kind": "done",   "data": "exit=0", "label": "r1" }
{ "cli": "codex", "kind": "error",  "data": "Process failed: ...", "label": "r1" }

{ "cli": "*", "kind": "phase",     "data": "MoA proposer round 2/2" }
{ "cli": "*", "kind": "info",      "data": "mode=moa → CLIs [...]" }
{ "cli": "*", "kind": "batch_done","data": "mode=moa rounds=3 final=4823c" }
{ "cli": "*", "kind": "error",     "data": "..." }
```

## File-system layout (runtime)

```
council-of-clis/
├── server.py
├── modes.py
├── pyproject.toml
├── static/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── prompts/
│   ├── status.example.md      ← committed (template)
│   └── status.md              ← gitignored (your private context)
├── conversations/             ← gitignored (per-user runtime data)
│   └── <id>/
│       ├── prompt.md
│       ├── events.jsonl
│       ├── final.md
│       └── responses/<cli>__<label>.md
├── examples/
│   └── status-templates/
├── docs/
│   ├── modes-deep-dive.md
│   └── cli-installation.md
├── tests/
│   └── test_modes.py
└── .github/workflows/ci.yml
```

## Why no database

A SQLite index for cross-conversation search is on the roadmap. For now, the file system is
the database: `events.jsonl` is append-only and parsable with any tool, and `.md` files are
plain text. This keeps the dependency surface tiny and makes debugging a `cat` away.

## Why no auth

Council binds to `127.0.0.1:8765` by default. If you want LAN access, you accept the trust
boundary and add auth yourself. Out of scope for v0.x.

## Cross-platform notes

- Paths in code use `pathlib.Path`; no Windows/POSIX assumptions.
- Subprocess `env={"TERM": "dumb"}` to defeat CLIs that buffer differently in TTY vs pipe modes.
- WebSocket message ordering: each CLI's lines arrive in order per-pane, but interleaving
  between panes is unspecified (deliberate — they run truly in parallel).

## How to add a new mode

1. Write `async def mode_<name>(prompt, clis, ws, conv, run_cli, **kw) -> ModeResult` in `modes.py`.
2. Add it to `MODES` dict at the bottom of `modes.py`.
3. Add one `<option>` to the `<select>` in `static/index.html`.
4. Add a row to the modes table in `README.md` and `docs/modes-deep-dive.md`.
5. Add a unit test in `tests/test_modes.py` with `_FakeRunCLI`.

## How to add a new CLI

1. Add an entry to the `CLIS` dict in `server.py` with command, mode (`stdin`|`argv`), and
   availability check.
2. Add a checkbox to `static/index.html`.
3. Add a row to the CLI table in `README.md`.
