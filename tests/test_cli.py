import argparse
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from main import choose_terminal
from test_chat import reply
from zanbot.cli import InteractiveTerminal, PlainTerminal, format_status, windows_console_input
from zanbot.commands import COMMANDS, complete
from zanbot.config import Config
from zanbot.memory import MemoryStore
from zanbot.memory_extract import MemoryWorker
from zanbot.session import (ChatSession, MemoryNotice, ReplyFailed, ReplyFinished, ReplyStarted, Status,
                            StatusChanged)


def texts(result):
    start, suggestions = result
    return start, [s.text for s in suggestions]


class CompletionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(Path(directory.name) / "memory.db")
        self.session = ChatSession(Config(api_key="k"), lambda event: None, store=self.store)

    def test_command_names_with_help(self):
        start, suggestions = complete(self.session, "/")
        self.assertEqual(start, 0)
        self.assertEqual([s.text for s in suggestions], [c.name for c in COMMANDS])
        self.assertIn("Delete a memory", next(s.meta for s in suggestions if s.text == "/forget"))
        self.assertEqual(texts(complete(self.session, "/ME")), (0, ["/memory"]))

    def test_first_argument_suggestions(self):
        self.assertEqual(texts(complete(self.session, "/memory o")), (8, ["on", "off"]))
        start, kinds = texts(complete(self.session, "/remember p"))
        self.assertEqual((start, kinds), (10, ["profile", "preference", "project"]))

    def test_forget_suggests_ids_with_preview(self):
        first = self.store.add("用户养了一只猫")
        self.store.add("长" * 50)
        start, suggestions = complete(self.session, "/forget ")
        self.assertEqual(start, 8)
        self.assertEqual([s.text for s in suggestions], [str(first + 1), str(first)])
        self.assertEqual(suggestions[0].meta, "长" * 40 + "…")
        self.assertEqual(suggestions[1].meta, "用户养了一只猫")

    def test_no_suggestions_for_free_text_or_messages(self):
        for text in ["你好", "/remember profile 用", "/clear x", "/unknown a"]:
            with self.subTest(text=text):
                self.assertEqual(complete(self.session, text), (len(text), []))

    def test_memory_suggestions_need_a_working_store(self):
        no_memory = ChatSession(Config(api_key="k"), lambda event: None)
        self.assertEqual(complete(no_memory, "/forget "), (8, []))
        broken = Mock(active=Mock(side_effect=sqlite3.OperationalError("locked")))
        self.assertEqual(complete(ChatSession(Config(api_key="k"), lambda e: None, store=broken), "/forget "), (8, []))


class StatusTests(unittest.TestCase):
    def test_format_status(self):
        self.assertEqual(format_status(Status("chatgpt", "m", 3, True, True)),
                         " chatgpt · m · 3 memories · auto-memory on · saving…    /help for commands")
        self.assertEqual(format_status(Status("anthropic", "m", 0, False, False)),
                         " anthropic · m · 0 memories · auto-memory paused    /help for commands")
        self.assertEqual(format_status(Status("anthropic", "m", None, False, False)),
                         " anthropic · m · memory off    /help for commands")

    def test_worker_listener_receives_notices_and_busy_clears(self):
        updates = []
        worker = MemoryWorker(None, extract_fn=lambda *job: [])
        worker.listener = updates.append
        worker.submit("c", "u", "a")
        self.assertTrue(worker.close(5))
        self.assertEqual(updates, [[]])
        self.assertFalse(worker.busy)
        self.assertEqual(worker.take_notices(), [])

    def test_live_notices_emit_events(self):
        events = []
        worker = Mock()
        session = ChatSession(Config(api_key="k"), events.append, worker=worker)
        session.live_notices()
        worker.listener(["  ✓ Remembered #1: x"])
        self.assertEqual(events, [MemoryNotice("  ✓ Remembered #1: x"), StatusChanged()])


class ChooseTerminalTests(unittest.TestCase):
    def args(self, **values):
        return argparse.Namespace(**{"plain": False, "no_history": False, **values})

    @patch("main.sys")
    def test_selection(self, sys_mock):
        sys_mock.stdin.isatty.return_value = sys_mock.stdout.isatty.return_value = True
        with patch("main.memory_directory", return_value=Path("data")):
            terminal = choose_terminal(self.args())
            self.assertIsInstance(terminal, InteractiveTerminal)
            self.assertEqual(terminal.history_path, Path("data") / "history.txt")
            self.assertIsNone(choose_terminal(self.args(no_history=True)).history_path)
        self.assertIs(type(choose_terminal(self.args(plain=True))), PlainTerminal)
        sys_mock.stdout.isatty.return_value = False
        self.assertIs(type(choose_terminal(self.args())), PlainTerminal)


