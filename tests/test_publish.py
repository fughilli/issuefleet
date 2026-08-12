import unittest

from issuefleet.httpx import ApiError
from issuefleet.publish import (
    DiscordBotPublisher,
    DiscordWebhookPublisher,
    PublishError,
    chunk,
)


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


class DiscordBotPublisherTest(unittest.TestCase):
    def test_posts_to_the_channel_messages_endpoint_as_the_bot(self):
        rec = _Recorder()
        DiscordBotPublisher("tok-123", "42", transport=rec).publish("hi there")
        self.assertEqual(len(rec.calls), 1)
        method, url, headers, payload = rec.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://discord.com/api/v10/channels/42/messages")
        # The "Bot " prefix is what distinguishes a bot token from a user token.
        self.assertEqual(headers["Authorization"], "Bot tok-123")
        self.assertTrue(headers["User-Agent"].startswith("DiscordBot"))
        self.assertEqual(payload, {"content": "hi there"})

    def test_numeric_channel_id_is_accepted(self):
        rec = _Recorder()
        DiscordBotPublisher("t", 987654321, transport=rec).publish("hi")
        self.assertIn("/channels/987654321/messages", rec.calls[0][1])

    def test_no_username_override_in_bot_mode(self):
        rec = _Recorder()
        DiscordBotPublisher("t", "1", transport=rec).publish("hi")
        self.assertNotIn("username", rec.calls[0][3])

    def test_long_text_is_chunked_into_multiple_posts(self):
        rec = _Recorder()
        big = "\n".join(f"line {i}" for i in range(1000))
        DiscordBotPublisher("t", "1", transport=rec).publish(big)
        self.assertGreater(len(rec.calls), 1)
        self.assertTrue(all(len(c[3]["content"]) <= 2000 for c in rec.calls))

    def test_empty_summary_posts_nothing(self):
        rec = _Recorder()
        DiscordBotPublisher("t", "1", transport=rec).publish("   ")
        self.assertEqual(rec.calls, [])

    def test_transport_failure_becomes_publish_error(self):
        pub = DiscordBotPublisher("t", "1", transport=_Recorder(fail=True))
        with self.assertRaises(PublishError):
            pub.publish("hi")

    def test_token_is_not_echoed_into_the_error(self):
        # The PublishError is logged and can reach an operator's terminal; the
        # bot token must not ride along with it.
        pub = DiscordBotPublisher("super-secret-token", "1", transport=_Recorder(fail=True))
        with self.assertRaises(PublishError) as ctx:
            pub.publish("hi")
        self.assertNotIn("super-secret-token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
