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
from issuefleet.mailbox import Mailbox
from issuefleet.model import PHASE_ACTIVE
from issuefleet.sigbot import SignalError

log = logging.getLogger("issuefleet.fleet_manager")

_SEEN_QUESTIONS_CAP = 1000
_ISSUE_KEY_RE = re.compile(r"^\s*([A-Za-z]{2,}-\d+)\b[:\-\s]*", re.ASCII)


class FleetManager:
    def __init__(self, config, tracker, signal, advisor, registry, *, clock=time.time):
        self.cfg = config
        self.fm = config.fleet_manager
        self.tracker = tracker
        self.signal = signal
        self.advisor = advisor
        self.registry = registry
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
        msgs = self.signal.messages(after_id=cursor, limit=100)
        if not msgs:
            return
        # First run: establish a baseline at the newest message rather than
        # replaying the whole group history as goals.
        if cursor is None:
            self.state["signal_cursor"] = msgs[-1].id
            self._save_state()
            log.info("fleet manager: Signal baseline set; listening for new messages")
            return

        ours = self._bot_identity()
        new_cursor = msgs[-1].id
        for m in msgs:
            if m.id == cursor:
                continue  # `after_id` is usually exclusive, but don't rely on it
            if m.author and m.author.lower() in ours:
                continue  # our own send, echoed back in the log
            text = (m.text or "").strip()
            if not text:
                continue
            try:
                self._handle_inbound(m, text)
            except Exception:
                log.exception("fleet manager: handling Signal message %s failed", m.id)
        self.state["signal_cursor"] = new_cursor
        self._save_state()

    def _handle_inbound(self, m, text: str) -> None:
        if text.lower().startswith("goal:"):
            self._file_goal(m, text[len("goal:"):].strip())
            return
        key = self._leading_issue_key(text)
        if key and self._worker_for_key(key):
            self._route_answer(key, self._strip_key(text), author=m.author)
            return
        if self.state["pending"]:
            oldest = self.state["pending"][0]
            self._route_answer(oldest["issue_key"], text, author=m.author, pending=oldest)
            return
        self._file_goal(m, text)

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
        issue, _unknown = self.tracker.create_issue(
            title=title,
            description=description,
            team=self.fm.board_team,
            project=self.fm.board_project,
            use_context_project=False,
        )
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
        for rec in self.registry.all():
            try:
                self._triage_worker(rec)
            except Exception:
                log.exception("fleet manager: triage of %s failed", rec.issue_key)
        self.state["seen_questions"] = self.state["seen_questions"][-_SEEN_QUESTIONS_CAP:]

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
            self.state["seen_questions"].append(msg.id)
            qtext = (msg.payload.get("text") or "").strip()
            if not qtext:
                continue
            if issue is None:
                issue = self.tracker.get_issue(rec.issue_id)
            ticket = (
                f"{issue.title}\n\n{issue.description}" if issue else rec.issue_title
            )
            verdict = self.advisor.triage(
                BlockedQuestion(rec.issue_key, qtext, ticket, self._board())
            )
            if verdict.answerable:
                self._deliver_to_worker(
                    rec,
                    "fleet-manager",
                    f"{verdict.answer}\n\n(Answered from context by the fleet manager. "
                    "If this is wrong, use `agentctl ask` again.)",
                )
                self.signal.send(
                    f"🤖 Auto-answered {rec.issue_key} from context: {qtext[:120]}"
                )
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
        self.state["last_report"] = now
        self.signal.send(self._report_text())

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
