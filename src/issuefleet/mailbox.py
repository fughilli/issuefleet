"""Filesystem mailbox: a worker's only channel to the outside world.

Layout (root = <worktree>/.agent/mailbox):

    inbox/            orchestrator -> agent      (writer: orchestrator)
    inbox/consumed/   moved here by the agent turn loop after injection
    outbox/           agent -> orchestrator      (writer: agentctl)
    outbox/archive/   moved here by the orchestrator after a successful relay
    tmp/              staging for atomic writes

One JSON file per message, named ``<seq:06d>-<kind>-<id>.json``. Each pending
directory has exactly one writer process, so sequence allocation never races;
the cross-process handoff relies only on rename() atomicity. Messages are
never deleted here — consumed/archived files are the durable audit trail and
get archived wholesale at teardown.

Outbox kinds: status, question, ready.
Inbox kinds:  reply, pr_feedback, pr_closed, info, shutdown, unclaimed.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from issuefleet.model import now_iso

_NAME_RE = re.compile(r"^(\d{6})-([a-z_]+)-([0-9a-f]+)\.json$")

OUTBOX_KINDS = ("status", "question", "ready")
INBOX_KINDS = ("reply", "pr_feedback", "pr_closed", "info", "shutdown", "unclaimed")


@dataclass
class Message:
    seq: int
    kind: str
    id: str
    ts: str
    payload: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def filename(self) -> str:
        return f"{self.seq:06d}-{self.kind}-{self.id}.json"


class MailboxError(Exception):
    pass


class Mailbox:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.inbox_consumed = self.inbox / "consumed"
        self.outbox = self.root / "outbox"
        self.outbox_archive = self.outbox / "archive"
        self.tmp = self.root / "tmp"

    def ensure(self) -> "Mailbox":
        for d in (self.inbox_consumed, self.outbox_archive, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # -- writing ----------------------------------------------------------

    def put_inbox(self, kind: str, payload: dict[str, Any]) -> Message:
        if kind not in INBOX_KINDS:
            raise MailboxError(f"unknown inbox kind {kind!r}")
        return self._put(self.inbox, self.inbox_consumed, kind, payload)

    def put_outbox(self, kind: str, payload: dict[str, Any]) -> Message:
        if kind not in OUTBOX_KINDS:
            raise MailboxError(f"unknown outbox kind {kind!r}")
        return self._put(self.outbox, self.outbox_archive, kind, payload)

    def _put(self, box: Path, moved: Path, kind: str, payload: dict[str, Any]) -> Message:
        self.ensure()
        msg = Message(
            seq=self._next_seq(box, moved),
            kind=kind,
            id=uuid.uuid4().hex[:12],
            ts=now_iso(),
            payload=payload,
        )
        tmp = self.tmp / f"{os.getpid()}-{msg.id}.json"
        tmp.write_text(
            json.dumps(
                {"seq": msg.seq, "kind": msg.kind, "id": msg.id, "ts": msg.ts, "payload": msg.payload},
                indent=2,
            )
        )
        dest = box / msg.filename()
        os.rename(tmp, dest)
        msg.path = dest
        return msg

    def _next_seq(self, box: Path, moved: Path) -> int:
        # Sequence must stay monotonic even after messages move out of the
        # pending dir, so scan both.
        top = 0
        for d in (box, moved):
            for p in d.iterdir() if d.is_dir() else ():
                m = _NAME_RE.match(p.name)
                if m:
                    top = max(top, int(m.group(1)))
        return top + 1

    # -- reading ----------------------------------------------------------

    def pending_inbox(self) -> list[Message]:
        return self._pending(self.inbox)

    def pending_outbox(self) -> list[Message]:
        return self._pending(self.outbox)

    def _pending(self, box: Path) -> list[Message]:
        msgs = []
        if not box.is_dir():
            return msgs
        for p in box.iterdir():
            m = _NAME_RE.match(p.name)
            if not m or not p.is_file():
                continue
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                # A half-visible file can't happen via rename(); a corrupt one
                # shouldn't wedge the box. Skip it; it stays for inspection.
                continue
            msgs.append(
                Message(
                    seq=data.get("seq", int(m.group(1))),
                    kind=data.get("kind", m.group(2)),
                    id=data.get("id", m.group(3)),
                    ts=data.get("ts", ""),
                    payload=data.get("payload", {}),
                    path=p,
                )
            )
        return sorted(msgs, key=lambda m: m.seq)

    # -- acknowledging ----------------------------------------------------

    def consume_inbox(self, msg: Message) -> None:
        self._move(msg, self.inbox_consumed)

    def archive_outbox(self, msg: Message, receipt: dict[str, Any] | None = None) -> None:
        """Move a relayed outbox message to the archive, recording how it was
        delivered. Archiving is the relay's commit point: a message still in
        outbox/ is retried next tick (at-least-once); the posted-body marker
        makes the retry a no-op if the previous attempt actually landed."""
        if receipt and msg.path:
            data = json.loads(msg.path.read_text())
            data["receipt"] = receipt
            msg.path.write_text(json.dumps(data, indent=2))
        self._move(msg, self.outbox_archive)

    def _move(self, msg: Message, dest_dir: Path) -> None:
        if msg.path is None:
            raise MailboxError(f"message {msg.id} has no path")
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.rename(msg.path, dest_dir / msg.path.name)
        msg.path = dest_dir / msg.path.name
