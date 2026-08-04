"""A minimal Anthropic tool-use loop over the stdlib transport.

The fleet manager is an *agent*, not a dispatch table: a message arriving in the
Signal group is handed to a model that can look at the fleet, read issues, and
act — filing a goal, answering a blocked worker — before replying in plain
English. This module is the loop that makes that possible.

Hand-rolled over ``httpx.urllib_transport`` for the same reason as the Linear
and GitHub clients: the daemon core is stdlib-only, so there is no `anthropic`
SDK here. That means we own the wire contract, which is small but has three
sharp edges worth naming:

- **Parallel tool use.** One assistant turn may contain several ``tool_use``
  blocks. Every result must come back in a SINGLE user message — splitting them
  across messages trains the model to stop calling tools in parallel.
- **Verbatim echo.** The assistant's ``content`` is appended unchanged, thinking
  blocks included. Editing or dropping them breaks the next turn's signature
  check.
- **A tool that raises is not a loop failure.** It comes back as a
  ``tool_result`` with ``is_error``, so the model can apologise, try another
  tool, or tell the human — which is almost always better than a traceback.

Every call is bounded by ``max_turns``; a loop that hits the cap returns what it
has rather than spinning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from issuefleet.httpx import ApiError, urllib_transport

log = logging.getLogger("issuefleet.agent")

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TOKENS = 4096


class AgentError(Exception):
    """The loop could not produce an answer. Callers degrade rather than crash —
    the fleet manager falls back to its deterministic dispatch."""


@dataclass
class Tool:
    """One callable exposed to the model. ``run`` takes the parsed ``input``
    dict and returns a string the model will read; raising is fine and is
    reported back as an error result."""

    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], str]

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _text_of(content: list) -> str:
    return "\n".join(
        b.get("text", "") for b in content if b.get("type") == "text" and b.get("text")
    ).strip()


def run_agent(
    *,
    api_key: str,
    system: str,
    user_message: str,
    tools: list[Tool],
    model: str = DEFAULT_MODEL,
    transport=urllib_transport,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Run the model to completion, executing tools as it asks for them, and
    return its final text. Raises AgentError on transport failure or a refusal.
    """
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": user_message}]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    last_text = ""

    for turn in range(max_turns):
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "system": system,
            "messages": messages,
            "tools": [t.spec() for t in tools],
        }
        try:
            resp = transport("POST", API_URL, headers, body)
        except ApiError as e:
            raise AgentError(f"Anthropic API call failed: {e}") from e

        content = resp.get("content", []) or []
        stop = resp.get("stop_reason")
        last_text = _text_of(content) or last_text

        if stop == "refusal":
            raise AgentError("the model declined this request")
        if stop != "tool_use":
            # end_turn, max_tokens, or anything else terminal: this is the answer.
            if stop == "max_tokens":
                log.warning("agent: hit max_tokens; returning a possibly truncated reply")
            return last_text

        # Echo the assistant turn back unchanged — thinking blocks included.
        messages.append({"role": "assistant", "content": content})

        # Execute EVERY tool_use block, and return all results in ONE user
        # message (see the module docstring).
        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            tool = by_name.get(name)
            if tool is None:
                out, is_error = f"No such tool: {name}", True
            else:
                try:
                    out, is_error = tool.run(block.get("input") or {}), False
                except Exception as e:  # a tool failing is information, not a crash
                    log.warning("agent: tool %s failed: %s", name, e)
                    out, is_error = f"{type(e).__name__}: {e}", True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": out if isinstance(out, str) else json.dumps(out),
                    "is_error": is_error,
                }
            )
        if not results:
            return last_text
        messages.append({"role": "user", "content": results})

    log.warning("agent: hit the %d-turn cap; returning the last text", max_turns)
    return last_text or "I ran out of turns working on that — please narrow the question."
