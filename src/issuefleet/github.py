"""GitHub REST v3 Forge implementation (fine-grained PAT over urllib).

Pushing is NOT done here — branches go out over the repo's existing SSH
remote (gitops.py); the token only opens/reads/updates PRs.
"""

from __future__ import annotations

import logging
import re

from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.model import PrFeedback, PullRequest

log = logging.getLogger("issuefleet.github")

API_ROOT = "https://api.github.com"

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
    )


class GithubForge:
    def __init__(self, token: str, slug: str, transport=urllib_transport):
        self.token = token
        self.slug = slug  # "owner/name"
        self.owner = slug.split("/")[0]
        self.transport = transport

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        return self.transport(
            method,
            f"{API_ROOT}{path}",
            {
                "Authorization": f"Bearer {self.token}",
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
