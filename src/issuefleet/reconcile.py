"""The reconcile loop: one tick observes everything and converges the fleet.

Design rules (from the brief):
- per-worker exception isolation — one sick worker never stalls the fleet;
- every credentialed act is at-least-once with explicit dedupe: outbox
  messages stay pending until relayed (archive = ack), and every posted body
  carries an HTML-comment marker so a retry after a half-failure is a no-op;
- restart-safe: claims are idempotent (adopt existing worktrees/branches),
  the registry is written before side effects that a liveness check can
  repair, and orchestrator-origin comments use deterministic marker ids.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from issuefleet import marker, MARKER_PREFIX
from issuefleet import worker as worker_mod
from issuefleet.config import Config, ProjectConfig
from issuefleet.mailbox import Mailbox
from issuefleet.model import (
    PHASE_ACTIVE,
    PHASE_CRASHED,
    Issue,
    WorkerRecord,
)
from issuefleet.registry import Registry

log = logging.getLogger("issuefleet")

_SEEN_IDS_CAP = 1000

# Activity content types the orchestrator itself emits into sessions; a
# `prompted` webhook carrying one of these is an echo, never a user prompt.
_AGENT_EMITTED_ACTIVITY_TYPES = {"thought", "action", "elicitation", "response", "error"}


def _is_user_prompt(evt) -> bool:
    """True only for prompted events that are genuinely a human talking to
    the agent. Positive signals: activity type 'prompt', or a user actor.
    Echoes carry an agent-emitted activity type and/or an app/integration
    actor. When both fields are absent we accept (a lost user prompt is
    unrecoverable — session prompts aren't pollable) and rely on the turn
    loop's ready-restore to bound any residual echo to a single no-op turn."""
    if evt.activity_type in _AGENT_EMITTED_ACTIVITY_TYPES:
        return False
    if evt.activity_type == "prompt":
        return True
    if evt.actor_type in ("application", "oauthclientapp", "oauth_client", "integration", "app"):
        return False
    return True


def slugify(title: str, max_len: int = 32) -> str:
    out = []
    for ch in title.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:max_len].rstrip("-") or "issue"


