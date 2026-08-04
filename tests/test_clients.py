"""Linear/GitHub client tests via an injected fake transport — request
construction and response mapping, fully offline."""

import os
import tempfile
import unittest
from pathlib import Path

from issuefleet import config, creds, oauth
from issuefleet.github import GithubForge, parse_repo_slug
from issuefleet.httpx import ApiError
from issuefleet.linear import (
    AppTokenProvider,
    LinearClient,
    LinearTracker,
    client_from_config,
)

MINIMAL = {
    "projects": [
        {
            "name": "splanc",
            "linear_project": "Splanc",
            "repo": "/tmp/x",
            "claim": {"strategy": "label", "value": "agent"},
        }
    ]
}


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        return self.responses.pop(0)


class LinearClientTest(unittest.TestCase):
    def test_auth_header_is_raw_key_no_bearer(self):
        t = RecordingTransport([{"data": {"viewer": {"id": "u1", "name": "n", "email": "e"}}}])
        LinearTracker(LinearClient("lin_api_XXX", transport=t)).get_viewer_id()
        auth = t.calls[0]["headers"]["Authorization"]
        self.assertEqual(auth, "lin_api_XXX")
        self.assertNotIn("Bearer", auth)

    def test_open_issues_paginates_and_maps(self):
        node = {
            "id": "i1",
            "identifier": "FUG-7",
            "title": "T",
            "description": None,
            "url": "https://linear.app/x/issue/FUG-7",
            "priority": 2,
            "createdAt": "2026-07-01T00:00:00.000Z",
            "state": {"name": "Todo", "type": "unstarted"},
            "labels": {"nodes": [{"name": "agent"}]},
            "assignee": None,
            "team": {"id": "team-1"},
        }
        node2 = dict(node, id="i2", identifier="FUG-8")
        t = RecordingTransport(
            [
                {"data": {"projects": {"nodes": [{"id": "p1", "name": "Splanc"}]}}},
                {"data": {"__type": {"fields": [{"name": "delegate"}]}}},  # schema probe
                {
                    "data": {
                        "project": {
                            "issues": {
                                "nodes": [node],
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                            }
                        }
                    }
                },
                {
                    "data": {
                        "project": {
                            "issues": {
                                "nodes": [node2],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                },
            ]
        )
        tracker = LinearTracker(LinearClient("k", transport=t))
        cfg = config.parse(MINIMAL)
        issues = tracker.open_issues(cfg.projects[0])
        self.assertEqual([i.key for i in issues], ["FUG-7", "FUG-8"])
        self.assertEqual(issues[0].labels, ["agent"])
        self.assertEqual(issues[0].description, "")  # None normalized
        # Second page passed the cursor.
        self.assertEqual(t.calls[3]["payload"]["variables"]["after"], "c1")

    def test_issue_fields_adapt_to_schema(self):
        # Workspace WITH Issue.delegate: field included, mapped through.
        t = RecordingTransport(
            [{"data": {"__type": {"fields": [{"name": "delegate"}, {"name": "id"}]}}}]
        )
        tracker = LinearTracker(LinearClient("k", transport=t))
        self.assertIn("delegate { id }", tracker.issue_fields())
        # Workspace WITHOUT it: omitted, so queries can't 400.
        t2 = RecordingTransport([{"data": {"__type": {"fields": [{"name": "id"}]}}}])
        tracker2 = LinearTracker(LinearClient("k", transport=t2))
        self.assertNotIn("delegate", tracker2.issue_fields())
        # Introspection result is cached: one call each.
        tracker.issue_fields()
        self.assertEqual(len(t.calls), 1)

    def test_delegate_id_mapped(self):
        from issuefleet.linear import _to_issue

        issue = _to_issue(
            {
                "id": "i1", "identifier": "FUG-14", "title": "t", "description": "",
                "url": "u", "priority": 0, "createdAt": "",
                "state": {"name": "Todo", "type": "unstarted"},
                "labels": {"nodes": []}, "assignee": None,
                "delegate": {"id": "app-user-1"}, "team": {"id": "tm"}, "project": None,
            }
        )
        self.assertEqual(issue.delegate_id, "app-user-1")
        self.assertIsNone(issue.assignee_id)

    def test_set_state_resolves_team_state_by_name(self):
        t = RecordingTransport(
            [
                {"data": {"team": {"states": {"nodes": [
                    {"id": "s1", "name": "In Progress"},
                    {"id": "s2", "name": "Done"},
                ]}}}},
                {"data": {"issueUpdate": {"success": True}}},
            ]
        )
        tracker = LinearTracker(LinearClient("k", transport=t))
        tracker._issue_team["i1"] = "team-1"
        tracker.set_state("i1", "in progress")  # case-insensitive
        self.assertEqual(t.calls[1]["payload"]["variables"], {"id": "i1", "state": "s1"})

    def test_assign_issue_sends_assignee_id(self):
        t = RecordingTransport([{"data": {"issueUpdate": {"success": True}}}])
        tracker = LinearTracker(LinearClient("k", transport=t))
        tracker.assign_issue("i1", "user-bot")
        self.assertEqual(
            t.calls[0]["payload"]["variables"], {"id": "i1", "assignee": "user-bot"}
        )

    def test_assign_issue_raises_on_failure(self):
        from issuefleet.linear import LinearError

        t = RecordingTransport([{"data": {"issueUpdate": {"success": False}}}])
        tracker = LinearTracker(LinearClient("k", transport=t))
        with self.assertRaises(LinearError):
            tracker.assign_issue("i1", "user-bot")

    def _created_node(self, **kw):
        node = {
            "id": "new-1",
            "identifier": "FUG-99",
            "title": "Broke out of WORKLOG",
            "description": None,
            "url": "https://linear.app/x/issue/FUG-99",
            "priority": 2,
            "createdAt": "2026-07-31T00:00:00.000Z",
            "state": {"name": "Todo", "type": "unstarted"},
            "labels": {"nodes": []},
            "assignee": None,
            "team": {"id": "team-1"},
            "project": {"id": "proj-1"},
        }
        node.update(kw)
        return node

    def test_create_issue_inherits_team_and_project_from_context(self):
        t = RecordingTransport(
            [
                {"data": {"__type": {"fields": [{"name": "delegate"}]}}},  # schema probe
                # get_issue for the context project id (resolved first)
                {"data": {"issue": self._created_node(id="ctx", identifier="FUG-1")}},
                # team labels lookup (one label requested)
                {"data": {"team": {"labels": {"nodes": [{"id": "l1", "name": "backlog"}]}}}},
                {"data": {"issueCreate": {"success": True, "issue": self._created_node()}}},
            ]
        )
        tracker = LinearTracker(LinearClient("k", transport=t))
        tracker._issue_team["ctx"] = "team-1"  # context issue's team already known
        issue, unknown = tracker.create_issue(
            title="Broke out of WORKLOG",
            description="body <!-- issuefleet:msg:abc -->",
            priority=2,
            labels=["backlog", "nope"],
            context_issue_id="ctx",
        )
        self.assertEqual(issue.key, "FUG-99")
        self.assertEqual(unknown, ["nope"])  # unresolved label reported, not fatal
        inp = t.calls[-1]["payload"]["variables"]["input"]
        self.assertEqual(inp["teamId"], "team-1")
        self.assertEqual(inp["projectId"], "proj-1")  # inherited from context issue
        self.assertEqual(inp["priority"], 2)
        self.assertEqual(inp["labelIds"], ["l1"])
        self.assertIn("issuefleet:msg:abc", inp["description"])

    def test_create_issue_no_project_omits_project_id(self):
        t = RecordingTransport(
            [{"data": {"issueCreate": {"success": True, "issue": self._created_node()}}}]
        )
        tracker = LinearTracker(LinearClient("k", transport=t))
        tracker._issue_team["ctx"] = "team-1"
        tracker.create_issue(
            title="T", context_issue_id="ctx", use_context_project=False
        )
        inp = t.calls[-1]["payload"]["variables"]["input"]
        self.assertNotIn("projectId", inp)
        self.assertNotIn("priority", inp)
        self.assertNotIn("labelIds", inp)

    def test_find_issue_by_marker_returns_none_when_probe_unsupported(self):
        # Backend rejects the content filter -> best-effort probe yields None
        # so the caller proceeds rather than wedging the relay.
        t = RecordingTransport([{"errors": [{"message": "unsupported filter"}]}])
        tracker = LinearTracker(LinearClient("k", transport=t))
        self.assertIsNone(tracker.find_issue_by_marker("issuefleet:msg:abc"))


class GithubForgeTest(unittest.TestCase):
    def test_parse_repo_slug_forms(self):
        for url in (
            "git@github.com:fughilli/splanc.git",
            "git@github.com:fughilli/splanc",
            "ssh://git@github.com/fughilli/splanc.git",
            "https://github.com/fughilli/splanc.git",
            "https://github.com/fughilli/splanc",
        ):
            self.assertEqual(parse_repo_slug(url), "fughilli/splanc", url)
        with self.assertRaises(ValueError):
            parse_repo_slug("not-a-remote")

    def _pr_json(self, n=5, state="open", merged_at=None):
        return {
            "number": n,
            "html_url": f"https://github.com/o/r/pull/{n}",
            "state": state,
            "merged_at": merged_at,
            "head": {"ref": "agent/fug-1-x"},
            "base": {"ref": "main"},
        }

    def test_open_pr_request_and_headers(self):
        t = RecordingTransport([self._pr_json()])
        forge = GithubForge("tok", "fughilli/splanc", transport=t)
        pr = forge.open_pr("agent/fug-1-x", "main", "Title", "Body")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/repos/fughilli/splanc/pulls", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(call["payload"]["head"], "agent/fug-1-x")
        self.assertEqual(pr.number, 5)
        self.assertFalse(pr.merged)

    def test_find_pr_uses_owner_qualified_head(self):
        t = RecordingTransport([[self._pr_json()]])
        forge = GithubForge("tok", "fughilli/splanc", transport=t)
        pr = forge.find_pr("agent/fug-1-x")
        self.assertIn("head=fughilli:agent/fug-1-x", t.calls[0]["url"])
        self.assertEqual(pr.number, 5)

    def test_merged_detected_from_merged_at(self):
        t = RecordingTransport([self._pr_json(state="closed", merged_at="2026-07-29T00:00:00Z")])
        forge = GithubForge("tok", "o/r", transport=t)
        self.assertTrue(forge.get_pr(5).merged)

    def test_pr_feedback_normalizes_three_sources(self):
        t = RecordingTransport(
            [
                [{"id": 1, "user": {"login": "alice"}, "body": "top-level", "html_url": "u1"}],
                [
                    {"id": 2, "user": {"login": "bob"}, "body": "please fix", "state": "CHANGES_REQUESTED", "html_url": "u2"},
                    {"id": 3, "user": {"login": "carol"}, "body": "", "state": "APPROVED"},
                ],
                [{"id": 4, "user": {"login": "bob"}, "body": "rename", "path": "src/x.py", "html_url": "u3"}],
            ]
        )
        forge = GithubForge("tok", "o/r", transport=t)
        fb = forge.pr_feedback(5)
        self.assertEqual([f.id for f in fb], ["ic-1", "rv-2", "rc-4"])  # empty review dropped
        self.assertEqual(fb[1].body, "[CHANGES_REQUESTED] please fix")
        self.assertEqual(fb[2].path, "src/x.py")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class AppTokenTest(unittest.TestCase):
    def test_fetch_app_token_sends_client_credentials(self):
        seen = {}

        def post_form(url, fields):
            seen["url"] = url
            seen["fields"] = fields
            return {"access_token": "app_tok", "token_type": "Bearer", "expires_in": 2591999}

        token, ttl = oauth.fetch_app_token("cid", "csecret", post_form=post_form)
        self.assertEqual((token, ttl), ("app_tok", 2591999))
        self.assertEqual(seen["url"], oauth.TOKEN_URL)
        self.assertEqual(seen["fields"]["grant_type"], "client_credentials")
        self.assertEqual(seen["fields"]["client_id"], "cid")
        self.assertEqual(seen["fields"]["client_secret"], "csecret")
        # default agent scopes, comma-joined
        self.assertEqual(seen["fields"]["scope"], ",".join(oauth.AGENT_SCOPES))

    def test_fetch_app_token_raises_without_access_token(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.fetch_app_token("c", "s", post_form=lambda u, f: {"error": "nope"})

    def test_provider_caches_then_refetches_on_expiry(self):
        calls = []
        clock = FakeClock()

        def fetch(cid, csecret, scopes):
            calls.append((cid, csecret))
            return f"tok{len(calls)}", 1000  # skew is 300 -> good for ~700s

        p = AppTokenProvider("c", "s", fetch=fetch, clock=clock)
        self.assertEqual(p.token(), "tok1")
        clock.t = 699
        self.assertEqual(p.token(), "tok1")  # still cached (expires_at = 700)
        clock.t = 700
        self.assertEqual(p.token(), "tok2")  # refetched at the skew boundary
        self.assertEqual(len(calls), 2)

    def test_provider_force_refresh(self):
        calls = []

        def fetch(cid, csecret, scopes):
            calls.append(1)
            return f"tok{len(calls)}", 1000

        p = AppTokenProvider("c", "s", fetch=fetch, clock=FakeClock())
        self.assertEqual(p.token(), "tok1")
        self.assertEqual(p.token(force_refresh=True), "tok2")

    def test_client_uses_bearer_from_provider(self):
        t = RecordingTransport([{"data": {"viewer": {"id": "u1", "name": "n", "email": "e"}}}])
        p = AppTokenProvider("c", "s", fetch=lambda *a: ("app_tok", 1000), clock=FakeClock())
        LinearTracker(LinearClient(token_provider=p, transport=t)).get_viewer_id()
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "Bearer app_tok")

    def test_client_refetches_and_retries_once_on_401(self):
        fetched = []

        def fetch(cid, csecret, scopes):
            fetched.append(1)
            return f"tok{len(fetched)}", 1000

        class Flaky:
            def __init__(self):
                self.n = 0
                self.auths = []

            def __call__(self, method, url, headers, payload):
                self.auths.append(headers["Authorization"])
                self.n += 1
                if self.n == 1:
                    raise ApiError(401, url, "not authenticated")
                return {"data": {"viewer": {"id": "u1", "name": "n", "email": "e"}}}

        flaky = Flaky()
        p = AppTokenProvider("c", "s", fetch=fetch, clock=FakeClock())
        LinearTracker(LinearClient(token_provider=p, transport=flaky)).get_viewer_id()
        self.assertEqual(flaky.n, 2)  # failed once, retried once
        self.assertEqual(flaky.auths, ["Bearer tok1", "Bearer tok2"])  # fresh token on retry

    def test_static_key_401_is_not_retried(self):
        class Always401:
            def __init__(self):
                self.n = 0

            def __call__(self, *a):
                self.n += 1
                raise ApiError(401, "u", "not authenticated")

        t = Always401()
        with self.assertRaises(ApiError):
            LinearTracker(LinearClient("lin_api_x", transport=t)).get_viewer_id()
        self.assertEqual(t.n, 1)  # no retry for a static key

    def test_client_requires_exactly_one_credential(self):
        with self.assertRaises(ValueError):
            LinearClient()
        with self.assertRaises(ValueError):
            LinearClient("k", token_provider=AppTokenProvider("c", "s", fetch=lambda *a: ("t", 1)))


class CredsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.parse(MINIMAL)
        self.cfg.linear_api_key_file = Path(self.tmp.name) / "linear.key"
        self.cfg.github_token_file = Path(self.tmp.name) / "github.key"
        self._saved = {
            k: os.environ.pop(k, None) for k in ("LINEAR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_env_wins_over_file(self):
        self.cfg.linear_api_key_file.write_text("from-file")
        os.environ["LINEAR_API_KEY"] = "from-env"
        key, source = creds.resolve_linear_key(self.cfg)
        self.assertEqual((key, source), ("from-env", "env $LINEAR_API_KEY"))

    def test_file_fallback(self):
        self.cfg.linear_api_key_file.write_text("from-file\n")
        key, source = creds.resolve_linear_key(self.cfg)
        self.assertEqual(key, "from-file")
        self.assertIn("linear.key", source)

    def test_github_env_order(self):
        os.environ["GH_TOKEN"] = "second"
        os.environ["GITHUB_TOKEN"] = "first"
        tok, source = creds.resolve_github_token(self.cfg)
        self.assertEqual((tok, source), ("first", "env $GITHUB_TOKEN"))

    def test_missing_raises_actionable_error(self):
        with self.assertRaisesRegex(creds.CredentialError, "linear.app/settings/api"):
            creds.resolve_linear_key(self.cfg)
        with self.assertRaisesRegex(creds.CredentialError, "fine-grained PAT"):
            creds.resolve_github_token(self.cfg)

    def test_permission_check(self):
        f = self.cfg.github_token_file
        f.write_text("tok")
        f.chmod(0o600)
        self.assertTrue(creds.file_permissions_ok(f))
        f.chmod(0o644)
        self.assertFalse(creds.file_permissions_ok(f))

    def test_uses_app_token_only_for_client_credentials(self):
        self.assertFalse(creds.linear_uses_app_token(self.cfg))  # default "auto"
        self.cfg.linear_auth = "client_credentials"
        self.assertTrue(creds.linear_uses_app_token(self.cfg))

    def test_resolve_oauth_client_needs_id_and_secret(self):
        self.cfg.linear_oauth_client_secret_file = Path(self.tmp.name) / "client.secret"
        with self.assertRaisesRegex(creds.CredentialError, "linear_oauth_client_id"):
            creds.resolve_linear_oauth_client(self.cfg)
        self.cfg.linear_oauth_client_id = "cid"
        with self.assertRaisesRegex(creds.CredentialError, "client secret"):
            creds.resolve_linear_oauth_client(self.cfg)
        self.cfg.linear_oauth_client_secret_file.write_text("shhh\n")
        self.assertEqual(creds.resolve_linear_oauth_client(self.cfg), ("cid", "shhh"))

    def test_client_from_config_picks_app_token_path(self):
        self.cfg.linear_auth = "client_credentials"
        self.cfg.linear_oauth_client_id = "cid"
        self.cfg.linear_oauth_client_secret_file = Path(self.tmp.name) / "client.secret"
        self.cfg.linear_oauth_client_secret_file.write_text("shhh")
        client = client_from_config(self.cfg)
        self.assertIsNotNone(client.token_provider)
        self.assertEqual(client.auth, "oauth")

    def test_client_from_config_static_key_path(self):
        self.cfg.linear_api_key_file.write_text("lin_api_static")
        client = client_from_config(self.cfg)
        self.assertIsNone(client.token_provider)
        self.assertEqual(client.api_key, "lin_api_static")


if __name__ == "__main__":
    unittest.main()
