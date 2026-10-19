# Zanbot

English | [简体中文](README.zh-CN.md)

A multi-turn terminal chat assistant built with Python and requests, supporting Anthropic API keys and Sign in with ChatGPT.

## ChatGPT plan sign-in (PowerShell)

```powershell
uv sync
uv run python main.py --login
uv run python main.py --models
uv run python main.py --provider chatgpt --model <slug-from-model-list>
```

Replace `<slug-from-model-list>` with a model slug returned by `--models` before running the last command. The browser opens for **Continue with ChatGPT**. Authorize Zanbot to use your plan. No Anthropic or OpenAI API key is required in this mode. Account eligibility, model access, and usage limits are determined by OpenAI; sign-in alone does not guarantee inference access. Review app permissions and usage at [ChatGPT Settings](https://chatgpt.com/settings/usage).

Optional defaults: set `ZANBOT_PROVIDER=chatgpt` and `CHATGPT_MODEL` in your environment. The default provider remains `anthropic`.

- `--accounts`: show saved account registrations and the active account.
- `--add-account`: register another account/workspace, keeping existing registrations.
- `--account <client-id>`: select a saved registration, then launch chat again.
- `--login`: sign in again with the selected registration, or register the first account.
- `--logout`: attempt remote revocation and clear the active account's local tokens; retain its registration for later sign-in.

On Windows, encrypted credentials live under `%LOCALAPPDATA%/zanbot`, outside this repository. The encryption key is held in the OS credential manager through `keyring`. Other platforms use `~/.config/zanbot` and require a configured secure OS keyring. Token renewal is automatic and serialized across processes. If renewal fails or a request returns HTTP 401, run `--login` again. Never copy tokens into project files.

The ChatGPT provider uses the public Responses API with `store=false` and `stream=true`. It sends full conversation history and saves a turn only after `response.completed`. It does not use ChatGPT website endpoints or another application's cached credentials. The current plan-sharing route does not accept `max_output_tokens`, so `Config.max_tokens` applies only to Anthropic.

See the [integration design](docs/chatgpt-sign-in.md) and [official sign-in documentation](https://developers.openai.com/siwc/token-sharing-open-source/sign-in). Automated tests mock authentication and inference; a real browser sign-in and completed inference are required to verify your account's access.

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
- `/memory`: list saved long-term memories with their IDs.
- `/remember [kind] <text>`: save a memory. Optional kind: profile, preference, project, event, relationship, or note (default).
- `/forget <id>`: delete a memory and erase its text.
- `/memory off` / `/memory on`: pause or resume automatic memory for this session.
- `/exit`, Ctrl+C, or end of input: exit the application.
- Blank input is ignored, and failed requests are not saved to the history.

History is kept in memory and is lost when the application exits. Each turn sends the full history, so longer conversations use more input tokens and may reach the context limit. Use `/clear` to reset it.
The current version supports multi-turn text conversations and streaming output. Tool calling is not yet implemented.

Long-term memories are stored in `%LOCALAPPDATA%/zanbot/memory.db` on Windows (`~/.config/zanbot/memory.db` elsewhere), outside this repository and unencrypted. Each turn adds today's date and the saved memories (up to 2,000 characters, profile first) to the system prompt as background data. Text that looks like a password, key, or token is refused. After each completed turn, a background request asks the model which durable facts about you to add, update, or delete; changes appear as a `✓ Remembered #id` line, undoable with `/forget <id>`. Extraction uses one extra request per turn, counted against your usage. Set `ZANBOT_MEMORY_MODEL` to use a cheaper model for it (default: the chat model, same provider). Failed or interrupted turns are never extracted, and extraction failures never interrupt chat. Start with `--no-memory` to neither read nor write memories. See the [memory design](docs/long-term-memory.md).
A turn is saved only after the complete ending event is received. If the stream is interrupted, the displayed fragments are not saved to the history; you can enter your message again to retry.

## File responsibilities

- `main.py`: terminal interaction.
- `zanbot/config.py`: environment variables and configuration.
- `zanbot/llm.py`: HTTP requests and error handling.
- `zanbot/streaming.py`: SSE event parsing, text callbacks, and complete response assembly.
- `zanbot/agent.py`: conversation history and individual turns.
- `zanbot/prompts.py`: the system prompt and memory injection.
- `zanbot/memory.py`: local SQLite memory storage, secret filtering, and recall.
- `zanbot/memory_extract.py`: extraction prompt, reply validation, and the background worker.
- `zanbot/chatgpt_auth.py`: OAuth, encrypted credentials, account selection, renewal, and logout.
- `zanbot/chatgpt.py`: ChatGPT model catalog and Responses API streaming.

## Token Monitor integration

Completed ChatGPT responses with API-reported usage automatically produce Codex-compatible JSONL records under `%LOCALAPPDATA%/zanbot/usage/codex` on Windows (`~/.config/zanbot/usage/codex` elsewhere). In Token Monitor, add this absolute directory to the **Codex custom scan paths**. Keep Codex tracking enabled. Zanbot appears under Codex because Token Monitor has no native Zanbot parser; project breakdown identifies the working directory and model breakdown shows the returned model.

Records contain timestamp, model, working directory, and token counters. They never contain prompts, answers, email addresses, or authentication tokens. Cached input and reasoning counts remain subsets of input and output, avoiding double counting. All turns in one chat share a session log with cumulative counters, deduplicated by response ID. `/clear` or restarting the app starts a new session. Previously completed requests cannot be recovered because usage was not saved. Interrupted/failed responses and Anthropic usage are not exported by this adapter.

The installed Token Monitor 0.66.0 parser was verified against a real Zanbot response. Its displayed cost is an API-price estimate, not a ChatGPT subscription bill or remaining plan quota. Usage logs may be included in Token Monitor sync if you have enabled that feature.

Implementation: `zanbot/usage.py`. References: [Token Monitor source paths](https://github.com/Javis603/token-monitor/blob/main/src/shared/clientSources.js), [custom scan paths](https://github.com/Javis603/token-monitor/blob/main/src/shared/customScanPaths.js).

## Offline tests

```powershell
uv run python -m unittest discover -s tests -v
```

Tests use a mocked API and do not consume API credits.
