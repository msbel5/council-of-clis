# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-17

### Added

- Initial public release.
- FastAPI + WebSocket server (`server.py`) that streams subprocess output line-by-line.
- 4 CLI adapters: `codex`, `claude`, `copilot`, `gemini` with auto-availability detection.
- 6 orchestration modes in `modes.py`:
  - `parallel` — independent fan-out
  - `debate` — 2-round revision after seeing peers (Du et al. 2023)
  - `cascade` — drafter → critic → reviser → validator
  - `moa` — Mixture-of-Agents (Wang et al. 2024)
  - `router` — classify task and route to specialist (RouteLLM)
  - `consensus` — vote until unanimous or 3 rounds
- Single-page web UI in `static/` with 4 streaming panes and mode selector.
- Status injection via `prompts/status.md` (gitignored), seeded from `status.example.md` on first run.
- Per-conversation file-system persistence in `conversations/<id>/`.
- MIT license, basic CI (`ruff` + `pytest`), unit tests for mode dispatchers.
