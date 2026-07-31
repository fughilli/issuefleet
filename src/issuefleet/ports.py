"""Narrow interfaces (ports) between the reconcile loop and the world.

Real implementations: linear.LinearTracker, github.GithubForge, gitops.Git,
runner.TmuxRunner. Tests substitute in-memory fakes. Keeping these narrow is
deliberate — GitLab/Jira *could* slot in behind them, and that is as far as
pluggability goes (brief §8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from issuefleet.model import Comment, Issue, PrFeedback, PullRequest, WorkerRecord


class Tracker(Protocol):
    def get_viewer_id(self) -> str: ...

    def eligible_issues(self, project) -> list[Issue]:
        """Open issues in the project matching the claim rule."""
        ...

    def get_issue(self, issue_id: str) -> Issue | None: ...

    def comments_since(self, issue_id: str, cursor: str | None) -> list[Comment]:
        """All comments strictly newer than the cursor (ISO timestamp),
        oldest first, including our own (the caller filters and advances)."""
        ...

    def post_comment(self, issue_id: str, body: str) -> None: ...

    def has_comment_marker(self, issue_id: str, msg_id: str) -> bool: ...

    def set_state(self, issue_id: str, state_name: str) -> None: ...

    def emit_activity(self, session_id: str, content: dict) -> None:
        """Linear agents platform: emit a typed activity (thought / action /
        elicitation / response / error) into an agent session."""
        ...

    def find_agent_session(self, issue_id: str) -> str | None:
        """Linear agents platform: id of this app's most-recent still-open
        agent session on an issue, so a poll-claimed worker (missed webhook)
        can bind its session. None when there is none / not the app identity."""
        ...

    def resolve_project_id(self, project) -> str:
        """Tracker-native project id for a configured project (used to route
        agent-session claims to the right [[projects]] entry)."""
        ...


class Forge(Protocol):
    def find_pr(self, head_branch: str) -> PullRequest | None: ...

    def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest: ...

    def update_pr(self, number: int, title: str, body: str) -> None: ...

    def get_pr(self, number: int) -> PullRequest: ...

    def pr_feedback(self, number: int) -> list[PrFeedback]:
        """Issue comments, review bodies, and inline review comments,
        normalized. Caller dedupes by id."""
        ...

    def push_spec(self) -> tuple[str, str]:
        """(https url, authorization header value) for git push/clone with
        this forge's scoped token."""
        ...


class Git(Protocol):
    def create_worktree(self, repo: Path, branch: str, base_ref: str, path: Path) -> None:
        """Idempotent: adopt an existing worktree/branch rather than failing."""
        ...

    def add_worktree_exclude(self, repo: Path, path: Path, pattern: str) -> None:
        """Ignore a pattern via the per-worktree info/exclude — never the
        repo's own .gitignore."""
        ...

    def has_commits_ahead(self, worktree: Path, base_ref: str) -> bool: ...

    def push(self, worktree: Path, branch: str, url=None, auth_header=None) -> None:
        """Push with --force-with-lease (re-submissions may rebase), to the
        forge's HTTPS URL with its scoped token — never the operator's SSH
        key."""
        ...

    def remove_worktree(self, repo: Path, path: Path, branch: str) -> None: ...

    def delete_remote_branch(self, repo: Path, branch: str, url=None, auth_header=None) -> None: ...


class Runner(Protocol):
    def start(self, rec: WorkerRecord, config) -> None:
        """Idempotent: a live session for this worker is left alone."""
        ...

    def alive(self, rec: WorkerRecord) -> bool: ...

    def stop(self, rec: WorkerRecord) -> None: ...
