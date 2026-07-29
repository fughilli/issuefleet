"""The worker turn loop: a dumb consumer of ``turns.decide()``.

``turnloop step`` performs at most one turn and exits with the decision's
exit code (cron/test-friendly). ``turnloop run`` loops forever inside the
container's tmux pane: stepping on 0, polling the inbox on 10/20/40, exiting
on 30, and retrying claude failures a bounded number of times.

Claude Code invocation (see AGENT_BUILD_PROMPT.md §5.2): prompt on stdin,
``--session-id`` on the first turn and ``--resume`` after, so one worker is
one coherent conversation across turns and restarts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from issuefleet.agent_runtime import turns
from issuefleet.agent_runtime.agentctl import find_agent_dir
from issuefleet.mailbox import Mailbox

MAX_CLAUDE_RETRIES = 3


def run_claude(prompt: str, state: turns.TurnState, agent_dir: Path) -> int:
    workspace = agent_dir.parent
    cmd = ["claude", "-p", "--output-format", "json"]
    if state.phase == turns.PHASE_FRESH or state.turns_taken == 0:
        cmd += ["--session-id", state.session_uuid]
    else:
        cmd += ["--resume", state.session_uuid]
    cmd += list(state.claude_args)

    logs = agent_dir / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"turn-{state.turns_taken + 1:04d}.json"
    with open(log_path, "w") as log:
        proc = subprocess.run(
            cmd, input=prompt, stdout=log, stderr=subprocess.STDOUT, text=True, cwd=workspace
        )
    return proc.returncode


def step(agent_dir: Path) -> int:
    state = turns.TurnState.load(agent_dir)
    mb = Mailbox(agent_dir / "mailbox").ensure()
    decision = turns.decide(agent_dir, mb, state)
    turns.commit(decision, agent_dir, mb, state)
    if decision.action != "run":
        return decision.exit_code

    rc = run_claude(decision.prompt, state, agent_dir)
    # agentctl may have moved phase to waiting/ready during the turn — reload
    # before recording the completed turn, so we don't clobber it.
    state = turns.TurnState.load(agent_dir)
    state.turns_taken += 1
    state.save(agent_dir)
    if rc != 0:
        print(f"turnloop: claude exited {rc} (see logs/turn-{state.turns_taken:04d}.json)")
        return turns.EXIT_ERROR
    return turns.EXIT_CONTINUE


def run(agent_dir: Path) -> int:
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
            if failures > MAX_CLAUDE_RETRIES:
                print(f"turnloop: {failures - 1} consecutive claude failures; giving up")
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
