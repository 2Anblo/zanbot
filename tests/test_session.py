from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from test_chat import reply
from zanbot.commands import COMMANDS
from zanbot.config import Config
from zanbot.memory import MemoryStore
from zanbot.session import (ChatSession, Cleared, ExitRequested, Info, MemoryNotice, ReplyFailed,
                            ReplyFinished, ReplyStarted, TextDelta)


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.session = ChatSession(Config(api_key="test-key"), self.events.append)

    def info_text(self):
        return "\n".join(line for event in self.events if isinstance(event, Info) for line in event.lines)


@patch("zanbot.llm.requests.post")
class ReplyTests(SessionTestCase):
    def test_reply_events_and_history(self, post):
        post.return_value = reply("你好呀")
        self.session.submit("  你好  ")
        self.assertEqual(self.events, [ReplyStarted(), TextDelta("你好呀"), ReplyFinished("你好呀", False)])
        self.assertEqual(post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "你好"}])
        self.assertEqual(len(self.session.messages), 2)

    def test_failure_emits_event_and_keeps_history(self, post):
        post.side_effect = requests.ConnectionError()
        self.session.submit("你好")
        self.assertEqual(self.events[0], ReplyStarted())
        self.assertIsInstance(self.events[1], ReplyFailed)
        self.assertEqual(self.session.messages, [])

    def test_blank_input_is_ignored(self, post):
        self.session.submit("   ")
        self.assertEqual(self.events, [])
        post.assert_not_called()

    def test_leading_space_sends_slash_text_to_model(self, post):
        post.return_value = reply()
        self.session.submit(" /etc/hosts 是什么文件")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"], "/etc/hosts 是什么文件")

    def test_successful_turn_is_submitted_for_extraction(self, post):
        post.return_value = reply("答案")
        worker = Mock()
        session = ChatSession(Config(api_key="test-key"), self.events.append, store=Mock(recall=Mock(return_value=[])),
                              worker=worker)
        session.submit("问题")
        worker.submit.assert_called_once_with(session.config, "问题", "答案")


class CommandTests(SessionTestCase):
    @patch("zanbot.llm.requests.post")
    def test_unknown_command_is_not_sent_and_suggests(self, post):
        self.session.submit("/memroy")
        post.assert_not_called()
        self.assertIn("Unknown command /memroy. Did you mean /memory?", self.info_text())
        self.events.clear()
        self.session.submit("/zzz")
        self.assertIn("Unknown command /zzz. Type /help", self.info_text())

    def test_help_lists_every_registered_command(self):
        self.session.submit("/help")
        text = self.info_text()
        for command in COMMANDS:
            self.assertIn(command.name, text)
            self.assertIn(command.help, text)
        self.assertIn("/forget <id>", text)

    def test_clear_resets_history_and_session_id(self):
        self.session.messages.append({"role": "user", "content": "旧"})
        old_id = self.session.config.session_id
        self.session.submit("/CLEAR")
        self.assertEqual(self.session.messages, [])
        self.assertNotEqual(self.session.config.session_id, old_id)
        self.assertEqual(self.events, [Cleared()])

    def test_exit_is_an_event(self):
        self.session.submit("/exit")
        self.assertEqual(self.events, [ExitRequested()])

    def test_memory_commands_without_store(self):
        for text in ["/memory", "/remember 用户养猫", "/forget 1"]:
            self.session.submit(text)
        self.assertEqual(self.info_text().splitlines(), ["Memory is off for this session."] * 3)

    def test_memory_commands_with_store(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Mock(wait=Mock(return_value=True), take_notices=Mock(return_value=["  ✓ Remembered #9: x"]))
            session = ChatSession(Config(api_key="k"), self.events.append,
                                  store=MemoryStore(Path(directory) / "memory.db"), worker=worker)
            session.submit("/remember profile 用户名叫 Zhanbo")
            session.submit("/memory")
            session.submit("/memory off")
        self.assertIn(MemoryNotice("  ✓ Remembered #9: x"), self.events)
        text = self.info_text()
        self.assertIn("Remembered #1.", text)
        self.assertIn("#1 [profile] 用户名叫 Zhanbo", text)
        self.assertIn("Automatic memory is paused for this session.", text)
        self.assertFalse(worker.enabled)


class CloseTests(SessionTestCase):
    def test_close_reports_pending_and_unfinished_work(self):
        worker = Mock(wait=Mock(return_value=False), close=Mock(return_value=False),
                      take_notices=Mock(return_value=[]))
        ChatSession(Config(api_key="k"), self.events.append, worker=worker).close(10)
        self.assertIn("Saving memories from the latest turn...", self.info_text())
        self.assertIn("Memory extraction did not finish", self.info_text())
        worker.close.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
