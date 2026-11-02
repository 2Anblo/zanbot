import json


def iter_events(lines):
    """Decode UTF-8 SSE data fields, separated by blank lines."""
    data = []
    for raw_line in lines:
        line = raw_line.decode("utf-8")
        if not line:
            if data:
                event = json.loads("\n".join(data))
                if not isinstance(event, dict):
                    raise ValueError("The stream returned an invalid event.")
                yield event
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)


def _start_block(block):
    """Return a fresh copy of a content block from content_block_start, or raise if unsupported."""
    kind = block.get("type") if isinstance(block, dict) else None
    if kind == "text" and isinstance(block.get("text"), str):
        return {"type": "text", "text": block["text"]}
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.get("thinking") or "", "signature": block.get("signature") or ""}
    if kind == "redacted_thinking" and isinstance(block.get("data"), str):
        return {"type": "redacted_thinking", "data": block["data"]}
    if kind == "tool_use":
        if not block.get("id") or not isinstance(block["id"], str) or not block.get("name") or not isinstance(block["name"], str):
            raise ValueError("The stream returned a tool call without an id or name.")
        return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input") or {}}
    raise ValueError(f"This version does not support {kind!r} content blocks.")


# Which delta type may extend which block, and the block field it appends to.
_DELTA_FIELDS = {
    ("text", "text_delta"): ("text", "text"),
    ("thinking", "thinking_delta"): ("thinking", "thinking"),
    ("thinking", "signature_delta"): ("signature", "signature"),
}


def read_message(lines, on_text=None, on_event=None):
    """Display text as it arrives and return only a completed message.

    Thinking blocks are kept verbatim (they must be sent back unchanged) but never shown.
    Tool input arrives as JSON fragments and is parsed only when its block ends.
    """
    content = []
    active_index = None
    started = False
    stop_reason = None
    shown_text = False
    tool_json = None  # Fragments of the active tool_use block's input.
    bad_tool_input = False  # A tool input that did not parse; fatal unless explained by max_tokens.

    def emit(text):
        if text and on_text is not None:
            on_text(text)

    for event in iter_events(lines):
        kind = event.get("type")
        if kind == "error":
            raise ValueError("The API reported a streaming error. Please try again later.")
        if kind == "message_start":
            if started:
                raise ValueError("The stream returned a duplicate message start.")
            started = True
        elif kind == "content_block_start":
            index = event.get("index")
            if not started or active_index is not None or index != len(content):
                raise ValueError("The stream returned an unexpected content block.")
            block = _start_block(event.get("content_block"))
            active_index = index
            content.append(block)
            if block["type"] == "text":
                # Separate visible text blocks, but never start the answer with a blank line.
                if shown_text:
                    emit("\n")
                shown_text = True
                emit(block["text"])
            elif block["type"] == "tool_use":
                tool_json = []
                if on_event is not None:
                    on_event({"type": "tool_use_start", "id": block["id"], "name": block["name"]})
        elif kind == "content_block_delta":
            delta = event.get("delta")
            if active_index is None or event.get("index") != active_index or not isinstance(delta, dict):
                raise ValueError("The stream returned a delta without an active block.")
            block = content[active_index]
            if block["type"] == "tool_use" and delta.get("type") == "input_json_delta":
                if not isinstance(delta.get("partial_json"), str):
                    raise ValueError("The stream returned an unsupported tool input delta.")
                tool_json.append(delta["partial_json"])
                continue
            field = _DELTA_FIELDS.get((block["type"], delta.get("type")))
            if field is None or not isinstance(delta.get(field[0]), str):
                raise ValueError(f"The stream returned an unsupported {delta.get('type')!r} delta.")
            block[field[1]] += delta[field[0]]
            if block["type"] == "text":
                emit(delta["text"])
        elif kind == "content_block_stop":
            if active_index is None or event.get("index") != active_index:
                raise ValueError("The stream returned an unexpected block stop.")
            block = content[active_index]
            # A tool without parameters arrives as a single empty fragment; keep its {} input then.
            if block["type"] == "tool_use" and "".join(tool_json).strip():
                try:
                    block["input"] = json.loads("".join(tool_json))
                except ValueError:
                    bad_tool_input = True
            if block["type"] == "tool_use" and not isinstance(block["input"], dict):
                bad_tool_input = True
            tool_json = None
            active_index = None
        elif kind == "message_delta":
            delta = event.get("delta")
            if not started or active_index is not None or not isinstance(delta, dict):
                raise ValueError("The stream returned an invalid message delta.")
            if delta.get("stop_reason") is not None:
                stop_reason = delta["stop_reason"]
        elif kind == "message_stop":
            if not started or not content or active_index is not None or not isinstance(stop_reason, str):
                raise ValueError("The stream ended with an incomplete message.")
            if bad_tool_input:
                if stop_reason == "max_tokens":
                    raise ValueError("The answer hit max_tokens while preparing a tool call.")
                raise ValueError("The stream returned invalid tool input.")
            return {"content": content, "stop_reason": stop_reason}
        # Ignore ping and unknown event types for forward compatibility.

    raise ValueError("The stream ended before message_stop. The response is incomplete.")
