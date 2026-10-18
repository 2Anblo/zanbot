"""ChatGPT plan inference through the public Responses API."""

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
            if not content:
                raise ValueError("ChatGPT did not return a text answer.")
            return {"content": content, "stop_reason": "end_turn",
                    "usage": response.get("usage"), "model": response.get("model"), "id": response.get("id")}
    raise ValueError("The stream ended before response.completed.")


def create_message(messages, config, on_text=None):
    from zanbot.llm import APIError

    try:
        token = get_access_token()
        inputs = []
        for message in messages:
            content = message["content"]
            text = content if isinstance(content, str) else "\n".join(block["text"] for block in content)
            inputs.append({"role": message["role"], "content": text})
        with requests.post(
            RESOURCE + "/responses",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": config.model, "instructions": SYSTEM_PROMPT, "input": inputs,
                  "store": False, "stream": True},
            stream=True, timeout=(10, 120),
        ) as response:
            if not response.ok:
                hints = {401: "Run --login to renew your session.",
                         403: "Check ChatGPT plan sharing permission for Zanbot.",
                         429: "Your ChatGPT plan usage limit may have been reached."}
                raise APIError(f"ChatGPT HTTP {response.status_code}: " + hints.get(
                    response.status_code, "Check the selected model and ChatGPT service status."))
            result = read_response(response.iter_lines(chunk_size=1), on_text)
            record_chatgpt_usage(result, config.model, config.session_id)
            return result
    except (AuthError, OSError, Timeout) as exc:
        raise APIError(str(exc)) from exc
    except requests.RequestException as exc:
        raise APIError("The ChatGPT network request failed or timed out.") from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise APIError("ChatGPT returned an invalid or incomplete response. Check plan access and usage limits.") from exc
