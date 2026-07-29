"""Linear/GitHub client tests via an injected fake transport — request
construction and response mapping, fully offline."""

import os
import tempfile
import unittest
from pathlib import Path

from issuefleet import config, creds
from issuefleet.github import GithubForge, parse_repo_slug
from issuefleet.linear import LinearClient, LinearTracker

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
        self.assertEqual(t.calls[2]["payload"]["variables"]["after"], "c1")

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


if __name__ == "__main__":
    unittest.main()
