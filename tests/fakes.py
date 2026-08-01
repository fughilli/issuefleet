"""In-memory fakes for the tracker/forge/git/runner seams, so the whole
reconcile loop is testable with no container, no network, no credentials."""

from __future__ import annotations

from pathlib import Path

from issuefleet import MARKER_PREFIX
from issuefleet.model import Comment, Issue, PrFeedback, PullRequest


def make_issue(n=1, **kw):
    base = dict(
        id=f"issue-{n}",
        key=f"FUG-{n}",
        title=f"Fix thing {n}",
        description="Please fix it.",
        url=f"https://linear.app/x/issue/FUG-{n}",
        priority=0,
        state_name="Todo",
        state_type="unstarted",
        labels=["agent"],
        created_at=f"2026-07-{n:02d}T00:00:00+00:00",
    )
    base.update(kw)
    return Issue(**base)


class FakeTracker:
    """Linear stand-in. Test code mutates .issues / .comments directly."""

    def __init__(self):
        self.viewer_id = "viewer-bot"
        self.app_identity = False  # True mirrors the OAuth/agent-app identity
        self.issues: dict[str, Issue] = {}
        self.comments: dict[str, list[Comment]] = {}  # issue_id -> comments
        self.posted: list[tuple[str, str]] = []  # (issue_id, body)
        self.state_changes: list[tuple[str, str]] = []  # (issue_id, state_name)
        self.fail_next_post = 0  # countdown of post_comment calls to fail
        self.fail_get_issue: set[str] = set()  # issue_ids whose get_issue raises
        self.activities: list[tuple[str, dict]] = []  # (session_id, content)
        self.sessions: dict[str, str] = {}  # issue_id -> discoverable session id
        self.created: list[dict] = []  # issueCreate inputs the bot filed
        self.issue_team: dict[str, str] = {}  # issue_id -> team_id
        self.team_labels: dict[str, dict[str, str]] = {}  # team_id -> {name: id}
        self.fail_next_create = 0
        self._comment_seq = 0
        self._created_seq = 0

    def add_issue(self, issue: Issue) -> Issue:
        self.issues[issue.id] = issue
        self.comments.setdefault(issue.id, [])
        return issue

    def human_comment(self, issue_id: str, body: str, author="alice") -> Comment:
        self._comment_seq += 1
        c = Comment(
            id=f"c{self._comment_seq}",
            author_id=f"user-{author}",
            author_name=author,
            body=body,
            created_at=f"2026-07-29T00:00:{self._comment_seq:02d}+00:00",
        )
        self.comments[issue_id].append(c)
        return c

    # -- Tracker interface -------------------------------------------------

    def get_viewer_id(self) -> str:
        return self.viewer_id

    def eligible_issues(self, project) -> list[Issue]:
        open_issues = [i for i in self.issues.values() if i.open]
        if project.claim.strategy == "agent":
            # Mirror LinearTracker: delegation sets `delegate` (assignee
            # accepted too, belt and braces).
            return [
                i for i in open_issues if self.viewer_id in (i.assignee_id, i.delegate_id)
            ]
        return [i for i in open_issues if project.claim.matches(i)]

    def get_issue(self, issue_id: str) -> Issue | None:
        if issue_id in self.fail_get_issue:
            raise ConnectionError("fake Linear outage on get_issue")
        return self.issues.get(issue_id)

    def comments_since(self, issue_id: str, cursor: str | None) -> list[Comment]:
        out = []
        for c in self.comments.get(issue_id, []):
            if cursor is None or c.created_at > cursor:
                out.append(c)
        return sorted(out, key=lambda c: c.created_at)

    def post_comment(self, issue_id: str, body: str) -> None:
        if self.fail_next_post > 0:
            self.fail_next_post -= 1
            raise ConnectionError("fake Linear outage")
        self._comment_seq += 1
        self.comments.setdefault(issue_id, []).append(
            Comment(
                id=f"bot{self._comment_seq}",
                author_id=self.viewer_id,
                author_name="issuefleet",
                body=body,
                created_at=f"2026-07-29T00:00:{self._comment_seq:02d}+00:00",
            )
        )
        self.posted.append((issue_id, body))

    def has_comment_marker(self, issue_id: str, msg_id: str) -> bool:
        needle = MARKER_PREFIX + msg_id
        return any(needle in c.body for c in self.comments.get(issue_id, []))

    def set_state(self, issue_id: str, state_name: str) -> None:
        self.state_changes.append((issue_id, state_name))
        if issue_id in self.issues:
            self.issues[issue_id].state_name = state_name
            if state_name == "Done":
                self.issues[issue_id].state_type = "completed"

    def emit_activity(self, session_id: str, content: dict) -> None:
        self.activities.append((session_id, content))

    def find_agent_session(self, issue_id: str) -> str | None:
        if not self.app_identity:
            return None
        return self.sessions.get(issue_id)

    def resolve_project_id(self, project) -> str:
        return project.linear_project

    def team_for_issue(self, issue_id: str) -> str:
        return self.issue_team.get(issue_id, "team-1")

    def find_issue_by_marker(self, needle: str) -> Issue | None:
        for i in self.issues.values():
            if needle in (i.description or ""):
                return i
        return None

    def create_issue(self, *, title, description="", priority=None, labels=None,
                      team=None, project=None, use_context_project=True,
                      context_issue_id=None):
        if self.fail_next_create > 0:
            self.fail_next_create -= 1
            raise ConnectionError("fake Linear outage on create_issue")
        team_id = team or (self.team_for_issue(context_issue_id) if context_issue_id else None)
        if team_id is None:
            raise ValueError("no team for create_issue")
        known = self.team_labels.get(team_id, {})
        unknown = [n for n in (labels or []) if n.lower() not in known]
        if project is not None:
            project_id = project
        elif use_context_project and context_issue_id:
            src = self.issues.get(context_issue_id)
            project_id = src.project_id if src else None
        else:
            project_id = None
        self._created_seq += 1
        n = self._created_seq
        issue = Issue(
            id=f"new-{n}",
            key=f"FUG-{100 + n}",
            title=title,
            description=description,
            url=f"https://linear.app/x/issue/FUG-{100 + n}",
            priority=priority or 0,
            state_name="Todo",
            state_type="unstarted",
            project_id=project_id,
        )
        self.issues[issue.id] = issue
        self.issue_team[issue.id] = team_id
        self.created.append({
            "title": title, "description": description, "priority": priority,
            "labels": labels or [], "team": team_id, "project_id": project_id,
        })
        return issue, unknown


