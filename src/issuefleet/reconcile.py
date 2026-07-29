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

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
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
            except Exception:
                log.exception("polling %s failed; skipping claims for it this tick", project.name)
                eligible[project.name] = []

        try:
            self._claim(eligible)
        except Exception:
            log.exception("claim pass failed; will retry next tick")

    # ------------------------------------------------------------- servicing

    def _service(self, rec: WorkerRecord) -> None:
        project = self.cfg.project(rec.project)
        mailbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")

        issue = self.tracker.get_issue(rec.issue_id)
        reason = self._unclaim_reason(project, issue)
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

    def _unclaim_reason(self, project: ProjectConfig, issue: Issue | None) -> str | None:
        if issue is None:
            return "issue disappeared from the tracker"
        if not issue.open:
            return f"issue was closed ({issue.state_name})"
        claim = project.claim
        # For the `state` strategy, claiming itself moves the issue out of the
        # claim state, so only closure un-claims (checked above).
        if claim.strategy == "label" and claim.value not in issue.labels:
            return f"label {claim.value!r} was removed"
        if claim.strategy == "assignee" and issue.assignee_id != claim.value:
            return "assignee changed"
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
                    self.tracker.post_comment(
                        rec.issue_id, f"🤖 {msg.payload['text']}\n\n{marker(msg.id)}"
                    )
                    mailbox.archive_outbox(msg, receipt={"relayed": "linear"})
                elif msg.kind == "question":
                    self.tracker.post_comment(
                        rec.issue_id,
                        "🤖❓ **The agent is blocked on a question** — it will idle "
                        f"until someone replies on this issue:\n\n{msg.payload['text']}"
                        f"\n\n{marker(msg.id)}",
                    )
                    mailbox.archive_outbox(msg, receipt={"relayed": "linear"})
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

        rec.pr_number, rec.pr_url = pr.number, pr.url
        rec.touch()
        self.registry.save()
        self._post_once(
            rec.issue_id,
            f"prlink-{rec.issue_id}-{pr.number}",
            f"🤖 Pull request ready: {pr.url}",
        )
        mailbox.put_inbox("info", {"text": f"Your PR is open at {pr.url}. Review feedback will be forwarded to you."})
        mailbox.archive_outbox(msg, receipt={"pr": pr.number, "url": pr.url})

    def _ingest_comments(self, rec: WorkerRecord, mailbox: Mailbox) -> None:
        viewer = self.tracker.get_viewer_id()
        comments = self.tracker.comments_since(rec.issue_id, rec.comment_cursor)
        advanced = False
        for c in comments:
            if rec.comment_cursor is None or c.created_at > rec.comment_cursor:
                rec.comment_cursor = c.created_at
                advanced = True
            if c.author_id == viewer or MARKER_PREFIX in c.body:
                continue  # belt and braces: never re-ingest our own posts
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
        # 1. Signal the agent (it exits its loop on the next decide()).
        try:
            mailbox.ensure().put_inbox("shutdown", {"reason": reason})
        except OSError:
            pass  # worktree may already be gone

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

    def _claim(self, eligible: dict[str, list[Issue]]) -> None:
        active = [w for w in self.registry.all() if w.phase == PHASE_ACTIVE]
        capacity = self.cfg.max_workers - len(active)
        if capacity <= 0:
            return

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
        for issue, project in queue[:capacity]:
            try:
                self._claim_one(issue, project)
            except Exception:
                log.exception("claiming %s failed; will retry next tick", issue.key)

    def _claim_one(self, issue: Issue, project: ProjectConfig) -> None:
        branch = project.branch_template.format(key=issue.key.lower(), slug=slugify(issue.title))
        worktree = self.cfg.worktree_root / project.name / issue.key
        tmux_session = f"issuefleet-{project.name}-{issue.key}"
        log.info("claiming %s -> %s (%s)", issue.key, branch, worktree)

        self.git.create_worktree(project.repo, branch, project.base_ref, worktree)
        self.git.add_worktree_exclude(project.repo, worktree, ".agent/")
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
        )
        # Register before the runner/tracker side effects: if we crash here,
        # the next tick's liveness check starts the session; if we crashed
        # before this line, the next tick re-runs the (idempotent) setup.
        self.registry.add(rec)
        self.runner.start(rec, self.cfg)
        self.tracker.set_state(issue.id, project.state_in_progress)
        self._post_once(
            issue.id,
            f"claim-{issue.id}",
            f"🤖 Claimed by issuefleet. Branch `{branch}`, worktree `{worktree}`.\n"
            f"Watch live: `tmux attach -t {tmux_session}` on the orchestrator host "
            f"(or `issuefleet logs {issue.key}`).",
        )
