"""Plain data types shared across the tracker/forge/runner seams."""

from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass, field
from typing import Any


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Issue:
    """A tracker issue, tracker-agnostic."""

    id: str  # opaque tracker id (Linear UUID)
    key: str  # human identifier, e.g. "FUG-12"
    title: str
    description: str
    url: str
    priority: int  # Linear: 0=none, 1=urgent .. 4=low
    state_name: str
    state_type: str  # Linear workflow state type: triage/backlog/unstarted/started/completed/canceled
    labels: list[str] = field(default_factory=list)
    assignee_id: str | None = None
    delegate_id: str | None = None  # Linear agents: delegation sets this, not assignee
    created_at: str = ""
    project_id: str | None = None  # Linear project UUID (session-claim routing)

    @property
    def open(self) -> bool:
        return self.state_type not in ("completed", "canceled")

    def sort_key(self) -> tuple:
        # Priority 0 means "no priority" in Linear; queue it after Low.
        return (self.priority if self.priority > 0 else 5, self.created_at)


@dataclass
class Comment:
    id: str
    author_id: str
    author_name: str
    body: str
    created_at: str


@dataclass
class PullRequest:
    number: int
    url: str
    state: str  # "open" | "closed"
    merged: bool
    head: str
    base: str
    head_sha: str = ""  # head commit SHA — the ref CI results attach to
    # GitHub computes these asynchronously and only returns them on the
    # single-PR GET (never the list endpoint), so both are None until a
    # verdict lands: `mergeable` is False and `mergeable_state` == "dirty"
    # exactly when the branch conflicts with its base.
    mergeable: bool | None = None
    mergeable_state: str | None = None


@dataclass
class CiCheck:
    """One CI signal on a commit: a Checks-API check run or a legacy commit
    status, normalized to a common shape."""

    name: str
    passed: bool
    url: str | None = None


@dataclass
class CiStatus:
    """The aggregate CI verdict for a commit, folded from the check-runs and
    the combined commit-status endpoints.

    ``settled`` is False while anything is still queued/in_progress/pending —
    the daemon holds off notifying until the run has actually finished, so the
    agent hears one terminal result per commit rather than a stream of
    intermediate states. ``total`` is 0 when no CI is configured on the repo
    (nothing to report). ``state`` is "success" or "failure" once settled."""

    sha: str
    settled: bool
    state: str  # "success" | "failure" | "pending" | "none"
    total: int = 0
    failing: list[CiCheck] = field(default_factory=list)


@dataclass
class PrFeedback:
    """One piece of inbound PR feedback (issue comment, review body, or inline
    review comment), normalized with enough context to act on."""

    id: str
    kind: str  # "comment" | "review" | "review_comment"
    reviewer: str
    body: str
    path: str | None = None  # file path, for inline comments
    url: str | None = None


# Registry-side worker phases. The agent-side turn phase (running/waiting/
# ready) lives in the worktree's .agent/state.json; the registry only tracks
# what the orchestrator must survive a restart knowing.
PHASE_ACTIVE = "active"  # session should be running; restart it if dead
PHASE_CRASHED = "crashed"  # gave up restarting; worktree kept for inspection


@dataclass
class WorkerRecord:
    issue_id: str
    issue_key: str
    issue_title: str
    issue_url: str
    project: str  # config [[projects]] name
    repo: str  # absolute path of the main checkout
    branch: str
    worktree: str  # absolute path
    base_ref: str
    session_uuid: str  # Claude Code session id, pinned at creation
    tmux_session: str
    phase: str = PHASE_ACTIVE
    claim_origin: str = "poll"  # "poll" (label/assignee/state rule) | "session"
    agent_session_id: str | None = None  # Linear agent session, if any
    session_lookup_attempts: int = 0  # poll-side session discovery tries (bounded)
    pr_number: int | None = None
    pr_url: str | None = None
    # True once we've told the agent its PR conflicts; re-armed to False when
    # the PR reads mergeable again, so each conflict episode notifies once.
    conflict_notified: bool = False
    restarts: int = 0
    comment_cursor: str | None = None  # ISO timestamp of newest ingested Linear comment
    seen_feedback_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def touch(self) -> None:
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkerRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
