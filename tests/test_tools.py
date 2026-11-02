from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_agent_loop import streamed
from test_streaming import text_block, tool_block
from zanbot.agent import run_turn
from zanbot.config import Config
from zanbot.tools import Tool, ToolContext, ToolError, ToolRegistry, default_tools, validate


def echo_tool(side_effect="none", run=None):
    return Tool("echo", "Echo text.",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                 "additionalProperties": False},
                side_effect, run or (lambda args, ctx: args["text"]))


def call(name, input=None, id="t1"):
    return {"type": "tool_use", "id": id, "name": name, "input": {} if input is None else input}


class ValidateTests(unittest.TestCase):
    schema = {"type": "object",
              "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}, "all": {"type": "boolean"}},
              "required": ["path"], "additionalProperties": False}

    def test_accepts_valid_input(self):
        self.assertIsNone(validate({"path": "a", "limit": 3, "all": True}, self.schema))

    def test_reports_each_kind_of_problem(self):
        cases = [({}, "missing required 'path'"),
                 ({"path": 1}, "'path' must be a string"),
                 ({"path": "a", "limit": True}, "'limit' must be an integer"),
                 ({"path": "a", "limit": 1.5}, "'limit' must be an integer"),
                 ({"path": "a", "extra": 1}, "unexpected 'extra'")]
        for value, message in cases:
            with self.subTest(value=value):
                self.assertIn(message, validate(value, self.schema))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.context = ToolContext(notes_dir=Path("unused"))

    def test_schemas_are_sorted_api_definitions(self):
        b = Tool("b", "B.", {"type": "object", "properties": {}}, "none", lambda a, c: "")
        a = Tool("a", "A.", {"type": "object", "properties": {}}, "local", lambda a, c: "")
        registry = ToolRegistry([b, a], self.context)
        self.assertEqual(registry.schemas(), [
            {"name": "a", "description": "A.", "input_schema": {"type": "object", "properties": {}}},
            {"name": "b", "description": "B.", "input_schema": {"type": "object", "properties": {}}},
        ])
        self.assertTrue(registry.has_side_effect("a"))
        self.assertFalse(registry.has_side_effect("b"))
        self.assertFalse(registry.has_side_effect("missing"))

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ValueError):
            ToolRegistry([echo_tool(), echo_tool()], self.context)

    def test_execute_returns_output(self):
        self.assertEqual(ToolRegistry([echo_tool()], self.context).execute(call("echo", {"text": "hi"})), ("hi", False))

    def test_errors_become_results_for_the_model(self):
        def broken(args, ctx):
            raise ToolError("No such note.")

        def crashing(args, ctx):
            raise OSError("disk unplugged")

        cases = [(ToolRegistry([echo_tool()], self.context), call("nope"), "Unknown tool 'nope'. Available: echo"),
                 (ToolRegistry([echo_tool()], self.context), call("echo", {"text": 1}), "Invalid input"),
                 (ToolRegistry([echo_tool(run=broken)], self.context), call("echo", {"text": "x"}), "No such note."),
                 (ToolRegistry([echo_tool(run=crashing)], self.context), call("echo", {"text": "x"}), "disk unplugged")]
        for registry, tool_call, message in cases:
            with self.subTest(message=message):
                output, is_error = registry.execute(tool_call)
                self.assertTrue(is_error)
                self.assertIn(message, output)


class NoteToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.notes = Path(self.temp.name) / "notes"
        self.registry = default_tools(self.notes)

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, tool_name, **input):
        return self.registry.execute(call(tool_name, input))

    def test_list_dir_before_any_note_exists(self):
        self.assertEqual(self.run_tool("list_dir"), ("(empty)", False))

    def test_write_then_list(self):
        self.assertEqual(self.run_tool("write_note", name="todo.md", text="买牛奶"),
                         ("Appended 3 characters to todo.md.", False))
        self.run_tool("write_note", name="todo.md", text="交作业")
        (self.notes / "drafts").mkdir()
        self.assertEqual((self.notes / "todo.md").read_text(encoding="utf-8"), "买牛奶\n交作业\n")
        self.assertEqual(self.run_tool("list_dir"), ("drafts/\ntodo.md", False))

    def test_list_dir_stays_inside_the_notes_folder(self):
        self.notes.mkdir()
        for path in ("..", "../..", "C:/Windows", "/etc", "sub/../../x"):
            with self.subTest(path=path):
                output, is_error = self.run_tool("list_dir", path=path)
                self.assertTrue(is_error)
                self.assertIn("inside the notes folder", output)

    def test_list_dir_missing_subfolder(self):
        self.notes.mkdir()
        output, is_error = self.run_tool("list_dir", path="nope")
        self.assertTrue(is_error)
        self.assertIn("No folder named 'nope'", output)

    def test_write_note_rejects_unsafe_names(self):
        for name in ("../x.md", "a/b.md", "a\\b.md", "", ".hidden", "con", "x" * 101, "a:b.md"):
            with self.subTest(name=name):
                output, is_error = self.run_tool("write_note", name=name, text="x")
                self.assertTrue(is_error)
        self.assertFalse(self.notes.exists() and any(self.notes.iterdir()))

    def test_write_note_limits_text_size(self):
        output, is_error = self.run_tool("write_note", name="big.md", text="x" * 20_001)
        self.assertTrue(is_error)
        self.assertIn("20,000", output)

    def test_side_effects_are_declared(self):
        self.assertFalse(self.registry.has_side_effect("list_dir"))
        self.assertTrue(self.registry.has_side_effect("write_note"))


@patch("zanbot.llm.requests.post")
class PermissionGateTests(unittest.TestCase):
    """Until N4 can ask the user, calls with side effects are refused unless hooks allow them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.notes = Path(self.temp.name) / "notes"
        self.registry = default_tools(self.notes)

    def tearDown(self):
        self.temp.cleanup()

    def turn(self, post, hooks=None):
        post.side_effect = [streamed(tool_block('{"name": "a.md", "text": "hi"}', name="write_note"), stop="tool_use"),
                            streamed(text_block("Ok"))]
        effects = []
        run_turn([], "Write a note", Config(api_key="k"), tools=self.registry, hooks=hooks, effects=effects)
        return post.call_args.kwargs["json"]["messages"][2]["content"][0], effects

    def test_denied_by_default(self, post):
        result, effects = self.turn(post)
        self.assertTrue(result["is_error"])
        self.assertIn("did not allow", result["content"])
        self.assertFalse((self.notes / "a.md").exists())
        self.assertEqual(effects, [])

    def test_runs_when_hooks_allow(self, post):
        hooks = Mock(ask_permission=Mock(return_value=True))
        result, effects = self.turn(post, hooks)
        self.assertNotIn("is_error", result)
        self.assertEqual((self.notes / "a.md").read_text(encoding="utf-8"), "hi\n")
        self.assertEqual([c["name"] for c in effects], ["write_note"])
        self.assertEqual(hooks.ask_permission.call_args.args[0]["name"], "write_note")

    def test_read_only_tools_never_ask(self, post):
        post.side_effect = [streamed(tool_block("{}"), stop="tool_use"), streamed(text_block("Ok"))]
        hooks = Mock()
        run_turn([], "List", Config(api_key="k"), tools=self.registry, hooks=hooks)
        hooks.ask_permission.assert_not_called()


@patch("builtins.print")
@patch("main.load_config", return_value=Config(api_key="k"))
@patch("zanbot.llm.requests.post")
class MainWiringTests(unittest.TestCase):
    def run_main(self, post, *flags):
        from main import main

        post.return_value = streamed(text_block("Hi"))
        with patch("builtins.input", side_effect=["Hello", "/exit"]):
            self.assertEqual(main(["--no-memory", "--plain", *flags]), 0)
        return post.call_args.kwargs["json"]

    def test_tools_are_offered_by_default(self, post, config, output):
        sent = self.run_main(post)
        self.assertEqual([t["name"] for t in sent["tools"]], ["list_dir", "write_note"])

    def test_no_tools_flag(self, post, config, output):
        self.assertNotIn("tools", self.run_main(post, "--no-tools"))


if __name__ == "__main__":
    unittest.main()
