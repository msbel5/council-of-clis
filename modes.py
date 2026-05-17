"""Council orchestration modes.

Each mode is a coroutine that takes:
    (prompt, clis, ws, conv, run_cli_fn) → completion summary

`run_cli_fn` is the wired-up `stream_cli(cli, prompt, ws, conv)` from server.py,
but extended to RETURN the captured stdout text (in addition to streaming it).

Modes implemented (Mami's request — they talk to each other):
    parallel        — fan out, no interaction (default, existing behavior)
    debate          — round 1: all answer; round 2: each sees others, revises
    cascade         — drafter → critic → reviser → validator (linear pipeline)
    moa             — Mixture-of-Agents (Wang et al. 2024): N proposers + aggregator
    router          — first CLI classifies task type, routes to best specialist
    consensus       — debate + explicit AGREE/DISAGREE voting until unanimous or N rounds

Status messages stream as `{"cli": "*", "kind": "phase", "data": "<phase name>"}`.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol, TypedDict

# ---- Protocol -------------------------------------------------------------


class WSLike(Protocol):
    async def send_json(self, data: dict) -> None: ...


class RunCLI(Protocol):
    """Wired version of stream_cli that also returns the captured stdout."""

    async def __call__(
        self,
        cli: str,
        prompt: str,
        ws: WSLike,
        conv: object,
        label: str = "",
    ) -> str: ...


class ModeResult(TypedDict):
    mode: str
    rounds: int
    final_text: str
    per_cli_history: dict[str, list[str]]


# ---- Prompt packaging helpers --------------------------------------------


def pack_for_revision(
    original_prompt: str,
    self_answer: str,
    others: dict[str, str],
) -> str:
    """Compose round-2+ prompt: original task + self's prior answer + others' answers."""
    parts = [
        "You are participating in a multi-agent council.",
        "",
        "## Original task",
        original_prompt,
        "",
        "## Your previous answer",
        self_answer or "(no prior answer)",
        "",
        "## Other agents' answers (verbatim)",
    ]
    for other_cli, other_text in others.items():
        parts.append(f"### {other_cli}")
        parts.append(other_text.strip() or "(empty)")
        parts.append("")
    parts.append(
        "## Your job now",
    )
    parts.append(
        "Produce an IMPROVED answer. Either:\n"
        "  (a) refine your prior answer using insights from others, OR\n"
        "  (b) adopt a different position with a 1-sentence justification.\n"
        "End with a single line: 'VOTE: AGREE_WITH=<cli|self>' "
        "or 'VOTE: STAND_ALONE' if your answer is novel."
    )
    return "\n".join(parts)


def pack_for_critique(original_prompt: str, drafter_output: str, drafter_cli: str) -> str:
    return (
        "You are reviewing another agent's draft answer.\n\n"
        f"## Original task\n{original_prompt}\n\n"
        f"## Draft by {drafter_cli}\n{drafter_output}\n\n"
        "## Your job\n"
        "Critique this draft concretely:\n"
        "1. List specific factual errors or weak spots.\n"
        "2. List what's correct.\n"
        "3. End with one of: 'VERDICT: APPROVE' | 'VERDICT: REVISE' | 'VERDICT: REJECT'\n"
        "If REVISE, suggest exactly 1-3 changes."
    )


def pack_for_revision_after_critique(
    original_prompt: str, my_draft: str, critique: str, critic_cli: str
) -> str:
    return (
        f"## Original task\n{original_prompt}\n\n"
        f"## Your previous draft\n{my_draft}\n\n"
        f"## Critique from {critic_cli}\n{critique}\n\n"
        "## Your job\n"
        "Produce a revised answer addressing the critique. "
        "If the critique missed something, say so and stand your ground. "
        "End with: 'STATUS: REVISED' | 'STATUS: UNCHANGED'."
    )


def pack_for_aggregation(
    original_prompt: str, proposals: dict[str, str]
) -> str:
    parts = [
        "You are the aggregator in a Mixture-of-Agents council.",
        "",
        "## Original task",
        original_prompt,
        "",
        "## Proposals from agents",
    ]
    for cli, text in proposals.items():
        parts.append(f"### {cli}")
        parts.append(text.strip() or "(empty)")
        parts.append("")
    parts.append(
        "## Your job\n"
        "Synthesize ONE final answer that is better than any individual proposal. "
        "You may copy verbatim from any agent if they nailed a part. "
        "Resolve disagreements by reasoning from first principles. "
        "Cite which agent contributed which insight in inline comments like [from claude]."
    )
    return "\n".join(parts)


# ---- Modes ----------------------------------------------------------------


async def _phase(ws: WSLike, msg: str) -> None:
    await ws.send_json({"cli": "*", "kind": "phase", "data": msg})


async def mode_parallel(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
) -> ModeResult:
    """Existing behavior — all CLIs answer independently, no interaction."""
    await _phase(ws, "PARALLEL: fan out, no interaction")
    tasks = {cli: asyncio.create_task(run_cli(cli, prompt, ws, conv, "r1")) for cli in clis}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    history = {
        cli: [str(r) if not isinstance(r, Exception) else f"[error: {r}]"]
        for cli, r in zip(tasks.keys(), results)
    }
    return ModeResult(mode="parallel", rounds=1, final_text="", per_cli_history=history)


async def mode_debate(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
    max_rounds: int = 2,
) -> ModeResult:
    """All answer → all see each other → revise. Optionally vote on convergence."""
    history: dict[str, list[str]] = {cli: [] for cli in clis}

    # Round 1: all answer independently
    await _phase(ws, f"DEBATE round 1/{max_rounds}: independent answers")
    r1_tasks = {cli: asyncio.create_task(run_cli(cli, prompt, ws, conv, "r1")) for cli in clis}
    r1 = await asyncio.gather(*r1_tasks.values(), return_exceptions=True)
    for cli, res in zip(r1_tasks.keys(), r1):
        history[cli].append(str(res) if not isinstance(res, Exception) else f"[error: {res}]")

    # Rounds 2..max: each agent sees everyone else's prior round, revises
    for round_no in range(2, max_rounds + 1):
        await _phase(ws, f"DEBATE round {round_no}/{max_rounds}: revise after seeing others")
        revise_tasks = {}
        for cli in clis:
            others = {
                other: history[other][-1] for other in clis if other != cli
            }
            packed = pack_for_revision(prompt, history[cli][-1], others)
            revise_tasks[cli] = asyncio.create_task(
                run_cli(cli, packed, ws, conv, f"r{round_no}")
            )
        rr = await asyncio.gather(*revise_tasks.values(), return_exceptions=True)
        for cli, res in zip(revise_tasks.keys(), rr):
            history[cli].append(
                str(res) if not isinstance(res, Exception) else f"[error: {res}]"
            )

    # Pick final: last-round output of first CLI by default; UI may show all and let user pick
    final = history[clis[0]][-1] if clis else ""
    return ModeResult(
        mode="debate", rounds=max_rounds, final_text=final, per_cli_history=history
    )


async def mode_cascade(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
) -> ModeResult:
    """Drafter → critic → reviser → validator. Roles rotate through `clis` order."""
    if len(clis) < 2:
        await _phase(ws, "CASCADE needs >=2 CLIs; falling back to parallel")
        return await mode_parallel(prompt, clis, ws, conv, run_cli)

    history: dict[str, list[str]] = {cli: [] for cli in clis}
    drafter = clis[0]
    critic = clis[1]
    reviser = clis[0]  # original drafter revises
    validator = clis[2] if len(clis) > 2 else clis[1]

    await _phase(ws, f"CASCADE step 1/4: {drafter} drafts")
    draft = await run_cli(drafter, prompt, ws, conv, "draft")
    history[drafter].append(draft)

    await _phase(ws, f"CASCADE step 2/4: {critic} critiques")
    critique = await run_cli(critic, pack_for_critique(prompt, draft, drafter), ws, conv, "critique")
    history[critic].append(critique)

    await _phase(ws, f"CASCADE step 3/4: {reviser} revises")
    revised = await run_cli(
        reviser,
        pack_for_revision_after_critique(prompt, draft, critique, critic),
        ws,
        conv,
        "revise",
    )
    history[reviser].append(revised)

    if validator != reviser:
        await _phase(ws, f"CASCADE step 4/4: {validator} validates")
        validation = await run_cli(
            validator, pack_for_critique(prompt, revised, reviser), ws, conv, "validate"
        )
        history[validator].append(validation)

    return ModeResult(
        mode="cascade", rounds=4, final_text=revised, per_cli_history=history
    )


async def mode_moa(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
    proposer_rounds: int = 2,
) -> ModeResult:
    """Mixture-of-Agents (Wang et al. 2024).

    Round 1..N: every proposer answers (sees prior round of all proposers if round>1).
    Final: first CLI in `clis` is the aggregator and synthesizes one answer.
    """
    if len(clis) < 2:
        await _phase(ws, "MoA needs >=2 CLIs; falling back to parallel")
        return await mode_parallel(prompt, clis, ws, conv, run_cli)

    aggregator = clis[0]
    proposers = clis[1:] if len(clis) > 1 else clis
    history: dict[str, list[str]] = {cli: [] for cli in clis}

    for r in range(1, proposer_rounds + 1):
        await _phase(ws, f"MoA proposer round {r}/{proposer_rounds}")
        tasks = {}
        for p in proposers:
            if r == 1:
                packed = prompt
            else:
                others = {o: history[o][-1] for o in proposers if o != p}
                packed = pack_for_revision(prompt, history[p][-1], others)
            tasks[p] = asyncio.create_task(run_cli(p, packed, ws, conv, f"prop_r{r}"))
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for p, res in zip(tasks.keys(), results):
            history[p].append(
                str(res) if not isinstance(res, Exception) else f"[error: {res}]"
            )

    await _phase(ws, f"MoA aggregation by {aggregator}")
    proposals = {p: history[p][-1] for p in proposers}
    final = await run_cli(aggregator, pack_for_aggregation(prompt, proposals), ws, conv, "aggregate")
    history[aggregator].append(final)

    return ModeResult(
        mode="moa",
        rounds=proposer_rounds + 1,
        final_text=final,
        per_cli_history=history,
    )


async def mode_router(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
) -> ModeResult:
    """First CLI classifies the task and routes to one specialist.

    Heuristic specialization (rough, override via status.md if needed):
        codex     → code-heavy tasks
        claude    → architecture, prose, planning
        copilot   → quick shell/utility
        gemini    → research/factual
    """
    if not clis:
        return ModeResult(mode="router", rounds=0, final_text="", per_cli_history={})

    history: dict[str, list[str]] = {cli: [] for cli in clis}
    classifier = clis[0]
    spec_pool = clis  # full set, in registration order

    classify_prompt = (
        "Classify the following user task into exactly ONE category, "
        "then output the category name on a line starting with 'ROUTE: '. "
        "Categories:\n"
        "  CODE — write or refactor code\n"
        "  ARCH — architecture, planning, prose\n"
        "  SHELL — shell command or quick utility\n"
        "  RESEARCH — find facts, summarize external info\n\n"
        f"User task: {prompt}\n\n"
        "Respond with one paragraph reasoning, then 'ROUTE: <CATEGORY>'."
    )
    await _phase(ws, f"ROUTER classifying via {classifier}")
    classification = await run_cli(classifier, classify_prompt, ws, conv, "classify")
    history[classifier].append(classification)

    category = "ARCH"
    for line in classification.splitlines():
        if line.strip().startswith("ROUTE:"):
            category = line.split(":", 1)[1].strip().upper()
            break

    # Pick specialist
    prefs = {
        "CODE": ["codex", "claude"],
        "ARCH": ["claude", "codex"],
        "SHELL": ["copilot", "codex"],
        "RESEARCH": ["gemini", "claude"],
    }
    specialist = None
    for c in prefs.get(category, ["claude", "codex"]):
        if c in spec_pool:
            specialist = c
            break
    specialist = specialist or spec_pool[0]

    await _phase(ws, f"ROUTER → {category} → {specialist}")
    answer = await run_cli(specialist, prompt, ws, conv, "answer")
    history[specialist].append(answer)

    return ModeResult(
        mode="router",
        rounds=2,
        final_text=answer,
        per_cli_history=history,
    )


async def mode_consensus(
    prompt: str,
    clis: list[str],
    ws: WSLike,
    conv: object,
    run_cli: RunCLI,
    max_rounds: int = 3,
) -> ModeResult:
    """Iterative debate until either all VOTE: AGREE_WITH=same target, or max_rounds hit.

    Stopping signal: scan each CLI's last line for 'VOTE: AGREE_WITH=<target>'.
    Convergence = all CLIs vote the same target. Else continue.
    """
    history: dict[str, list[str]] = {cli: [] for cli in clis}

    # Round 1
    await _phase(ws, f"CONSENSUS round 1/{max_rounds}: independent answers")
    r1 = {cli: asyncio.create_task(run_cli(cli, prompt, ws, conv, "r1")) for cli in clis}
    res1 = await asyncio.gather(*r1.values(), return_exceptions=True)
    for cli, r in zip(r1.keys(), res1):
        history[cli].append(str(r) if not isinstance(r, Exception) else f"[error: {r}]")

    final = history[clis[0]][-1] if clis else ""

    for rd in range(2, max_rounds + 1):
        # Check convergence
        votes = {cli: _extract_vote(history[cli][-1]) for cli in clis}
        await _phase(ws, f"CONSENSUS votes after round {rd - 1}: {votes}")
        agree_set = {v for v in votes.values() if v}
        if len(agree_set) == 1 and "stand_alone" not in agree_set:
            target = next(iter(agree_set))
            await _phase(ws, f"CONSENSUS reached — all vote {target}")
            # Pick winner's last text as final
            winner = target if target in history else clis[0]
            final = history[winner][-1]
            return ModeResult(
                mode="consensus", rounds=rd - 1, final_text=final, per_cli_history=history
            )

        await _phase(ws, f"CONSENSUS round {rd}/{max_rounds}: revise")
        tasks = {}
        for cli in clis:
            others = {o: history[o][-1] for o in clis if o != cli}
            packed = pack_for_revision(prompt, history[cli][-1], others)
            tasks[cli] = asyncio.create_task(run_cli(cli, packed, ws, conv, f"r{rd}"))
        rr = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for cli, r in zip(tasks.keys(), rr):
            history[cli].append(str(r) if not isinstance(r, Exception) else f"[error: {r}]")
        final = history[clis[0]][-1]

    await _phase(ws, f"CONSENSUS: max rounds ({max_rounds}) reached without unanimity")
    return ModeResult(
        mode="consensus", rounds=max_rounds, final_text=final, per_cli_history=history
    )


def _extract_vote(text: str) -> str:
    """Find the LAST line that starts with 'VOTE:' and return the target."""
    for line in reversed(text.strip().splitlines()):
        s = line.strip()
        if s.upper().startswith("VOTE:"):
            payload = s.split(":", 1)[1].strip().lower()
            if "agree_with=" in payload:
                return payload.split("agree_with=", 1)[1].strip()
            if "stand_alone" in payload:
                return "stand_alone"
    return ""


# ---- Registry --------------------------------------------------------------

MODES: dict[str, Callable[..., Awaitable[ModeResult]]] = {
    "parallel": mode_parallel,
    "debate": mode_debate,
    "cascade": mode_cascade,
    "moa": mode_moa,
    "router": mode_router,
    "consensus": mode_consensus,
}
