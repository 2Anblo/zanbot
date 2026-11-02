import io
import unittest
from unittest.mock import Mock, patch

import requests

from test_chat import events_for, reply, wire_lines
from zanbot.agent import run_turn
from zanbot.config import Config
from zanbot.llm import APIError
from zanbot.streaming import iter_events, read_message


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(api_key="test-key")

    @patch("zanbot.llm.requests.post")
    def test_text_is_delivered_before_stream_finishes(self, post):
        chunks = []
        history = []
        events = events_for("Hello")

        def lines():
            yield from wire_lines(events[:3])
            self.assertEqual(chunks, ["Hello"])
            self.assertEqual(history, [])
            yield from wire_lines(events[3:])

        response = Mock(ok=True, iter_lines=Mock(return_value=lines()))
        post.return_value = response
        self.assertEqual(run_turn(history, "Hi", self.config, chunks.append), ("Hello", False))
        self.assertEqual(len(history), 2)
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        response.close.assert_called_once()

    @patch("zanbot.llm.requests.post")
    def test_utf8_decoding_with_real_requests_line_iterator(self, post):
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(b"\n".join(wire_lines(events_for("你好🌍"))) + b"\n")
        post.return_value = response
        chunks = []
        self.assertEqual(run_turn([], "Hello", self.config, chunks.append)[0], "你好🌍")
        self.assertEqual(chunks, ["你好🌍"])
        self.assertTrue(response.raw.closed)

    def test_multiline_sse_and_comments(self):
        events = list(iter_events(iter([
            b": keepalive", b"event: ping", b'data: {"type":', b'data: "ping"}', b"",
        ])))
        self.assertEqual(events, [{"type": "ping"}])

    def test_multiple_blocks_and_unknown_events(self):
        first = events_for("First")
        second = events_for("Second")
        for event in second[1:4]:
            event["index"] = 1
        events = [*first[:4], {"type": "ping"}, {"type": "future_event"},
                  *second[1:]]
        chunks = []
        result = read_message(wire_lines(events), chunks.append)
        self.assertEqual("".join(chunks), "First\nSecond")
        self.assertEqual([block["text"] for block in result["content"]], ["First", "Second"])

    @patch("zanbot.llm.requests.post")
    def test_interrupted_stream_never_saves_partial_history(self, post):
        endings = [None, requests.ConnectionError(), requests.Timeout(),
                   requests.exceptions.ChunkedEncodingError(),
                   {"type": "error", "error": {"type": "overloaded_error"}}]
        for ending in endings:
            with self.subTest(ending=ending):
                chunks = []
                history = []

                def lines():
                    yield from wire_lines(events_for("Partial")[:3])
                    if isinstance(ending, Exception):
                        raise ending
                    if ending is not None:
                        yield from wire_lines([ending])

                response = Mock(ok=True, iter_lines=Mock(return_value=lines()))
                post.return_value = response
                with self.assertRaises(APIError):
                    run_turn(history, "Hello", self.config, chunks.append)
                self.assertEqual(chunks, ["Partial"])
                self.assertEqual(history, [])
                response.close.assert_called_once()

    @patch("zanbot.llm.requests.post")
    def test_ctrl_c_closes_response_and_preserves_history(self, post):
        def lines():
            yield from wire_lines(events_for("Partial")[:3])
            raise KeyboardInterrupt

        response = Mock(ok=True, iter_lines=Mock(return_value=lines()))
        post.return_value = response
        history = []
        with self.assertRaises(KeyboardInterrupt):
            run_turn(history, "Hello", self.config)
        self.assertEqual(history, [])
        response.close.assert_called_once()

    @patch("zanbot.llm.requests.post")
    def test_http_error_closes_response(self, post):
        response = Mock(ok=False, status_code=401)
        post.return_value = response
        with self.assertRaisesRegex(APIError, "Invalid API key"):
            run_turn([], "Hello", self.config)
        response.close.assert_called_once()
        response.iter_lines.assert_not_called()

    def test_premature_message_stop_is_rejected(self):
        events = events_for()[:3] + [{"type": "message_stop"}]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            read_message(wire_lines(events))

    @patch("builtins.print")
    @patch("builtins.input", side_effect=["Hello", "/exit"])
    @patch("main.load_config")
    @patch("zanbot.llm.requests.post")
    def test_cli_flushes_chunks_without_printing_full_answer_again(self, post, config, input_mock, output):
        from main import main

        config.return_value = self.config
        post.return_value = reply("Unique reply")
        self.assertEqual(main(["--no-memory", "--plain"]), 0)
        output.assert_any_call("Unique reply", end="", flush=True)
        matching = [call for call in output.call_args_list if "Unique reply" in str(call)]
        self.assertEqual(len(matching), 1)


def message(*blocks, stop="end_turn"):
    """Build a full event stream from (start_block, [deltas]) pairs."""
    events = [{"type": "message_start", "message": {"role": "assistant", "content": []}}]
    for index, (start, deltas) in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": d} for d in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop}}, {"type": "message_stop"}]
    return events


def text_block(text):
    return {"type": "text", "text": ""}, [{"type": "text_delta", "text": text}]


def thinking_block(thinking="", signature="sig"):
    deltas = [{"type": "thinking_delta", "thinking": thinking}] if thinking else []
    return {"type": "thinking", "thinking": "", "signature": ""}, [*deltas, {"type": "signature_delta", "signature": signature}]


