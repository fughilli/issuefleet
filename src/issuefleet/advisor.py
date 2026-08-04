"""Triage advisor: decide whether a blocked worker's question can be answered
from context, or must go to the human.

This is the fleet manager's one genuinely "agentic" decision, isolated behind a
seam so the whole loop stays testable offline. ``ConservativeAdvisor`` (the
default) never auto-answers — it always escalates, which is the safe default: a
wrong auto-answer wastes a worker turn and can mislead it, whereas escalating
merely asks the human. ``ClaudeAdvisor`` is an optional LLM backend that calls
the Anthropic Messages API over the stdlib transport (the same hand-rolled
urllib approach as the Linear/GitHub clients — no SDK dependency, so the daemon
image stays stdlib-only); it is live-only and defaults off.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from issuefleet.httpx import ApiError, urllib_transport

log = logging.getLogger("issuefleet.advisor")

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"


@dataclass
class BlockedQuestion:
    issue_key: str
    question: str
    ticket_context: str  # the worker's own issue (title + description)
    board_context: str  # a summary of the top-level goals board


@dataclass
class Triage:
    answerable: bool
    answer: str = ""  # posted to the worker's issue when answerable
    reason: str = ""  # why (for logs and the escalation note)


class Advisor(Protocol):
    def triage(self, q: BlockedQuestion) -> Triage:
        """Answer the question from context, or decline (escalate to human)."""


class ConservativeAdvisor:
    """Never auto-answers; always escalates to the human. Deterministic, needs
    no LLM, and is the safe default."""

    def triage(self, q: BlockedQuestion) -> Triage:
        return Triage(answerable=False, reason="conservative advisor always escalates")


_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["answerable", "answer", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the triage brain of a software 'fleet manager' that supervises "
    "autonomous coding-agent workers. A worker is blocked on a question. Decide "
    "whether it can be answered CLEARLY and UNAMBIGUOUSLY from the provided "
    "ticket and top-level board context ALONE. If yes, set answerable=true and "
    "give a concise, directly-usable answer for the worker. If it needs a human "
    "decision, information not present in the context, or a judgment call the "
    "context does not settle, set answerable=false and leave answer empty. Be "
    "conservative: when in doubt, escalate (answerable=false)."
)


class ClaudeAdvisor:
    """LLM-backed triage over the Anthropic Messages API. Any failure degrades
    to escalation (answerable=false) so a flaky model never silently drops a
    worker's question — it just reaches the human instead."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, transport=urllib_transport):
        self._api_key = api_key
        self._model = model
        self._transport = transport

    def triage(self, q: BlockedQuestion) -> Triage:
        prompt = (
            f"Worker issue: {q.issue_key}\n\n"
            f"Ticket context:\n{q.ticket_context or '(none)'}\n\n"
            f"Top-level board (goals) context:\n{q.board_context or '(none)'}\n\n"
            f"The worker is blocked and asked:\n{q.question}"
        )
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "thinking": {"type": "adaptive"},
            "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = self._transport(
                "POST",
                API_URL,
                {
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                body,
            )
        except ApiError:
            log.exception("advisor API call for %s failed; escalating", q.issue_key)
            return Triage(answerable=False, reason="advisor API error; escalated")
        return self._parse(resp)

    @staticmethod
    def _parse(resp: dict) -> Triage:
        text = ""
        for block in resp.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                break
        if not text:
            return Triage(answerable=False, reason="advisor returned no text; escalated")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return Triage(answerable=False, reason="advisor returned non-JSON; escalated")
        answerable = bool(data.get("answerable"))
        answer = str(data.get("answer") or "").strip()
        # A "yes" with no actual answer is treated as escalation — never post an
        # empty answer to a worker.
        if answerable and not answer:
            return Triage(
                answerable=False, reason="advisor said answerable but gave no answer; escalated"
            )
        return Triage(answerable=answerable, answer=answer, reason=str(data.get("reason") or ""))


def build_advisor(kind: str, api_key: str | None, model: str = DEFAULT_MODEL) -> Advisor:
    """Pick the advisor backend. Falls back to conservative (with a warning)
    when 'claude' is requested but no API key resolves, so a missing key
    degrades the fleet manager to escalate-everything rather than crashing it."""
    if kind == "claude":
        if not api_key:
            log.warning(
                "advisor='claude' but no Anthropic API key resolved; "
                "falling back to the conservative advisor (escalate everything)"
            )
            return ConservativeAdvisor()
        return ClaudeAdvisor(api_key, model=model)
    return ConservativeAdvisor()
