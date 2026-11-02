from zanbot.llm import APIError, create_message

# Blocks a reply may contain in this version. Thinking blocks are kept in history unchanged.
SUPPORTED_BLOCKS = {"text", "thinking", "redacted_thinking"}


def text_of(content):
    """Join the visible text of a list of content blocks, skipping thinking and tool blocks."""
    return "\n".join(block["text"] for block in content if block.get("type") == "text")


def run_turn(messages, user_input, config, on_text=None, system=None):
    # Update history only after success so failed inputs do not carry into later turns.
    pending = [*messages, {"role": "user", "content": user_input}]
    response = create_message(pending, config, on_text, system)
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, list) or not content or any(
        not isinstance(block, dict)
        or block.get("type") not in SUPPORTED_BLOCKS
        or (block.get("type") == "text" and not isinstance(block.get("text"), str))
        for block in content
    ):
        raise APIError("The API did not return the expected text response.")
    if response.get("stop_reason") not in {"end_turn", "max_tokens", "stop_sequence", "refusal"}:
        raise APIError("The API returned a stop reason that this version does not support.")
    if not any(block["type"] == "text" for block in content):
        raise APIError("The API returned no text (it may have hit max_tokens while thinking). Please try again.")
    messages.extend([pending[-1], {"role": "assistant", "content": content}])
    return text_of(content), response["stop_reason"] == "max_tokens"
