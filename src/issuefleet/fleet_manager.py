"""The fleet manager: a host-side singleton that bridges a Signal group (via a
sigbot service) to the worker fleet.

It runs alongside the reconcile loop in the daemon and, each tick:

  1. **Ingests Signal** — new messages from the group become either a *goal*
     (recorded as an issue on the dedicated top-level board, optionally assigned
     to the fleet so a worker claims it) or an *answer* routed back to a blocked
     worker (delivered straight into that worker's mailbox inbox, the same
     channel the reconciler uses — no Linear round-trip, so the app-identity
     comment filter can't eat it).
  2. **Watches the fleet** — a worker that emits an `agentctl ask` question is
     triaged: if the ticket + board context clearly answers it, the answer is
     delivered to the worker; otherwise the question is escalated to the human
     over Signal and tracked as pending until they reply.
  3. **Reports progress** — a periodic fleet summary to the group.

Credentials (sigbot key, Linear key, any advisor key) stay host-side, exactly
like the rest of the daemon — the manager never runs in a container. State
(Signal cursor, seen questions, pending escalations) persists to
``fleet_manager.json`` so a restart resumes without re-forwarding or
re-processing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from issuefleet import MARKER_PREFIX, marker
from issuefleet.advisor import BlockedQuestion
from issuefleet.agent import AgentError, Tool, run_agent
from issuefleet.mailbox import Mailbox
from issuefleet.model import PHASE_ACTIVE
from issuefleet.sigbot import SignalError

log = logging.getLogger("issuefleet.fleet_manager")

_SEEN_QUESTIONS_CAP = 5000
_PAGE = 100  # Signal messages fetched per page
_MAX_DRAIN_PAGES = 50  # backstop against a pathological flood in one tick
_ISSUE_KEY_RE = re.compile(r"^\s*([A-Za-z]{2,}-\d+)\b[:\-\s]*", re.ASCII)

# Acknowledgement reactions. Signal replaces a reaction rather than stacking
# them, so _DONE supersedes _SEEN with no explicit clear.
_SEEN = "\N{EYES}"
_DONE = "\N{WHITE HEAVY CHECK MARK}"

_AGENT_SYSTEM = """\
You are the fleet manager for a team of autonomous coding agents. You speak to \
one operator in a Signal group; each message you receive is from them, and your \
reply is posted straight back into that group.

Work out what the message actually is and respond to that. It is usually one of:

- A question about the fleet or the work ("what's going on in the Splanc \
project?"). Investigate with your tools, then answer in plain English. Do not \
file it as an issue.
- New work the operator wants done. Record it with file_goal.
- An answer to a blocked worker's question. Deliver it with reply_to_worker; \
check pending_escalations to see who is waiting and, if more than one worker is, \
ask which they mean instead of guessing.
- Ordinary conversation. Just reply.

Investigate before you answer: a question about a project usually needs \
list_workers and list_open_issues at minimum. Answer from what the tools return, \
and say plainly when something isn't in them rather than guessing.

Reply as a person would in a chat: a few sentences of prose, no headings, no \
markdown tables, no bullet lists unless you are genuinely enumerating several \
items. Lead with the answer. Mention issue keys and PR numbers inline. Keep it \
short — the operator is on their phone.

