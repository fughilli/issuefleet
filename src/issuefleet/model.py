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
    # The commit the merge produced on the base branch (squash/rebase/merge
    # all set it). This is the canonical mainline SHA a dependent worker pins
    # to once an upstream PR lands — empty until the PR is actually merged.
    merge_commit_sha: str = ""


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


def new_upstream_link(
    project: str, branch: str, path: str, base_ref: str, base_sha: str = ""
) -> dict[str, Any]:
    """One cross-project contribution a worker is making: a self-contained
    local clone of a *sibling* fleet project, nested inside this worker's
    worktree at ``path`` (relative), on ``branch`` cut from ``base_ref``.

    Kept as a plain dict rather than a dataclass so it round-trips through the
    registry JSON with no custom (de)serialization — WorkerRecord.from_dict is
    generic and would otherwise hand back bare dicts after a reload, silently
    breaking attribute access. The keys are the whole contract:

    - ``pr_number`` / ``pr_url`` / ``head_sha``: set once the change is pushed
      and a PR opened on the sibling repo (``head_sha`` is the pushed tip, the
      CI-testable SHA the agent can pin experimentally);
    - ``merged`` / ``merge_sha``: set when that PR lands, ``merge_sha`` being
      the canonical mainline commit to repoint the pin at before the worker's
      own PR merges;
    - ``merge_notified`` / ``closed_notified``: once-only wake latches, so a
      merged/closed upstream PR wakes the dependent worker exactly once.
    """
    return {
        "project": project,
        "branch": branch,
        "path": path,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "pr_number": None,
        "pr_url": None,
        "head_sha": None,
        "merged": False,
        "merge_sha": None,
        "merge_notified": False,
        "closed_notified": False,
    }


# Registry-side worker phases. The agent-side turn phase (running/waiting/
# ready) lives in the worktree's .agent/state.json; the registry only tracks
# what the orchestrator must survive a restart knowing.
PHASE_ACTIVE = "active"  # session should be running; restart it if dead
PHASE_CRASHED = "crashed"  # gave up restarting; worktree kept for inspection
# Operator took the branch to work on it locally: the container is stopped and
# the worktree removed (so the branch is free to check out), but the claim is
# held. The daemon does nothing with a released worker until it is adopted back.
PHASE_RELEASED = "released"


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
    claim_origin: str = "poll"  # "poll" (label/assignee/state rule) | "session" | "adopt"
    # Set while phase == released: when the branch was handed to the operator,
    # and the agent's turn count at that moment. The turn count is restored on
    # adopt so the resumed session uses `claude --resume` (its conversation
    # survives the release) rather than trying to re-create its own session id.
    released_at: str | None = None
    released_turns: int = 0
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
    # Cross-project contributions this worker has staged (see new_upstream_link).
    # Plain dicts so they survive the generic registry round-trip untouched.
    upstream_links: list[dict[str, Any]] = field(default_factory=list)
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
