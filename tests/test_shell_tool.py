from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from zanbot import tools
from zanbot.tools import default_tools

WINDOWS = sys.platform == "win32"


def call(command):
    return {"type": "tool_use", "id": "t1", "name": "run_shell", "input": {"command": command}}


class ShellToolTests(unittest.TestCase):
    """Runs real commands: PowerShell on Windows, sh elsewhere."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.registry = default_tools(self.workspace)

    def run_command(self, windows, posix):
        return self.registry.execute(call(windows if WINDOWS else posix))

    def test_output_and_exit_code(self):
        self.assertEqual(self.run_command("Write-Output hi", "echo hi"), ("Exit code 0\nhi", False))

    def test_failure_is_an_error_with_its_output(self):
        output, is_error = self.run_command("Write-Output oops; exit 3", "echo oops; exit 3")
        self.assertTrue(is_error)
        self.assertEqual(output, "Exit code 3\noops")

    def test_runs_in_the_workspace(self):
        output, _ = self.run_command("(Get-Location).Path", "pwd")
        self.assertEqual(Path(output.splitlines()[1]).resolve(), self.workspace.resolve())

    def test_chinese_output_survives(self):
        output, _ = self.run_command("Write-Output '你好'", "echo 你好")
        self.assertEqual(output, "Exit code 0\n你好")

    def test_no_output(self):
        self.assertEqual(self.run_command("$null = 1", "true"), ("Exit code 0 (no output)", False))

    def test_timeout_stops_the_command(self):
        with patch.object(tools, "SHELL_TIMEOUT", 2):
            start = time.monotonic()
            output, is_error = self.run_command("Write-Output started; Start-Sleep 20",
                                                "echo started; sleep 20")
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(is_error)
        self.assertIn("Timed out after 2 seconds", output)

    def test_permission_is_asked_every_time(self):
        self.assertTrue(self.registry.needs_permission("run_shell"))
        self.assertFalse(self.registry.allows_always("run_shell"))
        self.assertTrue(self.registry.has_side_effect("run_shell"))


if __name__ == "__main__":
    unittest.main()
