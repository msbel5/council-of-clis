# Contributing

This is a small personal project. PRs welcome with the following bar:

1. **ruff clean.** Run `uv run ruff check .` before pushing.
2. **pytest green.** Run `uv run pytest -q`.
3. **No new top-level dependencies** without a one-line justification in the PR description.
4. **One feature per PR.** Refactor and feature in the same PR will be rejected.

## How to work on this

```powershell
git clone https://github.com/msbel5/council-of-clis.git
cd council-of-clis
uv venv && .\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
python server.py    # http://localhost:8765
```

## Common changes

- **New orchestration mode** → see "How to add a new mode" in [ARCHITECTURE.md](ARCHITECTURE.md).
- **New CLI adapter** → see "How to add a new CLI" in [ARCHITECTURE.md](ARCHITECTURE.md).
- **UI tweak** → `static/`, no build step, hard refresh the browser.

## Pull requests

- Title: short imperative ("Add Gemini cancel-on-disconnect", not "Gemini stuff").
- Body: what changed, why, and the test you ran.
- CI must pass.

## Issues

- Bug reports: minimal reproduction, expected vs actual, your OS + Python version.
- Feature requests: use the template, link any prior discussion.
