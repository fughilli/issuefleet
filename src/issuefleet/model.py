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
    created_at: str = ""

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
    pr_number: int | None = None
    pr_url: str | None = None
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
