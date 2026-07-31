"""agentctl — the mailbox verbs the worker agent uses to talk to the world.

Runs inside the container, invoked by the agent via Bash during a turn. It
only ever touches files under ``.agent/``; the orchestrator on the host does
the credentialed relaying.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from issuefleet.agent_runtime import turns
from issuefleet.mailbox import Mailbox


def find_agent_dir(start: Path | None = None) -> Path:
    env = os.environ.get("ISSUEFLEET_AGENT_DIR")
    if env:
        return Path(env)
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".agent" / "state.json").is_file():
            return candidate / ".agent"
    raise SystemExit("agentctl: no .agent directory found above cwd (are you in the worktree?)")


def _text_arg(args) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text().strip()
    if args.text:
        return " ".join(args.text).strip()
    raise SystemExit("agentctl: provide text or --file")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentctl", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="post a progress update (relayed as a Linear comment)")
    p.add_argument("text", nargs="*")
    p.add_argument("--file", help="read the text from a file")

    p = sub.add_parser("ask", help="ask a blocking question, then idle until a human replies")
    p.add_argument("text", nargs="*")
    p.add_argument("--file")

    p = sub.add_parser("ready", help="declare the issue satisfied; hand over PR title and body")
    p.add_argument("--title", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file")

    sub.add_parser(
        "idle",
        help="declare there is nothing left to do; stop taking turns until a human writes",
    )

    sub.add_parser("inbox", help="show pending inbound messages (peek; the turn loop consumes)")

    args = ap.parse_args(argv)
    agent_dir = find_agent_dir()
    mb = Mailbox(agent_dir / "mailbox").ensure()
    state = turns.TurnState.load(agent_dir)

    if args.cmd == "status":
        mb.put_outbox("status", {"text": _text_arg(args)})
        print("status queued for relay")
    elif args.cmd == "ask":
        mb.put_outbox("question", {"text": _text_arg(args)})
        state.phase = turns.PHASE_WAITING
        state.save(agent_dir)
        print("question queued; the loop will idle after this turn until a human replies")
    elif args.cmd == "ready":
        body = args.body if args.body is not None else Path(args.body_file).read_text()
        mb.put_outbox("ready", {"title": args.title, "body": body})
        state.phase = turns.PHASE_READY
        state.save(agent_dir)
        print("ready queued; the orchestrator will push the branch and open/update the PR")
    elif args.cmd == "idle":
        state.phase = turns.PHASE_IDLE
        state.save(agent_dir)
        print("standing by; the loop will idle until a human replies or review feedback arrives")
    elif args.cmd == "inbox":
        pending = mb.pending_inbox()
        if not pending:
            print("inbox empty")
        for m in pending:
            print(f"[{m.seq:06d}] {m.kind}: {m.payload}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
