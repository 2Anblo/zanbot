"""Export non-secret usage records for Token Monitor's Codex parser."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
from filelock import FileLock, Timeout


def usage_directory():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "zanbot" / "usage" / "codex"


def record_chatgpt_usage(response, requested_model, session_id=None):
    """Accumulate deduplicated response usage within one conversation log."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
        return

    def detail(field, name, limit):
        values = usage.get(field) or {}
        value = values.get(name, 0) if isinstance(values, dict) else 0
        return min(value, limit) if type(value) is int and value >= 0 else 0

    counters = {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cached_input_tokens": detail("input_tokens_details", "cached_tokens", input_tokens),
        "reasoning_output_tokens": detail("output_tokens_details", "reasoning_tokens", output_tokens),
        "total_tokens": input_tokens + output_tokens,
    }
    response_id = response.get("id") or str(uuid.uuid4())
    session_id = session_id or "zanbot-" + hashlib.sha256(str(response_id).encode()).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    model = response.get("model") or requested_model
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "originator": "zanbot",
         "cwd": str(Path.cwd()), "model_provider": "openai", "source": "cli"}},
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "response_id": response_id,
            "model": model, "last_token_usage": counters, "total_token_usage": counters}}},
    ]
    temporary = None
    try:
        directory = usage_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (session_id + ".jsonl")
        with FileLock(str(target) + ".lock", timeout=5):
            previous = target.read_text(encoding="utf-8") if target.exists() else ""
            totals = {key: 0 for key in counters}
            for line in previous.splitlines():
                info = json.loads(line).get("payload", {}).get("info", {})
                if info.get("response_id") == response_id:
                    return
                if "total_token_usage" in info:
                    totals = info["total_token_usage"]
            records[-1]["payload"]["info"]["total_token_usage"] = {
                key: totals.get(key, 0) + value for key, value in counters.items()
            }
            if previous:
                records = records[1:]
            temporary = directory / (session_id + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(previous + "".join(json.dumps({"timestamp": timestamp, **record}) + "\n"
                                         for record in records), encoding="utf-8")
            os.replace(temporary, target)
    except (OSError, ValueError, Timeout):
        print("Warning: Could not save Token Monitor usage data.", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
