from datetime import date
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from test_chat import reply, wire_lines
from zanbot.agent import run_turn
from zanbot.config import Config
from zanbot.llm import APIError
from zanbot.prompts import MEMORY_GUIDE, MEMORY_OFF, MEMORY_PAUSED, build_system_prompt
from zanbot.session import ChatSession


def status(code):
    return Mock(ok=False, status_code=code)


@patch("zanbot.llm.time.sleep")
@patch("zanbot.llm.requests.post")
class AnthropicRetryTests(unittest.TestCase):
    config = Config(api_key="test-key")

    def test_transient_status_is_retried_once(self, post, sleep):
        for code in (502, 503, 504, 529):
            with self.subTest(code=code):
                post.reset_mock()
                first = status(code)
                post.side_effect = [first, reply("好了")]
                self.assertEqual(run_turn([], "你好", self.config)[0], "好了")
                self.assertEqual(post.call_count, 2)
                first.close.assert_called_once()
                sleep.assert_called_with(1.5)

    def test_second_failure_reports_the_retry(self, post, sleep):
        post.side_effect = [status(504), status(504)]
        with self.assertRaisesRegex(APIError, "HTTP 504: Anthropic is temporarily unavailable.*retried once"):
            run_turn([], "你好", self.config)
        self.assertEqual(post.call_count, 2)

    def test_other_errors_are_not_retried(self, post, sleep):
        for code, message in [(401, "Invalid API key"), (500, "Anthropic had an internal error"),
                              (400, "Check your model")]:
            with self.subTest(code=code):
                post.reset_mock()
                post.side_effect = [status(code)]
                with self.assertRaisesRegex(APIError, message):
                    run_turn([], "你好", self.config)
                self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_network_error_on_retry_is_reported(self, post, sleep):
        post.side_effect = [status(503), requests.ConnectionError()]
        with self.assertRaisesRegex(APIError, "network request failed"):
            run_turn([], "你好", self.config)


def chatgpt_context(response):
    context = MagicMock(status_code=response.status_code)
    context.__enter__.return_value = response
    return context


@patch("zanbot.llm.time.sleep")
@patch("zanbot.chatgpt.record_chatgpt_usage")
@patch("zanbot.chatgpt.get_access_token", return_value="test-oauth-token")
@patch("zanbot.chatgpt.requests.post")
class ChatGPTRetryTests(unittest.TestCase):
    config = Config(api_key="", model="m", provider="chatgpt")

    def test_gateway_timeout_is_retried_once(self, post, token, usage, sleep):
        done = Mock(ok=True, status_code=200, iter_lines=Mock(return_value=wire_lines([
            {"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "好了"}]}]}}])))
        post.side_effect = [chatgpt_context(status(504)), chatgpt_context(done)]
        self.assertEqual(run_turn([], "你好", self.config)[0], "好了")
        self.assertEqual(post.call_count, 2)

    def test_second_failure_has_a_clear_hint(self, post, token, usage, sleep):
        post.side_effect = [chatgpt_context(status(504)), chatgpt_context(status(504))]
        with self.assertRaises(APIError) as raised:
            run_turn([], "你好", self.config)
        self.assertRegex(str(raised.exception), "ChatGPT HTTP 504: ChatGPT is temporarily unavailable.*retried")
        self.assertNotIn("selected model", str(raised.exception))
        self.assertEqual(post.call_count, 2)


class MemoryPromptTests(unittest.TestCase):
    today = date(2026, 10, 18)

    def test_each_memory_state(self):
        on = build_system_prompt([], self.today)
        self.assertIn(MEMORY_GUIDE, on)
        self.assertIn("never say that you have remembered", on)
        self.assertIn("/clear only starts a new conversation", on)
        self.assertNotIn(MEMORY_PAUSED, on)
        self.assertIn(MEMORY_PAUSED, build_system_prompt([], self.today, "paused"))
        off = build_system_prompt([], self.today, "off")
        self.assertIn(MEMORY_OFF, off)
        self.assertNotIn(MEMORY_GUIDE, off)

    def test_session_reports_its_memory_state(self):
        def state(store, worker):
            prompt = ChatSession(Config(api_key="k"), lambda e: None, store, worker).system_prompt()
            return "off" if MEMORY_OFF in prompt else "paused" if MEMORY_PAUSED in prompt else "on"

        store = Mock(recall=Mock(return_value=[]))
        self.assertEqual(state(None, None), "off")
        self.assertEqual(state(store, Mock(enabled=True)), "on")
        self.assertEqual(state(store, Mock(enabled=False)), "paused")


if __name__ == "__main__":
    unittest.main()
