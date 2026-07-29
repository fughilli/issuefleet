"""Durable fleet registry: the state that lets a restarted daemon re-adopt
running workers instead of orphaning or re-claiming them.

A single JSON file under the state dir, written atomically (tmp + rename).
Everything else about a worker is reconstructable from the worktree itself
(mailbox, .agent/state.json) or from the APIs; the registry holds only the
bindings — issue ⇒ branch/worktree/session — plus cursors and counters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from issuefleet.model import WorkerRecord

SCHEMA_VERSION = 1


class Registry:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "registry.json"
        self.archive_root = self.state_dir / "archive"
        self.workers: dict[str, WorkerRecord] = {}  # keyed by issue_id
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except json.JSONDecodeError:
            # A torn write is impossible via rename(); a hand-mangled file is
            # not ours to guess at. Fail loudly rather than silently starting
            # with an empty fleet and double-claiming everything.
            raise RuntimeError(
                f"registry {self.path} is corrupt; fix or remove it before starting"
            )
        for d in data.get("workers", []):
            rec = WorkerRecord.from_dict(d)
            self.workers[rec.issue_id] = rec

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "workers": [w.to_dict() for w in self.workers.values()],
                },
                indent=2,
            )
        )
        os.rename(tmp, self.path)

    def add(self, rec: WorkerRecord) -> None:
        self.workers[rec.issue_id] = rec
        self.save()

    def remove(self, issue_id: str) -> None:
        self.workers.pop(issue_id, None)
        self.save()

    def get(self, issue_id: str) -> WorkerRecord | None:
        return self.workers.get(issue_id)

    def all(self) -> list[WorkerRecord]:
        return sorted(self.workers.values(), key=lambda w: w.created_at)

    def archive_dir_for(self, rec: WorkerRecord) -> Path:
        """Durable per-worker archive location, outside the worktree, so the
        transcript and mailbox outlive the branch."""
        stamp = rec.created_at.replace(":", "").replace("+", "Z")
        return self.archive_root / f"{rec.project}-{rec.issue_key}-{stamp}"
