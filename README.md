# Zanbot

English | [简体中文](README.zh-CN.md)

A multi-turn terminal chat assistant built with Python, requests, and the Anthropic Messages API.

## Run (PowerShell)

```powershell
uv sync
$env:ANTHROPIC_API_KEY = "your-api-key"
uv run python main.py
```

If you have already saved the key as a Windows user environment variable but your current terminal has not loaded it:

```powershell
$env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
```

The default model is `claude-sonnet-4-5`. Set the `ANTHROPIC_MODEL` environment variable to use a different model.
Do not put your actual API key in project files.

- Enter a message and press Enter to start or continue a conversation.
- Responses appear incrementally as text arrives, without waiting for the entire answer.
- `/clear`: clear the history and start a new conversation.
- `/exit`, Ctrl+C, or end of input: exit the application.
- Blank input is ignored, and failed requests are not saved to the history.

History is kept in memory and is lost when the application exits. Each turn sends the full history, so longer conversations use more input tokens and may reach the context limit. Use `/clear` to reset it.
The current version supports multi-turn text conversations and streaming output. Tool calling and persistent memory are not yet implemented.
A turn is saved only after the complete ending event is received. If the stream is interrupted, the displayed fragments are not saved to the history; you can enter your message again to retry.

## File responsibilities

- `main.py`: terminal interaction.
- `zanbot/config.py`: environment variables and configuration.
- `zanbot/llm.py`: HTTP requests and error handling.
- `zanbot/streaming.py`: SSE event parsing, text callbacks, and complete response assembly.
- `zanbot/agent.py`: conversation history and individual turns.
- `zanbot/prompts.py`: the system prompt.

## Offline tests

```powershell
uv run python -m unittest discover -s tests -v
```

Tests use a mocked API and do not consume API credits.
