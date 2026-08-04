import unittest

from issuefleet.advisor import (
    BlockedQuestion,
    ClaudeAdvisor,
    ConservativeAdvisor,
    build_advisor,
)
from issuefleet.httpx import ApiError


def q(question="Which database should I use?"):
    return BlockedQuestion(
        issue_key="FUG-9",
        question=question,
        ticket_context="Title: add caching\nUse Redis for the cache layer.",
        board_context="Goal: ship the caching feature this week.",
    )


class ConservativeAdvisorTest(unittest.TestCase):
    def test_always_escalates(self):
        t = ConservativeAdvisor().triage(q())
        self.assertFalse(t.answerable)
        self.assertEqual(t.answer, "")


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        if self.error:
            raise self.error
        return self.response


def _msg(text):
    return {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": text}]}


class ClaudeAdvisorTest(unittest.TestCase):
    def test_request_shape(self):
        t = RecordingTransport(_msg('{"answerable": false, "answer": "", "reason": "x"}'))
        ClaudeAdvisor("sk-ant-xxx", model="claude-opus-4-8", transport=t).triage(q())
        call = t.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "sk-ant-xxx")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        body = call["payload"]
        self.assertEqual(body["model"], "claude-opus-4-8")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertIn("Which database", body["messages"][0]["content"])

    def test_answerable_verdict(self):
        t = RecordingTransport(_msg('{"answerable": true, "answer": "Use Redis.", "reason": "ticket says so"}'))
        v = ClaudeAdvisor("k", transport=t).triage(q())
        self.assertTrue(v.answerable)
        self.assertEqual(v.answer, "Use Redis.")

    def test_answerable_but_empty_answer_escalates(self):
        t = RecordingTransport(_msg('{"answerable": true, "answer": "  ", "reason": "?"}'))
        self.assertFalse(ClaudeAdvisor("k", transport=t).triage(q()).answerable)

    def test_non_json_escalates(self):
        t = RecordingTransport(_msg("sorry, I cannot"))
        self.assertFalse(ClaudeAdvisor("k", transport=t).triage(q()).answerable)

    def test_no_text_block_escalates(self):
        t = RecordingTransport({"content": [{"type": "thinking", "thinking": "..."}]})
        self.assertFalse(ClaudeAdvisor("k", transport=t).triage(q()).answerable)

    def test_api_error_escalates(self):
        t = RecordingTransport(error=ApiError(500, "url", "boom"))
        v = ClaudeAdvisor("k", transport=t).triage(q())
        self.assertFalse(v.answerable)
        self.assertIn("API error", v.reason)


class BuildAdvisorTest(unittest.TestCase):
    def test_default_conservative(self):
        self.assertIsInstance(build_advisor("conservative", None), ConservativeAdvisor)

    def test_claude_without_key_falls_back(self):
        self.assertIsInstance(build_advisor("claude", None), ConservativeAdvisor)

    def test_claude_with_key(self):
        self.assertIsInstance(build_advisor("claude", "sk-ant-x"), ClaudeAdvisor)


if __name__ == "__main__":
    unittest.main()
