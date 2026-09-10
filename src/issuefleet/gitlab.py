"""GitLab REST v4 Forge implementation (personal / group / project access token).

The GitLab analog of github.GithubForge. Merge requests stand in for pull
requests, MR notes for review feedback, and commit statuses (the pipeline +
external-status aggregate) for CI. Like the GitHub forge, it also supplies the
credentials for git-over-HTTPS pushes and clones (push_spec): branches go out
with the same scoped token that opens MRs — never an SSH key. With the base
branch protected, the bot is MR-only by construction.

GitLab is routinely self-hosted, so the instance ``host`` is carried through:
the API base is ``https://<host>/api/v4`` and the project is addressed by its
URL-encoded ``group/subgroup/project`` path, which also covers nested groups.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse

from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.model import CiCheck, CiStatus, PrFeedback, PullRequest

log = logging.getLogger("issuefleet.gitlab")

# Commit-status / job states that count as still-running (verdict not settled).
_PENDING_STATES = frozenset(
    {"pending", "running", "created", "waiting_for_resource", "preparing", "scheduled"}
)
# States that count as a failure worth surfacing. success/skipped are green or
# benign; canceled/manual are human/superseded stops, not code failures, so
# they're left out to avoid false alarms (mirrors the GitHub forge's choices).
_FAILING_STATES = frozenset({"failed"})


def _to_pr(d: dict) -> PullRequest:
    """Normalize a GitLab merge request into the tracker-agnostic PullRequest.

    ``number`` is the MR ``iid`` (the per-project id in URLs and the UI), not
    the global ``id``. Conflict signalling is folded into the same fields the
    reconcile loop already reads for GitHub: a merge request with conflicts
    reports ``mergeable=False`` / ``mergeable_state="dirty"`` so the existing
    rebase-nudge path fires unchanged; a cleanly-mergeable one reports True; and
    anything GitLab is still computing (or is blocked for a non-conflict reason)
    stays None, so the agent isn't nagged to rebase over a pending pipeline.

    Only ``has_conflicts`` decides "dirty". For a few seconds after a push
    GitLab can answer ``detailed_merge_status="conflict"`` for the *previous*
    head while ``has_conflicts`` is still false; acting on that field alone
    sends the agent to rebase onto a base that never moved."""
    has_conflicts = bool(d.get("has_conflicts"))
    merge_status = d.get("detailed_merge_status") or d.get("merge_status")
    if has_conflicts:
        mergeable, mergeable_state = False, "dirty"
    elif merge_status in ("mergeable", "can_be_merged"):
        mergeable, mergeable_state = True, "clean"
    else:
        mergeable, mergeable_state = None, merge_status
    state = d.get("state", "")  # opened | closed | merged | locked
    return PullRequest(
        number=d["iid"],
        url=d.get("web_url", ""),
        # Map GitLab's four states onto the port's open/closed vocabulary; the
        # merged flag is carried separately.
        state="open" if state == "opened" else "closed",
        merged=state == "merged" or d.get("merged_at") is not None,
        head=d.get("source_branch", ""),
        base=d.get("target_branch", ""),
        head_sha=d.get("sha", "") or "",
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        merge_commit_sha=d.get("merge_commit_sha") or d.get("squash_commit_sha") or "",
    )


class GitlabForge:
    def __init__(self, token, slug: str, host: str = "gitlab.com", transport=urllib_transport):
        """token: a PAT string (personal, group, or project access token), or a
        zero-arg callable returning a current token. slug: the project path
        ``group/name`` (possibly nested). host: the instance hostname."""
        self.token = token
        self.slug = slug  # "group/name" (possibly nested)
        self.host = host
        self.transport = transport
        self.api_root = f"https://{host}/api/v4"
        # GitLab addresses a project by its URL-encoded full path.
        self.project_id = urllib.parse.quote(slug, safe="")

    def _current_token(self) -> str:
        return self.token() if callable(self.token) else self.token

    def push_spec(self) -> tuple[str, str]:
        """(url, authorization-header-value) for git push/clone over HTTPS with
        this forge's token. Passed to git as http.extraheader rather than
        embedded in the URL, so the token can't leak into error messages or
        process listings. GitLab accepts an access token as the HTTP basic
        password with any non-empty username; ``oauth2`` is the conventional
        one."""
        basic = base64.b64encode(f"oauth2:{self._current_token()}".encode()).decode()
        return (f"https://{self.host}/{self.slug}.git", f"basic {basic}")

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        return self.transport(
            method,
            f"{self.api_root}{path}",
            {
                # A PAT authenticates via PRIVATE-TOKEN; this header also accepts
                # group/project access tokens (all three are opaque bearer-ish
                # strings to the API).
                "PRIVATE-TOKEN": self._current_token(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            payload,
        )

    def _mr(self, path: str = "") -> str:
        return f"/projects/{self.project_id}/merge_requests{path}"

    # -- Forge port --------------------------------------------------------

    def find_pr(self, head_branch: str) -> PullRequest | None:
        mrs = self._call(
            "GET",
            self._mr(f"?state=opened&source_branch={urllib.parse.quote(head_branch, safe='')}"),
        )
        return _to_pr(mrs[0]) if mrs else None

    def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        return _to_pr(
            self._call(
                "POST",
                self._mr(),
                {
                    "source_branch": head,
                    "target_branch": base,
                    "title": title,
                    "description": body,
                },
            )
        )

    def update_pr(self, number: int, title: str, body: str) -> None:
        self._call("PUT", self._mr(f"/{number}"), {"title": title, "description": body})

    def close_pr(self, number: int) -> None:
        self._call("PUT", self._mr(f"/{number}"), {"state_event": "close"})

    def get_pr(self, number: int) -> PullRequest:
        return _to_pr(self._call("GET", self._mr(f"/{number}")))

    def pr_feedback(self, number: int) -> list[PrFeedback]:
        """Human notes on the merge request, normalized. GitLab folds both
        discussion comments and inline diff comments into the notes endpoint;
        ``DiffNote`` entries carry a file ``position`` (mapped to review_comment
        with a path), the rest are plain comments. System notes (label changes,
        pipeline events, …) are dropped — they aren't feedback to act on."""
        out: list[PrFeedback] = []
        for n in self._call("GET", self._mr(f"/{number}/notes?sort=asc&order_by=created_at")):
            if n.get("system"):
                continue
            author = (n.get("author") or {}).get("username", "?")
            if n.get("type") == "DiffNote":
                path = (n.get("position") or {}).get("new_path") or (
                    n.get("position") or {}
                ).get("old_path")
                out.append(
                    PrFeedback(
                        id=f"dn-{n['id']}",
                        kind="review_comment",
                        reviewer=author,
                        body=n.get("body") or "",
                        path=path,
                    )
                )
            else:
                out.append(
                    PrFeedback(
                        id=f"nt-{n['id']}",
                        kind="comment",
                        reviewer=author,
                        body=n.get("body") or "",
                    )
                )
        return out

    def ci_status(self, ref: str) -> CiStatus:
        """Fold the commit-statuses endpoint for ``ref`` into one verdict. It
        aggregates pipeline jobs and external commit statuses — GitLab's analog
        of the GitHub check-runs + commit-status surfaces — so a repo using
        either or both is covered. A status is still running while its state is
        pending/running/created; a verdict is ``settled`` only when none are.
        A status fails on ``failed``; success/skipped are green/benign and
        canceled/manual are deliberately not counted as failures. With no
        statuses at all, ``total`` is 0 and ``state`` is "none"."""
        failing: list[CiCheck] = []
        total = 0
        pending = False

        statuses = self._call(
            "GET", f"/projects/{self.project_id}/repository/commits/{ref}/statuses"
        )
        for s in statuses:
            total += 1
            st = s.get("status")
            if st in _PENDING_STATES:
                pending = True
                continue
            if st in _FAILING_STATES:
                failing.append(
                    CiCheck(
                        name=s.get("name") or "status",
                        passed=False,
                        url=s.get("target_url"),
                    )
                )

        if total == 0:
            return CiStatus(sha=ref, settled=True, state="none", total=0)
        if pending:
            return CiStatus(sha=ref, settled=False, state="pending", total=total)
        state = "failure" if failing else "success"
        return CiStatus(sha=ref, settled=True, state=state, total=total, failing=failing)

    # -- doctor support ----------------------------------------------------

    def whoami(self) -> str:
        try:
            return self._call("GET", "/user")["username"]
        except ApiError as e:
            if e.status in (401, 403):
                raise
            return "(unknown)"

    def repo_accessible(self) -> bool:
        self._call("GET", f"/projects/{self.project_id}")
        return True
