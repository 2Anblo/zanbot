import json
import unittest
from unittest.mock import Mock, patch

import requests

from main import main
from zanbot.agent import run_turn
from zanbot.config import Config, load_config
from zanbot.llm import APIError


def events_for(text="你好", stop="end_turn"):
    return [
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop}},
        {"type": "message_stop"},
    ]


def wire_lines(events):
    for event in events:
        yield f"event: {event['type']}".encode()
        yield ("data: " + json.dumps(event, ensure_ascii=False)).encode("utf-8")
        yield b""


def reply(text="你好", stop="end_turn"):
    return Mock(ok=True, iter_lines=Mock(return_value=wire_lines(events_for(text, stop))))


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(api_key="test-key")

    @patch("zanbot.llm.requests.post")
    def test_second_turn_sends_history(self, post):
        post.side_effect = [reply("记住了"), reply("小明")]
        history = []
        run_turn(history, "我叫小明", self.config)
        text, truncated = run_turn(history, "我叫什么？", self.config)
        sent = post.call_args.kwargs["json"]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[0]["content"], "我叫小明")
        self.assertEqual(sent[1]["content"][0]["text"], "记住了")
        self.assertEqual(text, "小明")
        self.assertFalse(truncated)
        self.assertEqual(len(history), 4)

    @patch("zanbot.llm.requests.post")
    def test_failures_preserve_history(self, post):
        history = [{"role": "user", "content": "你好"},
                   {"role": "assistant", "content": "你好"}]
        original = list(history)
        for result in [requests.Timeout(), Mock(ok=False, status_code=401),
                       Mock(ok=True, iter_lines=Mock(return_value=iter([]))),
                       Mock(ok=True, iter_lines=Mock(return_value=iter([b"data: invalid", b""])))]:
            with self.subTest(result=result):
                post.side_effect = result if isinstance(result, Exception) else None
                post.return_value = result
                with self.assertRaises(APIError):
                    run_turn(history, "失败的输入", self.config)
                self.assertEqual(history, original)

    @patch("zanbot.llm.requests.post", return_value=reply(stop="max_tokens"))
    def test_truncation_is_reported(self, post):
        self.assertTrue(run_turn([], "你好", self.config)[1])

    @patch("builtins.print")
    @patch("builtins.input", side_effect=["", "第一轮", "/clear", "新对话", "/exit"])
    @patch("main.load_config")
    @patch("zanbot.llm.requests.post")
    def test_cli_clear_and_exit(self, post, config, input_mock, print_mock):
        config.return_value = self.config
        post.side_effect = [reply(), reply()]
        self.assertEqual(main(["--no-memory"]), 0)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["json"]["messages"],
                         [{"role": "user", "content": "新对话"}])

    @patch("builtins.print")
    @patch("builtins.input", side_effect=["失败", "重试", "/exit"])
    @patch("main.load_config")
    @patch("zanbot.llm.requests.post")
    def test_cli_continues_after_failure(self, post, config, input_mock, print_mock):
        config.return_value = self.config
        post.side_effect = [requests.ConnectionError(), reply()]
        self.assertEqual(main(["--no-memory"]), 0)
        self.assertEqual(post.call_args.kwargs["json"]["messages"],
                         [{"role": "user", "content": "重试"}])

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_key_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
            load_config()


if __name__ == "__main__":
    unittest.main()
