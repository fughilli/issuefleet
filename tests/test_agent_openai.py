"""Responses wire-contract tests. Scripted HTTP replies, real tool dispatch."""

import copy
import json
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from issuefleet.agent import AgentError, Tool, run_agent
from issuefleet.httpx import ApiError


def message(text="done", phase="final_answer"):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
        "phase": phase, "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call(call_id="call_1", name="write", arguments='{"title":"one"}'):
    return {
        "id": "fc_" + call_id, "type": "function_call", "status": "completed",
        "call_id": call_id, "name": name, "arguments": arguments,
    }


def response(*output, **fields):
    return {"id": "resp_1", "status": "completed", "output": list(output), **fields}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, copy.deepcopy(body)))
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class OpenAIAgentTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tool = Tool("write", "Record a title", {
            "type": "object",
            "properties": {"title": {"type": "string"}, "assign": {"type": "boolean"}},
            "required": ["title"],
        }, lambda args: self.calls.append(args) or "recorded")

    def run_with(self, transport, **kwargs):
        return run_agent(
            api_key="test-secret", system="system", user_message="user",
            tools=kwargs.pop("tools", [self.tool]), provider="openai", transport=transport,
            **kwargs,
        )

    def test_request_uses_responses_with_provider_default_and_explicit_effort(self):
        t = Transport(response(message()))
        self.assertEqual(self.run_with(t, reasoning_effort="high"), "done")
        method, url, headers, body = t.requests[0]
        self.assertEqual((method, url), ("POST", "https://api.openai.com/v1/responses"))
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertEqual(body["max_output_tokens"], 32768)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["instructions"], "system")
        self.assertEqual(body["input"], [{"role": "user", "content": "user"}])
        self.assertFalse(body["store"])
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertNotIn("previous_response_id", body)
        self.assertNotIn("thinking", body)
        self.assertEqual(body["tools"], [{
            "type": "function", "name": "write", "description": "Record a title",
            "parameters": self.tool.input_schema, "strict": False,
        }])

    def test_model_and_token_override_and_omitted_effort(self):
        t = Transport(response(message()))
        self.run_with(t, model="another-model", max_tokens=16000)
        body = t.requests[0][3]
        self.assertEqual(body["model"], "another-model")
        self.assertEqual(body["max_output_tokens"], 16000)
        self.assertNotIn("reasoning", body)

    def test_multiple_calls_and_all_output_items_round_trip_verbatim(self):
        output = [
            {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "ciphertext"},
            message("working", "commentary"),
            call("a"), call("b", arguments='{"title":"two", "assign":false}'),
        ]
        t = Transport(response(*output), response(message("complete")))
        self.assertEqual(self.run_with(t), "complete")
        self.assertEqual(self.calls, [{"title": "one"}, {"title": "two", "assign": False}])
        continuation = t.requests[1][3]["input"]
        self.assertEqual(continuation[1:5], output)
        self.assertEqual(continuation[5:], [
            {"type": "function_call_output", "call_id": "a", "output": "recorded"},
            {"type": "function_call_output", "call_id": "b", "output": "recorded"},
        ])
        # The next turn must not mutate already-issued request bodies.
        self.assertEqual(len(t.requests[0][3]["input"]), 1)

    def test_preserves_prior_turns_when_tools_run_again(self):
        t = Transport(response(call("a")), response(call("b")), response(message()))
        self.assertEqual(self.run_with(t), "done")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([i.get("call_id") for i in t.requests[2][3]["input"][1:]],
                         ["a", "a", "b", "b"])

    def test_invalid_arguments_return_errors_without_executing(self):
        for arguments in ('{', '[]', 'null', '{}', '{"title":42}',
                          '{"title":"x","assign":"false"}', '{"title":NaN}'):
            with self.subTest(arguments=arguments):
                t = Transport(response(call(arguments=arguments)), response(message("recovered")))
                self.assertEqual(self.run_with(t), "recovered")
                output = t.requests[1][3]["input"][-1]["output"]
                self.assertIn("Invalid arguments", json.loads(output)["error"])
        self.assertEqual(self.calls, [])

    def test_unknown_tool_returns_error_without_executing(self):
        t = Transport(response(call(name="unknown")), response(message()))
        self.run_with(t)
        self.assertEqual(self.calls, [])
        self.assertIn("No such tool", t.requests[1][3]["input"][-1]["output"])

    def test_callback_exception_can_be_reported_to_the_model(self):
        def write_then_fail(args):
            self.calls.append(args)
            raise RuntimeError("failed after writing")
        self.tool.run = write_then_fail
        t = Transport(response(call()), response(message("write may have happened")))
        self.assertEqual(self.run_with(t), "write may have happened")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("failed after writing", t.requests[1][3]["input"][-1]["output"])

    def test_unserializable_tool_output_is_an_error_result(self):
        self.tool.run = lambda args: {"not_json": object()}
        t = Transport(response(call()), response(message()))
        self.assertEqual(self.run_with(t), "done")
        self.assertIn("TypeError", t.requests[1][3]["input"][-1]["output"])

    def test_transport_errors_are_marked_before_and_after_tool_execution(self):
        for exception in (ApiError(503, "url", "down"), TimeoutError("timeout"),
                          ValueError("bad response JSON")):
            with self.subTest(exception=type(exception).__name__):
                for after_tool in (False, True):
                    replies = ([response(call())] if after_tool else []) + [exception]
                    with self.assertRaises(AgentError) as caught:
                        self.run_with(Transport(*replies))
                    self.assertEqual(caught.exception.tools_executed, after_tool)

    def test_callback_that_writes_then_raises_still_disables_replay(self):
        def write_then_fail(args):
            self.calls.append(args)
            raise RuntimeError("lost acknowledgement")
        self.tool.run = write_then_fail
        with self.assertRaises(AgentError) as caught:
            self.run_with(Transport(response(call()), response(status="failed")))
        self.assertTrue(caught.exception.tools_executed)
        self.assertEqual(len(self.calls), 1)

    def test_incomplete_failed_and_refused_batches_never_execute_any_tools(self):
        refusal = message()
        refusal["content"] = [{"type": "refusal", "refusal": "declined"}]
        for reply in (
            response(call(), status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            response(call(), status="failed", error={"message": "failed"}),
            response(call(), status="queued"), response(call(), refusal),
            response(call(), message(), error={"code": "bad"}),
        ):
            with self.subTest(reply=reply):
                with self.assertRaises(AgentError) as caught:
                    self.run_with(Transport(reply))
                self.assertFalse(caught.exception.tools_executed)
        self.assertEqual(self.calls, [])

    def test_missing_or_malformed_output_fails_closed(self):
        malformed = [
            None, [], {}, response(), response(output=None),
            response(42), response({"type": "function_call"}),
            response(call(arguments={})), response(call(name=None)),
            response({**call(), "call_id": None}),
            response({**call(), "status": "in_progress"}),
            response({"type": "unknown"}),
            response({**message(), "content": None}),
            response({**message(), "role": "user"}),
            response({**message(), "content": [{"type": "output_text", "text": 1}]}),
            response({**message(), "content": [1]}),
        ]
        for reply in malformed:
            with self.subTest(reply=reply):
                with self.assertRaises(AgentError):
                    self.run_with(Transport(reply))
        self.assertEqual(self.calls, [])

    def test_entire_output_batch_validated_before_first_effect(self):
        with self.assertRaises(AgentError):
            self.run_with(Transport(response(call(), {"type": "function_call"})))
        self.assertEqual(self.calls, [])

    def test_duplicate_call_ids_in_one_response_fail_before_any_effect(self):
        with self.assertRaisesRegex(AgentError, "duplicate") as caught:
            self.run_with(Transport(response(call(), call())))
        self.assertFalse(caught.exception.tools_executed)
        self.assertEqual(self.calls, [])

    def test_duplicate_call_ids_across_turns_never_execute_twice(self):
        with self.assertRaisesRegex(AgentError, "duplicate") as caught:
            self.run_with(Transport(response(call()), response(call())))
        self.assertTrue(caught.exception.tools_executed)
        self.assertEqual(len(self.calls), 1)

    def test_only_final_answer_is_returned(self):
        t = Transport(response(message("working", "commentary"), message("finished")))
        self.assertEqual(self.run_with(t), "finished")
        for output in (message("still working", "commentary"), message(""),
                       {"type": "reasoning", "encrypted_content": "opaque", "summary": []}):
            with self.assertRaisesRegex(AgentError, "no final answer"):
                self.run_with(Transport(response(output)))

    def test_turn_budget_stops_before_unacknowledgeable_tool_effect(self):
        t = Transport(response(call("a")), response(call("b")))
        with self.assertRaisesRegex(AgentError, "2-turn cap") as caught:
            self.run_with(t, max_turns=2)
        self.assertTrue(caught.exception.tools_executed)
        self.assertEqual(len(t.requests), 2)
        self.assertEqual(len(self.calls), 1)

    def test_one_turn_budget_allows_final_answer_but_no_tools(self):
        self.assertEqual(self.run_with(Transport(response(message())), max_turns=1), "done")
        with self.assertRaisesRegex(AgentError, "1-turn cap") as caught:
            self.run_with(Transport(response(call())), max_turns=1)
        self.assertFalse(caught.exception.tools_executed)
        self.assertEqual(self.calls, [])

    def test_invalid_configuration_does_not_make_requests(self):
        t = Transport()
        for kwargs in ({"max_turns": 0}, {"max_tokens": 0}, {"tools": [self.tool, self.tool]}):
            with self.assertRaises(AgentError):
                self.run_with(t, **kwargs)
        self.assertEqual(t.requests, [])

    def test_default_transport_has_a_reasoning_appropriate_timeout(self):
        with patch("issuefleet.agent.urllib_transport", return_value=response(message())) as transport:
            self.assertEqual(run_agent(api_key="k", system="s", user_message="u", tools=[],
                                       provider="openai"), "done")
        self.assertEqual(transport.call_args.kwargs, {"timeout_s": 300})

    def test_real_http_transport_completes_a_multi_tool_conversation(self):
        # Exercise actual serialization, authentication headers, urllib, and
        # tool results over HTTP. No provider account or remote effects needed.
        requests = []
        replies = [response(call("a"), call("b", arguments='{"title":"two"}')),
                   response(message("two recorded"))]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers["Authorization"],
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                payload = json.dumps(replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/responses"
                with patch("issuefleet.agent.OPENAI_API_URL", url):
                    answer = run_agent(api_key="local-test", system="s", user_message="u",
                                       tools=[self.tool], provider="openai")
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.assertEqual(answer, "two recorded")
        self.assertEqual(self.calls, [{"title": "one"}, {"title": "two"}])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][:2], ("/v1/responses", "Bearer local-test"))
        self.assertEqual([i["call_id"] for i in requests[1][2]["input"][-2:]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
