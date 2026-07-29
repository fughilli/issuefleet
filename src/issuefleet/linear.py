"""Linear GraphQL client + Tracker implementation.

The personal API key goes in the Authorization header **raw** (no Bearer
prefix). Pagination is cursor-based; we page issues fully and read the most
recent comments per issue (threads driven by this tool are short-lived).
"""

from __future__ import annotations

import logging

from issuefleet import MARKER_PREFIX
from issuefleet.config import ProjectConfig
from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.model import Comment, Issue

log = logging.getLogger("issuefleet.linear")

API_URL = "https://api.linear.app/graphql"

_ISSUE_FIELDS = """
    id
    identifier
    title
    description
    url
    priority
    createdAt
    state { name type }
    labels { nodes { name } }
    assignee { id }
    team { id }
    project { id }
"""


class LinearError(Exception):
    pass


def _to_issue(node: dict) -> Issue:
    return Issue(
        id=node["id"],
        key=node["identifier"],
        title=node["title"] or "",
        description=node.get("description") or "",
        url=node["url"],
        priority=int(node.get("priority") or 0),
        state_name=node["state"]["name"],
        state_type=node["state"]["type"],
        labels=[l["name"] for l in node.get("labels", {}).get("nodes", [])],
        assignee_id=(node.get("assignee") or {}).get("id"),
        created_at=node.get("createdAt", ""),
        project_id=(node.get("project") or {}).get("id"),
    )


class LinearClient:
    def __init__(self, api_key: str, transport=urllib_transport, auth: str = "auto"):
        """auth: 'api_key' (raw Authorization header, personal keys),
        'oauth' (Bearer, OAuth/agent tokens), or 'auto' (infer from the
        token prefix: lin_oauth_* is Bearer, everything else raw)."""
        self.api_key = api_key
        self.transport = transport
        if auth == "auto":
            auth = "oauth" if api_key.startswith("lin_oauth_") else "api_key"
        self.auth = auth

    def auth_header(self) -> str:
        return f"Bearer {self.api_key}" if self.auth == "oauth" else self.api_key

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        resp = self.transport(
            "POST",
            API_URL,
            {
                "Authorization": self.auth_header(),
                "Content-Type": "application/json",
            },
            {"query": query, "variables": variables or {}},
        )
        if resp.get("errors"):
            raise LinearError(f"GraphQL errors: {resp['errors']}")
        return resp.get("data", {})


