"""GitHub REST v3 Forge implementation (app installation token or PAT).

The forge also supplies the credentials for git-over-HTTPS pushes and
clones (push_spec): branches go out with the same scoped token that opens
PRs — never an SSH key, which would carry the operator's full push rights.
With branch protection on the base ref, the bot is PR-only by construction.
"""

from __future__ import annotations

import base64
import logging
import re

from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.model import CiCheck, CiStatus, PrFeedback, PullRequest

log = logging.getLogger("issuefleet.github")

API_ROOT = "https://api.github.com"

# Check-run conclusions that count as a failure worth surfacing. neutral,
# skipped, and stale are benign; cancelled is usually a human/superseded stop,
# not a code failure, so it's left out to avoid false alarms.
_FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)

_SSH_RE = re.compile(r"^(?:ssh://)?git@[^:/]+[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$")
_HTTPS_RE = re.compile(r"^https?://[^/]+/(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$")


def parse_repo_slug(remote_url: str) -> str:
    """owner/name from an SSH or HTTPS remote URL."""
    remote_url = remote_url.strip()
    for rx in (_SSH_RE, _HTTPS_RE):
        m = rx.match(remote_url)
        if m:
            return m.group("slug")
    raise ValueError(f"cannot parse owner/name from remote url {remote_url!r}")


def _to_pr(d: dict) -> PullRequest:
    return PullRequest(
        number=d["number"],
        url=d["html_url"],
        state=d["state"],
        merged=bool(d.get("merged")) or d.get("merged_at") is not None,
        head=d["head"]["ref"],
        base=d["base"]["ref"],
        head_sha=d["head"].get("sha", ""),
        # Present only on the single-PR GET; absent (-> None) from the list
        # endpoint find_pr() uses, which is fine — conflict detection reads
        # the PR fetched by get_pr().
        mergeable=d.get("mergeable"),
        mergeable_state=d.get("mergeable_state"),
        merge_commit_sha=d.get("merge_commit_sha") or "",
    )


class GithubForge:
    def __init__(self, token, slug: str, transport=urllib_transport):
        """token: a PAT string, or a zero-arg callable returning a current
        token (GitHub App installation tokens expire hourly)."""
        self.token = token
        self.slug = slug  # "owner/name"
        self.owner = slug.split("/")[0]
        self.transport = transport

    def _current_token(self) -> str:
        return self.token() if callable(self.token) else self.token

    def push_spec(self) -> tuple[str, str]:
        """(url, authorization-header-value) for git push/clone over HTTPS
        with this forge's token. Passed to git as http.extraheader rather
        than embedded in the URL, so the token can't leak into error
        messages or process listings via the remote URL."""
        basic = base64.b64encode(f"x-access-token:{self._current_token()}".encode()).decode()
        return (f"https://github.com/{self.slug}.git", f"basic {basic}")

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        return self.transport(
            method,
            f"{API_ROOT}{path}",
            {
                "Authorization": f"Bearer {self._current_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            payload,
        )

    # -- Forge port --------------------------------------------------------

    def find_pr(self, head_branch: str) -> PullRequest | None:
        prs = self._call(
            "GET", f"/repos/{self.slug}/pulls?state=open&head={self.owner}:{head_branch}"
        )
        return _to_pr(prs[0]) if prs else None

    def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        return _to_pr(
            self._call(
                "POST",
                f"/repos/{self.slug}/pulls",
                {"title": title, "body": body, "head": head, "base": base},
            )
        )

    def update_pr(self, number: int, title: str, body: str) -> None:
        self._call("PATCH", f"/repos/{self.slug}/pulls/{number}", {"title": title, "body": body})

    def close_pr(self, number: int) -> None:
        self._call("PATCH", f"/repos/{self.slug}/pulls/{number}", {"state": "closed"})

    def get_pr(self, number: int) -> PullRequest:
        return _to_pr(self._call("GET", f"/repos/{self.slug}/pulls/{number}"))

    def pr_feedback(self, number: int) -> list[PrFeedback]:
        """Issue comments + review bodies + inline review comments, with
        stable prefixed ids so the caller's dedupe never collides across the
        three endpoints."""
        out: list[PrFeedback] = []
        for c in self._call("GET", f"/repos/{self.slug}/issues/{number}/comments"):
            out.append(
                PrFeedback(
                    id=f"ic-{c['id']}",
                    kind="comment",
                    reviewer=c["user"]["login"],
                    body=c.get("body") or "",
                    url=c.get("html_url"),
                )
            )
        for r in self._call("GET", f"/repos/{self.slug}/pulls/{number}/reviews"):
            if not (r.get("body") or "").strip():
                continue  # approval clicks with no text aren't actionable
            out.append(
                PrFeedback(
                    id=f"rv-{r['id']}",
                    kind="review",
                    reviewer=r["user"]["login"],
                    body=f"[{r.get('state', 'COMMENTED')}] {r['body']}",
                    url=r.get("html_url"),
                )
            )
        for c in self._call("GET", f"/repos/{self.slug}/pulls/{number}/comments"):
            out.append(
                PrFeedback(
                    id=f"rc-{c['id']}",
                    kind="review_comment",
                    reviewer=c["user"]["login"],
                    body=c.get("body") or "",
                    path=c.get("path"),
                    url=c.get("html_url"),
                )
            )
        return out

    def ci_status(self, ref: str) -> CiStatus:
        """Fold the check-runs API and the combined commit-status API for
        ``ref`` into one verdict.

        GitHub carries CI on two independent surfaces — the modern Checks API
        (``check_runs``) and legacy commit statuses (``statuses``) — and a repo
        can use either or both, so both are consulted and merged. A run is
        ``settled`` only when nothing is still queued/in_progress (check runs)
        or pending (statuses); until then the caller shouldn't notify. A
        check run counts as failing on any non-passing *conclusion* except the
        benign neutral/skipped/stale/cancelled ones; a status fails on
        failure/error. With no checks or statuses at all, ``total`` is 0 and
        ``state`` is "none"."""
        failing: list[CiCheck] = []
        total = 0
        pending = False

        runs = self._call("GET", f"/repos/{self.slug}/commits/{ref}/check-runs")
        for r in runs.get("check_runs", []):
            total += 1
            if r.get("status") != "completed":
                pending = True
                continue
            if r.get("conclusion") in _FAILING_CONCLUSIONS:
                failing.append(
                    CiCheck(
                        name=r.get("name") or "check",
                        passed=False,
                        url=r.get("html_url") or r.get("details_url"),
                    )
                )

        combined = self._call("GET", f"/repos/{self.slug}/commits/{ref}/status")
        for s in combined.get("statuses", []):
            total += 1
            st = s.get("state")
            if st == "pending":
                pending = True
                continue
            if st in ("failure", "error"):
                failing.append(
                    CiCheck(
                        name=s.get("context") or "status",
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
            return self._call("GET", "/user")["login"]
        except ApiError as e:
            # Fine-grained PATs without user scope can still use the repo;
            # fall back to a repo permission probe.
            if e.status in (401, 403):
                raise
            return "(unknown)"

    def repo_accessible(self) -> bool:
        self._call("GET", f"/repos/{self.slug}")
        return True
