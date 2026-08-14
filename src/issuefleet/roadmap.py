"""The roadmap bot: a host-side singleton that summarizes ongoing work in the
configured project(s) and publishes the summary to stakeholders.

It runs alongside the reconcile loop in the daemon and, each tick, checks
whether its cadence has elapsed; if so it:

  1. **Reads the work** — open issues in each configured Linear project, grouped
     by workflow state, are gathered into a compact context blob and *diffed*
     against the last published snapshot: each issue is tagged as new,
     progressed, or unchanged, and issues that have since closed (features
     landed, work canceled) get their own section so a delivered feature is
     announced rather than silently dropped.
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
rest of the daemon. State (the last-published timestamp and a per-issue
snapshot to diff the next report against) persists to ``roadmap.json`` so a
restart doesn't immediately re-publish and the next update reads as a diff.
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
        # The snapshot of the current run, built by render() and committed to
        # state only once a surface actually accepts the report (see
        # publish_now). Anchoring the diff to what was *published* — not merely
        # rendered — keeps a preview (`roadmap` with no --publish) from silently
        # advancing the baseline.
        self._pending_snapshot: dict | None = None

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
        # Per-issue state as of the last published report, keyed by issue id.
        # Empty on first run (or after an upgrade from a last_report-only file),
        # which the renderer reads as "give a full baseline this time".
        data.setdefault("snapshot", {})
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
        if ok > 0 and self._pending_snapshot is not None:
            # Advance the baseline only now that stakeholders have actually seen
            # this state, so tomorrow's report diffs against what was published
            # (and a just-landed issue is announced once, then drops out).
            self.state["snapshot"] = self._pending_snapshot
            self._save_state()
        return ok > 0

    # --------------------------------------------------------- summary

    def render(self) -> str:
        """The summary text, built but not published. Shared by the daemon tick
        and the `roadmap` CLI preview.

        Builds a diff against the last *published* snapshot: each open issue is
        annotated with what changed since then, and issues that have left the
        open set (features that landed, work that was canceled) get their own
        section so a completed workstream is announced rather than silently
        dropped. The freshly-computed snapshot is stashed on the bot and only
        committed by publish_now once a surface accepts the report."""
        prev = self.state.get("snapshot") or {}
        blocks: list[str] = []
        snapshot: dict = {}
        open_total = 0
        for ref in self.rm.projects:
            try:
                issues = self.tracker.open_issues_in_project(ref)
            except Exception:
                log.exception("roadmap: reading project %r failed; skipping it", ref)
                continue
            open_total += len(issues)
            for i in issues:
                snapshot[i.id] = self._snap(i, ref)
            blocks.append(self._project_block(ref, issues, prev))

        closed = self._closed_since(prev, snapshot)
        self._pending_snapshot = snapshot

        # Nothing open and nothing newly landed: genuinely nothing to say.
        if open_total == 0 and not closed:
            return ""

        context = self._compose_context(blocks, closed, first_report=not prev)
        return self._summarize(context)

    @staticmethod
    def _snap(issue, ref: str) -> dict:
        """The bit of an issue worth remembering between runs: enough to detect
        progress (state change) and to attribute a later closure to a project."""
        return {
            "key": issue.key,
            "title": issue.title,
            "project": ref,
            "state_name": issue.state_name,
            "state_type": issue.state_type,
            "priority": issue.priority,
        }

    def _closed_since(self, prev: dict, current: dict) -> list[tuple]:
        """Issues that were open at the last report but have since left the open
        set. We look each one up to learn its *current* state: a `completed`
        issue is a delivered feature worth announcing, a `canceled` one is worth
        a note; anything still open just moved out of the configured projects
        and is ignored. Returns (issue, prev_record) pairs, ordered by key."""
        out: list[tuple] = []
        for iid, rec in prev.items():
            if iid in current:
                continue
            try:
                issue = self.tracker.get_issue(iid)
            except Exception:
                log.exception("roadmap: looking up closed issue %s failed; skipping it", iid)
                continue
            if issue is None:
                continue
            if issue.state_type in ("completed", "canceled"):
                out.append((issue, rec))
        return sorted(out, key=lambda pair: pair[0].key)

    def _project_block(self, ref: str, issues: list, prev: dict) -> str:
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
                lines.append(f"- {i.key} [{prio}]: {i.title} — {self._progress(i, prev.get(i.id))}")
                snippet = self._snippet(i.description)
                if snippet:
                    lines.append(f"    {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _progress(issue, prev_rec: dict | None) -> str:
        """How an open issue has moved since the last published report, as a
        short tag the summarizer can lean on to avoid rehashing steady work."""
        if prev_rec is None:
            return "NEW since last update"
        was = prev_rec.get("state_name")
        if was and was != issue.state_name:
            return f"progressed: {was} → {issue.state_name}"
        return "no change since last update"

    def _completed_block(self, closed: list[tuple]) -> str:
        """The 'what landed' section: issues completed or canceled since the
        last report. Split so a delivered feature reads as a win, not a
        disappearance. Prefers the issue's live title, falling back to the
        remembered one if the lookup came back thin."""
        multi = len(self.rm.projects) > 1
        landed, dropped = [], []
        for issue, rec in closed:
            title = issue.title or rec.get("title", "")
            suffix = f" (in {rec.get('project')})" if multi and rec.get("project") else ""
            line = f"- {issue.key}: {title}{suffix}"
            (dropped if issue.state_type == "canceled" else landed).append(line)
        parts: list[str] = []
        if landed:
            parts.append(
                "Completed since the last update (features delivered — announce these):\n"
                + "\n".join(landed)
            )
        if dropped:
            parts.append("Canceled since the last update:\n" + "\n".join(dropped))
        return "\n\n".join(parts)

    def _compose_context(self, blocks: list[str], closed: list[tuple], *, first_report: bool) -> str:
        """Stitch the per-project blocks, the completed/canceled section, and a
        one-line framing note into the blob handed to the model (and, offline,
        shown verbatim). The framing travels *in* the context so both paths — LLM
        and deterministic — carry the same instructions and stay honest."""
        if first_report:
            note = (
                "This is the FIRST roadmap update: no prior state exists, so give a "
                "full baseline of the current workstreams."
            )
        else:
            note = (
                "This is a FOLLOW-UP update. Each open issue is tagged with how it has "
                "moved since the last update; lead with what progressed or landed and "
                "don't re-describe unchanged workstreams in detail."
            )
        sections = [note, *blocks]
        completed = self._completed_block(closed)
        if completed:
            sections.append(completed)
        return "\n\n".join(sections)

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
