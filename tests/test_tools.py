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
        self.context = ToolContext(workspace=Path("unused"))

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


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.registry = default_tools(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, tool_name, **input):
        return self.registry.execute(call(tool_name, input))

    def write(self, name, data):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def test_list_dir(self):
        self.assertEqual(self.run_tool("list_dir"), ("(empty)", False))
        self.write("todo.md", "x")
        (self.workspace / "drafts").mkdir()
        self.assertEqual(self.run_tool("list_dir"), ("drafts/\ntodo.md", False))
        output, is_error = self.run_tool("list_dir", path="nope")
        self.assertTrue(is_error)
        self.assertIn("No folder named 'nope'", output)

    def test_no_tool_can_leave_the_workspace(self):
        outside = self.root / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        attempts = [("list_dir", {"path": ".."}), ("list_dir", {"path": "C:/Windows"}),
                    ("read_file", {"path": "../secret.txt"}), ("read_file", {"path": str(outside)}),
                    ("read_file", {"path": "sub/../../secret.txt"}),
                    ("write_file", {"path": "../evil.txt", "content": "x"}),
                    ("edit_file", {"path": "../secret.txt", "old_text": "secret", "new_text": "gone"})]
        for name, input in attempts:
            with self.subTest(name=name, input=input):
                output, is_error = self.registry.execute(call(name, input))
                self.assertTrue(is_error)
                self.assertIn("must be inside the workspace", output)
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret")
        self.assertFalse((self.root / "evil.txt").exists())

    def test_a_link_pointing_outside_is_refused(self):
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        try:
            (self.workspace / "link.txt").symlink_to(self.root / "secret.txt")
        except OSError:
            self.skipTest("Creating symbolic links needs extra rights on this system.")
        output, is_error = self.run_tool("read_file", path="link.txt")
        self.assertTrue(is_error)
        self.assertIn("must be inside the workspace", output)

    def test_absolute_paths_inside_the_workspace_are_fine(self):
        self.write("a.txt", "hi")
        self.assertEqual(self.run_tool("read_file", path=str(self.workspace / "a.txt")), ("hi", False))

    def test_read_file_whole_and_in_parts(self):
        self.write("lines.txt", "".join(f"line {i}\n" for i in range(1, 11)))
        self.assertEqual(self.run_tool("read_file", path="lines.txt")[0].count("line"), 10)
        output, _ = self.run_tool("read_file", path="lines.txt", offset=4, limit=2)
        self.assertEqual(output, "line 4\nline 5\n\n[Lines 4-5 of 10. Use offset and limit to read other lines.]")
        output, _ = self.run_tool("read_file", path="lines.txt", offset=50)
        self.assertEqual(output, "[The file has only 10 lines.]")

    def test_read_file_refusals(self):
        self.write("image.png", b"\x89PNG\x00\x00binary")
        for path, message in (("image.png", "binary"), ("missing.txt", "No file named"), (".", "No file named")):
            with self.subTest(path=path):
                output, is_error = self.run_tool("read_file", path=path)
                self.assertTrue(is_error)
                self.assertIn(message, output)

    def test_write_file_creates_and_replaces(self):
        self.assertEqual(self.run_tool("write_file", path="notes/todo.md", content="买牛奶\n"),
                         ("Created notes/todo.md (4 characters).", False))
        self.assertEqual(self.run_tool("write_file", path="notes/todo.md", content="交作业\r\n"),
                         ("Replaced notes/todo.md (5 characters).", False))
        self.assertEqual((self.workspace / "notes/todo.md").read_bytes(), "交作业\r\n".encode("utf-8"))

    def test_write_file_limits(self):
        (self.workspace / "folder").mkdir()
        for input, message in (({"path": "big.txt", "content": "x" * 200_001}, "200,000"),
                               ({"path": "folder", "content": "x"}, "is a folder")):
            with self.subTest(message=message):
                output, is_error = self.registry.execute(call("write_file", input))
                self.assertTrue(is_error)
                self.assertIn(message, output)

    def test_edit_file_replaces_exactly_one_occurrence(self):
        path = self.write("todo.md", "- 买牛奶\r\n- 交作业\r\n")
        self.assertEqual(self.run_tool("edit_file", path="todo.md", old_text="- 买牛奶", new_text="- 买豆浆"),
                         ("Edited todo.md: replaced 5 characters with 5.", False))
        # Windows line endings are kept.
        self.assertEqual(path.read_bytes(), "- 买豆浆\r\n- 交作业\r\n".encode("utf-8"))

    def test_edit_file_refuses_missing_repeated_or_empty_text(self):
        path = self.write("todo.md", "- a\n- a\n")
        for old, message in (("- b", "was not found"), ("- a", "appears 2 times"), ("", "must not be empty")):
            with self.subTest(old=old):
                output, is_error = self.run_tool("edit_file", path="todo.md", old_text=old, new_text="x")
                self.assertTrue(is_error)
                self.assertIn(message, output)
        self.assertEqual(path.read_text(encoding="utf-8"), "- a\n- a\n")

    def test_permissions_are_declared(self):
        self.assertFalse(self.registry.needs_permission("list_dir"))
        self.assertFalse(self.registry.needs_permission("read_file"))
        for name in ("write_file", "edit_file"):
            self.assertTrue(self.registry.has_side_effect(name))
            self.assertTrue(self.registry.needs_permission(name))
            self.assertTrue(self.registry.allows_always(name))

    def test_ask_each_time_never_allows_always(self):
        tool = Tool("danger", "D.", {"type": "object", "properties": {}}, "local", lambda a, c: "",
                    permission="ask_each_time")
        registry = ToolRegistry([tool], ToolContext(workspace=self.workspace))
        self.assertTrue(registry.needs_permission("danger"))
        self.assertFalse(registry.allows_always("danger"))


