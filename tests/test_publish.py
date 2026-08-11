import unittest

from issuefleet.httpx import ApiError
from issuefleet.publish import DiscordWebhookPublisher, PublishError, chunk


class ChunkTest(unittest.TestCase):
    def test_blank_input_yields_nothing(self):
        self.assertEqual(chunk(""), [])
        self.assertEqual(chunk("   \n  "), [])

    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk("hello world", limit=100), ["hello world"])

    def test_splits_on_line_boundaries(self):
        text = "\n".join(["aaaa", "bbbb", "cccc"])  # 4-char lines
        # limit 10 fits "aaaa\nbbbb" (9) but not a third line
        out = chunk(text, limit=10)
        self.assertEqual(out, ["aaaa\nbbbb", "cccc"])
        # every piece is within the limit and nothing is lost
        self.assertTrue(all(len(p) <= 10 for p in out))
        self.assertEqual("\n".join(out).replace("\n", ""), text.replace("\n", ""))

    def test_line_longer_than_limit_is_hard_split(self):
        out = chunk("x" * 25, limit=10)
        self.assertEqual(out, ["x" * 10, "x" * 10, "x" * 5])

    def test_long_line_between_normal_lines(self):
        out = chunk("ok\n" + "y" * 12 + "\ndone", limit=10)
        self.assertEqual(out, ["ok", "y" * 10, "y" * 2 + "\ndone"])


class _Recorder:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, method, url, headers, payload):
        if self.fail:
            raise ApiError(429, url, "rate limited")
        self.calls.append((method, url, headers, payload))
        return {}


class DiscordPublisherTest(unittest.TestCase):
    def test_posts_content_to_webhook(self):
        rec = _Recorder()
        DiscordWebhookPublisher("https://discord/webhook/abc", transport=rec).publish("hi there")
        self.assertEqual(len(rec.calls), 1)
        method, url, headers, payload = rec.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://discord/webhook/abc")
        self.assertEqual(payload["content"], "hi there")
        self.assertNotIn("username", payload)

    def test_username_override_is_sent(self):
        rec = _Recorder()
        DiscordWebhookPublisher(
            "https://d/w", username="Roadmap Bot", transport=rec
        ).publish("hi")
        self.assertEqual(rec.calls[0][3]["username"], "Roadmap Bot")

    def test_long_text_is_chunked_into_multiple_posts(self):
        rec = _Recorder()
        big = "\n".join(f"line {i}" for i in range(1000))
        DiscordWebhookPublisher("https://d/w", transport=rec).publish(big)
        self.assertGreater(len(rec.calls), 1)
        self.assertTrue(all(len(c[3]["content"]) <= 2000 for c in rec.calls))

    def test_empty_summary_posts_nothing(self):
        rec = _Recorder()
        DiscordWebhookPublisher("https://d/w", transport=rec).publish("   ")
        self.assertEqual(rec.calls, [])

    def test_transport_failure_becomes_publish_error(self):
        pub = DiscordWebhookPublisher("https://d/w", transport=_Recorder(fail=True))
        with self.assertRaises(PublishError):
            pub.publish("hi")


if __name__ == "__main__":
    unittest.main()
