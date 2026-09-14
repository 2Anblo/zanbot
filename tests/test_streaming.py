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
        self.assertEqual(main(), 0)
        output.assert_any_call("Unique reply", end="", flush=True)
        matching = [call for call in output.call_args_list if "Unique reply" in str(call)]
        self.assertEqual(len(matching), 1)


if __name__ == "__main__":
    unittest.main()
