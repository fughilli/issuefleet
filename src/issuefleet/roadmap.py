"""The roadmap bot: a host-side singleton that summarizes ongoing work in the
configured project(s) and publishes the summary to stakeholders.

It runs alongside the reconcile loop in the daemon and, each tick, checks
whether its cadence has elapsed; if so it:

  1. **Reads the work** — open issues in each configured Linear project, grouped
     by workflow state, are gathered into a compact context blob.
  2. **Writes the update** — with an Anthropic key, Claude turns that blob into a
     crisp stakeholder update using a *configurable* system prompt (the default
     is the "daily workstream summary" prompt from the brief). Without a key it
     falls back to a deterministic grouped listing, so the bot still produces a
     useful — if less polished — report offline.
  3. **Publishes it** — to every enabled publish surface (Discord first; see
     ``publish.py``). The run's timestamp is committed only after at least one
     surface accepts it, so a transient webhook outage retries next tick rather
     than skipping a whole interval.

Credentials (the Anthropic key, each surface's secret) stay host-side like the
rest of the daemon. State (just the last-published timestamp) persists to
``roadmap.json`` so a restart doesn't immediately re-publish.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from issuefleet.httpx import ApiError, urllib_transport
from issuefleet.publish import PublishError

log = logging.getLogger("issuefleet.roadmap")

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 2048

# Linear priority (0 none, 1 urgent .. 4 low) -> a label for the context blob.
_PRIORITY = {0: "no priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


class RoadmapBot:
    def __init__(
        self,
        config,
        tracker,
        publishers,
        *,
        agent_key=None,
        clock=time.time,
        transport=urllib_transport,
    ):
        self.cfg = config
        self.rm = config.roadmap
        self.tracker = tracker
        self.publishers = list(publishers)
        # An Anthropic key turns the summary agentic; without it the bot emits
        # the deterministic grouped listing (see _summarize).
        self.agent_key = agent_key
        self._clock = clock
        self._transport = transport
        self.state_path = Path(config.state_dir) / "roadmap.json"
        self.state = self._load_state()

    # ------------------------------------------------------------- state

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            log.warning("roadmap.json is corrupt; starting fresh")
            data = {}
        data.setdefault("last_report", None)  # None = never published yet
        return data

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.rename(tmp, self.state_path)

    # -------------------------------------------------------------- tick

    def tick(self) -> None:
        interval = self.rm.interval_s
        if interval <= 0:
            return  # cadence disabled: publish only on demand (CLI `roadmap --publish`)
        now = self._clock()
        last = self.state.get("last_report")
        if last is not None and now - last < interval:
            return
        try:
            published = self.publish_now()
        except Exception:
            log.exception("roadmap: building the summary failed; will retry next tick")
            return
        # Commit the timestamp only when a surface actually accepted the report,
        # so a dead webhook retries next tick instead of burning a full interval.
        if published:
            self.state["last_report"] = now
            self._save_state()

    # ---------------------------------------------------------- publish

    def publish_now(self) -> bool:
        """Build the summary and push it to every surface. Returns True if at
        least one surface accepted it. Raises only if building the summary
        itself fails (a surface failure is logged, not raised, so one dead
        webhook can't sink the others)."""
        text = self.render()
        if not text.strip():
            log.info("roadmap: no open work to report; nothing published")
            return False
        if not self.publishers:
            log.warning("roadmap: enabled but no publish surface configured; nothing published")
            return False
        ok = 0
        for pub in self.publishers:
            try:
                pub.publish(text)
                ok += 1
                log.info("roadmap: published to %s", pub.name)
            except PublishError:
                log.exception("roadmap: publishing to %s failed", pub.name)
        return ok > 0

    # --------------------------------------------------------- summary

    def render(self) -> str:
        """The summary text, built but not published. Shared by the daemon tick
        and the `roadmap` CLI preview."""
        context, count = self._gather_context()
        if count == 0:
            return ""
        return self._summarize(context)

    def _gather_context(self) -> tuple[str, int]:
        """Open issues across the configured projects, grouped by workflow
        state, as a compact blob for the model (or the fallback). Returns
        (text, total_issue_count). A project that fails to read is logged and
        skipped rather than sinking the whole report."""
        blocks: list[str] = []
        total = 0
        for ref in self.rm.projects:
            try:
                issues = self.tracker.open_issues_in_project(ref)
            except Exception:
                log.exception("roadmap: reading project %r failed; skipping it", ref)
                continue
            total += len(issues)
            blocks.append(self._project_block(ref, issues))
        return "\n\n".join(blocks), total

    def _project_block(self, ref: str, issues: list) -> str:
        header = f"Project: {ref}\nOpen issues ({len(issues)}):"
        if not issues:
            return header + "\n(none)"
        # Group by workflow state; within a state, most-urgent first.
        by_state: dict[str, list] = {}
        for i in issues:
            by_state.setdefault(i.state_name, []).append(i)
        lines = [header]
        for state in sorted(by_state):
            group = sorted(by_state[state], key=lambda i: i.sort_key())
            lines.append(f"\n{state} ({len(group)}):")
            for i in group:
                prio = _PRIORITY.get(i.priority, "no priority")
                lines.append(f"- {i.key} [{prio}]: {i.title}")
                snippet = self._snippet(i.description)
                if snippet:
                    lines.append(f"    {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _snippet(description: str, limit: int = 200) -> str:
        """A one-line gist of an issue description for the context blob: the
        first non-empty line, truncated. Keeps the prompt small while still
        giving the model something to summarize beyond the title."""
        for line in (description or "").splitlines():
            line = line.strip()
            if line:
                return line[:limit] + ("…" if len(line) > limit else "")
        return ""

    def _summarize(self, context: str) -> str:
        """Turn the context blob into a stakeholder update. Uses Claude when a
        key is available; otherwise returns a deterministic grouped listing so
        the bot still reports something offline."""
        if self.agent_key:
            try:
                return self._call_model(context)
            except ApiError:
                log.exception("roadmap: model call failed; falling back to a plain listing")
        return self._deterministic_summary(context)

    def _call_model(self, context: str) -> str:
        body = {
            "model": self.rm.model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "thinking": {"type": "adaptive"},
            "system": self.rm.system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Here is the current state of the work to summarize.\n\n" + context
                    ),
                }
            ],
        }
        resp = self._transport(
            "POST",
            API_URL,
            {
                "x-api-key": self.agent_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            body,
        )
        text = "\n".join(
            b.get("text", "")
            for b in resp.get("content", [])
            if b.get("type") == "text" and b.get("text")
        ).strip()
        if not text:
            log.warning("roadmap: model returned no text; falling back to a plain listing")
            return self._deterministic_summary(context)
        return text

    @staticmethod
    def _deterministic_summary(context: str) -> str:
        """The no-LLM report: a titled dump of the grouped listing. Less polished
        than the model's prose, but honest and useful — and it keeps the whole
        bot testable offline."""
        return "🗺️ **Roadmap update**\n\n" + context
