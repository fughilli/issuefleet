"""Jira client + tracker tests via an injected fake transport — request
construction and response mapping, fully offline (mirrors test_clients.py)."""

import base64
import unittest

from issuefleet import MARKER_PREFIX, marker
from issuefleet.config import ProjectConfig, ClaimRule
from issuefleet.httpx import ApiError
from issuefleet.jira import (
    JiraClient,
    JiraError,
    JiraTracker,
    adf_to_text,
    text_to_adf,
)


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        return self.responses.pop(0)


SITE = "https://acme.atlassian.net"


def _project(strategy="label", value="agent"):
    return ProjectConfig(
        name="p",
        linear_project="",
        jira_project="PROJ",
        repo="/tmp/x",
        claim=ClaimRule(strategy=strategy, value=value),
    )


def _issue_node(key="PROJ-1", status="To Do", category="new", priority="High",
                labels=None, assignee=None, summary="T", description=None):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "status": {"name": status, "statusCategory": {"key": category}},
            "priority": {"name": priority} if priority else None,
            "labels": labels if labels is not None else ["agent"],
            "assignee": assignee,
            "project": {"key": "PROJ"},
            "created": "2026-08-01T00:00:00.000+0000",
        },
    }


class JiraClientTest(unittest.TestCase):
    def test_basic_auth_header_is_base64_email_token(self):
        t = RecordingTransport([{"accountId": "a1"}])
        JiraTracker(JiraClient(SITE, email="me@acme.com", token="tok", transport=t)).get_viewer_id()
        auth = t.calls[0]["headers"]["Authorization"]
        self.assertTrue(auth.startswith("Basic "))
        self.assertEqual(
            base64.b64decode(auth.split(" ", 1)[1]).decode(), "me@acme.com:tok"
        )
        self.assertEqual(t.calls[0]["url"], f"{SITE}/rest/api/3/myself")

    def test_bearer_auth_for_server_pat(self):
        t = RecordingTransport([{"accountId": "a1"}])
        JiraTracker(JiraClient(SITE, token="pat", auth="bearer", transport=t)).get_viewer_id()
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "Bearer pat")

    def test_site_trailing_slash_stripped(self):
        self.assertEqual(JiraClient(SITE + "/", token="t").site, SITE)


class AdfTest(unittest.TestCase):
    def test_adf_to_text_flattens_paragraphs_and_breaks(self):
        doc = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "line one"}]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "a"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "b"},
                ]},
            ],
        }
        self.assertEqual(adf_to_text(doc), "line one\na\nb")

    def test_adf_to_text_tolerates_none_and_plain_string(self):
        self.assertEqual(adf_to_text(None), "")
        self.assertEqual(adf_to_text("already text"), "already text")

    def test_text_to_adf_roundtrips_through_flatten(self):
        text = "hello\n\nworld"
        self.assertEqual(adf_to_text(text_to_adf(text)), text)

    def test_text_to_adf_marker_survives(self):
        body = f"Ready for review.\n\n{marker('abc')}"
        doc = text_to_adf(body)
        self.assertIn(MARKER_PREFIX + "abc", adf_to_text(doc))


class JiraTrackerIssuesTest(unittest.TestCase):
    def test_eligible_issues_jql_filters_and_maps(self):
        t = RecordingTransport([
            {"issues": [
                _issue_node("PROJ-1", labels=["agent"]),
                _issue_node("PROJ-2", labels=["other"]),
            ]},
        ])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        issues = tr.eligible_issues(_project("label", "agent"))
        # search hits /search/jql with a project-scoped, non-done JQL
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{SITE}/rest/api/3/search/jql")
        self.assertIn('project = "PROJ"', call["payload"]["jql"])
        self.assertIn("statusCategory != Done", call["payload"]["jql"])
        # claim filter keeps only the "agent"-labelled issue
        self.assertEqual([i.key for i in issues], ["PROJ-1"])
        i = issues[0]
        self.assertEqual(i.id, "PROJ-1")
        self.assertEqual(i.url, f"{SITE}/browse/PROJ-1")
        self.assertEqual(i.priority, 2)  # High
        self.assertEqual(i.state_type, "unstarted")  # category "new"
        self.assertTrue(i.open)

    def test_search_paginates_on_next_page_token(self):
        t = RecordingTransport([
            {"issues": [_issue_node("PROJ-1")], "nextPageToken": "tok2"},
            {"issues": [_issue_node("PROJ-2")]},
        ])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        issues = tr.open_issues(_project())
        self.assertEqual([i.key for i in issues], ["PROJ-1", "PROJ-2"])
        self.assertEqual(t.calls[1]["payload"]["nextPageToken"], "tok2")

    def test_done_issue_maps_to_completed_and_is_closed(self):
        from issuefleet.jira import _to_issue

        node = _issue_node("PROJ-9", status="Done", category="done", priority="Lowest")
        issue = _to_issue(node, SITE)
        self.assertEqual(issue.state_type, "completed")
        self.assertFalse(issue.open)
        self.assertEqual(issue.priority, 5)

    def test_get_issue_404_returns_none(self):
        t = RecordingTransport([])

        def boom(method, url, headers, payload):
            raise ApiError(404, url, "not found")

        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=boom))
        self.assertIsNone(tr.get_issue("PROJ-404"))

    def test_get_issue_maps_assignee_account_id(self):
        node = _issue_node("PROJ-3", assignee={"accountId": "acc-7"})
        t = RecordingTransport([node])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        issue = tr.get_issue("PROJ-3")
        self.assertEqual(issue.assignee_id, "acc-7")
        self.assertEqual(t.calls[0]["url"].split("?")[0], f"{SITE}/rest/api/3/issue/PROJ-3")


