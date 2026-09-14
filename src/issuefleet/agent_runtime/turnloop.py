"""The worker turn loop: a dumb consumer of ``turns.decide()``.

``turnloop step`` performs at most one turn and exits with the decision's
exit code (cron/test-friendly). ``turnloop run`` loops forever inside the
container's tmux pane: stepping on 0, polling the inbox on 10/20/40, exiting
on 30, and retrying runtime failures a bounded number of times.

Prompts go on stdin. Claude uses the fleet session UUID; Codex assigns a
thread ID, which is persisted immediately and resumed explicitly thereafter.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from issuefleet.agent_runtime import runtimes, turns
from issuefleet.agent_runtime.agentctl import find_agent_dir
from issuefleet.mailbox import Mailbox

MAX_RUNTIME_RETRIES = 3
MAX_NOOP_TURNS = 2  # continuation turns with no output/commit before auto-idle
# The container may still be settling its bind mounts when the loop starts;
# retry the git preflight a few times before declaring the worktree broken so
# a slow-mounting filesystem doesn't trip a spurious exit.
GIT_PREFLIGHT_TRIES = 3
GIT_PREFLIGHT_SLEEP_S = 2


def _git_head(workspace: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except OSError:
        return None


def preflight_git(workspace: Path) -> bool:
    """Is the worktree's git actually usable in *this* container?

    A worker runs in a linked worktree whose ``.git`` is a pointer to the
    shared repo's git-common-dir — a host path OUTSIDE the ``-w`` mount that
    the launcher bind-mounts into the container at its identical location. If
    that mount is missing (observed after a host-crash restart, FUG-116: only
    ``/workspace`` came up mounted), the pointer resolves to a path that isn't
    there and every git command fails with "not a git repository" — yet the
    session is still ``alive`` from the orchestrator's side, so it has no
    signal and the worker wedges. Resolving ``HEAD`` touches both the admin
    gitdir and the common object store, so it's a faithful probe.

    Returns True the moment git works; on repeated failure prints a loud,
    specific diagnostic (naming the unreachable ``.git`` pointer) and returns
    False, so ``run`` can exit and let the orchestrator relaunch this worker
    with a fresh mount — a transient miss self-heals, a persistent one climbs
    to ``max_restarts`` and is reported to the operator, and either beats a
    confused agent burning turns on git errors.
    """
    for attempt in range(1, GIT_PREFLIGHT_TRIES + 1):
        if _git_head(workspace) is not None:
            return True
        try:
            pointer = (workspace / ".git").read_text().strip()
        except OSError:
            pointer = "(no .git file)"
        print(
            f"turnloop: git is not usable in {workspace} "
            f"(attempt {attempt}/{GIT_PREFLIGHT_TRIES}); .git -> {pointer}",
            flush=True,
        )
        if attempt < GIT_PREFLIGHT_TRIES:
            time.sleep(GIT_PREFLIGHT_SLEEP_S)
    print(
        "turnloop: git is broken in this container — the worktree's "
        "git-common-dir is not mounted (FUG-116: a lost .git mount after a "
        "host restart). Exiting so the orchestrator relaunches this worker "
        "with a fresh mount; if it recurs the crash path will report it.",
        flush=True,
    )
    return False


def summarize_event(line: str, runtime: str = "claude") -> str | None:
    """One compact human line per interesting stream-json event, printed to
    our stdout — which is the tmux pane, i.e. what `issuefleet attach` and
    `issuefleet logs -f` show. Non-JSON lines (stderr, crash output) pass
    through verbatim so failures are diagnosable from the pane alone."""
    line = line.strip()
    if not line:
        return None
    ev = runtimes.parse_event(line)
    if ev is None:
        return line
    if runtime == "codex":
        return runtimes.summarize_codex(ev)
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return f"· session {str(ev.get('session_id', ''))[:8]} model={ev.get('model', '?')}"
    if kind == "assistant":
        parts = []
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                text = " ".join(block["text"].split())
                parts.append(text[:200] + ("…" if len(text) > 200 else ""))
            elif block.get("type") == "tool_use":
                inp = block.get("input", {})
                detail = str(
                    inp.get("description") or inp.get("command") or inp.get("file_path") or ""
                )
                parts.append(f"→ {block.get('name', '?')} {detail[:120]}".rstrip())
        return "\n".join(parts) or None
    if kind == "result":
        head = "✗ turn errored" if ev.get("is_error") else "✓ turn complete"
        pieces = [head]
        if ev.get("duration_ms"):
            pieces.append(f"{ev['duration_ms'] / 1000:.0f}s")
        if ev.get("total_cost_usd") is not None:
            pieces.append(f"${ev['total_cost_usd']:.2f}")
        return " ".join(pieces)
    return None  # user/tool_result events are noise at pane granularity


def run_claude(prompt: str, state: turns.TurnState, agent_dir: Path) -> int:
    """Backward-compatible entry point for Claude workers."""
    return run_runtime(prompt, state, agent_dir)


def run_runtime(prompt: str, state: turns.TurnState, agent_dir: Path) -> int:
    workspace = agent_dir.parent
    try:
        cmd = runtimes.command(state)
    except ValueError as exc:
        print(f"turnloop: {exc}", flush=True)
        return 1
    codex_turn = runtimes.CodexTurn(state.runtime_session_id) if state.runtime == "codex" else None
    protocol_failed = False

    logs = agent_dir / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"turn-{state.turns_taken + 1:04d}.jsonl"
    attempt = 0
    while log_path.exists():
        attempt += 1
        log_path = logs / f"turn-{state.turns_taken + 1:04d}-retry-{attempt:02d}.jsonl"
    print(f"turnloop: turn {state.turns_taken + 1} starting ({state.runtime})", flush=True)
    with open(log_path, "x") as log:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=workspace,
            )
        except OSError as exc:
            diagnostic = f"turnloop: cannot launch {state.runtime}: {exc}\n"
            log.write(diagnostic)
            print(diagnostic, end="", flush=True)
            return 127

        def _feed():
            # Fed from a thread: a large prompt plus an early-chatty child
            # could otherwise deadlock on full pipes.
            try:
                proc.stdin.write(prompt)
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                event = runtimes.parse_event(line)
                if event is not None:
                    if codex_turn:
                        codex_turn.observe(event, agent_dir)
                    elif event.get("type") == "result" and event.get("is_error"):
                        protocol_failed = True
                summary = summarize_event(line, state.runtime)
                if summary:
                    print(summary, flush=True)
            rc = proc.wait()
        finally:
            # A state/log write failure must not leave an orphaned child
            # continuing to mutate the checkout after the loop exits.
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            feeder.join()
    if rc == 0 and (protocol_failed or (codex_turn and not codex_turn.succeeded)):
        print(f"turnloop: {state.runtime} did not report a successful persistent turn", flush=True)
        return 1
    return rc


def _prepare_codex_prompt(agent_dir: Path, decision: turns.Decision) -> None:
    """Journal input before launch, so a crash retries the actual request.

    A thread ID may be emitted before Codex accepts its initial prompt. The
    ID alone therefore does not prove it knows the brief yet. Retrying the
    saved input works both before and after thread creation. Newly arrived
    messages are appended once, before the decision consumes their inboxes.
    """
    pending = agent_dir / "pending-codex-turn.json"
    if pending.exists():
        saved = json.loads(pending.read_text())
        decision.prompt = saved["prompt"]
        consumed_ids = set(saved.get("message_ids", []))
        new_messages = [message for message in decision.consume if message.id not in consumed_ids]
        if new_messages:
            decision.prompt += "\n\n" + turns.format_inbound(new_messages)
    else:
        consumed_ids = set()
    consumed_ids.update(message.id for message in decision.consume)
    tmp = pending.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"prompt": decision.prompt, "message_ids": sorted(consumed_ids)}))
    tmp.replace(pending)


def step(agent_dir: Path) -> int:
    state = turns.TurnState.load(agent_dir)
    mb = Mailbox(agent_dir / "mailbox").ensure()
    decision = turns.decide(agent_dir, mb, state)
    if (state.runtime == "codex"
            and (agent_dir / "pending-codex-turn.json").exists()
            and decision.action in ("idle", "idle_ready", "budget")):
        # agentctl may have asked/submitted before the CLI failed or the
        # process died. An unfinished turn must not look like healthy idle
        # after restart and bypass the bounded runtime/orchestrator retries.
        # Preserve its phase and request: a human reply can still retry it,
        # and shutdown always takes precedence over this error gate.
        print(f"turnloop: unfinished Codex turn cannot settle in {state.phase}; "
              "see pending-codex-turn.json and logs", flush=True)
        return turns.EXIT_ERROR
    if state.runtime == "codex" and decision.action == "run":
        _prepare_codex_prompt(agent_dir, decision)
    turns.commit(decision, agent_dir, mb, state)
    if decision.action != "run":
        return decision.exit_code

    workspace = agent_dir.parent
    # ⚙️: acknowledge that the agent is actively taking turns on the user's
    # input — once per work cycle (a wake or the fresh first turn, not every
    # continuation turn). Emitted BEFORE pre_turn_seq so a silent turn's
    # ready/idle-restore logic below still sees "no output from the turn".
    starting_cycle = decision.wake_from_phase is not None or not decision.resume
    if starting_cycle and not state.working_acked:
        # A `thought`: the agent genuinely is working, so the Linear session
        # should read "Working…" (active) until it settles below.
        mb.put_outbox("ack", {"text": "⚙️ On it — the agent is taking turns.",
                              "activity": "thought"})
        state.working_acked = True
        state.save(agent_dir)

    pre_turn_seq = mb.last_outbox_seq()
    pre_head = _git_head(workspace)
    runner = run_claude if state.runtime == "claude" else run_runtime
    rc = runner(decision.prompt, state, agent_dir)
    if state.runtime == "codex" and rc == 0:
        (agent_dir / "pending-codex-turn.json").unlink(missing_ok=True)
    # agentctl may have moved phase to waiting/ready/idle during the turn —
    # reload before recording the completed turn, so we don't clobber it.
    state = turns.TurnState.load(agent_dir)
    emitted = mb.last_outbox_seq() != pre_turn_seq
    committed = _git_head(workspace) != pre_head
    if rc != 0:
        # A FAILED turn must never adjust phase: counting failures as
        # "no-op" turns parked a 100%-failing worker into innocent-looking
        # idle (observed live: root-refused claude, 4 instant failures,
        # status showed 'idle'). Failures stay on the loud EXIT_ERROR path.
        state.noop_turns = 0
    elif (
        decision.wake_from_phase in (turns.PHASE_READY, turns.PHASE_IDLE)
        and state.phase == turns.PHASE_RUNNING
        and not emitted
    ):
        # Woken out of an idle state by a message that needed no response:
        # go back to idling. Without this, the running phase grants endless
        # continuation turns to an agent with nothing left to do.
        print("turnloop: wake produced no response; returning to idle")
        state.phase = decision.wake_from_phase
        state.noop_turns = 0
    elif (
        state.phase == turns.PHASE_RUNNING
        and state.ever_ready
        and decision.wake_from_phase is None
        and decision.resume
        and not emitted
        and not committed
    ):
        # Backstop for agents that finish but never say so: consecutive
        # continuation turns producing neither a message nor a commit are
        # going nowhere — park the loop instead of grinding the budget.
        # Gated on ever_ready: BEFORE a first submission, quiet turns are
        # usually legitimate codebase exploration (observed live: a worker
        # got parked at turn 3 mid-exploration), and the auto-turn budget
        # is the intended brake for that regime.
        state.noop_turns += 1
        if state.noop_turns >= MAX_NOOP_TURNS:
            print(
                f"turnloop: {state.noop_turns} continuation turns with no output or "
                "commit; standing by (equivalent to `agentctl idle`)"
            )
            state.phase = turns.PHASE_IDLE
            state.noop_turns = 0
    else:
        state.noop_turns = 0
    # ✅: close the acknowledgment loop once the agent settles after working.
    # ready/idle are completions; a pending question (waiting) is itself the
    # response, so it ends the cycle silently. Never on a failed turn.
    #
    # The ✅ carries `response`: Linear reads a `response` activity as "work
    # completed" and moves the session to `complete`, so an idling worker no
    # longer hangs in "Working…" and then false-errors on the inactivity
    # timeout (FUG-98). ready also emits its own PR-link `response`; a second
    # one here is harmless and keeps the ⚙️/✅ bracketing symmetric.
    if state.working_acked and rc == 0:
        if state.phase in (turns.PHASE_READY, turns.PHASE_IDLE):
            mb.put_outbox("ack", {"text": "✅ Done for now.", "activity": "response"})
            state.working_acked = False
        elif state.phase == turns.PHASE_WAITING:
            state.working_acked = False
    state.turns_taken += 1
    state.save(agent_dir)
    if rc != 0:
        print(f"turnloop: {state.runtime} exited {rc} (see logs/turn-{state.turns_taken:04d}*.jsonl)")
        return turns.EXIT_ERROR
    return turns.EXIT_CONTINUE


def run(agent_dir: Path) -> int:
    # Fail fast (and cleanly) if git isn't usable here: a worker whose
    # git-common-dir mount was lost on a restart can't commit or rebase, so
    # exit and let the orchestrator relaunch it rather than wedge (FUG-116).
    if not preflight_git(agent_dir.parent):
        return turns.EXIT_ERROR
    failures = 0
    while True:
        code = step(agent_dir)
        state = turns.TurnState.load(agent_dir)
        if code == turns.EXIT_CONTINUE:
            failures = 0
            continue
        if code == turns.EXIT_SHUTDOWN:
            print("turnloop: shutdown/unclaimed received; exiting")
            return 0
        if code == turns.EXIT_ERROR:
            failures += 1
            if failures > MAX_RUNTIME_RETRIES:
                print(f"turnloop: {failures} consecutive {state.runtime} failures; giving up")
                return turns.EXIT_ERROR
            time.sleep(min(60, 5 * 2**failures))
            continue
        # 10 / 20 / 40: idle until the inbox changes.
        time.sleep(state.idle_poll_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="turnloop", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("step", help="one decision (and at most one turn); exit code = decision")
    sub.add_parser("run", help="loop until shutdown")
    sub.add_parser("decide", help="print the pending decision without acting (debug)")
    args = ap.parse_args(argv)

    agent_dir = find_agent_dir()
    if args.cmd == "step":
        return step(agent_dir)
    if args.cmd == "run":
        return run(agent_dir)
    if args.cmd == "decide":
        state = turns.TurnState.load(agent_dir)
        mb = Mailbox(agent_dir / "mailbox").ensure()
        d = turns.decide(agent_dir, mb, state)
        print(json.dumps({"action": d.action, "exit_code": d.exit_code, "resume": d.resume,
                          "prompt": (d.prompt or "")[:400]}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
