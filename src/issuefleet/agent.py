"""Provider-specific tool loops over the stdlib HTTP transport.

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

OpenAI uses Responses, replaying the complete output (including encrypted
reasoning and message phases) with each tool result. Requests are stateless.
An incomplete response must never cause tool execution or be called a final
answer. Every call is bounded by ``max_turns``.
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
OPENAI_API_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-6-astra"
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TOKENS = 4096
DEFAULT_OPENAI_MAX_TOKENS = 32768
MODEL_TIMEOUT_S = 300


class AgentError(Exception):
    """The loop could not produce an answer.

    ``tools_executed`` is conservative: a callback may mutate remote state and
    then raise. Once any callback was attempted, callers must not replay the
    request through a fallback dispatcher.
    """

    def __init__(self, message: str, *, tools_executed: bool = False):
        super().__init__(message)
        self.tools_executed = tools_executed


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

    def openai_spec(self) -> dict:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
            # Preserve optional fields: omission means "do not update" for
            # mutation tools. Responses otherwise normalizes to strict mode,
            # which changes the schema to require those fields.
            "strict": False,
        }


def _model_transport(method: str, url: str, headers: dict, body: dict) -> dict:
    # Reasoning models regularly exceed the 30s timeout of tracker requests.
    return urllib_transport(method, url, headers, body, timeout_s=MODEL_TIMEOUT_S)


def _validate_arguments(value, schema: dict, path: str = "arguments") -> None:
    """Validate the JSON-schema subset used by fleet tools before any effects.

    Provider-side best-effort function calling is not an input validator (for
    example, the string "false" must never become a truthy assignment flag).
    """
    kinds = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    types = schema.get("type")
    if isinstance(types, str):
        types = [types]
    if types and not any(kinds[k](value) for k in types if k in kinds):
        raise ValueError(f"{path} must have type {' or '.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']!r}")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _validate_arguments(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path} has an unexpected field: {key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_arguments(item, schema["items"], f"{path}[{index}]")


class _ToolExecutor:
    def __init__(self, tools: list[Tool]):
        self.by_name = {tool.name: tool for tool in tools}
        self.tools_executed = False

    def run(self, name: str, arguments) -> tuple[str, bool]:
        tool = self.by_name.get(name)
        if tool is None:
            return f"No such tool: {name}", True
        try:
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            _validate_arguments(arguments, tool.input_schema)
        except ValueError as e:
            return f"Invalid arguments: {e}", True
        try:
            self.tools_executed = True
            out = tool.run(arguments)
            return out if isinstance(out, str) else json.dumps(out, allow_nan=False), False
        except Exception as e:  # the callback may already have made a change
            log.warning("agent: tool %s failed: %s", name, e)
            return f"{type(e).__name__}: {e}", True


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
    model: str | None = None,
    provider: str = "anthropic",
    reasoning_effort: str | None = None,
    transport=_model_transport,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tokens: int | None = None,
) -> str:
    """Run a bounded tool loop using an independently selected API provider.

    The default remains Anthropic for existing deployments. OpenAI defaults to
    Astra and uses the Responses API. No retry is attempted here; after tools
    run, retrying the operator request could duplicate external mutations.
    """
    if provider not in ("anthropic", "openai"):
        raise AgentError(f"Unsupported agent provider: {provider}")
    if max_tokens is None:
        max_tokens = DEFAULT_OPENAI_MAX_TOKENS if provider == "openai" else DEFAULT_MAX_TOKENS
    if max_turns < 1 or max_tokens < 1:
        raise AgentError("max_turns and max_tokens must be positive")
    if len({tool.name for tool in tools}) != len(tools):
        raise AgentError("Tool names must be unique")
    executor = _ToolExecutor(tools)
    runner = _run_openai if provider == "openai" else _run_anthropic
    try:
        return runner(
            api_key=api_key, system=system, user_message=user_message, tools=tools,
            model=model or (DEFAULT_OPENAI_MODEL if provider == "openai" else DEFAULT_MODEL),
            reasoning_effort=reasoning_effort, transport=transport, executor=executor,
            max_turns=max_turns, max_tokens=max_tokens,
        )
    except AgentError as e:
        e.tools_executed = e.tools_executed or executor.tools_executed
        raise


def _run_anthropic(
    *, api_key, system, user_message, tools, model, reasoning_effort, transport,
    executor, max_turns, max_tokens,
) -> str:
    if reasoning_effort is not None:
        raise AgentError("reasoning_effort is only supported by the OpenAI manager provider")
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
        except (ApiError, OSError, ValueError) as e:
            raise AgentError(f"Anthropic API call failed: {e}") from e

        if not isinstance(resp, dict):
            raise AgentError("Anthropic returned a malformed response")
        content = resp.get("content", []) or []
        if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
            raise AgentError("Anthropic returned malformed content")
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
            out, is_error = executor.run(name, block.get("input", {}))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": out,
                    "is_error": is_error,
                }
            )
        if not results:
            return last_text
        messages.append({"role": "user", "content": results})

    log.warning("agent: hit the %d-turn cap; returning the last text", max_turns)
    return last_text or "I ran out of turns working on that — please narrow the question."


def _openai_output(resp, seen_call_ids: set[str]) -> tuple[list[dict], list[dict], str]:
    """Validate an entire response before allowing any tool in it to execute."""
    if not isinstance(resp, dict):
        raise AgentError("OpenAI returned a malformed response")
    if resp.get("error"):
        raise AgentError(f"OpenAI response failed: {resp['error']}")
    if resp.get("status") != "completed":
        raise AgentError(
            f"OpenAI response was not completed (status={resp.get('status')!r}, "
            f"details={resp.get('incomplete_details')!r})"
        )
    output = resp.get("output")
    if not isinstance(output, list) or not output:
        raise AgentError("OpenAI returned no output")
    calls, text = [], []
    current_ids = set()
    for item in output:
        if not isinstance(item, dict):
            raise AgentError("OpenAI returned a malformed output item")
        kind = item.get("type")
        if item.get("status") not in (None, "completed"):
            raise AgentError("OpenAI returned an incomplete output item")
        if kind == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise AgentError("OpenAI returned a function call without a call_id")
            if call_id in seen_call_ids or call_id in current_ids:
                raise AgentError(f"OpenAI returned a duplicate function call_id: {call_id}")
            if not isinstance(item.get("name"), str) or not item["name"]:
                raise AgentError("OpenAI returned a function call without a name")
            if not isinstance(item.get("arguments"), str):
                raise AgentError("OpenAI returned malformed function arguments")
            current_ids.add(call_id)
            calls.append(item)
        elif kind == "message":
            content = item.get("content")
            if item.get("role") != "assistant" or not isinstance(content, list):
                raise AgentError("OpenAI returned a malformed assistant message")
            for block in content:
                if not isinstance(block, dict):
                    raise AgentError("OpenAI returned malformed message content")
                if block.get("type") == "refusal":
                    raise AgentError("the model declined this request")
                if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                    raise AgentError("OpenAI returned unsupported message content")
                # Commentary is preserved for continuation, never reported as
                # completion when the model has not produced a final answer.
                if item.get("phase") in (None, "final_answer"):
                    text.append(block["text"])
        elif kind != "reasoning":
            raise AgentError(f"OpenAI returned an unsupported output item: {kind!r}")
    return output, calls, "\n".join(text).strip()


def _reject_json_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


def _run_openai(
    *, api_key, system, user_message, tools, model, reasoning_effort, transport,
    executor, max_turns, max_tokens,
) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    items: list[dict] = [{"role": "user", "content": user_message}]
    seen_call_ids: set[str] = set()
    for turn in range(max_turns):
        body = {
            "model": model,
            "instructions": system,
            "input": list(items),
            "tools": [tool.openai_spec() for tool in tools],
            "max_output_tokens": max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if reasoning_effort is not None:
            body["reasoning"] = {"effort": reasoning_effort}
        try:
            resp = transport("POST", OPENAI_API_URL, headers, body)
        except (ApiError, OSError, ValueError) as e:
            raise AgentError(f"OpenAI API call failed: {e}") from e
        output, calls, text = _openai_output(resp, seen_call_ids)
        if not calls:
            if not text:
                raise AgentError("OpenAI returned no final answer")
            return text
        if turn == max_turns - 1:
            raise AgentError(f"OpenAI agent hit the {max_turns}-turn cap before a final answer")
        # Do not reconstruct messages: reasoning ciphertext, signatures and
        # assistant phases belong to the API and must survive unchanged.
        items.extend(output)
        for call in calls:
            seen_call_ids.add(call["call_id"])
            try:
                arguments = json.loads(call["arguments"], parse_constant=_reject_json_constant)
            except (ValueError, RecursionError) as e:
                result, is_error = f"Invalid arguments: {e}", True
            else:
                result, is_error = executor.run(call["name"], arguments)
            items.append({
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": json.dumps({"error": result}) if is_error else result,
            })
    raise AgentError("OpenAI agent exhausted its turn budget")
