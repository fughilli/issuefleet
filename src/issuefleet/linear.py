"""Linear GraphQL client + Tracker implementation.

The personal API key goes in the Authorization header **raw** (no Bearer
prefix). Pagination is cursor-based; we page issues fully and read the most
recent comments per issue (threads driven by this tool are short-lived).
"""

from __future__ import annotations

import logging
import time

from issuefleet import MARKER_PREFIX, oauth
from issuefleet.config import ProjectConfig
from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.model import Comment, Issue

log = logging.getLogger("issuefleet.linear")

API_URL = "https://api.linear.app/graphql"

# Refetch an app token this many seconds before it actually expires, so a
# request never rides an about-to-die token across the boundary.
_TOKEN_REFRESH_SKEW_S = 300

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
        delegate_id=(node.get("delegate") or {}).get("id"),
        created_at=node.get("createdAt", ""),
        project_id=(node.get("project") or {}).get("id"),
    )


class AppTokenProvider:
    """Caches a Linear app-actor token minted via client_credentials and
    refetches it when it nears expiry (or is forced after a 401). App tokens
    last ~30 days with no refresh token, so "just ask for another" IS the
    refresh mechanism — Linear's documented pattern. Thread-safety isn't
    needed: only the reconcile loop calls the client, one tick at a time."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scopes: list[str] | None = None,
        *,
        fetch=oauth.fetch_app_token,
        clock=time.monotonic,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._fetch = fetch
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self, *, force_refresh: bool = False) -> str:
        if force_refresh or self._token is None or self._clock() >= self._expires_at:
            token, ttl = self._fetch(self._client_id, self._client_secret, self._scopes)
            self._token = token
            # A tiny/zero ttl (unexpected) still yields forward progress: the
            # token is used at least once before we ask again.
            self._expires_at = self._clock() + max(ttl - _TOKEN_REFRESH_SKEW_S, 0)
            log.info("minted Linear app token (valid ~%ds)", ttl)
        return self._token


class LinearClient:
    def __init__(
        self,
        api_key: str | None = None,
        transport=urllib_transport,
        auth: str = "auto",
        token_provider: AppTokenProvider | None = None,
    ):
        """Supply exactly one credential source:
          - api_key: a static personal/OAuth token. auth selects how it's
            sent: 'api_key' (raw Authorization header, personal keys),
            'oauth' (Bearer, OAuth/agent tokens), or 'auto' (infer from the
            token prefix: lin_oauth_* is Bearer, everything else raw).
          - token_provider: an AppTokenProvider that mints/rotates a Bearer
            app-actor token (client_credentials); a 401 triggers a forced
            refetch and one retry."""
        if (api_key is None) == (token_provider is None):
            raise ValueError("LinearClient needs exactly one of api_key or token_provider")
        self.api_key = api_key
        self.token_provider = token_provider
        self.transport = transport
        if token_provider is not None:
            auth = "oauth"  # app-actor tokens are Bearer
        elif auth == "auto":
            auth = "oauth" if api_key.startswith("lin_oauth_") else "api_key"
        self.auth = auth

    def _token(self) -> str:
        return self.token_provider.token() if self.token_provider else self.api_key

    def auth_header(self) -> str:
        token = self._token()
        return f"Bearer {token}" if self.auth == "oauth" else token

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        resp = self._transport(query, variables)
        if resp.get("errors"):
            raise LinearError(f"GraphQL errors: {resp['errors']}")
        return resp.get("data", {})

    def _transport(self, query: str, variables: dict | None, _retried: bool = False) -> dict:
        try:
            return self.transport(
                "POST",
                API_URL,
                {
                    "Authorization": self.auth_header(),
                    "Content-Type": "application/json",
                },
                {"query": query, "variables": variables or {}},
            )
        except ApiError as e:
            # An app token can be revoked or expire mid-life; refetch once and
            # retry (Linear's prescribed 401 handling). Static keys can't be
            # rotated from here, so let their 401 propagate unchanged.
            if e.status == 401 and self.token_provider is not None and not _retried:
                log.warning("Linear returned 401; refetching app token and retrying once")
                self.token_provider.token(force_refresh=True)
                return self._transport(query, variables, _retried=True)
            raise


def client_from_config(cfg, transport=urllib_transport) -> LinearClient:
    """Build a LinearClient from config: a client_credentials app-token
    provider when linear_auth = 'client_credentials', otherwise a static
    personal/OAuth key resolved from env-or-file."""
    from issuefleet import creds

    if creds.linear_uses_app_token(cfg):
        client_id, client_secret = creds.resolve_linear_oauth_client(cfg)
        provider = AppTokenProvider(client_id, client_secret)
        return LinearClient(token_provider=provider, transport=transport)
    key, _ = creds.resolve_linear_key(cfg)
    return LinearClient(key, auth=cfg.linear_auth, transport=transport)


class LinearTracker:
    """Tracker port over Linear. One instance serves all configured projects
    (they share the workspace key); project ids and team states are cached."""

    def __init__(self, client: LinearClient):
        self.client = client
        # When authenticated as an app (agents platform), comments authored
        # by the viewer are the app's own — including Linear's unmarked
        # mirrors of session activities — and must be filtered on identity.
        self.app_identity = client.auth == "oauth"
        self._issue_fields: str | None = None
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

    def issue_fields(self) -> str:
        """Issue selection set, adapted to the workspace schema: `delegate`
        (what agent delegation actually sets — NOT `assignee`) is included
        only if the schema has it, probed once via introspection so a
        schema without it can't break every query."""
        if self._issue_fields is None:
            fields = _ISSUE_FIELDS
            try:
                data = self.client.graphql('{ __type(name: "Issue") { fields { name } } }')
                names = {f["name"] for f in (data.get("__type") or {}).get("fields", [])}
                if "delegate" in names:
                    fields += "    delegate { id }\n"
                else:
                    log.info("Issue.delegate not in this workspace's schema; "
                             "delegation-claims will rely on assignee/webhooks only")
            except Exception:
                log.warning("schema introspection failed; using base issue fields")
            self._issue_fields = fields
        return self._issue_fields

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
                   }""" % self.issue_fields(),
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
        issues = self.open_issues(project)
        if project.claim.strategy == "agent":
            # Delegation is pollable — so a dead webhook tunnel degrades to
            # poll latency instead of total deafness. Linear stores agent
            # delegation in `delegate` (verified live: the UI says
            # "assigned" but assignee stays untouched); accept either
            # field. (@-mentions remain webhook-only.)
            me = self.get_viewer_id()
            return [i for i in issues if me in (i.assignee_id, i.delegate_id)]
        return [i for i in issues if project.claim.matches(i)]

    def get_issue(self, issue_id: str) -> Issue | None:
        try:
            data = self.client.graphql(
                "query($id: String!) { issue(id: $id) { %s } }" % self.issue_fields(),
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

    def find_agent_session(self, issue_id: str) -> str | None:
        """Id of THIS app's most-recent still-open agent session on an issue,
        or None. Recovers the session binding when the `created` webhook was
        missed (a dead tunnel) and the worker was poll-claimed instead — so
        the session view is driven from polling too, keeping webhooks an
        accelerator rather than the sole source of truth. Only the app
        identity owns sessions; a personal key has none, so we skip the call.

        Linear's `agentSessions` root query takes no issue filter, so we page
        the app's recent sessions and match client-side. Best-effort: any API
        hiccup returns None and the caller stays on the comment path."""
        if not self.app_identity:
            return None
        try:
            me = self.get_viewer_id()
            data = self.client.graphql(
                """query($n: Int!) {
                     agentSessions(first: $n) {
                       nodes {
                         id createdAt endedAt archivedAt
                         issue { id } appUser { id }
                       }
                     }
                   }""",
                {"n": 100},
            )
        except (LinearError, ApiError):
            log.warning("agent-session discovery for %s failed; staying on the "
                        "comment path", issue_id, exc_info=True)
            return None
        mine = [
            n
            for n in data.get("agentSessions", {}).get("nodes", [])
            if (n.get("issue") or {}).get("id") == issue_id
            and (n.get("appUser") or {}).get("id") == me
            and not n.get("endedAt")
            and not n.get("archivedAt")
        ]
        if not mine:
            return None
        mine.sort(key=lambda n: n.get("createdAt") or "", reverse=True)
        return mine[0]["id"]

    def resolve_project_id(self, project: ProjectConfig) -> str:
        return self._project_id(project.linear_project)

    # -- issue authoring ---------------------------------------------------

    def team_for_issue(self, issue_id: str) -> str:
        """Team UUID of an existing issue (cached; fetches the issue if cold).
        Used to default the destination team when the bot files new issues."""
        team_id = self._issue_team.get(issue_id)
        if team_id is None:
            issue = self.get_issue(issue_id)
            if issue is None:
                raise LinearError(f"issue {issue_id} not found while resolving its team")
            team_id = self._issue_team[issue_id]
        return team_id

    def _resolve_team_id(self, ref: str) -> str:
        if len(ref) == 36 and ref.count("-") == 4:
            return ref  # already a UUID
        data = self.client.graphql(
            """query($ref: String!) {
                 teams(filter: {or: [{name: {eq: $ref}}, {key: {eq: $ref}}]}) {
                   nodes { id name key }
                 }
               }""",
            {"ref": ref},
        )
        nodes = data["teams"]["nodes"]
        if not nodes:
            raise LinearError(f"no Linear team named (or keyed) {ref!r}")
        if len(nodes) > 1:
            raise LinearError(f"multiple Linear teams match {ref!r}; use the UUID")
        return nodes[0]["id"]

    def _label_ids(self, team_id: str, names: list[str]) -> tuple[list[str], list[str]]:
        """Map label names to ids for a team. Returns (ids, unresolved names);
        unknown labels are reported, never fatal — filing the issue matters
        more than a missing tag the operator can add on review."""
        if not names:
            return [], []
        data = self.client.graphql(
            """query($id: String!) {
                 team(id: $id) { labels { nodes { id name } } }
               }""",
            {"id": team_id},
        )
        by_name = {n["name"].lower(): n["id"] for n in data["team"]["labels"]["nodes"]}
        ids, unknown = [], []
        for name in names:
            lid = by_name.get(name.lower())
            (ids.append(lid) if lid else unknown.append(name))
        return ids, unknown

    def find_issue_by_marker(self, needle: str) -> Issue | None:
        """Look for an existing issue whose description carries this marker,
        so a create that landed but wasn't acked (crash mid-relay) isn't
        filed twice. Best-effort: if the backend rejects the content filter we
        return None and let the caller proceed (a stray duplicate is trivially
        deleted; a wedged relay is not)."""
        try:
            data = self.client.graphql(
                "query($n: String!) { issues(filter: {description: {contains: $n}}, first: 1)"
                " { nodes { %s } } }" % _ISSUE_FIELDS,
                {"n": needle},
            )
        except (LinearError, ApiError):
            log.warning("issue-marker dedupe probe failed; proceeding without it", exc_info=True)
            return None
        nodes = data["issues"]["nodes"]
        return _to_issue(nodes[0]) if nodes else None

    def create_issue(
        self,
        *,
        title: str,
        description: str = "",
        priority: int | None = None,
        labels: list[str] | None = None,
        team: str | None = None,
        project: str | None = None,
        use_context_project: bool = True,
        context_issue_id: str | None = None,
    ) -> tuple[Issue, list[str]]:
        """File a new Linear issue. team/project default to those of
        ``context_issue_id`` (the issue the worker was delegated) so the bot
        drops new tickets alongside the one it was asked from. Returns the
        created Issue and any label names that didn't resolve."""
        if team is not None:
            team_id = self._resolve_team_id(team)
        elif context_issue_id is not None:
            team_id = self.team_for_issue(context_issue_id)
        else:
            raise LinearError("create_issue needs a team (no context issue to inherit one from)")

        project_id: str | None = None
        if project is not None:
            project_id = self._project_id(project)
        elif use_context_project and context_issue_id is not None:
            src = self.get_issue(context_issue_id)
            project_id = src.project_id if src else None

        label_ids, unknown = self._label_ids(team_id, labels or [])

        inp: dict = {"teamId": team_id, "title": title, "description": description}
        if priority is not None:
            inp["priority"] = priority
        if project_id is not None:
            inp["projectId"] = project_id
        if label_ids:
            inp["labelIds"] = label_ids

        data = self.client.graphql(
            "mutation($input: IssueCreateInput!) {"
            " issueCreate(input: $input) { success issue { %s } } }" % _ISSUE_FIELDS,
            {"input": inp},
        )
        result = data["issueCreate"]
        if not result["success"] or not result.get("issue"):
            raise LinearError(f"issueCreate for {title!r} reported failure")
        node = result["issue"]
        self._issue_team[node["id"]] = node["team"]["id"]
        return _to_issue(node), unknown

    def assign_issue(self, issue_id: str, assignee_id: str) -> None:
        """Set an issue's assignee. The fleet manager uses this to hand a
        freshly-filed goal to the fleet's own identity so it auto-claims under
        the 'agent' strategy (which treats assignee-or-delegate == the agent as
        eligible). Assigning to the viewer requires the daemon to authenticate
        as that identity (the agent app), which is the intended setup."""
        data = self.client.graphql(
            """mutation($id: String!, $assignee: String!) {
                 issueUpdate(id: $id, input: {assigneeId: $assignee}) { success }
               }""",
            {"id": issue_id, "assignee": assignee_id},
        )
        if not data["issueUpdate"]["success"]:
            raise LinearError(f"issueUpdate (assign) on {issue_id} reported failure")

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
