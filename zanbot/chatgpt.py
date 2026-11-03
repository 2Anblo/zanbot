"""ChatGPT plan inference through the public Responses API."""

import json

import requests
from filelock import Timeout

from zanbot.chatgpt_auth import AuthError, RESOURCE, get_access_token
from zanbot.prompts import SYSTEM_PROMPT
from zanbot.streaming import iter_events
from zanbot.usage import record_chatgpt_usage


def list_models():
    token = get_access_token()
    try:
        with requests.get(RESOURCE + "/models", headers={"Authorization": f"Bearer {token}"}, timeout=(10, 30)) as response:
            if not response.ok:
                raise AuthError(f"Could not list ChatGPT models (HTTP {response.status_code}).")
            models = response.json()["models"]
            return [model for model in models if model.get("visibility") == "list"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise AuthError("Could not read the ChatGPT model catalog.") from exc


def _tool_use(item):
    """A Responses function_call item as a tool_use block. Its call_id is the id results must answer."""
    if not item.get("call_id") or not item.get("name"):
        raise ValueError("ChatGPT returned a tool call without an id or name.")
    arguments = item.get("arguments") or ""
    try:
        # A tool without parameters may come back with empty arguments.
        tool_input = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        tool_input = None
    if not isinstance(tool_input, dict):
        raise ValueError("ChatGPT returned invalid tool input.")
    return {"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": tool_input}


def to_tools(schemas):
    """Anthropic-style tool definitions as Responses function tools."""
    # Not strict: strict mode needs every property listed as required, and ours have optional ones.
    return [{"type": "function", "name": s["name"], "description": s["description"],
             "parameters": s["input_schema"], "strict": False} for s in schemas]


def to_input(messages):
    """Zanbot's history (Anthropic shape) as Responses input items."""
    items = []
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        texts = []

        def flush():
            if texts:
                items.append({"role": role, "content": "\n".join(texts)})
                texts.clear()

        for block in content:
            kind = block.get("type")
            if kind == "text":
                texts.append(block["text"])
                continue
            flush()  # Keep text and calls in their original order.
            if kind == "tool_use":
                items.append({"type": "function_call", "call_id": block["id"], "name": block["name"],
                              "arguments": json.dumps(block["input"], ensure_ascii=False)})
            elif kind == "tool_result":
                output = block["content"]
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                              "output": "Error: " + output if block.get("is_error") else output})
            elif kind == "reasoning":
                items.append(block["item"])
            # Anthropic thinking blocks mean nothing to this provider and are skipped.
        flush()
    return items


def read_response(lines, on_text=None):
    completed_items = {}
    for event in iter_events(lines):
        kind = event.get("type")
        if kind == "response.output_item.done":
            index, item = event.get("output_index"), event.get("item")
            if not isinstance(index, int) or index < 0 or not isinstance(item, dict):
                raise ValueError("Invalid completed output item.")
            completed_items[index] = item
        elif kind == "response.output_text.delta":
            text = event.get("delta")
            if not isinstance(text, str):
                raise ValueError("Invalid text delta.")
            if on_text is not None:
                on_text(text)
        elif kind in {"error", "response.failed", "response.incomplete"}:
            raise ValueError("ChatGPT could not complete this response. Check your plan access and usage limits.")
        elif kind == "response.completed":
            response = event.get("response", {})
            if response.get("status") != "completed":
                raise ValueError("ChatGPT returned a non-completed response.")
            content = []
            # Some plan responses omit items from the final event after streaming them.
            output = response.get("output") or [completed_items[index] for index in sorted(completed_items)]
            for item in output:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            content.append({"type": "text", "text": part["text"]})
                        elif part.get("type") == "refusal":
                            text = part["refusal"]
                            content.append({"type": "text", "text": text})
                            if on_text is not None:
                                on_text(text)
                elif item.get("type") == "function_call":
                    content.append(_tool_use(item))
                elif item.get("type") == "reasoning":
                    # Opaque (encrypted) reasoning, sent back unchanged so the model keeps its train of thought.
                    content.append({"type": "reasoning", "item": item})
            calls = any(block["type"] == "tool_use" for block in content)
            if not calls and not any(block["type"] == "text" for block in content):
                raise ValueError("ChatGPT did not return a text answer.")
            # The rest of Zanbot speaks the Anthropic shape: tool calls are tool_use blocks.
            return {"content": content, "stop_reason": "tool_use" if calls else "end_turn",
                    "usage": response.get("usage"), "model": response.get("model"), "id": response.get("id")}
    raise ValueError("The stream ended before response.completed.")


def create_message(messages, config, on_text=None, system=None, tools=None, tool_choice=None):
    from zanbot.llm import APIError, post_with_retry, server_error_hint

    try:
        token = get_access_token()
        body = {"model": config.model, "instructions": system or SYSTEM_PROMPT, "input": to_input(messages),
                "store": False, "stream": True}
        if tools:
            body["tools"] = to_tools(tools)
            # With store=false the server keeps nothing, so ask for the reasoning to send back next step.
            body["include"] = ["reasoning.encrypted_content"]
        if tool_choice and tool_choice.get("type") == "none":
            body["tool_choice"] = "none"
        with post_with_retry(lambda: requests.post(
            RESOURCE + "/responses",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            stream=True, timeout=(10, 120),
        )) as response:
            if not response.ok:
                hints = {401: "Run --login to renew your session.",
                         403: "Check ChatGPT plan sharing permission for Zanbot.",
                         429: "Your ChatGPT plan usage limit may have been reached."}
                hint = (hints.get(response.status_code) or server_error_hint(response.status_code, "ChatGPT")
                        or "Check the selected model and ChatGPT service status.")
                raise APIError(f"ChatGPT HTTP {response.status_code}: {hint}")
            result = read_response(response.iter_lines(chunk_size=1), on_text)
            record_chatgpt_usage(result, config.model, config.session_id)
            return result
    except (AuthError, OSError, Timeout) as exc:
        raise APIError(str(exc)) from exc
    except requests.RequestException as exc:
        raise APIError("The ChatGPT network request failed or timed out.") from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise APIError("ChatGPT returned an invalid or incomplete response. Check plan access and usage limits.") from exc
