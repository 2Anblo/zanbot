import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zanbot.usage import record_chatgpt_usage


class UsageTests(unittest.TestCase):
    def test_multiple_turns_share_session_and_accumulate_usage(self):
        with tempfile.TemporaryDirectory() as temp, patch("zanbot.usage.usage_directory", return_value=Path(temp)):
            first = {"id": "first", "usage": {"input_tokens": 10, "output_tokens": 5}}
            second = {"id": "second", "usage": {"input_tokens": 20, "output_tokens": 7}}
            record_chatgpt_usage(first, "model", "zanbot-conversation")
            record_chatgpt_usage(second, "model", "zanbot-conversation")
            record_chatgpt_usage(second, "model", "zanbot-conversation")
            files = list(Path(temp).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            records = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual(sum(r["type"] == "session_meta" for r in records), 1)
            self.assertEqual(records[-1]["payload"]["info"]["total_token_usage"]["total_tokens"], 42)
            record_chatgpt_usage({**first, "id": "third"}, "model", "zanbot-new-conversation")
            self.assertEqual(len(list(Path(temp).glob("*.jsonl"))), 2)

    def test_usage_export_deduplicates_and_excludes_content(self):
        response = {"id": "response-test", "model": "actual-model", "access_token": "secret",
                    "content": "private conversation", "usage": {"input_tokens": 100,
                    "output_tokens": 50, "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens_details": {"reasoning_tokens": 10}}}
        with tempfile.TemporaryDirectory() as temp, patch("zanbot.usage.usage_directory", return_value=Path(temp)):
            record_chatgpt_usage(response, "requested-model")
            record_chatgpt_usage(response, "requested-model")
            files = list(Path(temp).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text()
            self.assertNotIn("secret", text)
            self.assertNotIn("private conversation", text)
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(records[1]["payload"]["model"], "actual-model")
            usage = records[2]["payload"]["info"]["last_token_usage"]
            self.assertEqual(usage["total_tokens"], 150)
            self.assertEqual(usage["cached_input_tokens"], 20)
            self.assertEqual(usage["reasoning_output_tokens"], 10)

    def test_missing_usage_is_not_invented(self):
        with tempfile.TemporaryDirectory() as temp, patch("zanbot.usage.usage_directory", return_value=Path(temp)):
            for response in ({}, {"usage": {}}, {"usage": {"input_tokens": -1, "output_tokens": 4}}):
                record_chatgpt_usage(response, "model")
            self.assertEqual(list(Path(temp).iterdir()), [])

    @patch("zanbot.usage.usage_directory", side_effect=OSError("Unavailable"))
    @patch("sys.stderr")
    def test_disk_failure_does_not_fail_chat(self, stderr, directory):
        record_chatgpt_usage({"usage": {"input_tokens": 1, "output_tokens": 2}}, "model")
        stderr.write.assert_called()
