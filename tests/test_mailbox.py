import tempfile
import unittest
from pathlib import Path

from issuefleet.mailbox import Mailbox, MailboxError


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mb = Mailbox(Path(self.tmp.name) / "mailbox").ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_and_read_outbox(self):
        m = self.mb.put_outbox("status", {"text": "formed a plan"})
        pending = self.mb.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "status")
        self.assertEqual(pending[0].payload["text"], "formed a plan")
        self.assertEqual(pending[0].id, m.id)

    def test_ordering_by_sequence(self):
        self.mb.put_outbox("status", {"text": "one"})
        self.mb.put_outbox("question", {"text": "two"})
        self.mb.put_outbox("status", {"text": "three"})
        kinds = [m.payload["text"] for m in self.mb.pending_outbox()]
        self.assertEqual(kinds, ["one", "two", "three"])

    def test_sequence_survives_archiving(self):
        # Seq must not reset after messages leave the pending dir, or sorting
        # breaks across the archive boundary.
        m1 = self.mb.put_outbox("status", {"text": "one"})
        self.mb.archive_outbox(m1, receipt={"relayed": True})
        m2 = self.mb.put_outbox("status", {"text": "two"})
        self.assertGreater(m2.seq, m1.seq)

    def test_archive_records_receipt_and_clears_pending(self):
        m = self.mb.put_outbox("ready", {"title": "T", "body": "B"})
        self.mb.archive_outbox(m, receipt={"pr": 42})
        self.assertEqual(self.mb.pending_outbox(), [])
        archived = list(self.mb.outbox_archive.glob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertIn('"pr": 42', archived[0].read_text())

    def test_inbox_consume(self):
        self.mb.put_inbox("reply", {"author": "alice", "text": "use approach B"})
        [m] = self.mb.pending_inbox()
        self.mb.consume_inbox(m)
        self.assertEqual(self.mb.pending_inbox(), [])
        self.assertEqual(len(list(self.mb.inbox_consumed.glob("*.json"))), 1)

    def test_file_issue_is_an_outbox_kind(self):
        m = self.mb.put_outbox("file_issue", {"title": "New ticket"})
        self.assertEqual(self.mb.pending_outbox()[0].id, m.id)

    def test_unknown_kinds_rejected(self):
        with self.assertRaises(MailboxError):
            self.mb.put_outbox("reply", {})  # reply is an inbox kind
        with self.assertRaises(MailboxError):
            self.mb.put_inbox("status", {})  # status is an outbox kind

    def test_corrupt_file_does_not_wedge_the_box(self):
        self.mb.put_outbox("status", {"text": "good"})
        (self.mb.outbox / "000099-status-deadbeef0000.json").write_text("{not json")
        pending = self.mb.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["text"], "good")

    def test_coalesce_dedupes_identical_pending_info(self):
        text = {"text": "Your session was restarted after a crash; continue."}
        m1 = self.mb.put_inbox("info", text, coalesce=True)
        m2 = self.mb.put_inbox("info", text, coalesce=True)
        pending = self.mb.pending_inbox()
        self.assertEqual(len(pending), 1)  # the second copy was not appended
        self.assertEqual(m2.id, m1.id)     # it returned the queued message

    def test_coalesce_writes_again_after_consume(self):
        # Once the agent has read (consumed) the note, a fresh copy is a real
        # event again — coalescing only suppresses *unread* duplicates.
        text = {"text": "restarted"}
        m1 = self.mb.put_inbox("info", text, coalesce=True)
        self.mb.consume_inbox(m1)
        m2 = self.mb.put_inbox("info", text, coalesce=True)
        self.assertNotEqual(m2.id, m1.id)
        self.assertEqual(len(self.mb.pending_inbox()), 1)

    def test_coalesce_leaves_distinct_payloads_and_kinds_alone(self):
        self.mb.put_inbox("info", {"text": "one"}, coalesce=True)
        self.mb.put_inbox("info", {"text": "two"}, coalesce=True)
        self.mb.put_inbox("reply", {"text": "one"}, coalesce=True)
        self.assertEqual(len(self.mb.pending_inbox()), 3)

    def test_coalesce_off_by_default_appends(self):
        self.mb.put_inbox("info", {"text": "dup"})
        self.mb.put_inbox("info", {"text": "dup"})
        self.assertEqual(len(self.mb.pending_inbox()), 2)

    def test_boxes_are_independent(self):
        self.mb.put_outbox("status", {"text": "out"})
        self.mb.put_inbox("reply", {"text": "in"})
        self.assertEqual(len(self.mb.pending_outbox()), 1)
        self.assertEqual(len(self.mb.pending_inbox()), 1)


if __name__ == "__main__":
    unittest.main()
