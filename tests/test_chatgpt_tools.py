import json
import unittest
from unittest.mock import Mock, patch

from test_agent_loop import AllowAll, FakeTools
from test_chat import wire_lines
from zanbot.agent import run_turn
from zanbot.chatgpt import read_response, to_input, to_tools
from zanbot.config import Config
from zanbot.llm import APIError
from zanbot.prompts import NO_TOOLS, TOOL_GUIDE
from zanbot.session import ChatSession

CONFIG = Config(api_key="", model="test", provider="chatgpt")


def call_item(arguments='{"path": "."}', call_id="call_1", name="list_dir"):
    return {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name, "arguments": arguments,
            "status": "completed"}


def message_item(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-opaque"}


def stream(*items, text=None):
    """The events of one response: optional streamed text, then each finished item, then completion."""
    events = [{"type": "response.output_text.delta", "delta": text}] if text else []
    events += [{"type": "response.output_item.done", "output_index": i, "item": item} for i, item in enumerate(items)]
    events.append({"type": "response.completed", "response": {"status": "completed", "output": list(items)}})
    return events


def posted(*streams):
    """A mock requests.post returning one streamed response per call."""
    contexts = []
    for events in streams:
        response = Mock(ok=True)
        response.iter_lines.return_value = wire_lines(events)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        contexts.append(context)
    return Mock(side_effect=contexts)


class ReadResponseTests(unittest.TestCase):
    def test_function_call_becomes_tool_use(self):
        result = read_response(wire_lines(stream(message_item("Looking."), call_item())))
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(result["content"], [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "call_1", "name": "list_dir", "input": {"path": "."}},
        ])

    def test_parameterless_call_and_reasoning_are_kept(self):
        result = read_response(wire_lines(stream(REASONING, call_item(arguments=""))))
        self.assertEqual(result["content"], [
            {"type": "reasoning", "item": REASONING},
            {"type": "tool_use", "id": "call_1", "name": "list_dir", "input": {}},
        ])

    def test_invalid_arguments_are_rejected(self):
        for arguments in ('{"path": ', "[1, 2]"):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "tool input"):
                read_response(wire_lines(stream(call_item(arguments=arguments))))

    def test_text_only_answer_is_unchanged(self):
        result = read_response(wire_lines(stream(message_item("Hi"))))
        self.assertEqual((result["content"], result["stop_reason"]), ([{"type": "text", "text": "Hi"}], "end_turn"))

    def test_reasoning_alone_is_not_an_answer(self):
        with self.assertRaises(ValueError):
            read_response(wire_lines(stream(REASONING)))


class ConversionTests(unittest.TestCase):
    def test_history_becomes_responses_input(self):
        history = [
            {"role": "user", "content": "列出笔记"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "", "signature": "anthropic-only"},
                {"type": "reasoning", "item": REASONING},
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "call_1", "name": "list_dir", "input": {"path": "."}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "<tool_output>\na.md\n</tool_output>"},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "No folder.", "is_error": True},
                {"type": "text", "text": "[Zanbot] limit reached"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ]
        self.assertEqual(to_input(history), [
            {"role": "user", "content": "列出笔记"},
            REASONING,
            {"role": "assistant", "content": "Looking."},
            {"type": "function_call", "call_id": "call_1", "name": "list_dir", "arguments": '{"path": "."}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "<tool_output>\na.md\n</tool_output>"},
            {"type": "function_call_output", "call_id": "call_2", "output": "Error: No folder."},
            {"role": "user", "content": "[Zanbot] limit reached"},
            {"role": "assistant", "content": "Done."},
        ])

    def test_tool_definitions(self):
        schema = {"type": "object", "properties": {}}
        self.assertEqual(to_tools([{"name": "list_dir", "description": "List.", "input_schema": schema}]),
                         [{"type": "function", "name": "list_dir", "description": "List.", "parameters": schema,
                           "strict": False}])


@patch("zanbot.chatgpt.get_access_token", return_value="token")
class LoopTests(unittest.TestCase):
    def test_tool_call_round_trip(self, token):
        post = posted(stream(REASONING, call_item()), stream(message_item("You have a.md."), text="You have a.md."))
        tools = FakeTools({"list_dir": ("a.md", False)})
        history, chunks = [], []
        with patch("zanbot.chatgpt.requests.post", post):
            answer = run_turn(history, "列出笔记", CONFIG, chunks.append, tools=tools, hooks=AllowAll())
        self.assertEqual(answer, ("You have a.md.", False))
        self.assertEqual(tools.calls, [("list_dir", {"path": "."})])
        first, second = (c.kwargs["json"] for c in post.call_args_list)
        self.assertEqual(first["tools"][0]["name"], "list_dir")
        self.assertEqual(first["include"], ["reasoning.encrypted_content"])
        self.assertNotIn("tool_choice", first)
        types = [item.get("type", "message") for item in second["input"]]
        self.assertEqual(types, ["message", "reasoning", "function_call", "function_call_output"])
        self.assertIn("a.md", second["input"][-1]["output"])

    def test_final_step_forbids_tools(self, token):
        from zanbot import agent

        post = posted(stream(call_item()), stream(message_item("Here is what I found.")))
        with patch("zanbot.chatgpt.requests.post", post), patch.object(agent, "MAX_STEPS", 1):
            run_turn([], "Go", CONFIG, tools=FakeTools())
        self.assertEqual(post.call_args.kwargs["json"]["tool_choice"], "none")

    def test_without_tools_the_request_is_unchanged(self, token):
        post = posted(stream(message_item("Hi")))
        with patch("zanbot.chatgpt.requests.post", post):
            run_turn([], "Hi", CONFIG)
        body = post.call_args.kwargs["json"]
        for key in ("tools", "tool_choice", "include"):
            self.assertNotIn(key, body)

    def test_session_offers_tools_to_chatgpt(self, token):
        post = posted(stream(message_item("Hi")))
        session = ChatSession(CONFIG, lambda event: None, tools=FakeTools())
        with patch("zanbot.chatgpt.requests.post", post):
            session.submit("Hi")
        body = post.call_args.kwargs["json"]
        self.assertIn("tools", body)
        self.assertIn(TOOL_GUIDE, body["instructions"])
        self.assertNotIn(NO_TOOLS, body["instructions"])


if __name__ == "__main__":
    unittest.main()
