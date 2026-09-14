from zanbot.llm import APIError, create_message


def run_turn(messages, user_input, config):
    # Update history only after success so failed inputs do not carry into later turns.
    pending = [*messages, {"role": "user", "content": user_input}]
    response = create_message(pending, config)
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, list) or not content or any(
        not isinstance(block, dict)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
        for block in content
    ):
        raise APIError("The API did not return the expected text response.")
    if response.get("stop_reason") not in {"end_turn", "max_tokens", "stop_sequence", "refusal"}:
        raise APIError("The API returned a stop reason that this version does not support.")
    messages.extend([pending[-1], {"role": "assistant", "content": content}])
    return "\n".join(block["text"] for block in content), response["stop_reason"] == "max_tokens"
