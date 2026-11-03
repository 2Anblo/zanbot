from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_agent_loop import streamed
from test_streaming import text_block, tool_block
from zanbot.config import Config
from zanbot.memory import MemoryStore
from zanbot.prompts import MEMORY_GUIDE, MEMORY_GUIDE_TOOLS, build_system_prompt
from zanbot.session import ChatSession
from zanbot.tools import default_tools


def call(name, **input):
    return {"type": "tool_use", "id": "t1", "name": name, "input": input}


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(Path(directory.name) / "memory.db")
        self.registry = default_tools(Path(directory.name), self.store)

    def test_remember_saves_right_away(self):
        output, is_error = self.registry.execute(call("remember", content="用户喜欢喝美式", kind="preference"))
        self.assertFalse(is_error)
        memory = self.store.active()[0]
        self.assertEqual((memory.content, memory.kind), ("用户喜欢喝美式", "preference"))
        self.assertEqual(output, f"Remembered #{memory.id}. The user can undo this with /forget {memory.id}.")

    def test_remember_refusals(self):
        for input, message in (({"content": "我的 key 是 sk-abcdefgh12345678"}, "looks like a password, key, or token"),
                               ({"content": "x", "kind": "secret"}, "must be one of")):
            with self.subTest(input=input):
                output, is_error = self.registry.execute(call("remember", **input))
                self.assertTrue(is_error)
                self.assertIn(message, output)
        self.assertEqual(self.store.active(), [])

    def test_forget(self):
        memory_id = self.store.add("用户养了一只猫")
        self.assertEqual(self.registry.describe(call("forget", id=memory_id)),
                         f"forget memory #{memory_id}: 用户养了一只猫")
        self.assertEqual(self.registry.execute(call("forget", id=memory_id)), (f"Forgot #{memory_id}.", False))
        self.assertEqual(self.store.active(), [])
        output, is_error = self.registry.execute(call("forget", id=memory_id))
        self.assertTrue(is_error)
        self.assertIn("No active memory has ID", output)
        self.assertEqual(self.registry.describe(call("forget", id=999)), "forget memory #999 (not found)")

    def test_permissions(self):
        self.assertFalse(self.registry.needs_permission("remember"))
        self.assertTrue(self.registry.has_side_effect("remember"))  # Reported if the turn later fails.
        self.assertTrue(self.registry.needs_permission("forget"))

    def test_only_offered_when_memory_is_on(self):
        names = [t["name"] for t in default_tools(Path(".")).schemas()]
        self.assertNotIn("remember", names)
        self.assertNotIn("forget", names)


class MemoryPromptWithToolsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(Path(directory.name) / "memory.db")
        self.memory_id = self.store.add("用户养了一只猫")

    def test_ids_and_tool_guide_only_with_tools(self):
        with_tools = build_system_prompt(self.store.recall(), date(2026, 11, 2), "on", tools=True)
        self.assertIn(MEMORY_GUIDE_TOOLS, with_tools)
        self.assertIn(f"- #{self.memory_id} [note] 用户养了一只猫", with_tools)
        without = build_system_prompt(self.store.recall(), date(2026, 11, 2), "on", tools="no-tools")
        self.assertIn(MEMORY_GUIDE, without)
        self.assertIn("- [note] 用户养了一只猫", without)
        self.assertNotIn("#", without.split("<memories>")[1])

    @patch("zanbot.llm.requests.post")
    def test_forget_question_shows_the_memory_text(self, post):
        asked = []
        session = ChatSession(Config(api_key="k"), lambda e: None, store=self.store,
                              tools=default_tools(Path("."), self.store),
                              ask=lambda request: asked.append(request) or "n")
        post.side_effect = [streamed(tool_block('{"id": %d}' % self.memory_id, name="forget"), stop="tool_use"),
                            streamed(text_block("Ok"))]
        session.submit("忘掉猫的事")
        self.assertEqual(asked[0].arguments, f"forget memory #{self.memory_id}: 用户养了一只猫")
        self.assertEqual(len(self.store.active()), 1)  # Answered "n".


if __name__ == "__main__":
    unittest.main()