Act only on what was asked. Don't file goals, deliver replies, or take any other \
action the operator didn't ask for, and don't re-verify work a tool already \
confirmed. If a tool reports it did something, trust it and don't repeat that \
fact back at length."""


class FleetManager:
    def __init__(
        self, config, tracker, signal, advisor, registry, *, clock=time.time, agent_key=None
    ):
        self.cfg = config
        self.fm = config.fleet_manager
        self.tracker = tracker
        self.signal = signal
        self.advisor = advisor
        self.registry = registry
        # An Anthropic key turns the inbound path agentic (see _handle_inbound).
        # Absent, the deterministic dispatch below is the whole behaviour.
        self.agent_key = agent_key
        self._clock = clock
        self.state_path = Path(config.state_dir) / "fleet_manager.json"
        self.state = self._load_state()
        self._bot_names: set[str] | None = None  # cached sigbot identity

    # ------------------------------------------------------------- state

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            log.warning("fleet_manager.json is corrupt; starting fresh")
            data = {}
        data.setdefault("signal_cursor", None)
        data.setdefault("seen_questions", [])
        # Pre-existing state files predate the baseline flag. They were written by
        # a daemon that had already drained (and escalated) whatever was in the
        # outboxes, so treat them as baselined — re-baselining would silently
        # swallow escalations that are genuinely still open.
        data.setdefault("questions_baselined", bool(data.get("seen_questions")))
        data.setdefault("pending", [])
        data.setdefault("last_report", None)  # None = never reported yet
        return data

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.rename(tmp, self.state_path)

    # -------------------------------------------------------------- tick

    def tick(self) -> None:
        self.registry.reload()
        try:
            self._ingest_signal()
        except SignalError:
            log.exception("fleet manager: reading Signal failed; will retry next tick")
        self._watch_fleet()
        try:
            self._maybe_report()
        except SignalError:
            log.exception("fleet manager: sending progress report failed; will retry")
        self._save_state()

    # ---------------------------------------------------------- identity

    def _bot_identity(self) -> set[str]:
        """Lower-cased names the sigbot service posts under, so we skip our own
        messages when reading the group log. Cached; best-effort."""
        if self._bot_names is None:
            names: set[str] = set()
            try:
                svc = self.signal.service()
                for k in ("name", "label", "group_name"):
                    v = svc.get(k)
                    if isinstance(v, str) and v:
                        names.add(v.lower())
            except SignalError:
                log.warning("fleet manager: could not read sigbot service identity", exc_info=True)
            self._bot_names = names
        return self._bot_names

    # ------------------------------------------------------ signal intake

    def _ingest_signal(self) -> None:
        cursor = self.state.get("signal_cursor")
        batch = self.signal.messages(after_id=cursor, limit=_PAGE)
        if not batch:
            return
        # First run: baseline at the newest message rather than replaying the
        # whole group history as goals. Per the sigbot client contract a bare
        # messages(limit=N) returns the most-recent N oldest-first, so [-1] is
        # the true newest.
        if cursor is None:
            self.state["signal_cursor"] = batch[-1].id
            self._save_state()
            log.info("fleet manager: Signal baseline set; listening for new messages")
            return

        ours = self._bot_identity()
        # Drain the full backlog: a burst of more than one page between ticks
        # must not be skipped by jumping the cursor straight to the newest id.
        # Bounded so a pathological flood can't spin a single tick forever.
        pages = 0
        while batch and pages < _MAX_DRAIN_PAGES:
            for m in batch:
                if m.id == cursor:
                    continue  # `after_id` is usually exclusive, but don't rely on it
                if m.outbound:
                    # Our own send, echoed back in the group log. This is the
                    # load-bearing check: sigbot stores NO sender/sender_name on
                    # outgoing rows, so the name comparison below can never
                    # identify them (author falls back to "unknown"). Without
                    # this the manager answers itself — observed filing its own
                    # "📥 Filed FUG-49" confirmation as a new goal, recursively.
                    continue
                if m.author and m.author.lower() in ours:
                    continue  # belt-and-braces for a service that omits direction
                text = (m.text or "").strip()
                if not text:
                    continue
                try:
                    self._handle_inbound(m, text)
                except Exception:
                    # Advance past a poison message rather than head-of-line
                    # block all of Signal ingestion; goal-filing surfaces its
                    # own failures to the group so nothing is silently lost.
                    log.exception("fleet manager: handling Signal message %s failed", m.id)
            self.state["signal_cursor"] = batch[-1].id
            self._save_state()
            if len(batch) < _PAGE:
                break
            pages += 1
            cursor = batch[-1].id
            batch = self.signal.messages(after_id=cursor, limit=_PAGE)

    def _handle_inbound(self, m, text: str) -> None:
        """Route one inbound Signal message.

        The manager is an agent: with an Anthropic key it hands the message to a
        tool loop that can inspect the fleet and act, then replies in plain
        English. _handle_scripted below is the fallback for a daemon with no key
        — a dispatch table that can only file goals and relay replies, which is
        why it answers a question by filing it as a ticket."""
        # 👀 on arrival, ✅ once we've actually answered — the operator can see
        # the manager picked a message up during the seconds an agent turn takes,
        # without either of those states costing a message in the group.
        self.signal.react(m.id, _SEEN)
        if self.agent_key:
            try:
                self._handle_agentically(m, text)
                self.signal.react(m.id, _DONE)
                return
            except AgentError as e:
                log.warning("fleet manager: agent failed (%s); using scripted dispatch", e)
        self._handle_scripted(m, text)
        # Only on the success path: a raised handler leaves 👀 standing, which
        # reads correctly as "seen, but stuck".
        self.signal.react(m.id, _DONE)

    def _handle_scripted(self, m, text: str) -> None:
        if text.lower().startswith("goal:"):
            self._file_goal(m, text[len("goal:"):].strip())
            return
        key = self._leading_issue_key(text)
        if key and self._worker_for_key(key):
            self._route_answer(key, self._strip_key(text), author=m.author)
            return
        pending = self.state["pending"]
        if len(pending) == 1:
            # Exactly one question outstanding — a bare reply answers it.
            self._route_answer(pending[0]["issue_key"], text, author=m.author, pending=pending[0])
            return
        if len(pending) > 1:
            # Ambiguous: don't guess which question a bare reply answers.
            self.signal.send(
                "❓ Multiple workers are waiting. Prefix your reply with the issue key "
                "(e.g. `FUG-12: ...`), or start with `goal:` to file a new goal."
            )
            return
        self._file_goal(m, text)

    # ------------------------------------------------------- agent path

    def _handle_agentically(self, m, text: str) -> None:
        """Hand the message to the tool loop and post whatever it says back.

        The agent decides what the message *is* — a question about the fleet, a
        goal to record, an answer for a blocked worker — instead of us guessing
        from prefixes. Its tools cover reads and the two write actions, so a
        single turn can investigate and then act."""
        reply = run_agent(
            api_key=self.agent_key,
            system=_AGENT_SYSTEM,
            user_message=(
                f"Message from {m.author or 'the operator'} in the Signal group:\n\n{text}"
            ),
            tools=self._agent_tools(m),
        )
        if reply:
            self.signal.send(reply)

    def _agent_tools(self, m) -> list:
        """The manager's tool surface. Reads first, then the two actions that
        change something — both of which the deterministic path also performs,
        so the agent has no powers the scripted dispatch lacks."""

        def list_workers(_):
            workers = self.registry.all()
            if not workers:
                return "No workers are registered."
            lines = []
            for w in workers:
                pending = [
                    p for p in self.state["pending"] if p["issue_key"].lower() == w.issue_key.lower()
                ]
                lines.append(
                    f"{w.issue_key} [{w.project}] phase={w.phase} "
                    f"restarts={w.restarts} branch={w.branch} "
                    f"PR={('#' + str(w.pr_number)) if w.pr_number else 'none'} "
                    f"awaiting_human={'yes' if pending else 'no'} — {w.issue_title}"
                )
            return "\n".join(lines)

        def list_open_issues(args):
            ref = str(args.get("project") or self.fm.board_project)
            issues = self.tracker.open_issues_in_project(ref)
            if not issues:
                return f"No open issues in {ref!r}."
            claimed = {w.issue_key.lower() for w in self.registry.all()}
            return "\n".join(
                f"{i.key}: {i.title} ({i.state_name})"
                f"{' [claimed by a worker]' if i.key.lower() in claimed else ''}"
                for i in issues[:100]
            )

        def get_issue(args):
            key = str(args.get("issue_key") or "").strip()
            # The tracker has no by-key lookup, so scan the boards we know: the
            # goals board plus every configured project (where worker issues live).
            refs = [self.fm.board_project] + [p.linear_project for p in self.cfg.projects]
            issue = None
            for ref in refs:
                try:
                    found = [i for i in self.tracker.open_issues_in_project(ref)
                             if i.key.lower() == key.lower()]
                except Exception:
                    continue  # a bad project ref shouldn't sink the whole lookup
                if found:
                    issue = found[0]
                    break
            if issue is None:
                return (
                    f"No open issue {key!r} in the goals board or any configured project. "
                    "It may be closed, or in a project this fleet doesn't watch."
                )
            return (
                f"{issue.key}: {issue.title}\nstate: {issue.state_name}\nurl: {issue.url}\n\n"
                f"{getattr(issue, 'description', '') or '(no description)'}"
            )

        def pending_escalations(_):
            pending = self.state["pending"]
            if not pending:
                return "No workers are waiting on the human."
            return "\n".join(f"{p['issue_key']}: {p['question']}" for p in pending)

        def file_goal(args):
            body = str(args.get("text") or "").strip()
            if not body:
                return "Refused: a goal needs text."
            before = self.tracker.find_issue_by_marker(MARKER_PREFIX + f"goal-{m.id}")
            self._file_goal(m, body)  # sends its own Signal confirmation
            after = self.tracker.find_issue_by_marker(MARKER_PREFIX + f"goal-{m.id}")
            if after is None or after is before:
                return "Filing the goal failed; the operator has been told."
            return f"Filed {after.key} on the board. Do not repeat this in your reply."

        def reply_to_worker(args):
            key = str(args.get("issue_key") or "").strip()
            body = str(args.get("text") or "").strip()
            if not key or not body:
                return "Refused: reply_to_worker needs both issue_key and text."
            rec = self._worker_for_key(key)
            if rec is None:
                return f"{key} has no active worker; nothing was delivered."
            if not self._deliver_to_worker(rec, m.author or "human (via Signal)", body):
                return f"Couldn't reach {key}'s worker (worktree gone); nothing was delivered."
            self.state["pending"] = [
                p for p in self.state["pending"] if p["issue_key"].lower() != key.lower()
            ]
            return f"Delivered to {key}'s worker and cleared its pending escalation."

        _KEY = {
            "type": "object",
            "properties": {"issue_key": {"type": "string", "description": "e.g. FUG-12"}},
            "required": ["issue_key"],
        }
        return [
            Tool("list_workers", "Every registered worker: issue, project, phase, turn, "
                 "branch, PR, and whether it is waiting on a human answer.",
                 {"type": "object", "properties": {}}, list_workers),
            Tool("list_open_issues", "Open issues in a Linear project. Omit 'project' for the "
                 "top-level goals board. Marks which are already claimed by a worker.",
                 {"type": "object",
                  "properties": {"project": {"type": "string",
                                             "description": "Linear project name; "
                                                            "defaults to the goals board"}}},
                 list_open_issues),
            Tool("get_issue", "Full title, state, URL and description for one issue.",
                 _KEY, get_issue),
            Tool("pending_escalations", "Questions from blocked workers currently awaiting "
                 "a human answer.", {"type": "object", "properties": {}}, pending_escalations),
            Tool("file_goal", "Record a NEW piece of work as an issue on the goals board. "
                 "Only for genuine new work the operator wants done — never to record a "
                 "question, and never for work an open issue already covers.",
                 {"type": "object",
                  "properties": {"text": {"type": "string",
                                          "description": "The goal, in the operator's words"}},
                  "required": ["text"]},
                 file_goal),
            Tool("reply_to_worker", "Deliver an answer into a blocked worker's mailbox, "
                 "unblocking it. Use when the operator is answering a pending question.",
                 {"type": "object",
                  "properties": {"issue_key": {"type": "string"},
                                 "text": {"type": "string"}},
                  "required": ["issue_key", "text"]},
                 reply_to_worker),
        ]

    @staticmethod
    def _leading_issue_key(text: str) -> str | None:
        match = _ISSUE_KEY_RE.match(text)
        return match.group(1).upper() if match else None

    @staticmethod
    def _strip_key(text: str) -> str:
        return _ISSUE_KEY_RE.sub("", text, count=1).strip() or text

    def _worker_for_key(self, key: str):
        return next(
            (w for w in self.registry.all() if w.issue_key.lower() == key.lower()), None
        )

    # ------------------------------------------------------------- goals

    def _file_goal(self, m, text: str) -> None:
        if not text:
            return
        needle = MARKER_PREFIX + f"goal-{m.id}"
        if self.tracker.find_issue_by_marker(needle) is not None:
            return  # filed on a previous attempt that crashed before advancing the cursor
        title = (text.splitlines()[0] or "New goal").strip()[:80]
        description = (
            f"{text}\n\nRecorded from Signal by the fleet manager.\n\n{marker(f'goal-{m.id}')}"
        )
        try:
            issue, _unknown = self.tracker.create_issue(
                title=title,
                description=description,
                team=self.fm.board_team,
                project=self.fm.board_project,
                use_context_project=False,
            )
        except Exception:
            # Surface the failure to the group rather than lose the goal
            # silently — the human can resend. (The cursor still advances, so a
            # poison message can't wedge ingestion.)
            log.exception("fleet manager: filing goal from Signal failed")
            try:
                self.signal.send("⚠️ Couldn't record that goal (tracker error); please resend.")
            except SignalError:
                pass
            return
        assigned = ""
        if self.fm.assign_goals:
            try:
                self.tracker.assign_issue(issue.id, self.tracker.get_viewer_id())
                assigned = " (assigned to the fleet)"
            except Exception:
                log.exception("fleet manager: assigning goal %s failed", issue.key)
        self.signal.send(f"📥 Filed {issue.key}: {title} — {issue.url}{assigned}")

    # ---------------------------------------------------------- answers

    def _route_answer(self, issue_key: str, text: str, author, pending=None) -> None:
        rec = self._worker_for_key(issue_key)
        if rec is None:
            self.signal.send(
                f"⚠️ {issue_key} has no active worker; your reply wasn't delivered."
            )
            if pending in self.state["pending"]:
                self.state["pending"].remove(pending)
            return
        who = author or "human (via Signal)"
        if not self._deliver_to_worker(rec, who, text):
            self.signal.send(f"⚠️ Couldn't reach {issue_key}'s worker; your reply wasn't delivered.")
            return
        self.signal.send(f"✅ Relayed your answer to {issue_key}.")
        # Clear every pending escalation for this issue — the human has spoken.
        self.state["pending"] = [
            p for p in self.state["pending"] if p["issue_key"].lower() != issue_key.lower()
        ]

    def _deliver_to_worker(self, rec, author: str, text: str) -> bool:
        """Write a reply into the worker's mailbox inbox (the reconciler's own
        wake channel). Returns False if the worktree is gone."""
        try:
            Mailbox(Path(rec.worktree) / ".agent" / "mailbox").ensure().put_inbox(
                "reply", {"author": author, "text": text, "source": "signal"}
            )
            return True
        except OSError:
            log.warning("fleet manager: worktree for %s is gone; cannot deliver", rec.issue_key)
            return False

    # ------------------------------------------------------- fleet watch

    def _watch_fleet(self) -> None:
        self._board_cache: str | None = None  # computed lazily, once per tick
        if not self.state.get("questions_baselined"):
            self._baseline_questions()
        for rec in self.registry.all():
            try:
                self._triage_worker(rec)
            except Exception:
                log.exception("fleet manager: triage of %s failed", rec.issue_key)
        self.state["seen_questions"] = self.state["seen_questions"][-_SEEN_QUESTIONS_CAP:]

    def _baseline_questions(self) -> None:
        """First run: mark the workers' already-ARCHIVED questions as seen, so
        history isn't replayed as live escalations.

        The Signal cursor is baselined the same way (see _ingest_signal) so the
        group's history isn't replayed as goals; without the equivalent here a
        fresh state_dir escalated every archived `agentctl ask` — including
        questions the worker resolved hours or days ago. _new_questions reads
        archived_outbox() as well as pending (the reconciler archives an outbox
        message as soon as it drains it, so a live question is usually already
        archived by the time we look), and that is exactly what makes an
        unbaselined first run replay the whole history.

        Only the archive is baselined. A question still in pending_outbox has
        not been drained by anyone yet, so it is genuinely unanswered and must
        still escalate on this very tick — that is the difference between "we
        booted late" and "we lost the question".
        """
        ids = []
        for rec in self.registry.all():
            try:
                mailbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")
                ids += [m.id for m in mailbox.archived_outbox() if m.kind == "question"]
            except OSError:
                continue  # worktree gone; nothing to baseline
        self.state["seen_questions"] = (self.state["seen_questions"] + ids)[
            -_SEEN_QUESTIONS_CAP:
        ]
        self.state["questions_baselined"] = True
        self._save_state()
        log.info(
            "fleet manager: question baseline set (%d archived question(s) marked seen); "
            "pending questions still escalate",
            len(ids),
        )

    def _board(self) -> str:
        # Only read the top-level board when a worker is actually blocked, and
        # at most once per tick.
        if self._board_cache is None:
            self._board_cache = self._board_summary()
        return self._board_cache

    def _triage_worker(self, rec) -> None:
        mailbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")
        questions = self._new_questions(rec, mailbox)
        if not questions:
            return
        issue = None
        for msg in questions:
            qtext = (msg.payload.get("text") or "").strip()
            if not qtext:
                self.state["seen_questions"].append(msg.id)
                continue
            # get_issue / advisor / signal.send below may raise; if so the
            # exception propagates to _watch_fleet and this question is NOT
            # marked seen, so it's retried next tick rather than lost.
            if issue is None:
                issue = self.tracker.get_issue(rec.issue_id)
            ticket = (
                f"{issue.title}\n\n{issue.description}" if issue else rec.issue_title
            )
            verdict = self.advisor.triage(
                BlockedQuestion(rec.issue_key, qtext, ticket, self._board())
            )
            if verdict.answerable:
                delivered = self._deliver_to_worker(
                    rec,
                    "fleet-manager",
                    f"{verdict.answer}\n\n(Answered from context by the fleet manager. "
                    "If this is wrong, use `agentctl ask` again.)",
                )
                if delivered:
                    self.signal.send(
                        f"🤖 Auto-answered {rec.issue_key} from context: {qtext[:120]}"
                    )
                # If the worktree is gone the worker is winding down; the
                # question is moot — mark seen (below) either way.
            else:
                self.signal.send(
                    f"❓ {rec.issue_key} is blocked and needs you:\n\n{qtext}\n\n"
                    f"Reply here to answer (or prefix with `{rec.issue_key}:`)."
                )
                self.state["pending"].append(
                    {
                        "msg_id": msg.id,
                        "issue_id": rec.issue_id,
                        "issue_key": rec.issue_key,
                        "question": qtext,
                    }
                )
            self.state["seen_questions"].append(msg.id)

    def _new_questions(self, rec, mailbox: Mailbox) -> list:
        seen = set(self.state["seen_questions"])
        by_id: dict[str, object] = {}
        for m in mailbox.pending_outbox() + mailbox.archived_outbox():
            if m.kind == "question" and m.id not in seen and m.id not in by_id:
                by_id[m.id] = m
        return sorted(by_id.values(), key=lambda m: m.seq)

    def _board_summary(self) -> str:
        try:
            issues = self.tracker.open_issues_in_project(self.fm.board_project)
        except Exception:
            log.exception("fleet manager: reading the top-level board failed")
            return ""
        if not issues:
            return "The top-level board has no open issues."
        lines = [f"- {i.key}: {i.title} ({i.state_name})" for i in issues[:50]]
        return "Open issues on the top-level board:\n" + "\n".join(lines)

    # ----------------------------------------------------------- reports

    def _maybe_report(self) -> None:
        interval = self.fm.report_interval_s
        if interval <= 0:
            return
        now = self._clock()
        last = self.state.get("last_report")
        if last is not None and now - last < interval:
            return
        # Commit the timestamp only after a successful send, so a transient
        # sigbot outage retries next tick instead of skipping a full interval.
        self.signal.send(self._report_text())
        self.state["last_report"] = now

    def _report_text(self) -> str:
        active = [w for w in self.registry.all() if w.phase == PHASE_ACTIVE]
        pending = self.state["pending"]
        lines = [
            f"📊 Fleet status: {len(active)} active worker(s), "
            f"{len(pending)} awaiting your input."
        ]
        for w in active:
            pr = f" — PR #{w.pr_number}" if w.pr_number else ""
            lines.append(f"• {w.issue_key}: {w.issue_title}{pr}")
        for p in pending:
            lines.append(f"⏳ {p['issue_key']} waiting on you: {p['question'][:80]}")
        return "\n".join(lines)
