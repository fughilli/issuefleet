"""Atlassian Jira REST client + Tracker implementation.

The second issue-source behind the ``Tracker`` port (ports.py), parallel to
``linear.LinearTracker``. Targets the Jira Cloud REST API v3 (also works
against Jira Server/Data Center with a Personal Access Token — set
``jira_auth = "bearer"``).

Two shapes differ from Linear and drive most of the code here:

- **Rich text is ADF.** Issue descriptions and comment bodies are Atlassian
  Document Format (a JSON tree), not markdown/plain text. ``adf_to_text``
  flattens it on the way in and ``text_to_adf`` wraps our plain comments on the
  way out — including the dedupe marker, which round-trips as literal text.
- **State changes are transitions, not assignments.** You can't set a status
  directly; you POST a *transition* whose target status is the one you want.
  ``set_state`` looks the transition up by target-status name.
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import urlencode

from issuefleet import MARKER_PREFIX
from issuefleet.config import ProjectConfig
from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.model import Comment, Issue

log = logging.getLogger("issuefleet.jira")

API_BASE = "/rest/api/3"

# Issue fields we ask for — everything the Issue model needs, nothing more.
_ISSUE_FIELDS = [
    "summary",
    "description",
    "status",
    "priority",
    "labels",
    "assignee",
    "project",
    "created",
]

# Jira priority name -> Linear-style rank (lower = more urgent; 0 = none, which
# Issue.sort_key queues last). The default Jira scheme is Highest..Lowest; an
# unknown or absent priority falls back to 0.
_PRIORITY_RANK = {
    "highest": 1,
    "blocker": 1,
    "critical": 1,
    "high": 2,
    "major": 2,
    "medium": 3,
    "normal": 3,
    "low": 4,
    "minor": 4,
    "lowest": 5,
    "trivial": 5,
}

# Jira statusCategory key -> the state_type vocabulary the model/reconcile use.
# Issue.open treats "completed"/"canceled" as closed; every Jira "done"-category
# status (Done, Won't Do, …) maps to completed, so closed issues drop out.
_STATE_TYPE = {
    "new": "unstarted",
    "indeterminate": "started",
    "done": "completed",
}


class JiraError(Exception):
    pass


def adf_to_text(doc) -> str:
    """Flatten an Atlassian Document Format tree to plain text. Tolerant: a
    plain string (some endpoints render text) or None both degrade cleanly."""
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        if ntype == "text":
            parts.append(node.get("text", ""))
        elif ntype == "hardBreak":
            parts.append("\n")
        walk(node.get("content", []))
        # Block nodes end with a newline so paragraphs/list items stay separate.
        if ntype in ("paragraph", "heading", "blockquote", "listItem", "codeBlock"):
            parts.append("\n")

    walk(doc.get("content", []))
    return "".join(parts).strip()


def text_to_adf(text: str) -> dict:
    """Wrap plain text as a minimal ADF document — one paragraph per line, so
    the dedupe marker on its own trailing line survives as its own text node.
    Empty lines become empty paragraphs (a text node may not be empty in ADF)."""
    content: list[dict] = []
    for line in text.split("\n"):
        if line:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
        else:
            content.append({"type": "paragraph"})
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph"}]}


def _priority_rank(name: str | None) -> int:
    return _PRIORITY_RANK.get((name or "").strip().lower(), 0)


def _to_issue(node: dict, site: str) -> Issue:
    f = node.get("fields") or {}
    status = f.get("status") or {}
    category = (status.get("statusCategory") or {}).get("key", "")
    assignee = f.get("assignee") or {}
    project = f.get("project") or {}
    key = node["key"]
    return Issue(
        id=key,  # Jira issue endpoints accept the key; key == id keeps routing simple
        key=key,
        title=f.get("summary") or "",
        description=adf_to_text(f.get("description")),
        url=f"{site}/browse/{key}",
        priority=_priority_rank((f.get("priority") or {}).get("name")),
        state_name=status.get("name", ""),
        state_type=_STATE_TYPE.get(category, "unstarted"),
        labels=list(f.get("labels") or []),
        assignee_id=assignee.get("accountId"),
        created_at=f.get("created", ""),
        project_id=project.get("key") or project.get("id"),
    )


def _jql_quote(value: str) -> str:
    """Quote a JQL string literal (project keys/label names are simple, but a
    stray quote or backslash must not break out of the clause)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class JiraClient:
    def __init__(
        self,
        site: str,
        email: str | None = None,
        token: str = "",
        auth: str = "basic",
        transport=urllib_transport,
    ):
        """``auth`` = "basic" (Atlassian Cloud: email + API token) or "bearer"
        (Jira Server/Data Center Personal Access Token; ``email`` unused)."""
        self.site = site.rstrip("/")
        self.email = email
        self.token = token
        self.auth = auth
        self.transport = transport

    def auth_header(self) -> str:
        if self.auth == "bearer":
            return f"Bearer {self.token}"
        raw = f"{self.email}:{self.token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def request(
        self, method: str, path: str, params: dict | None = None, body: dict | None = None
    ) -> dict:
        url = self.site + API_BASE + path
        if params:
            url += "?" + urlencode(params)
        return self.transport(
            method,
            url,
            {
                "Authorization": self.auth_header(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body,
        )


def client_from_config(cfg, transport=urllib_transport) -> JiraClient:
    """Build a JiraClient from config: the site/email are not secrets and live
    in the config; the token follows the env-then-file rule."""
    from issuefleet import creds

    token, _ = creds.resolve_jira_token(cfg)
    return JiraClient(
        cfg.jira_site, email=cfg.jira_email, token=token, auth=cfg.jira_auth, transport=transport
    )


class JiraTracker:
    """Tracker port over Jira. One instance serves every configured project
    (they share the site credential). Jira has no agent-session platform, so the
    Linear agents-platform methods (``emit_activity`` / ``find_agent_session``)
    are inert and ``app_identity`` is False — the reconcile loop then filters our
    own comments purely by the dedupe marker, as it does with a Linear personal
    key."""

    # Reconcile checks this via getattr; Jira never authenticates "as an app"
    # in the Linear agent-session sense, so identity-based comment filtering is
    # off and marker filtering carries it.
    app_identity = False

    def __init__(self, client: JiraClient):
        self.client = client
        self._viewer_id: str | None = None

    # -- identity ----------------------------------------------------------

    def myself(self) -> dict:
        return self.client.request("GET", "/myself")

    def get_viewer_id(self) -> str:
        if self._viewer_id is None:
            self._viewer_id = self.myself()["accountId"]
        return self._viewer_id

    # -- projects / issues -------------------------------------------------

    def _search(self, jql: str) -> list[Issue]:
        """Run a JQL search, paging Jira's token-based cursor to exhaustion."""
        issues: list[Issue] = []
        token: str | None = None
        while True:
            body: dict = {"jql": jql, "maxResults": 100, "fields": _ISSUE_FIELDS}
            if token:
                body["nextPageToken"] = token
            data = self.client.request("POST", "/search/jql", body=body)
            for node in data.get("issues", []):
                issues.append(_to_issue(node, self.client.site))
            token = data.get("nextPageToken")
            if not token:
                return issues

    def _open_jql(self, project: ProjectConfig) -> str:
        return (
            f"project = {_jql_quote(project.jira_project)} "
            "AND statusCategory != Done ORDER BY created ASC"
        )

    def open_issues(self, project: ProjectConfig) -> list[Issue]:
        return self._search(self._open_jql(project))

    def eligible_issues(self, project: ProjectConfig) -> list[Issue]:
        # The claim rule (label / assignee / state) filters client-side, exactly
        # as on Linear — assignee compares accountIds, state compares the Jira
        # status name. ('agent' is rejected for Jira fleets at config time.)
        return [i for i in self.open_issues(project) if project.claim.matches(i)]

    def get_issue(self, issue_id: str) -> Issue | None:
        try:
            node = self.client.request(
                "GET", f"/issue/{issue_id}", params={"fields": ",".join(_ISSUE_FIELDS)}
            )
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return _to_issue(node, self.client.site)

    # -- comments ----------------------------------------------------------

    def _recent_comments(self, issue_id: str, count: int = 100) -> list[Comment]:
        data = self.client.request(
            "GET",
            f"/issue/{issue_id}/comment",
            params={"maxResults": count, "orderBy": "created"},
        )
        out = []
        for n in data.get("comments", []):
            author = n.get("author") or {}
            out.append(
                Comment(
                    id=n["id"],
                    author_id=author.get("accountId", ""),
                    author_name=author.get("displayName", "unknown"),
                    body=adf_to_text(n.get("body")),
                    created_at=n.get("created", ""),
                )
            )
        return sorted(out, key=lambda c: c.created_at)

    def comments_since(self, issue_id: str, cursor: str | None) -> list[Comment]:
        return [
            c for c in self._recent_comments(issue_id) if cursor is None or c.created_at > cursor
        ]

    def post_comment(self, issue_id: str, body: str) -> None:
        data = self.client.request(
            "POST", f"/issue/{issue_id}/comment", body={"body": text_to_adf(body)}
        )
        if not data.get("id"):
            raise JiraError(f"comment on {issue_id} reported no id: {data}")

    def has_comment_marker(self, issue_id: str, msg_id: str) -> bool:
        needle = MARKER_PREFIX + msg_id
        return any(needle in c.body for c in self._recent_comments(issue_id))

    # -- workflow states (transitions) -------------------------------------

    def _transitions(self, issue_id: str) -> list[dict]:
        data = self.client.request("GET", f"/issue/{issue_id}/transitions")
        return data.get("transitions", [])

    def set_state(self, issue_id: str, state_name: str) -> None:
        """Move an issue to the workflow status named ``state_name`` by finding
        the transition whose *target* status matches (case-insensitive), falling
        back to a transition named the same. Jira has no direct status set."""
        transitions = self._transitions(issue_id)
        want = state_name.strip().lower()
        chosen = None
        for t in transitions:
            if (t.get("to") or {}).get("name", "").strip().lower() == want:
                chosen = t
                break
        if chosen is None:
            for t in transitions:
                if t.get("name", "").strip().lower() == want:
                    chosen = t
                    break
        if chosen is None:
            targets = sorted(
                (t.get("to") or {}).get("name", "?") for t in transitions
            )
            raise JiraError(
                f"no transition to status {state_name!r} available on {issue_id} "
                f"from its current status; reachable now: {targets}"
            )
        self.client.request(
            "POST", f"/issue/{issue_id}/transitions", body={"transition": {"id": chosen["id"]}}
        )

    # -- agents platform (Linear-only; inert here) -------------------------

    def emit_activity(self, session_id: str, content: dict) -> None:
        return None

    def find_agent_session(self, issue_id: str) -> str | None:
        return None

    def resolve_project_id(self, project: ProjectConfig) -> str:
        return project.jira_project
