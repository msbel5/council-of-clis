# First prompt to test Council

Once Council is running at <http://localhost:8765>:

1. Click `+ New conversation`.
2. Pick `parallel` mode and select the CLIs you have installed.
3. Paste:

```
Reply with exactly the word "OK" and nothing else. This is a smoke test.
```

4. Click Send (or `Ctrl+Enter`).

You should see each pane stream back "OK" within a few seconds. If any CLI errors, fix that
CLI's installation (see [docs/cli-installation.md](../docs/cli-installation.md)).

## A real first prompt

Edit your `prompts/status.md` first (📋 button) with your actual project context, then try
`debate` mode with this:

```
Suggest the best Python library for parsing structured logs in 2026. Compare 2-3 options
and recommend one. Be specific about tradeoffs.
```

In debate mode you'll see all CLIs answer (round 1), then each one revise after seeing what
the others said (round 2). End-of-round 2 will contain `VOTE: AGREE_WITH=<cli>` lines from
each — that's the consensus signal.