@patch("builtins.print")
@patch("zanbot.llm.requests.post")
class InteractiveTerminalTests(unittest.TestCase):
    def run_terminal(self, keys, history_path=None):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            terminal = InteractiveTerminal(history_path, input=pipe, output=DummyOutput())
            self.assertEqual(terminal.run(Config(api_key="test-key")), 0)
        return terminal

    def test_sends_messages_and_exits(self, post, print_mock):
        post.return_value = reply("好的")
        self.run_terminal("你好\r/exit\r")
        self.assertEqual(post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "你好"}])
        print_mock.assert_any_call("好的", end="", flush=True)

    def test_meta_enter_inserts_newline(self, post, print_mock):
        post.return_value = reply()
        self.run_terminal("第一行\x1b\r第二行\r/exit\r")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"], "第一行\n第二行")

    def test_legacy_ctrl_enter_sequence_inserts_newline(self, post, print_mock):
        # The legacy Windows reader reports Ctrl+Enter as Escape followed by Ctrl+J.
        post.return_value = reply()
        self.run_terminal("第一行\x1b\n第二行\r/exit\r")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"], "第一行\n第二行")

    def test_ctrl_c_clears_typed_text(self, post, print_mock):
        post.return_value = reply()
        self.run_terminal("草稿\x03你好\r/exit\r")
        self.assertEqual([c.kwargs["json"]["messages"][-1]["content"] for c in post.call_args_list], ["你好"])

    def test_two_ctrl_c_on_an_empty_line_exit(self, post, print_mock):
        terminal = self.run_terminal("\x03\x03")
        post.assert_not_called()
        self.assertFalse(terminal.exit_requested)  # Exited by Ctrl+C, not /exit.
        print_mock.assert_any_call("\nGoodbye!")

    def test_ctrl_c_presses_must_be_consecutive(self, post, print_mock):
        # Empty line press, typing, clearing, then one empty-line press: not an exit yet.
        post.return_value = reply()
        self.run_terminal("\x03x\x03\x03你好\r/exit\r")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][-1]["content"], "你好")

    def test_cancelled_reply_message(self, post, print_mock):
        PlainTerminal().render(ReplyFailed("Cancelled.", cancelled=True))
        print_mock.assert_called_once_with("\n(Cancelled. This turn was not saved.)")

    def test_history_is_saved_to_file(self, post, print_mock):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "history.txt"
            self.run_terminal("/help\r/exit\r", history_path=path)
            self.assertIn("+/help", path.read_text(encoding="utf-8"))

    def test_notices_wait_for_streaming_reply(self, post, print_mock):
        terminal = InteractiveTerminal()
        terminal.render(ReplyStarted())
        terminal.render(MemoryNotice("  ✓ Remembered #1: x"))
        self.assertNotIn(unittest.mock.call("  ✓ Remembered #1: x"), print_mock.call_args_list)
        terminal.render(ReplyFinished("答案", False))
        self.assertEqual(print_mock.call_args_list[-1], unittest.mock.call("  ✓ Remembered #1: x"))

    def test_falls_back_to_plain_input(self, post, print_mock):
        with patch.object(InteractiveTerminal, "build_prompt", side_effect=RuntimeError("no console")), \
                patch("builtins.input", side_effect=["/exit"]):
            self.assertEqual(InteractiveTerminal().run(Config(api_key="k")), 0)
        print_mock.assert_any_call("This terminal does not support interactive input; using plain input instead.")


@unittest.skipUnless(sys.platform == "win32", "Windows console input only")
class WindowsConsoleInputTests(unittest.TestCase):
    def key_presses(self, char, vk, state):
        from prompt_toolkit.input import win32
        from prompt_toolkit.win32_types import KEY_EVENT_RECORD

        # Avoid opening the real console: only the key translation is under test.
        with patch.object(win32, "Win32Input"), patch.object(win32.ConsoleInputReader, "__init__",
                                                            return_value=None):
            reader = windows_console_input().console_input_reader
        event = KEY_EVENT_RECORD()
        event.KeyDown, event.VirtualKeyCode, event.ControlKeyState = 1, vk, state
        event.uChar.UnicodeChar = char
        return [(press.key, press.data) for press in reader._event_to_key_presses(event)]

    def test_records_from_windows_terminal(self):
        # Values captured from Windows Terminal in legacy input mode.
        from prompt_toolkit.keys import Keys

        self.assertEqual(self.key_presses("\r", 13, 0), [(Keys.ControlM, "\r")])
        self.assertEqual(self.key_presses("\r", 13, 0x10), [(Keys.Escape, ""), (Keys.ControlM, "\r")])
        self.assertEqual(self.key_presses("\n", 13, 0x08), [(Keys.Escape, ""), (Keys.ControlJ, "\n")])
        self.assertEqual(self.key_presses("a", 65, 0x10), [("a", "a")])


if __name__ == "__main__":
    unittest.main()
