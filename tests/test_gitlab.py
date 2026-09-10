"""GitLab forge + forge-selection tests via an injected fake transport —
request construction and response mapping, fully offline."""

import unittest

from issuefleet.config import ProjectConfig, ClaimRule
from issuefleet.forge import build_forge, forge_kind, infer_kind
from issuefleet.github import GithubForge
from issuefleet.gitlab import GitlabForge
from issuefleet.giturl import parse_remote


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        return self.responses.pop(0)


class ParseRemoteTest(unittest.TestCase):
    def test_host_and_slug_forms(self):
        cases = {
            "git@github.com:fughilli/splanc.git": ("github.com", "fughilli/splanc"),
            "https://github.com/fughilli/splanc": ("github.com", "fughilli/splanc"),
            "git@gitlab.com:group/project.git": ("gitlab.com", "group/project"),
            "https://gitlab.com/group/project.git": ("gitlab.com", "group/project"),
            # Nested GitLab subgroups survive as part of the slug.
            "https://gitlab.example.com/group/sub/project": (
                "gitlab.example.com", "group/sub/project"),
            "ssh://git@gitlab.example.com/group/sub/project.git": (
                "gitlab.example.com", "group/sub/project"),
            # An oauth2@ (or token@) userinfo prefix on the HTTPS URL is stripped.
            "https://oauth2@gitlab.com/group/project.git": ("gitlab.com", "group/project"),
            # A self-hosted SSH remote with a custom port: the port is dropped
            # (the API and HTTPS clone don't use it) and never leaks into the slug.
            "ssh://git@gitlab.example.com:2222/group/sub/project.git": (
                "gitlab.example.com", "group/sub/project"),
            # An HTTP(S) port, by contrast, is kept — the API base and push URL
            # are built on it.
            "https://gitlab.example.com:8443/g/p.git": ("gitlab.example.com:8443", "g/p"),
        }
        for url, expected in cases.items():
            self.assertEqual(parse_remote(url), expected, url)

    def test_unparseable_raises(self):
        with self.assertRaises(ValueError):
            parse_remote("not-a-remote")


class ForgeSelectionTest(unittest.TestCase):
    def _project(self, forge=None):
        return ProjectConfig(
            name="p", linear_project="P", repo="/tmp/p",
            claim=ClaimRule("agent", ""), forge=forge,
        )

    def test_infer_kind_from_host(self):
        self.assertEqual(infer_kind("github.com"), "github")
        self.assertEqual(infer_kind("gitlab.com"), "gitlab")
        self.assertEqual(infer_kind("gitlab.example.com"), "gitlab")
        # Unknown self-hosted host defaults to github (GitHub Enterprise, etc).
        self.assertEqual(infer_kind("git.example.com"), "github")

    def test_explicit_forge_overrides_inference(self):
        # A self-hosted GitLab on an unrecognizable host, named explicitly.
        self.assertEqual(forge_kind(self._project(forge="gitlab"), "git.example.com"), "gitlab")
        # Explicit github wins even on a gitlab-looking host.
        self.assertEqual(forge_kind(self._project(forge="github"), "gitlab.com"), "github")

    def test_build_forge_picks_implementation(self):
        gh = build_forge(self._project(), "https://github.com/o/r.git",
                         lambda owner: "ghtok", lambda: "gltok")
        self.assertIsInstance(gh, GithubForge)
        self.assertEqual(gh.slug, "o/r")
        gl = build_forge(self._project(), "https://gitlab.com/g/p.git",
                         lambda owner: "ghtok", lambda: "gltok")
        self.assertIsInstance(gl, GitlabForge)
        self.assertEqual(gl.slug, "g/p")
        self.assertEqual(gl.host, "gitlab.com")

    def test_self_hosted_gitlab_carries_host(self):
        gl = build_forge(self._project(forge="gitlab"), "https://git.example.com/g/sub/p.git",
                         None, lambda: "gltok")
        self.assertIsInstance(gl, GitlabForge)
        self.assertEqual(gl.host, "git.example.com")
        self.assertEqual(gl.slug, "g/sub/p")

    def test_missing_gitlab_token_raises(self):
        with self.assertRaises(ValueError):
            build_forge(self._project(), "https://gitlab.com/g/p.git",
                        lambda owner: "ghtok", None)


