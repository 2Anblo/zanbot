from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from test_agent_loop import FakeTools, streamed
from test_streaming import text_block, tool_block
from zanbot.cli import InteractiveTerminal, PlainTerminal
from zanbot.config import Config
from zanbot.session import (ChatSession, Info, PermissionRequest, ReplyFailed, ReplyFinished, ReplyStarted,
                            TextDelta)
from zanbot.tools import default_tools


def write_call(text="hi", id="t1"):
    return tool_block('{"name": "a.md", "text": "%s"}' % text, name="write_note", id=id)


@patch("zanbot.llm.requests.post")
class SessionPermissionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.asked = []
        self.answers = []

    def ask(self, request):
        self.asked.append(request)
        return self.answers.pop(0)

    def session(self, tools, ask="default"):
        return ChatSession(Config(api_key="k"), self.events.append, tools=tools,
                           ask=self.ask if ask == "default" else ask)

    def turn(self, post, session, *answers):
        self.answers.extend(answers)
        post.side_effect = [streamed(write_call(), stop="tool_use"), streamed(text_block("Ok"))]
        session.submit("Write a note")
        return post.call_args.kwargs["json"]["messages"][-1]["content"][0]

    def test_yes_runs_the_tool(self, post):
        tools = FakeTools()
        result = self.turn(post, self.session(tools), "y")
        self.assertEqual(tools.calls, [("write_note", {"name": "a.md", "text": "hi"})])
        self.assertNotIn("is_error", result)
        self.assertEqual(self.asked, [PermissionRequest("write_note", '{"name": "a.md", "text": "hi"}')])
        self.assertIsInstance(self.events[-1], ReplyFinished)

    def test_no_and_unknown_answers_deny(self, post):
        for answer in ("n", "maybe"):
            with self.subTest(answer=answer):
                tools = FakeTools()
                result = self.turn(post, self.session(tools), answer)
                self.assertEqual(tools.calls, [])
                self.assertTrue(result["is_error"])
                self.assertIn("did not allow", result["content"])

    def test_always_allows_the_tool_until_clear(self, post):
        tools = FakeTools()
        session = self.session(tools)
        self.turn(post, session, "a")
        self.turn(post, session)  # Not asked again.
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(tools.calls), 2)
        session.submit("/clear")
        self.turn(post, session, "n")
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(len(tools.calls), 2)

    def test_frontend_that_cannot_ask_denies_and_says_why(self, post):
        tools = FakeTools()
        result = self.turn(post, self.session(tools, ask=None))
        self.assertEqual(tools.calls, [])
        # The model learns it cannot be asked here, so it does not offer to retry.
        self.assertIn("Retrying will not help", result["content"])
        self.assertNotIn("did not allow", result["content"])
        lines = [line for event in self.events if isinstance(event, Info) for line in event.lines]
        self.assertTrue(any("write_note was denied: this terminal cannot ask" in line for line in lines))

    def test_ctrl_c_while_asking_cancels_the_turn(self, post):
        def interrupted(request):
            raise KeyboardInterrupt

        tools = FakeTools()
        session = self.session(tools, ask=interrupted)
        post.side_effect = [streamed(write_call(), stop="tool_use"), streamed(text_block("Ok"))]
        session.submit("Write a note")
        self.assertEqual(self.events[-1], ReplyFailed("Cancelled.", cancelled=True))
        for thread in threading.enumerate():
            if thread.name == "zanbot-reply":
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(tools.calls, [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(session.messages, [])

    def test_long_arguments_are_shortened_for_display(self, post):
        self.answers.append("n")
        post.side_effect = [streamed(write_call("x" * 1000), stop="tool_use"), streamed(text_block("Ok"))]
        self.session(FakeTools()).submit("Write")
        self.assertLessEqual(len(self.asked[0].arguments), 301)
        self.assertTrue(self.asked[0].arguments.endswith("…"))


class LineBreakTests(unittest.TestCase):
    @patch("builtins.print")
    def test_info_during_a_reply_starts_on_its_own_line(self, print_mock):
        terminal = PlainTerminal()
        for event in (ReplyStarted(), Info(("(denied)",)), TextDelta("Ok"), TextDelta("\n"), Info(("next",))):
            terminal.render(event)
        self.assertEqual([c.args for c in print_mock.call_args_list],
                         [("\nZanbot: ",), (), ("(denied)",), ("Ok",), ("\n",), ("next",)])


@patch("builtins.print")
@patch("zanbot.llm.requests.post")
class InteractivePermissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.notes = Path(directory.name) / "notes"

    def run_terminal(self, post, keys):
        post.side_effect = [streamed(write_call(), stop="tool_use"), streamed(text_block("Ok"))]
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            terminal = InteractiveTerminal(input=pipe, output=DummyOutput())
            self.assertEqual(terminal.run(Config(api_key="k"), tools=default_tools(self.notes)), 0)

    def test_yes_writes_the_note(self, post, print_mock):
        self.run_terminal(post, "Write a note\ry\r/exit\r")
        self.assertEqual((self.notes / "a.md").read_text(encoding="utf-8"), "hi\n")
        print_mock.assert_any_call('Zanbot wants to run write_note({"name": "a.md", "text": "hi"})')

    def test_invalid_answer_asks_again(self, post, print_mock):
        self.run_terminal(post, "Write a note\rwhat\rn\r/exit\r")
        self.assertFalse((self.notes / "a.md").exists())
        print_mock.assert_any_call("Please answer y, n, or a.")


if __name__ == "__main__":
    unittest.main()
