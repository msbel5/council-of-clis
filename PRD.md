# Council of CLIs — Product Requirements Document

**Status:** Alpha (v0.1.0)
**Last updated:** 2026-05-17

## 1. Problem

Developers who pay for multiple LLM subscriptions (ChatGPT Plus/Team for Codex, Claude Code,
GitHub Copilot Pro, Google AI Pro) routinely run all of them — but each one in its own terminal.
The dominant pattern is:

1. Open terminal A, paste prompt into `codex`.
2. Open terminal B, paste same prompt into `claude`.
3. Wait for both.
4. Eyeball-diff the outputs.
5. Decide which one to trust.
6. Re-paste into a third CLI for tiebreak when they disagree.

This wastes 5-15 minutes per architecture decision, loses the question across terminals, and
provides no record of what each CLI actually said. The user repeats project context
(stack, constraints, current branch state) into every CLI separately, often forgetting bits.

## 2. Solution

A local web app that:

- Takes one prompt
- Prepends a user-edited status block (project context)
- Sends to N CLIs in parallel
- Streams their stdout into one browser tab, side-by-side
- Saves a structured per-conversation record
- Optionally orchestrates the CLIs through multi-agent patterns (debate, MoA, cascade, router,
  consensus voting) so they refine each other's answers without manual copy-paste

## 3. Non-goals

- **Not** an API gateway. Council shells out to existing CLIs you already pay for; it doesn't
  add or replace authentication.
- **Not** a chat memory system. There's no conversation continuity across "sends" — each send
  is a fresh subprocess. Context lives in the status block + the user's pasted prompt.
- **Not** a model router that picks the cheapest provider. The user picks via UI checkboxes.
- **Not** a deployed SaaS. Designed for local use only. No multi-tenancy, no auth.
- **Not** a replacement for IDE LLM integrations. Council shines for "I need 4 opinions on
  this design", not "complete this line".

## 4. Users

- Senior engineers comparing LLM outputs daily.
- Architects using debate/consensus modes for decision records.
- Researchers studying multi-agent collaboration patterns.
- Solo developers who paid for Claude + Copilot + Codex and want every penny out.

## 5. Functional requirements

### 5.1 CLI execution

- F-1.1: Spawn each selected CLI as `asyncio.create_subprocess_exec`.
- F-1.2: Stream stdout and stderr line-by-line over WebSocket to the browser.
- F-1.3: Support both stdin-piped prompts (codex, gemini) and argv-passed prompts
  (claude --print, copilot -sp).
- F-1.4: Auto-detect installed CLIs via `shutil.which()` on server boot.
- F-1.5: Disable unavailable CLIs in the UI with hover-tooltip explaining install path.

### 5.2 Status injection

- F-2.1: Persistent file at `prompts/status.md` (gitignored).
- F-2.2: On every send, re-read the file (no cache) so hot-edits take effect immediately.
- F-2.3: Prepend status block to every prompt before sending to CLIs.
- F-2.4: User can edit status via dialog in the UI; saves immediately.
- F-2.5: First run seeds `status.md` from `status.example.md` if missing.

### 5.3 Conversations

- F-3.1: Each send creates `conversations/<timestamp>-<uuid6>/`.
- F-3.2: Save prompt to `prompt.md`.
- F-3.3: Save each CLI's stdout to `responses/<cli>__<label>.md` (label = round tag).
- F-3.4: Save mode final synthesis to `final.md` (for modes that produce one).
- F-3.5: Append timeline events to `events.jsonl` for replay/debug.

### 5.4 Orchestration modes

- F-4.1: Mode selector in UI; default `parallel`.
- F-4.2: Modes: `parallel`, `debate`, `cascade`, `moa`, `router`, `consensus`.
- F-4.3: Each mode is a coroutine in `modes.py`, registered in `MODES` dict.
- F-4.4: Modes emit phase events (`{"cli": "*", "kind": "phase", "data": "..."}`) for UI feedback.
- F-4.5: Modes pack inter-round prompts via canonical packers (`pack_for_revision`,
  `pack_for_critique`, etc.) — never paste raw history.

### 5.5 UI

- F-5.1: Single HTML page with vanilla JS; no build step.
- F-5.2: 4 panes (one per CLI) with status-light (installed/missing).
- F-5.3: Live streaming output per pane.
- F-5.4: Mode dropdown + CLI checkboxes + include-status toggle.
- F-5.5: Status block editor in modal dialog.
- F-5.6: Phase messages highlighted in accent color when modes emit them.
- F-5.7: Ctrl+Enter to send.