def tool_block(*fragments, name="list_dir", id="toolu_1"):
    return ({"type": "tool_use", "id": id, "name": name, "input": {}},
            [{"type": "input_json_delta", "partial_json": f} for f in fragments])


class BlockParsingTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(api_key="test-key")

    def test_thinking_is_kept_verbatim_and_not_shown(self):
        chunks = []
        result = read_message(wire_lines(message(thinking_block("plan", "abc"), text_block("Hi"))), chunks.append)
        self.assertEqual(result["content"], [
            {"type": "thinking", "thinking": "plan", "signature": "abc"},
            {"type": "text", "text": "Hi"},
        ])
        # No separator newline before the first visible text block.
        self.assertEqual(chunks, ["Hi"])

    def test_redacted_thinking_is_kept_verbatim(self):
        redacted = {"type": "redacted_thinking", "data": "opaque"}
        result = read_message(wire_lines(message((redacted, []), text_block("Hi"))))
        self.assertEqual(result["content"][0], redacted)

    def test_tool_use_input_is_joined_from_fragments(self):
        events = message(text_block("Looking."), tool_block('{"pa', 'th": "no', 'tes"}'), stop="tool_use")
        result = read_message(wire_lines(events))
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(result["content"][1],
                         {"type": "tool_use", "id": "toolu_1", "name": "list_dir", "input": {"path": "notes"}})

    def test_tool_use_without_input_fragments_has_empty_input(self):
        result = read_message(wire_lines(message(tool_block(), stop="tool_use")))
        self.assertEqual(result["content"][0]["input"], {})

    def test_several_tool_uses_in_one_message(self):
        events = message(tool_block('{"a": 1}', id="t1"), tool_block('{"b": 2}', name="write_note", id="t2"),
                         stop="tool_use")
        content = read_message(wire_lines(events))["content"]
        self.assertEqual([(b["id"], b["name"], b["input"]) for b in content],
                         [("t1", "list_dir", {"a": 1}), ("t2", "write_note", {"b": 2})])

    def test_on_event_reports_tool_use_start(self):
        seen = []
        read_message(wire_lines(message(tool_block("{}"), stop="tool_use")), on_event=seen.append)
        self.assertEqual(seen, [{"type": "tool_use_start", "id": "toolu_1", "name": "list_dir"}])

    def test_parameterless_tool_sends_one_empty_fragment(self):
        # Seen from the real API: a tool without parameters streams partial_json "" once.
        result = read_message(wire_lines(message(tool_block(""), stop="tool_use")))
        self.assertEqual(result["content"][0]["input"], {})

    def test_invalid_tool_input_is_rejected(self):
        for fragments in (['{"path": '], ['["not", "an", "object"]']):
            with self.subTest(fragments=fragments):
                with self.assertRaisesRegex(ValueError, "invalid tool input"):
                    read_message(wire_lines(message(tool_block(*fragments), stop="tool_use")))

    def test_tool_input_cut_off_by_max_tokens_is_reported_as_truncation(self):
        events = message(tool_block('{"path": "no'), stop="max_tokens")
        with self.assertRaisesRegex(ValueError, "max_tokens"):
            read_message(wire_lines(events))

    def test_tool_use_without_id_or_name_is_rejected(self):
        start = {"type": "tool_use", "id": "", "name": "list_dir", "input": {}}
        with self.assertRaisesRegex(ValueError, "tool"):
            read_message(wire_lines(message((start, []), stop="tool_use")))

    def test_delta_of_wrong_kind_for_block_is_rejected(self):
        bad = ({"type": "text", "text": ""}, [{"type": "input_json_delta", "partial_json": "{}"}])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            read_message(wire_lines(message(bad)))

    def test_unknown_block_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "server_tool_use"):
            read_message(wire_lines(message(({"type": "server_tool_use", "id": "x", "name": "y"}, []))))

    @patch("zanbot.llm.requests.post")
    def test_run_turn_accepts_thinking_and_keeps_it_in_history(self, post):
        events = message(thinking_block("plan"), text_block("Answer"))
        post.return_value = Mock(ok=True, iter_lines=Mock(return_value=wire_lines(events)))
        history = []
        self.assertEqual(run_turn(history, "Hi", self.config), ("Answer", False))
        self.assertEqual([b["type"] for b in history[1]["content"]], ["thinking", "text"])

    @patch("zanbot.llm.requests.post")
    def test_run_turn_rejects_tool_use_when_no_tools_were_offered(self, post):
        events = message(tool_block("{}"), stop="tool_use")
        post.return_value = Mock(ok=True, iter_lines=Mock(return_value=wire_lines(events)))
        history = []
        with self.assertRaises(APIError):
            run_turn(history, "Hi", self.config)
        self.assertEqual(history, [])

    @patch("zanbot.llm.requests.post")
    def test_run_turn_without_any_text_is_an_error(self, post):
        events = message(thinking_block("plan"), stop="max_tokens")
        post.return_value = Mock(ok=True, iter_lines=Mock(return_value=wire_lines(events)))
        with self.assertRaisesRegex(APIError, "no text"):
            run_turn([], "Hi", self.config)


if __name__ == "__main__":
    unittest.main()
