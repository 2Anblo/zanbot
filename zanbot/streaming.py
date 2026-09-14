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


def read_message(lines, on_text=None):
    """Display text as it arrives and return only a completed message."""
    content = []
    active_index = None
    started = False
    stop_reason = None

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
            block = event.get("content_block")
            if not started or active_index is not None or index != len(content):
                raise ValueError("The stream returned an unexpected content block.")
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise ValueError("This version only supports text content blocks.")
            active_index = index
            content.append({"type": "text", "text": block["text"]})
            if index:
                emit("\n")
            emit(block["text"])
        elif kind == "content_block_delta":
            delta = event.get("delta")
            if active_index is None or event.get("index") != active_index:
                raise ValueError("The stream returned a delta without an active block.")
            if not isinstance(delta, dict) or delta.get("type") != "text_delta" or not isinstance(delta.get("text"), str):
                raise ValueError("The stream returned an unsupported text delta.")
            content[active_index]["text"] += delta["text"]
            emit(delta["text"])
        elif kind == "content_block_stop":
            if active_index is None or event.get("index") != active_index:
                raise ValueError("The stream returned an unexpected block stop.")
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
            return {"content": content, "stop_reason": stop_reason}
        # Ignore ping and unknown event types for forward compatibility.

    raise ValueError("The stream ended before message_stop. The response is incomplete.")