## 6. Non-functional requirements

- NF-1: Total LOC < 2000 (currently 1614).
- NF-2: Cold start to first response < 5 seconds (excluding CLI latency).
- NF-3: Memory footprint < 200 MB (excluding subprocess RSS).
- NF-4: No external network calls from the server itself (CLIs make their own).
- NF-5: pyright permissive + ruff clean.
- NF-6: Python 3.12+ only (no compat shims for older versions).

## 7. Mode protocols (deep dive)

### 7.1 Parallel

Round 1 only. All selected CLIs receive the same prompt, no awareness of each other.
Use when: you want to see raw differences.

### 7.2 Debate (2 rounds)

Round 1: independent answers (same as parallel).
Round 2: each CLI receives a packed prompt containing:
- the original task,
- its own R1 answer,
- the other CLIs' R1 answers,
- a directive to "either refine using insights from others, or stand alone with a 1-sentence
  justification" and to end with `VOTE: AGREE_WITH=<cli|self>` or `VOTE: STAND_ALONE`.

Use when: refining a position with peer awareness.

### 7.3 Cascade (4 steps)

Linear pipeline:
1. CLI A drafts.
2. CLI B critiques with verdict APPROVE/REVISE/REJECT.
3. CLI A revises addressing critique.
4. CLI C (or B) validates the revision.

Use when: code review workflow.

### 7.4 Mixture-of-Agents (proposer rounds + aggregator)

N proposer rounds (default 2). In round 2+, each proposer sees others' round (N-1) outputs and
proposes an improvement. Final round: an aggregator CLI synthesizes one merged answer with
inline `[from <cli>]` provenance.

Use when: hard architecture decision, you want the best raw quality.

### 7.5 Router

CLI A classifies the task into `CODE | ARCH | SHELL | RESEARCH`. Council routes to the
preferred specialist (codex for CODE, claude for ARCH, gemini for RESEARCH, copilot for SHELL —
falls back to whatever's available).

Use when: cost/latency matters more than diversity.

### 7.6 Consensus (max 3 rounds, early-stop on unanimity)

Same as Debate, but Council parses `VOTE:` lines and stops when all CLIs vote the same target.
If they never agree, stops at max rounds with last-round-of-first-CLI as final.

Use when: you need an explicit agreement signal in the artifact.

## 8. Scope cuts (deferred)

- Pluggable CLI registry (currently hardcoded for 4 tools).
- Re-streaming an old conversation in the UI (currently shows live only).
- Diff view across responses (read the .md files manually for now).
- Provider auth status check (rely on the CLI itself to report).
- Mobile/tablet UI (desktop-first, 4-column layout).
- Public deployment / hosted demo.
- PyPI package (use `pip install -e .` from git for now).

## 9. Risks

- **R-1**: A CLI hangs indefinitely (waiting for interactive input). Mitigation: TERM=dumb env
  + future cancel button + max-runtime per pane.
- **R-2**: Status block accidentally contains secrets. Mitigation: `status.md` gitignored;
  documented warning in README.
- **R-3**: WebSocket disconnect mid-stream. Mitigation: subprocess outputs continue, written
  to disk; UI reconnect (future).
- **R-4**: CLI vendor changes invocation flags. Mitigation: invocations isolated in `CLIS` dict
  in `server.py` — single-file patch on breakage.

## 10. Success criteria

- 5 minutes from `git clone` to first prompt sent and answered, given Python 3.12 + one CLI installed.
- 1 minute to add a new orchestration mode (one new function in `modes.py`, one entry in
  `MODES` dict, one dropdown option).
- < 50 lines of UI code to add a new feature like "save prompt as template".

## 11. Future work (post-v0.1)

- Persist mode results in a SQLite index for cross-conversation search.
- "Replay" button to re-run an old conversation with different CLIs/mode.
- Diff view between two responses (server-side using `difflib`).
- Status presets and per-conversation overrides.
- Plugin API for custom modes and custom CLI adapters.
- Optional Anthropic/OpenAI direct-SDK paths for when subscription CLIs aren't available.

## 12. Out of scope (won't do)

- Anything multi-user / multi-tenant.
- Anything that requires a cloud account.
- A general chat UI — this is specifically a multi-CLI fan-out tool.
