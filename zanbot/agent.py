"""One conversational turn: call the model, run the tools it asks for, and repeat until it answers."""

import json
import re

from zanbot.llm import APIError, create_message

# Blocks a final reply may contain. Thinking blocks are kept in history unchanged.
SUPPORTED_BLOCKS = {"text", "thinking", "redacted_thinking"}
FINAL_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "refusal"}

MAX_STEPS = 10
MAX_RESULT_CHARS = 10_000
STEP_LIMIT_NOTE = ("[Zanbot] The tool call limit for this turn has been reached. Answer now with what you "
                   "have, and say what is still undone.")
_TAG = re.compile(r"<(/?)(tool_output)", re.IGNORECASE)


DENIED = "The user did not allow this call. Do not retry it; continue without it or ask the user."


class NoHooks:
    """Default hooks: never cancelled, nothing to report, and no permission for side effects."""

    def check_cancelled(self):
        pass

    def ask_permission(self, call):
        return False

    def on_tool_start(self, call):
        pass

    def on_tool_finish(self, call, result):
        pass


def text_of(content):
    """Join the visible text of a list of content blocks, skipping thinking and tool blocks."""
    return "\n".join(block["text"] for block in content if block.get("type") == "text")


def format_result(output):
    """Wrap tool output as data the model can tell apart from instructions, truncating long output."""
    total = len(output)
    shown = output[:MAX_RESULT_CHARS]
    # Neutralize only our own tag, so ordinary text such as "a < b" or HTML stays readable.
    text = "<tool_output>\n" + _TAG.sub(r"‹\1\2", shown) + "\n</tool_output>"
    if total > MAX_RESULT_CHARS:
        text += (f"\n[Truncated: showing the first {MAX_RESULT_CHARS:,} of {total:,} characters. "
                 "If the tool has offset/limit parameters, use them to read the rest.]")
    return text


def effects_note(calls):
    """Tell the model which side effects a failed or cancelled turn left behind."""
    lines = []
    for call in calls:
        arguments = json.dumps(call["input"], ensure_ascii=False)
        if len(arguments) > 200:
            arguments = arguments[:200] + "…"
        lines.append(f"- {call['name']}({arguments})")
    return ("[Zanbot] An earlier turn failed or was cancelled after these tool calls had already run. "
            "That turn is not in the history, but its effects are real:\n" + "\n".join(lines))


def _validated(response, tools_allowed):
    content = response.get("content") if isinstance(response, dict) else None
    allowed = SUPPORTED_BLOCKS | ({"tool_use"} if tools_allowed else set())
    if not isinstance(content, list) or not content or any(
        not isinstance(block, dict)
        or block.get("type") not in allowed
        or (block.get("type") == "text" and not isinstance(block.get("text"), str))
        for block in content
    ):
        raise APIError("The API did not return the expected response.")
    stop = response.get("stop_reason")
    has_calls = any(block["type"] == "tool_use" for block in content)
    if stop == "tool_use" and tools_allowed:
        if not has_calls:
            raise APIError("The API asked to use a tool but sent no tool call.")
    elif stop not in FINAL_STOP_REASONS:
        raise APIError("The API returned a stop reason that this version does not support.")
    elif has_calls:
        # Usually max_tokens: the call is complete JSON but the model never finished asking for it.
        raise APIError("The answer was cut off while preparing a tool call. Please try again.")
    return content, stop


def _run_tool(tools, call, hooks, effects):
    hooks.on_tool_start(call)
    side_effect = tools.has_side_effect(call["name"])
    if side_effect and not hooks.ask_permission(call):
        output, is_error = DENIED, True
    else:
        if side_effect and effects is not None:
            # Recorded before running: a call that fails partway may still have changed something.
            effects.append(call)
        try:
            output, is_error = tools.execute(call)
        except Exception as exc:  # A broken tool is reported to the model, not to the user.
            output, is_error = f"The tool failed: {exc}", True
    result = {"type": "tool_result", "tool_use_id": call["id"], "content": format_result(str(output))}
    if is_error:
        result["is_error"] = True
    hooks.on_tool_finish(call, result)
    return result


def run_turn(messages, user_input, config, on_text=None, system=None, tools=None, hooks=None, effects=None):
    """Run one turn and return (answer, truncated), where answer is the last reply's text.

    History is extended only when the whole turn succeeds. tools provides schemas(), execute(call)
    -> (text, is_error) and has_side_effect(name); hooks may cancel between steps by raising from
    check_cancelled(). Calls with side effects are appended to effects as they start, so a caller
    can report them even if the turn later fails.
    """
    hooks = hooks or NoHooks()
    pending = [*messages, {"role": "user", "content": user_input}]
    schemas = tools.schemas() if tools is not None else None
    shown = {"any": False, "new_step": False}

    def emit(chunk):
        # Put each step's text on its own line.
        if shown["new_step"] and shown["any"]:
            on_text("\n")
        shown["new_step"] = False
        shown["any"] = True
        on_text(chunk)

    for step in range(MAX_STEPS + 1):
        final = step == MAX_STEPS
        if step:
            hooks.check_cancelled()
            shown["new_step"] = True
        if final:
            # pending[-1] is this turn's latest tool results, a list created below.
            pending[-1]["content"].append({"type": "text", "text": STEP_LIMIT_NOTE})
        # Send a snapshot: pending keeps growing after the request.
        response = create_message(list(pending), config, emit if on_text else None, system, tools=schemas,
                                  tool_choice={"type": "none"} if final else None)
        content, stop = _validated(response, tools_allowed=schemas is not None and not final)
        pending.append({"role": "assistant", "content": content})
        if stop != "tool_use":
            break
        results = []
        for call in (block for block in content if block["type"] == "tool_use"):
            hooks.check_cancelled()
            results.append(_run_tool(tools, call, hooks, effects))
        pending.append({"role": "user", "content": results})

    if step == 0 and not any(block["type"] == "text" for block in content):
        raise APIError("The API returned no text (it may have hit max_tokens while thinking). Please try again.")
    messages.extend(pending[len(messages):])
    return text_of(content), stop == "max_tokens"