@patch("zanbot.llm.requests.post")
class PermissionGateTests(unittest.TestCase):
    """Until N4 can ask the user, calls with side effects are refused unless hooks allow them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.notes = Path(self.temp.name) / "notes"
        self.notes.mkdir()
        self.registry = default_tools(self.notes)

    def tearDown(self):
        self.temp.cleanup()

    def turn(self, post, hooks=None):
        post.side_effect = [streamed(tool_block('{"path": "a.md", "content": "hi\\n"}', name="write_file"),
                                     stop="tool_use"),
                            streamed(text_block("Ok"))]
        effects = []
        run_turn([], "Write a note", Config(api_key="k"), tools=self.registry, hooks=hooks, effects=effects)
        return post.call_args.kwargs["json"]["messages"][2]["content"][0], effects

    def test_denied_by_default(self, post):
        # Default hooks have no way to ask, and the model is told so, not that the user refused.
        result, effects = self.turn(post)
        self.assertTrue(result["is_error"])
        self.assertIn("cannot ask the user for permission", result["content"])
        self.assertFalse((self.notes / "a.md").exists())
        self.assertEqual(effects, [])

    def test_runs_when_hooks_allow(self, post):
        hooks = Mock(ask_permission=Mock(return_value=True))
        result, effects = self.turn(post, hooks)
        self.assertNotIn("is_error", result)
        self.assertEqual((self.notes / "a.md").read_text(encoding="utf-8"), "hi\n")
        self.assertEqual([c["name"] for c in effects], ["write_file"])
        self.assertEqual(hooks.ask_permission.call_args.args[0]["name"], "write_file")

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
        self.assertEqual([t["name"] for t in sent["tools"]], ["edit_file", "list_dir", "read_file", "run_shell", "web_fetch", "write_file"])

    def test_no_tools_flag(self, post, config, output):
        self.assertNotIn("tools", self.run_main(post, "--no-tools"))


if __name__ == "__main__":
    unittest.main()