class FakeForge:
    """GitHub stand-in."""

    def __init__(self):
        self.prs: dict[int, PullRequest] = {}
        self.feedback: dict[int, list[PrFeedback]] = {}
        self.opened: list[dict] = []
        self.updated: list[dict] = []
        self.closed: list[int] = []
        self.fail_next_open = 0
        self._next = 100

    def merge(self, number: int) -> None:
        self.prs[number].state = "closed"
        self.prs[number].merged = True

    def close(self, number: int) -> None:  # test helper: simulate a human close
        self.prs[number].state = "closed"

    def close_pr(self, number: int) -> None:  # Forge port: the daemon closes it
        self.prs[number].state = "closed"
        self.closed.append(number)

    def add_feedback(self, number: int, body: str, kind="comment", reviewer="alice", path=None):
        fid = f"f{len(self.feedback.setdefault(number, [])) + 1}-{number}"
        self.feedback[number].append(
            PrFeedback(id=fid, kind=kind, reviewer=reviewer, body=body, path=path)
        )

    # -- Forge interface ---------------------------------------------------

    def find_pr(self, head_branch: str) -> PullRequest | None:
        for pr in self.prs.values():
            if pr.head == head_branch and pr.state == "open":
                return pr
        return None

    def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        if self.fail_next_open > 0:
            self.fail_next_open -= 1
            raise ConnectionError("fake GitHub outage")
        self._next += 1
        pr = PullRequest(
            number=self._next,
            url=f"https://github.example/pr/{self._next}",
            state="open",
            merged=False,
            head=head,
            base=base,
        )
        self.prs[pr.number] = pr
        self.opened.append({"number": pr.number, "head": head, "title": title, "body": body})
        return pr

    def update_pr(self, number: int, title: str, body: str) -> None:
        self.updated.append({"number": number, "title": title, "body": body})

    def get_pr(self, number: int) -> PullRequest:
        return self.prs[number]

    def pr_feedback(self, number: int) -> list[PrFeedback]:
        return list(self.feedback.get(number, []))

    def repo_accessible(self) -> bool:
        return True

    def push_spec(self):
        return ("https://github.example/o/r.git", "basic fake-token")


class FakeGit:
    """Worktree/branch/push stand-in. Records actions; creates real dirs so
    mailbox code has somewhere to write."""

    def __init__(self, tmp_root: Path):
        self.tmp_root = Path(tmp_root)
        self.pushed: list[str] = []
        self.removed: list[str] = []
        self.deleted_remote: list[str] = []
        self.ahead = True  # what has_commits_ahead reports
        self.push_specs: list[tuple] = []  # (url, auth_header) per push
        self.excludes: list[tuple[str, str]] = []  # (worktree, pattern)
        self.fail_next_push = 0
        self.fetched: list[tuple] = []  # (repo, url, auth_header) per fetch
        self.fail_next_fetch = 0

    def fetch(self, repo: Path, url=None, auth_header=None) -> None:
        if self.fail_next_fetch > 0:
            self.fail_next_fetch -= 1
            from issuefleet.gitops import GitError

            raise GitError("fake git fetch failure")
        self.fetched.append((str(repo), url, auth_header))

    def create_worktree(self, repo: Path, branch: str, base_ref: str, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def add_worktree_exclude(self, repo: Path, path: Path, pattern: str) -> None:
        self.excludes.append((str(path), pattern))

    def remove_worktree(self, repo: Path, path: Path, branch: str) -> None:
        self.removed.append(str(path))

    def delete_remote_branch(self, repo: Path, branch: str, url=None, auth_header=None) -> None:
        self.deleted_remote.append(branch)

    def has_commits_ahead(self, worktree: Path, base_ref: str) -> bool:
        return self.ahead

    def push(self, worktree: Path, branch: str, url=None, auth_header=None) -> None:
        if self.fail_next_push > 0:
            self.fail_next_push -= 1
            raise ConnectionError("fake git push failure")
        self.pushed.append(branch)
        self.push_specs.append((url, auth_header))


class FakeRunner:
    """tmux/container stand-in."""

    def __init__(self):
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.dead: set[str] = set()  # tmux_session names reported not-alive

    def start(self, rec, config) -> None:
        self.started.append(rec.tmux_session)
        self.dead.discard(rec.tmux_session)

    def alive(self, rec) -> bool:
        return rec.tmux_session in self.started and rec.tmux_session not in self.dead

    def stop(self, rec) -> None:
        self.stopped.append(rec.tmux_session)
        self.dead.add(rec.tmux_session)
