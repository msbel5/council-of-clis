# Council of CLIs

> Route one prompt to **codex**, **claude**, **copilot**, **gemini** at the same time. Watch them
> debate, cascade, vote, or synthesize one final answer — in your browser.

![status](https://img.shields.io/badge/status-alpha-orange)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

## What this is

A single-file FastAPI server + 1-page web UI that spawns up to four LLM command-line tools as
subprocesses, streams their stdout into 4 side-by-side panes, and orchestrates them through
research-backed multi-agent patterns (Mixture-of-Agents, Debate, Cascade, Router, Consensus).

Built for people who already have CLI subscriptions (ChatGPT Plus/Team for Codex, Claude Code,
GitHub Copilot Pro, Google AI Pro) and want to **stop pasting the same prompt into four
terminals**.

## Why

You probably hit at least one of these every week:

- Two LLMs disagree about an architecture decision. You alt-tab between terminals comparing answers.
- One LLM gives weak code, you copy it to a second LLM with "is this any good?". Manual loop.
- A bug fix needs three opinions before you trust the patch. You do it serially.
- You realize halfway through a chat that you forgot to mention "we use pydantic v2 not v1".
  Now you have to restart with the context every time.

Council solves all four with: parallel fan-out, structured peer-review modes, an injected
status block, and persistent per-conversation transcripts.

## Quickstart (Windows PowerShell)

```powershell
git clone https://github.com/msbel5/council-of-clis.git
cd council-of-clis

# Python 3.12+ required. uv is recommended.
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"

# First run seeds `prompts/status.md` from `prompts/status.example.md` (gitignored after).
python server.py
```

Open <http://localhost:8765>.

### Linux / macOS

```bash
git clone https://github.com/msbel5/council-of-clis.git
cd council-of-clis
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python server.py
```

## CLI requirements

Council shells out to whichever CLIs you have installed. Missing ones are auto-disabled in the UI.

| CLI | Install | Invocation pattern |
|---|---|---|
| **codex** | `npm install -g @openai/codex-cli` | `codex exec - …` (stdin) |
| **claude** | `npm install -g @anthropic-ai/claude-code` | `claude --print "<prompt>"` |
| **copilot** | `npm install -g @github/copilot` | `copilot -sp "<prompt>"` |
| **gemini** | `npm install -g @google/gemini-cli` | `gemini -p "<prompt>"` |

Need none of the above to install Council — but you need at least one CLI to do useful work.

## Orchestration modes

Pick from the dropdown in the UI before sending:

| Mode | Behavior | Best for | Paper |
|---|---|---|---|
| **parallel** | Each CLI answers independently. No interaction. | See raw differences. | — |
| **debate** | R1: all answer. R2: each sees others, revises. | Refine a position. | Du et al. 2023 — [arxiv 2305.14325](https://arxiv.org/abs/2305.14325) |
| **cascade** | Drafter → critic → reviser → validator. | Code review pipeline. | Self-Refine — [arxiv 2303.17651](https://arxiv.org/abs/2303.17651) |
| **moa** | Mixture-of-Agents: N proposer rounds + final aggregator. | Best raw quality (+5-15%). | Wang et al. 2024 — [arxiv 2406.04692](https://arxiv.org/abs/2406.04692) |
| **router** | First CLI classifies task type, routes to specialist. | Cost/latency optimization. | RouteLLM — [arxiv 2406.18665](https://arxiv.org/abs/2406.18665) |
| **consensus** | Debate + `VOTE:` lines until unanimous (max 3 rounds). | Need explicit agreement signal. | — |

Per Codex review, **2-round critique is the sweet spot** — more rounds usually burn tokens
without improving answer quality.

## Status block (the "no more re-explaining your project" feature)

Every prompt Council sends is prepended with a status block from `prompts/status.md`. Edit it
once in the UI (📋 button) and every subsequent prompt — to every CLI — gets that context.

The template lives at `prompts/status.example.md`; your personal version at `prompts/status.md`
is gitignored. See [`examples/status-templates/`](examples/status-templates/) for ready-made
templates (general coding, research, design review).

## Conversations

Each "send" creates a directory under `conversations/<id>/`:

```
conversations/2026-05-17-abc123/
├── prompt.md                   ← what you typed
├── events.jsonl                ← timeline (start, stdout chunks, done)
├── final.md                    ← orchestrated final (for modes that produce one)
└── responses/
    ├── codex__r1.md            ← per-CLI per-round captured stdout
    ├── claude__r1.md
    ├── codex__r2.md
    └── ...
```

You can diff or re-process these files with any tool you want. Council itself never modifies
them after the run ends.

## Architecture

```
Browser ── WebSocket ──► server.py (FastAPI)
                            │
                            ├─ asyncio.create_subprocess_exec per CLI
                            ├─ line-by-line stdout/stderr → JSON envelope → WS
                            ├─ modes.py (parallel|debate|cascade|moa|router|consensus)
                            └─ persists to conversations/<id>/
```

No database. No auth. Local-only by default (127.0.0.1:8765).

Full layer breakdown in [ARCHITECTURE.md](ARCHITECTURE.md).
Full product spec in [PRD.md](PRD.md).
Mode protocols in [docs/modes-deep-dive.md](docs/modes-deep-dive.md).

## Contributing

This is a personal project but PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). The bar is
modest: ruff clean, pytest green, no new dependencies without a one-line justification.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- Mixture-of-Agents — [Together AI's reference implementation](https://github.com/togethercomputer/moa)
- Multi-Agent Debate — [composable-models/llm_multiagent_debate](https://github.com/composable-models/llm_multiagent_debate)
- Inspired by the daily multi-CLI workflow of countless engineers comparing answers across LLMs
