# Orchestration modes — deep dive

Each mode is a coroutine in `modes.py`. The dispatcher in `server.py` looks them up in the
`MODES` dict. To add a new one, see [ARCHITECTURE.md → "How to add a new mode"](../ARCHITECTURE.md).

## Parallel

```
prompt → [codex] [claude] [copilot] [gemini]  (all independent, in parallel)
```

No interaction. Each CLI sees only your original prompt (+ injected status). Output to 4 panes.

**Use when:** you want to see the raw differences between CLIs. Good for "what does each one
think a generator function should look like" calibration.

**Quality lift over single best CLI:** ~0% (it's not a consensus mechanism, it's a comparison).

## Debate

```
Round 1:  prompt → [codex] [claude] [copilot] [gemini]
Round 2:  each CLI sees others' R1 answers + packed prompt:
              "your prior answer / others' answers / refine or stand alone / VOTE: AGREE_WITH="
```

**Use when:** you want one round of peer refinement.

**Quality lift:** +3-10% on benchmarks (Du et al. 2023, [arxiv 2305.14325](https://arxiv.org/abs/2305.14325)).
Best for tasks where multiple correct answers exist and refinement closes the gap.

**Watchpoint:** debate doesn't always converge — sometimes CLIs dig into their own positions.
Use `consensus` mode if you need an explicit "did they agree" signal.

## Cascade

```
Step 1:  codex   drafts
Step 2:  claude  critiques  (VERDICT: APPROVE | REVISE | REJECT)
Step 3:  codex   revises addressing critique
Step 4:  gemini  validates the revision
```

**Use when:** classic code-review flow. Drafter and critic must be different CLIs to
maximize cross-checking.

**Quality lift:** +5-12% on code-quality benchmarks (Self-Refine: [arxiv 2303.17651](https://arxiv.org/abs/2303.17651)).

**Watchpoint:** cascade is linear, so total wall-clock = sum of 4 CLI calls. Slower than
parallel/debate.

## Mixture-of-Agents (MoA)

```
Proposer round 1:  [claude] [copilot] [gemini]   (all propose independently)
Proposer round 2:  each proposer sees others' R1 and proposes improvement
Aggregation:       [codex] synthesizes ONE final answer with inline [from <cli>] citations
```

The first CLI in your selection is the **aggregator**; the rest are proposers.

**Use when:** you want the best raw quality on a hard problem and don't care about latency.

**Quality lift:** **+5-15%** on benchmarks (Wang et al. 2024, [arxiv 2406.04692](https://arxiv.org/abs/2406.04692)).
The single best lift among supported modes.

**Watchpoint:** the aggregator's prompt grows with each proposer (N proposers × full answer).
Token cost scales linearly with proposer count. Default 2 proposer rounds — more rarely helps.

**Reference implementation:** [togethercomputer/moa](https://github.com/togethercomputer/moa)

## Router

```
Classifier (first CLI):  "classify this task as CODE/ARCH/SHELL/RESEARCH and emit ROUTE: <CAT>"
Specialist (best for cat): answers
```

Specialist preferences (when available):

| Category | Preferred CLIs (in order) |
|---|---|
| CODE | codex, claude |
| ARCH | claude, codex |
| SHELL | copilot, codex |
| RESEARCH | gemini, claude |

**Use when:** cost or latency matters more than diversity. Single-best-LLM-per-task pattern.

**Quality lift:** 0-5%. It's primarily a cost optimization, not a consensus mechanism.

**Reference:** RouteLLM ([arxiv 2406.18665](https://arxiv.org/abs/2406.18665)).

## Consensus

```
Round 1:  all CLIs answer (each ends with VOTE: AGREE_WITH=<cli|self>)
After each round: parse VOTE lines. If all CLIs vote the same target → stop, return winner.
Else: round 2, round 3 (max). At max, return last round of first CLI as final.
```

**Use when:** you want an explicit agreement signal as part of the artifact (e.g. for an ADR
saying "all 4 CLIs agreed claude's answer is best").

**Quality lift:** +3-8% (debate + voting heuristic).

**Watchpoint:** CLIs are not perfectly disciplined about emitting `VOTE:` lines. If parsing
fails, the mode falls through to max rounds and returns the last round's first-CLI answer.

## Recommended modes per task

| Task | Mode | Why |
|---|---|---|
| "Compare how each CLI handles X" | parallel | Pure comparison, no consensus |
| "Refine this design once with peer feedback" | debate | One revision pass |
| "Code review of this PR" | cascade | Draft → critique → revise → validate |
| "Best architecture answer, latency be damned" | moa | Highest quality lift |
| "Quick utility / one-shot specialist" | router | Lowest token cost |
| "Need them to formally agree before I commit" | consensus | Explicit vote signal |

## Quality lift recap

Per Codex review of the architecture (2026-05-17):

| Mode | Lift vs single best CLI | Complexity |
|---|---|---|
| parallel | 0% | trivial |
| router | 0-5% | low |
| debate (2 rounds) | +3-10% | low |
| consensus | +3-8% | medium |
| cascade | +5-12% | medium |
| **moa** | **+5-15%** | medium |

Stopping rule for all multi-round modes: **2 critique rounds is the sweet spot.** Going to
3+ rounds usually burns tokens without improving the final answer.
