import requests

from zanbot.prompts import SYSTEM_PROMPT
from zanbot.streaming import read_message


class APIError(Exception):
    """An API error with a message suitable for display to the user."""


def create_message(messages, config, on_text=None):
    if config.provider == "chatgpt":
        from zanbot.chatgpt import create_message as create_chatgpt_message

        return create_chatgpt_message(messages, config, on_text)

    url = "https://api.anthropic.com/v1/messages"

    try:
        response = requests.post(
            url,
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.model,
                "max_tokens": config.max_tokens,
                "system": SYSTEM_PROMPT,
                "messages": messages,
                "stream": True,
            },
            stream=True,
            timeout=(10, 120),
        )
    except requests.Timeout as exc:
        raise APIError("The connection or response timed out. Please try again later.") from exc
    except requests.RequestException as exc:
        raise APIError("The network request failed. Please check your connection.") from exc
    try:
        if not response.ok:
            hints = {
                401: "Invalid API key. Please check ANTHROPIC_API_KEY.",
                403: "Your account does not have access permission.",
                404: "Model not found. Please check ANTHROPIC_MODEL.",
                429: "Request or quota limit reached. Please try again later.",
            }
            hint = hints.get(response.status_code, "Check your model, account credits, or service status. If the history is too long, use /clear to start a new conversation.")
            raise APIError(f"HTTP {response.status_code}: {hint}")
        # A small chunk size avoids buffering short events before displaying them.
        return read_message(response.iter_lines(chunk_size=1), on_text)
    except requests.Timeout as exc:
        raise APIError("The response timed out while streaming. Please try again later.") from exc
    except requests.RequestException as exc:
        raise APIError("The network connection failed while streaming. Please try again.") from exc
    except ValueError as exc:
        raise APIError(f"Invalid or incomplete stream: {exc}") from exc
    finally:
        response.close()
