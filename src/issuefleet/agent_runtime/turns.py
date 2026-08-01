"""Turn decisions: the one testable place that decides what the next turn is.

``decide()`` is pure — it reads the agent dir (brief, state, inbox) and
returns a Decision; it never launches anything. The turn loop is a dumb
consumer of decisions and their exit codes:

    0   run another turn (the Decision carries the prompt)
    10  idle: a question is pending, wait for a human reply
    20  idle: work submitted (ready), wait for review/merge
    30  shutdown / unclaimed — exit the loop
    40  auto-turn budget exhausted — idle instead of grinding
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from issuefleet.mailbox import Mailbox, Message

EXIT_CONTINUE = 0
EXIT_IDLE = 10
EXIT_READY = 20
EXIT_SHUTDOWN = 30
EXIT_BUDGET = 40
EXIT_ERROR = 50  # claude invocation failed; outer loop retries bounded

# Agent-side phases (distinct from the registry's host-side phase).
PHASE_FRESH = "fresh"  # no first turn yet
PHASE_RUNNING = "running"
PHASE_WAITING = "waiting"  # asked a question, blocked on a human
PHASE_READY = "ready"  # submitted; blocked on review/merge
PHASE_IDLE = "idle"  # declared done / standing by (agentctl idle); wakes like ready

# Inbox kinds that justify waking an idle agent. "info" is context-only: it
# rides along on the next turn but never triggers one by itself.
_WAKING_KINDS = ("reply", "pr_feedback", "pr_closed")


@dataclass
class TurnState:
    session_uuid: str = ""
    phase: str = PHASE_FRESH
    turns_taken: int = 0
    auto_turns: int = 0  # consecutive self-driven turns since last human contact
    noop_turns: int = 0  # consecutive continuation turns with no output or commit
    ever_ready: bool = False  # has this worker ever submitted? (gates auto-idle)
    working_acked: bool = False  # ⚙️ emitted for the current work cycle, awaiting its ✅
    max_auto_turns: int = 50
    budget_reported: bool = False
    idle_poll_s: int = 15
    claude_args: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, agent_dir: Path) -> "TurnState":
        p = Path(agent_dir) / "state.json"
        data = json.loads(p.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, agent_dir: Path) -> None:
        p = Path(agent_dir) / "state.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2))
        os.rename(tmp, p)


@dataclass
class Decision:
    action: str  # "run" | "idle" | "idle_ready" | "shutdown" | "budget"
    exit_code: int
    prompt: str | None = None
    resume: bool = True  # False only for the very first turn
    consume: list[Message] = field(default_factory=list)  # inbox msgs this turn ingests
    resets_auto_turns: bool = False
    post_status: str | None = None  # outbox status the loop should emit (budget trip)
    wake_from_phase: str | None = None  # phase this run-decision woke the agent out of


def decide(agent_dir: Path, mailbox: Mailbox, state: TurnState) -> Decision:
    pending = mailbox.pending_inbox()

    stops = [m for m in pending if m.kind in ("shutdown", "unclaimed")]
    if stops:
        return Decision(action="shutdown", exit_code=EXIT_SHUTDOWN, consume=stops)

    waking = [m for m in pending if m.kind in _WAKING_KINDS]
    context = [m for m in pending if m.kind == "info"]

    if state.phase == PHASE_FRESH:
        brief = (Path(agent_dir) / "brief.md").read_text()
        parts = [brief]
        if waking or context:
            parts.append(format_inbound(waking + context))
        return Decision(
            action="run",
            exit_code=EXIT_CONTINUE,
            prompt="\n\n".join(parts),
            resume=False,
            consume=waking + context,
            resets_auto_turns=True,
        )

    if waking:
        # Human contact: inject everything pending and reset the budget clock.
        return Decision(
            action="run",
            exit_code=EXIT_CONTINUE,
            prompt=format_inbound(waking + context),
            consume=waking + context,
            resets_auto_turns=True,
            wake_from_phase=state.phase,
        )

    if state.phase == PHASE_WAITING:
        return Decision(action="idle", exit_code=EXIT_IDLE)
    if state.phase in (PHASE_READY, PHASE_IDLE):
        return Decision(action="idle_ready", exit_code=EXIT_READY)

    if state.auto_turns >= state.max_auto_turns:
        return Decision(
            action="budget",
            exit_code=EXIT_BUDGET,
            post_status=(
                f"Auto-turn budget exhausted ({state.max_auto_turns} turns without "
                "human contact). Idling; reply on this issue to continue."
            )
            if not state.budget_reported
            else None,
        )

    return Decision(
        action="run",
        exit_code=EXIT_CONTINUE,
        prompt=_CONTINUE_PROMPT,
        consume=context,
    )


def commit(decision: Decision, agent_dir: Path, mailbox: Mailbox, state: TurnState) -> None:
    """Apply a decision's side effects (message consumption, counters, phase).
    Called by the turn loop right before launching the turn, so a crash
    mid-turn re-injects nothing twice but also loses nothing silently — the
    consumed copies remain in inbox/consumed for the audit trail."""
    for m in decision.consume:
        mailbox.consume_inbox(m)
    if decision.post_status:
        mailbox.put_outbox("status", {"text": decision.post_status})
        state.budget_reported = True
    if decision.action == "run":
        state.phase = PHASE_RUNNING
        if decision.resets_auto_turns:
            state.auto_turns = 0
        else:
            state.auto_turns += 1
    state.save(agent_dir)


def format_inbound(msgs: list[Message]) -> str:
    """Render inbound messages for prompt injection, with enough context
    (source, author, file path) to act on."""
    blocks = ["New messages have arrived on your issue:"]
    for m in sorted(msgs, key=lambda m: m.seq):
        p = m.payload
        if m.kind == "reply":
            head = f"Linear comment from {p.get('author', 'unknown')}"
        elif m.kind == "pr_feedback":
            head = f"PR {p.get('kind', 'comment')} from {p.get('reviewer', 'unknown')}"
            if p.get("path"):
                head += f" on `{p['path']}`"
        elif m.kind == "pr_closed":
            head = "Your PR was closed without merging"
        else:
            head = "Notice from the orchestrator"
        blocks.append(f"### {head}\n{p.get('text', p.get('body', ''))}")
    blocks.append(
        "Address these, then continue. Use `agentctl status` to report, "
        "`agentctl ask` if blocked, `agentctl ready` to (re-)submit — or, if "
        "these messages need no action from you, `agentctl idle`."
    )
    return "\n\n".join(blocks)


_CONTINUE_PROMPT = (
    "Continue working on the issue described in your original brief. "
    "Commit as you go. When the issue is satisfied, run `agentctl ready` "
    "with a PR title and body; post meaningful progress with `agentctl status`; "
    "if you are blocked on a decision only a human can make, run `agentctl ask`. "
    "If there is genuinely nothing left to do, run `agentctl idle` — do not spin."
)
