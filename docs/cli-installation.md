# CLI installation guide

Council shells out to 4 different LLM CLIs. Install whichever you have subscriptions for.

## Codex (OpenAI)

```powershell
npm install -g @openai/codex-cli
codex login        # one-time, opens browser
```

Requires ChatGPT Plus, Team, or Business subscription.

Council invocation: `codex exec - --skip-git-repo-check --sandbox read-only --model gpt-5.4`
(the `-` is the explicit stdin sentinel, recommended on Windows).

## Claude Code (Anthropic)

```powershell
npm install -g @anthropic-ai/claude-code
claude        # one-time login flow
```

Requires Claude Pro or Max subscription.

Council invocation: `claude --print "<prompt>"` (prompt passed as argv; stdin can carry extra context).

## GitHub Copilot CLI

Note: there are TWO Copilot CLIs. Council uses the NEW one (`copilot`), not the old
`gh copilot suggest` extension.

```powershell
npm install -g @github/copilot
copilot       # one-time auth
```

Requires GitHub Copilot Pro/Business/Enterprise.

Council invocation: `copilot -sp "<prompt>"`.

## Gemini CLI (Google)

```powershell
npm install -g @google/gemini-cli
gemini --auth
```

Requires Google AI Pro (Gemini Code Assist) subscription, or a free Google AI Studio API key.

Council invocation: `gemini -p "<prompt>"`.

## Verifying installation

After installing any CLI, restart Council (`python server.py`) and reload the browser. The
checkbox for that CLI should be active (not greyed out).

```powershell
# Quick check:
codex exec --help | head -5
claude --help | head -5
copilot --help | head -5
gemini --help | head -5
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Codex hangs forever with no output | Argument-mode prompt on Windows non-interactive parent | Council uses `codex exec -` (stdin); verify your local Council has the latest server.py |
| Claude says "no api key" | Not logged in | Run `claude` once interactively to complete OAuth |
| Copilot command not found, but `gh copilot` works | You have the old extension, not the new CLI | `npm install -g @github/copilot` |
| Gemini "capacity-related error" | High-traffic period | Retry or pick a different CLI; Gemini doesn't expose retry-after |