class LinearTracker:
    """Tracker port over Linear. One instance serves all configured projects
    (they share the workspace key); project ids and team states are cached."""

    def __init__(self, client: LinearClient):
        self.client = client
        self._viewer: dict | None = None
        self._project_ids: dict[str, str] = {}  # ProjectConfig.linear_project -> uuid
        self._team_states: dict[str, dict[str, str]] = {}  # team_id -> {lower name: state id}
        self._issue_team: dict[str, str] = {}  # issue_id -> team_id

    # -- identity ----------------------------------------------------------

    def viewer(self) -> dict:
        if self._viewer is None:
            self._viewer = self.client.graphql("{ viewer { id name email } }")["viewer"]
        return self._viewer

    def get_viewer_id(self) -> str:
        return self.viewer()["id"]

    # -- projects / issues -------------------------------------------------

    def _project_id(self, ref: str) -> str:
        if ref in self._project_ids:
            return self._project_ids[ref]
        if len(ref) == 36 and ref.count("-") == 4:
            self._project_ids[ref] = ref  # already a UUID
            return ref
        data = self.client.graphql(
            """query($name: String!) {
                 projects(filter: {name: {eq: $name}}) { nodes { id name } }
               }""",
            {"name": ref},
        )
        nodes = data["projects"]["nodes"]
        if not nodes:
            raise LinearError(f"no Linear project named {ref!r}")
        if len(nodes) > 1:
            raise LinearError(f"multiple Linear projects named {ref!r}; use the UUID")
        self._project_ids[ref] = nodes[0]["id"]
        return nodes[0]["id"]

    def open_issues(self, project: ProjectConfig) -> list[Issue]:
        pid = self._project_id(project.linear_project)
        issues, cursor = [], None
        while True:
            data = self.client.graphql(
                """query($id: String!, $after: String) {
                     project(id: $id) {
                       issues(
                         first: 50, after: $after,
                         filter: {state: {type: {nin: ["completed", "canceled"]}}}
                       ) {
                         nodes { %s }
                         pageInfo { hasNextPage endCursor }
                       }
                     }
                   }""" % _ISSUE_FIELDS,
                {"id": pid, "after": cursor},
            )
            conn = data["project"]["issues"]
            for node in conn["nodes"]:
                self._issue_team[node["id"]] = node["team"]["id"]
                issues.append(_to_issue(node))
            if not conn["pageInfo"]["hasNextPage"]:
                return issues
            cursor = conn["pageInfo"]["endCursor"]

    def eligible_issues(self, project: ProjectConfig) -> list[Issue]:
        return [i for i in self.open_issues(project) if project.claim.matches(i)]

    def get_issue(self, issue_id: str) -> Issue | None:
        try:
            data = self.client.graphql(
                "query($id: String!) { issue(id: $id) { %s } }" % _ISSUE_FIELDS,
                {"id": issue_id},
            )
        except (LinearError, ApiError) as e:
            if "not found" in str(e).lower() or "entity not found" in str(e).lower():
                return None
            raise
        node = data.get("issue")
        if node is None:
            return None
        self._issue_team[issue_id] = node["team"]["id"]
        return _to_issue(node)

    # -- comments ----------------------------------------------------------

    def _recent_comments(self, issue_id: str, count: int = 100) -> list[Comment]:
        data = self.client.graphql(
            """query($id: String!, $last: Int!) {
                 issue(id: $id) {
                   comments(last: $last) {
                     nodes { id body createdAt user { id name } }
                   }
                 }
               }""",
            {"id": issue_id, "last": count},
        )
        out = []
        for n in data["issue"]["comments"]["nodes"]:
            user = n.get("user") or {}
            out.append(
                Comment(
                    id=n["id"],
                    author_id=user.get("id", ""),
                    author_name=user.get("name", "unknown"),
                    body=n["body"] or "",
                    created_at=n["createdAt"],
                )
            )
        return sorted(out, key=lambda c: c.created_at)

    def comments_since(self, issue_id: str, cursor: str | None) -> list[Comment]:
        return [
            c for c in self._recent_comments(issue_id) if cursor is None or c.created_at > cursor
        ]

    def post_comment(self, issue_id: str, body: str) -> None:
        data = self.client.graphql(
            """mutation($id: String!, $body: String!) {
                 commentCreate(input: {issueId: $id, body: $body}) { success }
               }""",
            {"id": issue_id, "body": body},
        )
        if not data["commentCreate"]["success"]:
            raise LinearError(f"commentCreate on {issue_id} reported failure")

    def has_comment_marker(self, issue_id: str, msg_id: str) -> bool:
        needle = MARKER_PREFIX + msg_id
        return any(needle in c.body for c in self._recent_comments(issue_id))

    # -- agent sessions (Linear agents platform) ---------------------------

    def emit_activity(self, session_id: str, content: dict) -> None:
        """Emit an agent activity into a session. content is the typed
        payload, e.g. {"type": "thought", "body": "..."} — types: thought,
        action, elicitation, response, error."""
        data = self.client.graphql(
            """mutation($input: AgentActivityCreateInput!) {
                 agentActivityCreate(input: $input) { success }
               }""",
            {"input": {"agentSessionId": session_id, "content": content}},
        )
        if not data["agentActivityCreate"]["success"]:
            raise LinearError(f"agentActivityCreate on session {session_id} reported failure")

    def resolve_project_id(self, project: ProjectConfig) -> str:
        return self._project_id(project.linear_project)

    # -- workflow states ---------------------------------------------------

    def _states_for_issue(self, issue_id: str) -> dict[str, str]:
        team_id = self._issue_team.get(issue_id)
        if team_id is None:
            issue = self.get_issue(issue_id)
            if issue is None:
                raise LinearError(f"issue {issue_id} not found while resolving team")
            team_id = self._issue_team[issue_id]
        if team_id not in self._team_states:
            data = self.client.graphql(
                """query($id: String!) {
                     team(id: $id) { states { nodes { id name } } }
                   }""",
                {"id": team_id},
            )
            self._team_states[team_id] = {
                n["name"].lower(): n["id"] for n in data["team"]["states"]["nodes"]
            }
        return self._team_states[team_id]

    def set_state(self, issue_id: str, state_name: str) -> None:
        states = self._states_for_issue(issue_id)
        state_id = states.get(state_name.lower())
        if state_id is None:
            raise LinearError(
                f"workflow state {state_name!r} not found for issue {issue_id}; "
                f"available: {sorted(states)}"
            )
        data = self.client.graphql(
            """mutation($id: String!, $state: String!) {
                 issueUpdate(id: $id, input: {stateId: $state}) { success }
               }""",
            {"id": issue_id, "state": state_id},
        )
        if not data["issueUpdate"]["success"]:
            raise LinearError(f"issueUpdate on {issue_id} reported failure")