class Reconciler:
    def __init__(
        self,
        config: Config,
        registry: Registry,
        tracker,
        forges: dict[str, object],  # project name -> Forge
        git,
        runner,
    ):
        self.cfg = config
        self.registry = registry
        self.tracker = tracker
        self.forges = forges
        self.git = git
        self.runner = runner
        # Linear agent sessions: fed by the webhook thread, drained at tick.
        self._session_lock = threading.Lock()
        self._session_events: list = []
        self.pending_session_claims: dict[str, object] = {}  # issue_id -> SessionEvent
        self._poll_errors: dict[str, str] = {}  # project -> last error (log-spam collapse)

    # ------------------------------------------------------------------ tick

    # -------------------------------------------------- agent sessions

    def enqueue_session(self, evt) -> None:
        """Thread-safe intake for webhooks.SessionEvent; processed next tick."""
        with self._session_lock:
            self._session_events.append(evt)

    def _drain_session_events(self) -> None:
        with self._session_lock:
            events, self._session_events = self._session_events, []
        for evt in events:
            rec = self.registry.get(evt.issue_id) if evt.issue_id else None
            if rec is None and evt.session_id:
                rec = next(
                    (w for w in self.registry.all() if w.agent_session_id == evt.session_id),
                    None,
                )
            if evt.action == "prompted":
                if not _is_user_prompt(evt):
                    # Linear also delivers session events for activities WE
                    # emit; routing those into the inbox echoes the agent's
                    # own status back as a waking reply — an infinite
                    # relay->webhook->turn loop (observed live 2026-07-30).
                    log.info(
                        "dropping echoed session activity for %s (activity_type=%s actor=%s)",
                        evt.issue_key, evt.activity_type, evt.actor_type,
                    )
                    continue
                if rec is None:
                    log.warning("session prompt for unclaimed issue %s; dropped "
                                "(sender sees the ack/queue state in the session)", evt.issue_key)
                    continue
                Mailbox(Path(rec.worktree) / ".agent" / "mailbox").ensure().put_inbox(
                    "reply",
                    {"author": "agent-session", "text": evt.body or "", "source": "linear"},
                )
            elif evt.action == "created":
                if rec is not None:
                    rec.agent_session_id = evt.session_id
                    rec.touch()
                    self.registry.save()
                elif evt.issue_id:
                    self.pending_session_claims[evt.issue_id] = evt

    def _project_for_issue(self, issue: Issue) -> ProjectConfig | None:
        if issue.project_id is None:
            return None
        for project in self.cfg.projects:
            try:
                if self.tracker.resolve_project_id(project) == issue.project_id:
                    return project
            except Exception:
                log.exception("resolving project id for %s failed", project.name)
        return None

    def _claim_sessions(self) -> None:
        """Delegation/mention claims. These are explicit human acts, so they
        claim regardless of the project's poll-side claim rule."""
        for issue_id, evt in list(self.pending_session_claims.items()):
            if self.registry.get(issue_id) is not None:
                self.pending_session_claims.pop(issue_id)
                continue
            active = [w for w in self.registry.all() if w.phase == PHASE_ACTIVE]
            if len(active) >= self.cfg.max_workers:
                log.info("session claim for %s waiting: fleet full", evt.issue_key)
                continue  # stays pending until capacity frees
            try:
                issue = self.tracker.get_issue(issue_id)
                if issue is None or not issue.open:
                    self.pending_session_claims.pop(issue_id)
                    continue
                project = self._project_for_issue(issue)
                if project is None:
                    log.error("agent session for %s: issue's Linear project is not in the "
                              "config; ignoring", issue.key)
                    self._emit_activity_quietly(
                        evt.session_id,
                        {"type": "error",
                         "body": "This issue's project is not configured in issuefleet; "
                                 "add it to the fleet config to delegate here."},
                    )
                    self.pending_session_claims.pop(issue_id)
                    continue
                self._claim_one(issue, project, session=evt)
                self.pending_session_claims.pop(issue_id)
            except Exception:
                log.exception("session claim of %s failed; will retry next tick", evt.issue_key)

    def _emit_activity_quietly(self, session_id: str | None, content: dict) -> None:
        if not session_id:
            return
        try:
            self.tracker.emit_activity(session_id, content)
        except Exception:
            log.exception("agent activity emit failed (session %s)", session_id)

    def tick(self) -> None:
        self._drain_session_events()
        for rec in self.registry.all():
            try:
                self._service(rec)
            except Exception:
                log.exception("worker %s: reconcile failed; will retry next tick", rec.issue_key)

        # Poll for claimable work *after* servicing: a worker wound down this
        # tick (merged PR, closed issue) must not be re-claimed off a stale
        # snapshot taken before its state change landed.
        eligible: dict[str, list[Issue]] = {}
        for project in self.cfg.projects:
            try:
                eligible[project.name] = self.tracker.eligible_issues(project)
                if self._poll_errors.pop(project.name, None):
                    log.info("polling %s recovered", project.name)
            except Exception as e:
                # Full traceback once per distinct failure; a persistent
                # outage (DNS down, API outage) collapses to one line per
                # tick instead of a traceback storm.
                summary = f"{type(e).__name__}: {e}"
                if self._poll_errors.get(project.name) == summary:
                    log.error("polling %s still failing (%s); will retry", project.name, summary)
                else:
                    self._poll_errors[project.name] = summary
                    log.exception("polling %s failed; skipping claims for it this tick", project.name)
                eligible[project.name] = []

        try:
            self._claim_sessions()
            self._claim(eligible)
        except Exception:
            log.exception("claim pass failed; will retry next tick")

    # -------------------------------------------------------------- dry run

    def plan(self) -> list[str]:
        """What the next tick WOULD do — API reads only, zero writes to
        Linear, GitHub, or the filesystem. Used by `once --dry-run` and
        `doctor`'s would-claim report."""
        lines: list[str] = []
        for rec in self.registry.all():
            try:
                lines.extend(self._plan_worker(rec))
            except Exception as e:
                lines.append(f"{rec.issue_key}: cannot inspect ({e})")

        eligible: dict[str, list[Issue]] = {}
        for project in self.cfg.projects:
            try:
                eligible[project.name] = self.tracker.eligible_issues(project)
            except Exception as e:
                lines.append(f"{project.name}: cannot poll Linear ({e})")
                eligible[project.name] = []
        claim_now, waiting = self.claim_queue(eligible)
        for issue, project in claim_now:
            branch = project.branch_template.format(key=issue.key.lower(), slug=slugify(issue.title))
            lines.append(
                f"{issue.key}: would claim ({project.name}, priority {issue.priority}) "
                f"-> branch {branch}, worktree {self.cfg.worktree_root / project.name / issue.key}"
            )
        for issue, project in waiting:
            lines.append(f"{issue.key}: eligible but waiting (fleet full)")
        return lines or ["nothing to do"]

    def _plan_worker(self, rec: WorkerRecord) -> list[str]:
        lines = []
        project = self.cfg.project(rec.project)
        issue = self.tracker.get_issue(rec.issue_id)
        reason = self._unclaim_reason(project, issue, rec)
        if reason:
            return [f"{rec.issue_key}: would un-claim and tear down ({reason})"]
        if rec.phase == PHASE_CRASHED:
            return [f"{rec.issue_key}: crashed (kept for inspection); no action"]
        if not self.runner.alive(rec):
            if rec.restarts >= self.cfg.max_restarts:
                lines.append(f"{rec.issue_key}: session dead; would give up and report crash")
            else:
                lines.append(
                    f"{rec.issue_key}: session dead; would restart (attempt {rec.restarts + 1})"
                )
        mailbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")
        for msg in mailbox.pending_outbox():
            if msg.kind == "ready":
                lines.append(f"{rec.issue_key}: would push {rec.branch} and open/update PR")
            elif msg.kind == "file_issue":
                lines.append(
                    f"{rec.issue_key}: would file a new Linear issue "
                    f"({msg.payload.get('title', '?')!r})"
                )
            else:
                lines.append(f"{rec.issue_key}: would relay {msg.kind} to Linear")
        inbound = [
            c
            for c in self.tracker.comments_since(rec.issue_id, rec.comment_cursor)
            if MARKER_PREFIX not in c.body
        ]
        if inbound:
            lines.append(f"{rec.issue_key}: would ingest {len(inbound)} new Linear comment(s)")
        if rec.pr_number is not None:
            forge = self.forges[project.name]
            new_fb = [f for f in forge.pr_feedback(rec.pr_number) if f.id not in rec.seen_feedback_ids]
            if new_fb:
                lines.append(f"{rec.issue_key}: would forward {len(new_fb)} PR feedback item(s)")
            pr = forge.get_pr(rec.pr_number)
            if pr.merged:
                lines.append(f"{rec.issue_key}: PR #{pr.number} merged; would tear down")
            elif pr.state == "closed" and f"closed-{pr.number}" not in rec.seen_feedback_ids:
                lines.append(f"{rec.issue_key}: PR #{pr.number} closed unmerged; would notify agent")
        return lines or [f"{rec.issue_key}: up to date; no action"]

    # ------------------------------------------------------------- servicing

    def _service(self, rec: WorkerRecord) -> None:
        project = self.cfg.project(rec.project)
        mailbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")

        issue = self.tracker.get_issue(rec.issue_id)
        reason = self._unclaim_reason(project, issue, rec)
        if reason:
            log.info("worker %s: un-claiming (%s)", rec.issue_key, reason)
            self._wind_down(rec, project, mailbox, reason=reason, done=False)
            return

        if rec.phase == PHASE_CRASHED:
            return  # kept only so the issue isn't re-claimed; operator's move

        if not self.runner.alive(rec):
            if rec.restarts >= self.cfg.max_restarts:
                rec.phase = PHASE_CRASHED
                rec.touch()
                self.registry.save()
                self._emit_activity_quietly(
                    rec.agent_session_id,
                    {"type": "error", "body": "Worker session died repeatedly; giving up. "
                     "Worktree kept for inspection on the orchestrator host."},
                )
                self._post_once(
                    rec.issue_id,
                    f"crash-{rec.issue_id}-{rec.restarts}",
                    f"⚠️ Worker session died {rec.restarts + 1} times; giving up. "
                    f"Worktree kept for inspection at `{rec.worktree}` "
                    f"(branch `{rec.branch}`). Re-trigger by removing and re-adding "
                    f"the claim ({project.claim.strategy}={project.claim.value!r}).",
                )
                return
            log.warning("worker %s: session dead, restarting (%d so far)", rec.issue_key, rec.restarts)
            mailbox.ensure().put_inbox(
                "info", {"text": "Your session was restarted after a crash; check `git status` and continue."}
            )
            self.runner.start(rec, self.cfg)
            rec.restarts += 1
            rec.touch()
            self.registry.save()

        self._drain_outbox(rec, project, mailbox)
        self._ingest_comments(rec, mailbox)
        self._check_pr(rec, project, mailbox)

    def _unclaim_reason(
        self, project: ProjectConfig, issue: Issue | None, rec: WorkerRecord | None = None
    ) -> str | None:
        if issue is None:
            return "issue disappeared from the tracker"
        if not issue.open:
            return f"issue was closed ({issue.state_name})"
        # Session-originated claims (delegation/@-mention) aren't governed by
        # the poll-side claim rule; only closure winds them down.
        if rec is not None and rec.claim_origin == "session":
            return None
        claim = project.claim
        # For the `state` strategy, claiming itself moves the issue out of the
        # claim state, so only closure un-claims (checked above).
        if claim.strategy == "label" and claim.value not in issue.labels:
            return f"label {claim.value!r} was removed"
        if claim.strategy == "assignee" and issue.assignee_id != claim.value:
            return "assignee changed"
        if claim.strategy == "agent" and self.tracker.get_viewer_id() not in (
            issue.assignee_id,
            issue.delegate_id,
        ):
            return "no longer delegated to the agent"
        return None

    # ---------------------------------------------------------------- relays

    def _drain_outbox(self, rec: WorkerRecord, project: ProjectConfig, mailbox: Mailbox) -> None:
        for msg in mailbox.pending_outbox():
            try:
                if self.tracker.has_comment_marker(rec.issue_id, msg.id):
                    # A previous attempt posted but crashed before archiving.
                    mailbox.archive_outbox(msg, receipt={"deduped": True})
                    continue
                if msg.kind == "status":
                    if rec.agent_session_id:
                        # Session relays have no marker probe; a crash between
                        # emit and archive re-emits (a duplicate thought is
                        # cosmetic, unlike a duplicate comment).
                        self.tracker.emit_activity(
                            rec.agent_session_id, {"type": "thought", "body": msg.payload["text"]}
                        )
                        mailbox.archive_outbox(msg, receipt={"relayed": "agent-session"})
                    else:
                        self.tracker.post_comment(
                            rec.issue_id, f"🤖 {msg.payload['text']}\n\n{marker(msg.id)}"
                        )
                        mailbox.archive_outbox(msg, receipt={"relayed": "linear"})
                elif msg.kind == "question":
                    if rec.agent_session_id:
                        self.tracker.emit_activity(
                            rec.agent_session_id,
                            {"type": "elicitation", "body": msg.payload["text"]},
                        )
                        mailbox.archive_outbox(msg, receipt={"relayed": "agent-session"})
                    else:
                        self.tracker.post_comment(
                            rec.issue_id,
                            "🤖❓ **The agent is blocked on a question** — it will idle "
                            f"until someone replies on this issue:\n\n{msg.payload['text']}"
                            f"\n\n{marker(msg.id)}",
                        )
                        mailbox.archive_outbox(msg, receipt={"relayed": "linear"})
                elif msg.kind == "file_issue":
                    self._handle_file_issue(rec, mailbox, msg)
                elif msg.kind == "ready":
                    self._handle_ready(rec, project, mailbox, msg)
                else:
                    log.error("worker %s: unknown outbox kind %r; archiving", rec.issue_key, msg.kind)
                    mailbox.archive_outbox(msg, receipt={"error": "unknown kind"})
            except Exception:
                # Leave this and everything after it pending, preserving
                # order; next tick retries (dedupe via marker).
                log.exception("worker %s: relay of %s failed; will retry", rec.issue_key, msg.kind)
                return

    def _handle_file_issue(self, rec: WorkerRecord, mailbox: Mailbox, msg) -> None:
        """Relay a worker's request to author a new Linear issue. Dedupe is by
        a marker embedded in the new issue's description: a create that landed
        but wasn't acked (crash between the API call and archive) is found on
        retry instead of filed twice. The worker learns the new key/url via an
        `info` message so it can summarize back to the human."""
        needle = MARKER_PREFIX + msg.id
        existing = self.tracker.find_issue_by_marker(needle)
        if existing is not None:
            mailbox.put_inbox(
                "info",
                {"text": f"Issue already filed (deduped): {existing.key} — {existing.url}"},
            )
            mailbox.archive_outbox(msg, receipt={"issue": existing.key, "deduped": True})
            return

        p = msg.payload
        description = f"{p.get('description', '')}\n\n{marker(msg.id)}".strip()
        issue, unknown = self.tracker.create_issue(
            title=p["title"],
            description=description,
            priority=p.get("priority"),
            labels=p.get("labels") or [],
            team=p.get("team"),
            project=p.get("project"),
            use_context_project=p.get("use_context_project", True),
            context_issue_id=rec.issue_id,
        )
        text = f"Filed {issue.key}: {issue.title} — {issue.url}"
        if unknown:
            text += f"\n(labels not found, skipped: {', '.join(unknown)})"
        mailbox.put_inbox("info", {"text": text})
        mailbox.archive_outbox(msg, receipt={"issue": issue.key, "url": issue.url})

    def _handle_ready(self, rec: WorkerRecord, project: ProjectConfig, mailbox: Mailbox, msg) -> None:
        forge = self.forges[project.name]
        if not self.git.has_commits_ahead(Path(rec.worktree), rec.base_ref):
            # Wake the agent with a waking kind, or it idles in `ready` forever.
            mailbox.put_inbox(
                "reply",
                {
                    "author": "issuefleet-orchestrator",
                    "text": "Your `ready` was rejected: the branch has no commits on top of "
                    f"`{rec.base_ref}`. Commit your work (or explain via `agentctl ask`) "
                    "and submit again.",
                },
            )
            mailbox.archive_outbox(msg, receipt={"rejected": "no commits ahead"})
            return

        title = msg.payload.get("title") or f"{rec.issue_key}: {rec.issue_title}"
        body = msg.payload.get("body", "")
        body_full = f"{body}\n\nCloses-Linear: {rec.issue_key} ({rec.issue_url})"
        self.git.push(Path(rec.worktree), rec.branch)

        pr = None
        if rec.pr_number is not None:
            pr = forge.get_pr(rec.pr_number)
            if pr.state == "open":
                forge.update_pr(pr.number, title, body_full)
            else:
                pr = None
        if pr is None:
            pr = forge.find_pr(rec.branch)
            if pr is not None:
                forge.update_pr(pr.number, title, body_full)
        if pr is None:
            pr = forge.open_pr(rec.branch, rec.base_ref, title, body_full)

        newly_opened = rec.pr_number != pr.number
        rec.pr_number, rec.pr_url = pr.number, pr.url
        rec.touch()
        self.registry.save()
        if rec.agent_session_id:
            self._emit_activity_quietly(
                rec.agent_session_id,
                {"type": "response", "body": f"Pull request ready: {pr.url}\n\n{title}"},
            )
        else:
            self._post_once(
                rec.issue_id,
                f"prlink-{rec.issue_id}-{pr.number}",
                f"🤖 Pull request ready: {pr.url}",
            )
        if newly_opened:  # re-submissions to the same PR don't need re-telling
            mailbox.put_inbox(
                "info",
                {"text": f"Your PR is open at {pr.url}. Review feedback will be forwarded to you."},
            )
        mailbox.archive_outbox(msg, receipt={"pr": pr.number, "url": pr.url})

    def _ingest_comments(self, rec: WorkerRecord, mailbox: Mailbox) -> None:
        comments = self.tracker.comments_since(rec.issue_id, rec.comment_cursor)
        # The marker filters every post we author directly. Identity is only
        # a valid filter when we authenticate AS AN APP: then viewer-authored
        # comments are the app's own — notably Linear's unmarked mirrors of
        # session activities, which fed the agent its own words as waking
        # replies (observed live 2026-07-30). With a personal key the viewer
        # IS the operator, and identity filtering would eat their replies.
        app_viewer = (
            self.tracker.get_viewer_id() if getattr(self.tracker, "app_identity", False) else None
        )
        advanced = False
        for c in comments:
            if rec.comment_cursor is None or c.created_at > rec.comment_cursor:
                rec.comment_cursor = c.created_at
                advanced = True
            if MARKER_PREFIX in c.body or (app_viewer is not None and c.author_id == app_viewer):
                continue
            mailbox.ensure().put_inbox(
                "reply", {"author": c.author_name, "text": c.body, "source": "linear"}
            )
        if advanced:
            rec.touch()
            self.registry.save()

    def _check_pr(self, rec: WorkerRecord, project: ProjectConfig, mailbox: Mailbox) -> None:
        if rec.pr_number is None:
            return
        forge = self.forges[project.name]

        new_feedback = []
        for fb in forge.pr_feedback(rec.pr_number):
            if fb.id in rec.seen_feedback_ids:
                continue
            new_feedback.append(fb)
        for fb in new_feedback:
            mailbox.ensure().put_inbox(
                "pr_feedback",
                {
                    "reviewer": fb.reviewer,
                    "kind": fb.kind,
                    "path": fb.path,
                    "text": fb.body,
                    "url": fb.url,
                },
            )
            rec.seen_feedback_ids.append(fb.id)
        if new_feedback:
            rec.seen_feedback_ids = rec.seen_feedback_ids[-_SEEN_IDS_CAP:]
            rec.touch()
            self.registry.save()

        pr = forge.get_pr(rec.pr_number)
        if pr.merged:
            log.info("worker %s: PR #%d merged; winding down", rec.issue_key, pr.number)
            self._wind_down(rec, project, mailbox, reason=f"PR #{pr.number} merged", done=True)
        elif pr.state == "closed":
            sentinel = f"closed-{pr.number}"
            if sentinel not in rec.seen_feedback_ids:
                mailbox.ensure().put_inbox(
                    "pr_closed",
                    {"text": f"PR #{pr.number} was closed without being merged. Decide how to "
                     "respond: ask a question, revise and re-submit, or post a status."},
                )
                rec.seen_feedback_ids.append(sentinel)
                rec.touch()
                self.registry.save()

    # -------------------------------------------------------------- teardown

    def _wind_down(
        self, rec: WorkerRecord, project: ProjectConfig, mailbox: Mailbox, reason: str, done: bool
    ) -> None:
        # 1. Signal the agent (it exits its loop on the next decide()), and
        # close out the agent session so its UI doesn't hang on "working".
        try:
            mailbox.ensure().put_inbox("shutdown", {"reason": reason})
        except OSError:
            pass  # worktree may already be gone
        if rec.agent_session_id:
            self._emit_activity_quietly(
                rec.agent_session_id,
                {"type": "response" if done else "error",
                 "body": f"Worker wound down: {reason}."},
            )

        # 2. Archive mailbox + transcripts somewhere durable, outside the
        # worktree — the transcript must outlive the branch.
        agent_dir = Path(rec.worktree) / ".agent"
        if agent_dir.is_dir():
            dest = self.registry.archive_dir_for(rec)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                agent_dir, dest, ignore=shutil.ignore_patterns("bin", "tmp"), dirs_exist_ok=True
            )

        # 3. Stop the container/session, remove the worktree, prune.
        self.runner.stop(rec)
        self.git.remove_worktree(Path(rec.repo), Path(rec.worktree), rec.branch)

        # 4. Tracker/forge bookkeeping (best-effort; teardown must complete).
        try:
            if done:
                self.tracker.set_state(rec.issue_id, project.state_done)
                if project.delete_remote_branch:
                    self.git.delete_remote_branch(Path(rec.repo), rec.branch)
            self._post_once(
                rec.issue_id,
                f"winddown-{rec.issue_id}",
                f"🤖 Worker wound down: {reason}. "
                + ("" if done else "Branch left in place. ")
                + "Transcript archived host-side.",
            )
        except Exception:
            log.exception("worker %s: post-teardown bookkeeping failed", rec.issue_key)

        # 5. Drop the registry entry.
        self.registry.remove(rec.issue_id)

    def _post_once(self, issue_id: str, dedupe_id: str, text: str) -> None:
        """Orchestrator-origin comment with a deterministic marker id, so a
        crash-retry can't double-post."""
        if self.tracker.has_comment_marker(issue_id, dedupe_id):
            return
        self.tracker.post_comment(issue_id, f"{text}\n\n{marker(dedupe_id)}")

    # ---------------------------------------------------------------- claims

    def claim_queue(
        self, eligible: dict[str, list[Issue]]
    ) -> tuple[list[tuple[Issue, ProjectConfig]], list[tuple[Issue, ProjectConfig]]]:
        """Split eligible unclaimed issues into (claim now, waiting), honoring
        the global cap, per-project caps, and priority-then-age order."""
        active = [w for w in self.registry.all() if w.phase == PHASE_ACTIVE]
        capacity = max(0, self.cfg.max_workers - len(active))

        queue: list[tuple[Issue, ProjectConfig]] = []
        for project in self.cfg.projects:
            taken = len([w for w in active if w.project == project.name])
            cap = project.max_workers
            candidates = [
                i for i in eligible.get(project.name, []) if self.registry.get(i.id) is None
            ]
            candidates.sort(key=lambda i: i.sort_key())
            if cap is not None:
                candidates = candidates[: max(0, cap - taken)]
            queue.extend((i, project) for i in candidates)

        queue.sort(key=lambda pair: pair[0].sort_key())
        return queue[:capacity], queue[capacity:]

    def _claim(self, eligible: dict[str, list[Issue]]) -> None:
        claim_now, _waiting = self.claim_queue(eligible)
        for issue, project in claim_now:
            try:
                self._claim_one(issue, project)
            except Exception:
                log.exception("claiming %s failed; will retry next tick", issue.key)

    def _claim_one(self, issue: Issue, project: ProjectConfig, session=None) -> None:
        branch = project.branch_template.format(key=issue.key.lower(), slug=slugify(issue.title))
        worktree = self.cfg.worktree_root / project.name / issue.key
        tmux_session = f"issuefleet-{project.name}-{issue.key}"
        log.info("claiming %s -> %s (%s)%s", issue.key, branch, worktree,
                 " [agent session]" if session else "")

        self.git.create_worktree(project.repo, branch, project.base_ref, worktree)
        self.git.add_worktree_exclude(project.repo, worktree, ".agent/")
        for rel in worker_mod.inherit_repo_files(project.repo, worktree, self.cfg.copy_from_repo):
            self.git.add_worktree_exclude(project.repo, worktree, rel)
        session_uuid = worker_mod.provision(worktree, issue, branch, project.base_ref, self.cfg)

        rec = WorkerRecord(
            issue_id=issue.id,
            issue_key=issue.key,
            issue_title=issue.title,
            issue_url=issue.url,
            project=project.name,
            repo=str(project.repo),
            branch=branch,
            worktree=str(worktree),
            base_ref=project.base_ref,
            session_uuid=session_uuid,
            tmux_session=tmux_session,
            claim_origin="session" if session else "poll",
            agent_session_id=getattr(session, "session_id", None),
        )
        # Register before the runner/tracker side effects: if we crash here,
        # the next tick's liveness check starts the session; if we crashed
        # before this line, the next tick re-runs the (idempotent) setup.
        self.registry.add(rec)
        self.runner.start(rec, self.cfg)
        self.tracker.set_state(issue.id, project.state_in_progress)
        if session:
            self._emit_activity_quietly(
                rec.agent_session_id,
                {"type": "thought",
                 "body": f"Worker claimed: branch `{branch}` in an isolated worktree. "
                 "Plan and progress will stream here."},
            )
        else:
            self._post_once(
                issue.id,
                f"claim-{issue.id}",
                f"🤖 Claimed by issuefleet. Branch `{branch}`, worktree `{worktree}`.\n"
                f"Watch live: `tmux attach -t {tmux_session}` on the orchestrator host "
                f"(or `issuefleet logs {issue.key}`).",
            )