class GitlabForgeTest(unittest.TestCase):
    def _mr_json(self, iid=5, state="opened", merged_at=None, has_conflicts=False,
                 merge_status="mergeable"):
        return {
            "iid": iid,
            "id": 9000 + iid,
            "web_url": f"https://gitlab.com/g/p/-/merge_requests/{iid}",
            "state": state,
            "merged_at": merged_at,
            "source_branch": "agent/fug-1-x",
            "target_branch": "main",
            "sha": "deadbeef",
            "has_conflicts": has_conflicts,
            "detailed_merge_status": merge_status,
            "merge_commit_sha": None,
        }

    def test_push_spec_uses_oauth2_basic_and_host(self):
        forge = GitlabForge("tok", "g/p", host="gitlab.example.com", transport=lambda *a: {})
        url, auth = forge.push_spec()
        self.assertEqual(url, "https://gitlab.example.com/g/p.git")
        self.assertTrue(auth.startswith("basic "))
        import base64
        self.assertEqual(base64.b64decode(auth[len("basic "):]).decode(), "oauth2:tok")

    def test_open_mr_request_and_headers(self):
        t = RecordingTransport([self._mr_json()])
        forge = GitlabForge("tok", "g/p", transport=t)
        pr = forge.open_pr("agent/fug-1-x", "main", "Title", "Body")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/api/v4/projects/g%2Fp/merge_requests", call["url"])
        self.assertEqual(call["headers"]["PRIVATE-TOKEN"], "tok")
        self.assertEqual(call["payload"]["source_branch"], "agent/fug-1-x")
        self.assertEqual(call["payload"]["target_branch"], "main")
        self.assertEqual(call["payload"]["description"], "Body")
        self.assertEqual(pr.number, 5)
        self.assertEqual(pr.head_sha, "deadbeef")
        self.assertFalse(pr.merged)
        self.assertEqual(pr.state, "open")

    def test_nested_project_path_is_url_encoded(self):
        t = RecordingTransport([[self._mr_json()]])
        forge = GitlabForge("tok", "group/sub/proj", transport=t)
        forge.find_pr("agent/fug-1-x")
        self.assertIn("/projects/group%2Fsub%2Fproj/merge_requests", t.calls[0]["url"])
        self.assertIn("source_branch=agent%2Ffug-1-x", t.calls[0]["url"])

    def test_merged_state_detected(self):
        t = RecordingTransport([self._mr_json(state="merged", merged_at="2026-08-01T00:00:00Z")])
        pr = GitlabForge("tok", "g/p", transport=t).get_pr(5)
        self.assertTrue(pr.merged)
        self.assertEqual(pr.state, "closed")

    def test_conflict_maps_to_dirty_mergeable(self):
        t = RecordingTransport([self._mr_json(has_conflicts=True, merge_status="conflict")])
        pr = GitlabForge("tok", "g/p", transport=t).get_pr(5)
        self.assertIs(pr.mergeable, False)
        self.assertEqual(pr.mergeable_state, "dirty")

    def test_clean_mr_is_mergeable(self):
        pr = GitlabForge("tok", "g/p", transport=RecordingTransport(
            [self._mr_json(merge_status="mergeable")])).get_pr(5)
        self.assertIs(pr.mergeable, True)
        self.assertEqual(pr.mergeable_state, "clean")

    def test_checking_status_leaves_mergeable_unknown(self):
        # A non-conflict, not-yet-mergeable status must stay None so the loop
        # doesn't nag the agent to rebase over a pending pipeline.
        pr = GitlabForge("tok", "g/p", transport=RecordingTransport(
            [self._mr_json(merge_status="checking")])).get_pr(5)
        self.assertIsNone(pr.mergeable)

    def test_close_mr_uses_state_event(self):
        t = RecordingTransport([{}])
        GitlabForge("tok", "g/p", transport=t).close_pr(5)
        self.assertEqual(t.calls[0]["method"], "PUT")
        self.assertIn("/merge_requests/5", t.calls[0]["url"])
        self.assertEqual(t.calls[0]["payload"], {"state_event": "close"})

    def test_pr_feedback_normalizes_notes_and_drops_system(self):
        t = RecordingTransport(
            [
                [
                    {"id": 1, "author": {"username": "alice"}, "body": "top-level",
                     "system": False, "type": None},
                    {"id": 2, "author": {"username": "gitlab"}, "body": "changed the milestone",
                     "system": True, "type": None},
                    {"id": 3, "author": {"username": "bob"}, "body": "rename this",
                     "system": False, "type": "DiffNote",
                     "position": {"new_path": "src/x.py"}},
                ]
            ]
        )
        fb = GitlabForge("tok", "g/p", transport=t).pr_feedback(5)
        self.assertEqual([f.id for f in fb], ["nt-1", "dn-3"])  # system note dropped
        self.assertEqual(fb[0].kind, "comment")
        self.assertEqual(fb[1].kind, "review_comment")
        self.assertEqual(fb[1].path, "src/x.py")
        self.assertEqual(fb[1].reviewer, "bob")

    def test_ack_feedback_awards_eyes_on_a_note(self):
        t = RecordingTransport([{"id": 1, "name": "eyes"}])
        self.assertTrue(GitlabForge("tok", "g/p", transport=t).ack_feedback(5, "nt-42"))
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/merge_requests/5/notes/42/award_emoji", call["url"])
        self.assertEqual(call["payload"], {"name": "eyes"})

    def test_ack_feedback_awards_eyes_on_a_diff_note(self):
        t = RecordingTransport([{"id": 1, "name": "eyes"}])
        self.assertTrue(GitlabForge("tok", "g/p", transport=t).ack_feedback(5, "dn-7"))
        self.assertIn("/merge_requests/5/notes/7/award_emoji", t.calls[0]["url"])

    def test_ack_feedback_swallows_api_error(self):
        from issuefleet.httpx import ApiError

        def boom(*a):
            raise ApiError(409, "award_emoji", "already awarded")
        self.assertFalse(GitlabForge("tok", "g/p", transport=boom).ack_feedback(5, "nt-1"))

    def test_ci_status_success_from_statuses(self):
        t = RecordingTransport(
            [[
                {"name": "build", "status": "success"},
                {"name": "lint", "status": "skipped"},
            ]]
        )
        ci = GitlabForge("tok", "g/p", transport=t).ci_status("abc123")
        self.assertIn("/repository/commits/abc123/statuses", t.calls[0]["url"])
        self.assertTrue(ci.settled)
        self.assertEqual(ci.state, "success")
        self.assertEqual(ci.total, 2)
        self.assertEqual(ci.failing, [])

    def test_ci_status_collects_failures(self):
        t = RecordingTransport(
            [[
                {"name": "tests", "status": "failed", "target_url": "u-tests"},
                {"name": "canceled-job", "status": "canceled"},
                {"name": "deploy", "status": "success"},
            ]]
        )
        ci = GitlabForge("tok", "g/p", transport=t).ci_status("abc123")
        self.assertEqual(ci.state, "failure")
        self.assertEqual([(c.name, c.url) for c in ci.failing], [("tests", "u-tests")])

    def test_ci_status_pending_until_settled(self):
        t = RecordingTransport([[{"name": "build", "status": "running"}]])
        ci = GitlabForge("tok", "g/p", transport=t).ci_status("abc123")
        self.assertFalse(ci.settled)
        self.assertEqual(ci.state, "pending")

    def test_ci_status_none_when_no_statuses(self):
        ci = GitlabForge("tok", "g/p", transport=RecordingTransport([[]])).ci_status("abc123")
        self.assertTrue(ci.settled)
        self.assertEqual(ci.state, "none")
        self.assertEqual(ci.total, 0)

    def test_whoami_reads_username(self):
        t = RecordingTransport([{"username": "fleet-bot"}])
        self.assertEqual(GitlabForge("tok", "g/p", transport=t).whoami(), "fleet-bot")


if __name__ == "__main__":
    unittest.main()
