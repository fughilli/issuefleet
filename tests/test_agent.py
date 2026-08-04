"""The Anthropic tool-use loop, against a scripted fake transport. Offline: the
transport is injected, so nothing here touches the network."""

import unittest

from issuefleet.agent import AgentError, Tool, run_agent
from issuefleet.httpx import ApiError


def text_turn(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop}


def tool_turn(*calls):
    """calls: (id, name, input) triples — several means a parallel tool turn."""
    return {
        "content": [
            {"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in calls
        ],
        "stop_reason": "tool_use",
    }


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, method, url, headers, body):
        self.bodies.append(body)
        if not self.responses:
            raise AssertionError("transport called more times than scripted")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def echo_tool(calls, name="echo", result="ok"):
    return Tool(name, "echo", {"type": "object", "properties": {}},
                lambda inp: (calls.append(inp), result)[1])


class AgentLoopTest(unittest.TestCase):
    def run_with(self, transport, tools=()):
        return run_agent(
            api_key="k", system="s", user_message="u", tools=list(tools), transport=transport
        )

    def test_plain_answer_makes_one_call(self):
        t = FakeTransport(text_turn("hello"))
        self.assertEqual(self.run_with(t), "hello")
        self.assertEqual(len(t.bodies), 1)

    def test_tool_call_round_trip(self):
        calls = []
        t = FakeTransport(
            tool_turn(("tu_1", "echo", {"q": "x"})),
            text_turn("done"),
        )
        self.assertEqual(self.run_with(t, [echo_tool(calls)]), "done")
        self.assertEqual(calls, [{"q": "x"}])
        # Second request carries: user, assistant echo, tool_result user turn.
        msgs = t.bodies[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[2]["content"][0]["tool_use_id"], "tu_1")

    def test_parallel_tool_results_go_in_one_user_message(self):
        # Splitting them across messages trains the model out of parallel calls.
        calls = []
        t = FakeTransport(
            tool_turn(("tu_1", "echo", {"n": 1}), ("tu_2", "echo", {"n": 2})),
            text_turn("both"),
        )
        self.assertEqual(self.run_with(t, [echo_tool(calls)]), "both")
        msgs = t.bodies[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(len(msgs[2]["content"]), 2)
        self.assertEqual([c["tool_use_id"] for c in msgs[2]["content"]], ["tu_1", "tu_2"])

    def test_assistant_turn_is_echoed_verbatim(self):
        # Thinking blocks must survive untouched or the next turn 400s.
        assistant = {
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                {"type": "tool_use", "id": "tu_1", "name": "echo", "input": {}},
            ],
            "stop_reason": "tool_use",
        }
        t = FakeTransport(assistant, text_turn("fine"))
        self.run_with(t, [echo_tool([])])
        self.assertEqual(t.bodies[1]["messages"][1]["content"], assistant["content"])

    def test_raising_tool_becomes_an_error_result(self):
        def boom(_):
            raise ValueError("nope")

        t = FakeTransport(
            tool_turn(("tu_1", "explode", {})),
            text_turn("recovered"),
        )
        tool = Tool("explode", "boom", {"type": "object", "properties": {}}, boom)
        self.assertEqual(self.run_with(t, [tool]), "recovered")
        result = t.bodies[1]["messages"][2]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("nope", result["content"])

    def test_unknown_tool_is_reported_not_raised(self):
        t = FakeTransport(tool_turn(("tu_1", "ghost", {})), text_turn("ok"))
        self.assertEqual(self.run_with(t), "ok")
        self.assertTrue(t.bodies[1]["messages"][2]["content"][0]["is_error"])

    def test_refusal_raises(self):
        t = FakeTransport({"content": [], "stop_reason": "refusal"})
        with self.assertRaisesRegex(AgentError, "declined"):
            self.run_with(t)

    def test_transport_error_raises_agent_error(self):
        t = FakeTransport(ApiError(500, "u", "boom"))
        with self.assertRaisesRegex(AgentError, "failed"):
            self.run_with(t)

    def test_turn_cap_returns_last_text_instead_of_spinning(self):
        loops = [
            {"content": [{"type": "text", "text": "working"},
                         {"type": "tool_use", "id": "t", "name": "echo", "input": {}}],
             "stop_reason": "tool_use"}
            for _ in range(3)
        ]
        t = FakeTransport(*loops)
        out = run_agent(api_key="k", system="s", user_message="u",
                        tools=[echo_tool([])], transport=t, max_turns=3)
        self.assertEqual(out, "working")
        self.assertEqual(len(t.bodies), 3)

    def test_tools_are_advertised_in_the_request(self):
        t = FakeTransport(text_turn("hi"))
        self.run_with(t, [echo_tool([])])
        self.assertEqual([s["name"] for s in t.bodies[0]["tools"]], ["echo"])


if __name__ == "__main__":
    unittest.main()