class JiraTrackerCommentsTest(unittest.TestCase):
    def _comments_response(self):
        return {"comments": [
            {"id": "c1", "author": {"accountId": "u1", "displayName": "Alice"},
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "older"}]}]},
             "created": "2026-08-01T10:00:00.000+0000"},
            {"id": "c2", "author": {"accountId": "u2", "displayName": "Bob"},
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "newer"}]}]},
             "created": "2026-08-02T10:00:00.000+0000"},
        ]}

    def test_comments_since_filters_by_cursor_and_flattens_adf(self):
        t = RecordingTransport([self._comments_response()])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        new = tr.comments_since("PROJ-1", "2026-08-01T12:00:00.000+0000")
        self.assertEqual([c.id for c in new], ["c2"])
        self.assertEqual(new[0].body, "newer")
        self.assertEqual(new[0].author_name, "Bob")

    def test_post_comment_wraps_adf_and_checks_id(self):
        t = RecordingTransport([{"id": "c9"}])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        tr.post_comment("PROJ-1", "Hello world")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{SITE}/rest/api/3/issue/PROJ-1/comment")
        self.assertEqual(call["payload"]["body"]["type"], "doc")
        self.assertEqual(adf_to_text(call["payload"]["body"]), "Hello world")

    def test_post_comment_raises_without_id(self):
        t = RecordingTransport([{}])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        with self.assertRaises(JiraError):
            tr.post_comment("PROJ-1", "x")

    def test_has_comment_marker_reads_flattened_bodies(self):
        resp = {"comments": [
            {"id": "c1", "author": {"accountId": "u1", "displayName": "A"},
             "body": text_to_adf(f"done\n\n{marker('m1')}"),
             "created": "2026-08-01T10:00:00.000+0000"},
        ]}
        t = RecordingTransport([resp, resp])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        self.assertTrue(tr.has_comment_marker("PROJ-1", "m1"))
        self.assertFalse(tr.has_comment_marker("PROJ-1", "nope"))


class JiraTrackerStateTest(unittest.TestCase):
    def _transitions(self):
        return {"transitions": [
            {"id": "11", "name": "Start", "to": {"name": "In Progress"}},
            {"id": "21", "name": "Finish", "to": {"name": "Done"}},
        ]}

    def test_set_state_matches_target_status_and_posts_id(self):
        t = RecordingTransport([self._transitions(), {}])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        tr.set_state("PROJ-1", "in progress")  # case-insensitive on target name
        get, post = t.calls
        self.assertEqual(get["url"], f"{SITE}/rest/api/3/issue/PROJ-1/transitions")
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["payload"], {"transition": {"id": "11"}})

    def test_set_state_falls_back_to_transition_name(self):
        t = RecordingTransport([self._transitions(), {}])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        tr.set_state("PROJ-1", "Finish")
        self.assertEqual(t.calls[1]["payload"], {"transition": {"id": "21"}})

    def test_set_state_unreachable_status_raises_with_options(self):
        t = RecordingTransport([self._transitions()])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        with self.assertRaises(JiraError) as ctx:
            tr.set_state("PROJ-1", "Released")
        self.assertIn("In Progress", str(ctx.exception))


class JiraTrackerMiscTest(unittest.TestCase):
    def test_agent_platform_methods_are_inert(self):
        tr = JiraTracker(JiraClient(SITE, token="t"))
        self.assertFalse(tr.app_identity)
        self.assertIsNone(tr.find_agent_session("PROJ-1"))
        self.assertIsNone(tr.emit_activity("s", {"type": "thought"}))

    def test_resolve_project_id_is_the_key(self):
        tr = JiraTracker(JiraClient(SITE, token="t"))
        self.assertEqual(tr.resolve_project_id(_project()), "PROJ")

    def test_get_viewer_id_cached(self):
        t = RecordingTransport([{"accountId": "acc-1"}])
        tr = JiraTracker(JiraClient(SITE, email="e", token="t", transport=t))
        self.assertEqual(tr.get_viewer_id(), "acc-1")
        self.assertEqual(tr.get_viewer_id(), "acc-1")  # no second call
        self.assertEqual(len(t.calls), 1)


if __name__ == "__main__":
    unittest.main()
