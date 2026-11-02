import unittest
from unittest.mock import Mock, call, patch

from test_agent_loop import FakeTools, streamed
from test_streaming import text_block, tool_block
from zanbot.agent import run_turn
from zanbot.cli import PlainTerminal
from zanbot.config import Config
from zanbot.session import (ChatSession, Info, ReplyStarted, TextDelta, ToolDenied, ToolFinished, ToolStarted,
                            summarize)


@patch("zanbot.llm.requests.post")
class HookTests(unittest.TestCase):
    def run_one(self, post, block, hooks, tools=None):
        post.side_effect = [streamed(block, stop="tool_use"), streamed(text_block("Ok"))]
        run_turn([], "Go", Config(api_key="k"), tools=tools or FakeTools({"list_dir": ("a\nb", False)}), hooks=hooks)

    def test_a_tool_that_runs_reports_start_and_raw_output(self, post):
        hooks = Mock()
        self.run_one(post, tool_block('{"path": "."}'), hooks)
        hooks.on_tool_start.assert_called_once()
        self.assertEqual(hooks.on_tool_finish.call_args.args[1:], ("a\nb", False))
        hooks.on_tool_denied.assert_not_called()

    def test_a_denied_tool_reports_only_the_denial(self, post):
        for answer, asked in ((False, True), (None, False)):
            with self.subTest(answer=answer):
                hooks = Mock(ask_permission=Mock(return_value=answer))
                self.run_one(post, tool_block("{}", name="write_note"), hooks)
                hooks.on_tool_start.assert_not_called()
                hooks.on_tool_finish.assert_not_called()
                self.assertEqual(hooks.on_tool_denied.call_args.args[1], asked)


class SummarizeTests(unittest.TestCase):
    def test_summaries(self):
        self.assertEqual(summarize("Appended 3 characters to todo.md."), "Appended 3 characters to todo.md.")
        self.assertEqual(summarize("drafts/\ntodo.md\nideas.md"), "drafts/ … (3 lines)")
        self.assertEqual(summarize("  \n"), "(no output)")
        self.assertEqual(summarize("x" * 200), "x" * 80 + "…")


@patch("zanbot.llm.requests.post")
class SessionEventTests(unittest.TestCase):
    def setUp(self):
        self.events = []

    def tool_events(self):
        return [e for e in self.events if isinstance(e, (ToolStarted, ToolFinished, ToolDenied, Info))]

    def test_a_run_tool_emits_started_and_finished(self, post):
        post.side_effect = [streamed(tool_block('{"path": "."}'), stop="tool_use"), streamed(text_block("Ok"))]
        ChatSession(Config(api_key="k"), self.events.append,
                    tools=FakeTools({"list_dir": ("a\nb", False)})).submit("List")
        self.assertEqual(self.tool_events(), [ToolStarted("list_dir", '{"path": "."}'),
                                              ToolFinished("list_dir", True, "a … (2 lines)")])

    def test_errors_are_shown_as_failed(self, post):
        post.side_effect = [streamed(tool_block("{}"), stop="tool_use"), streamed(text_block("Ok"))]
        ChatSession(Config(api_key="k"), self.events.append,
                    tools=FakeTools({"list_dir": ("No folder named 'x'.", True)})).submit("List")
        self.assertEqual(self.tool_events()[-1], ToolFinished("list_dir", False, "No folder named 'x'."))

    def test_denials_say_whether_the_user_was_asked(self, post):
        for ask, asked in ((lambda request: "n", True), (None, False)):
            with self.subTest(asked=asked):
                self.events.clear()
                post.side_effect = [streamed(tool_block('{"name": "a.md", "text": "hi"}', name="write_note"),
                                             stop="tool_use"), streamed(text_block("Ok"))]
                ChatSession(Config(api_key="k"), self.events.append, tools=FakeTools(), ask=ask).submit("Write")
                self.assertEqual(self.tool_events(),
                                 [ToolDenied("write_note", '{"name": "a.md", "text": "hi"}', asked)])


@patch("builtins.print")
class RenderTests(unittest.TestCase):
    def printed(self, print_mock, *events):
        terminal = PlainTerminal()
        for event in events:
            terminal.render(event)
        return [c.args for c in print_mock.call_args_list]

    def test_tool_lines_between_steps(self, print_mock):
        printed = self.printed(print_mock, ReplyStarted(), TextDelta("Looking."), ToolStarted("list_dir", "{}"),
                               ToolFinished("list_dir", True, "a … (2 lines)"), TextDelta("\n"), TextDelta("Done"))
        self.assertEqual(printed, [("\nZanbot: ",), ("Looking.",), (), ("  → list_dir({})",),
                                   ("    ✓ a … (2 lines)",), ("Done",)])

    def test_tool_line_before_any_text_and_failure_mark(self, print_mock):
        printed = self.printed(print_mock, ReplyStarted(), ToolStarted("list_dir", "{}"),
                               ToolFinished("list_dir", False, "No folder."), TextDelta("Sorry"))
        self.assertEqual(printed, [("\nZanbot: ",), (), ("  → list_dir({})",), ("    ✗ No folder.",), ("Sorry",)])

    def test_denials(self, print_mock):
        printed = self.printed(print_mock, ReplyStarted(), ToolDenied("write_note", "{}", True),
                               ToolDenied("write_note", "{}", False))
        self.assertEqual(printed[2:], [("  ✗ write_note: not allowed",),
                                       ("  ✗ write_note was denied: this terminal cannot ask for permission. "
                                        "Run Zanbot without --plain to allow it.",)])


class PipeEncodingTests(unittest.TestCase):
    def stream(self, data=b"", tty=False):
        import io

        stream = io.TextIOWrapper(io.BytesIO(data), encoding="cp1252", errors="surrogateescape")
        stream.isatty = lambda: tty
        return stream

    def test_pipes_use_utf8(self):
        from main import use_utf8_for_pipes

        # What PowerShell sends: a UTF-8 byte order mark, then UTF-8 text.
        stdin = self.stream("﻿买牛奶\n".encode("utf-8"))
        stdout, stderr = self.stream(), self.stream()
        use_utf8_for_pipes(stdin, stdout, stderr)
        self.assertEqual(stdin.readline(), "买牛奶\n")
        stdout.write("  → ✓ ✗")  # Would raise UnicodeEncodeError in cp1252.
        stdout.flush()
        self.assertEqual(stdout.buffer.getvalue().decode("utf-8"), "  → ✓ ✗")

    def test_consoles_are_left_alone(self):
        from main import use_utf8_for_pipes

        streams = [self.stream(tty=True) for _ in range(3)]
        use_utf8_for_pipes(*streams)
        self.assertEqual([s.encoding for s in streams], ["cp1252"] * 3)


if __name__ == "__main__":
    unittest.main()
