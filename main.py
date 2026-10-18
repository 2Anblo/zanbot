import argparse
from dataclasses import replace
from datetime import date
import sqlite3
import uuid

from zanbot.agent import run_turn
from zanbot.config import load_config
from zanbot.llm import APIError
from zanbot.memory import KINDS, MemoryStore, MemoryStoreError
from zanbot.prompts import build_system_prompt


def print_chunk(text):
    print(text, end="", flush=True)


def handle_memory_command(store, user_input, session_id):
    """Run /memory, /remember, or /forget. Return False for other input."""
    command, _, rest = user_input.partition(" ")
    command, rest = command.lower(), rest.strip()
    if command not in {"/memory", "/remember", "/forget"}:
        return False
    if store is None:
        print("Memory is off for this session.")
        return True
    try:
        if command == "/memory":
            if rest:
                print("Usage: /memory")
                return True
            memories = store.active()
            if not memories:
                print("No memories saved yet. Use /remember <text> to add one.")
            for memory in memories:
                until = f" (until {memory.valid_until})" if memory.valid_until else ""
                print(f"#{memory.id} [{memory.kind}] {memory.content}{until}")
        elif command == "/remember":
            kind, _, content = rest.partition(" ")
            if kind.lower() not in KINDS or not content.strip():
                kind, content = "note", rest
            memory_id = store.add(content, kind=kind.lower(), session_id=session_id)
            print(f"Remembered #{memory_id}.")
        else:
            if not rest.lstrip("#").isdigit():
                print("Usage: /forget <id>")
            elif store.forget(int(rest.lstrip("#"))):
                print("Forgotten.")
            else:
                print("No active memory has that ID.")
    except MemoryStoreError as exc:
        print(f"Memory not saved: {exc}")
    except sqlite3.Error:
        print("Memory storage failed. Please try again.")
    return True


def recalled_system_prompt(store):
    memories = []
    if store is not None:
        try:
            memories = store.recall()
        except sqlite3.Error:
            print("Warning: Could not read memories for this turn.")
    return build_system_prompt(memories, date.today())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zanbot streaming chat")
    parser.add_argument("--provider", choices=["anthropic", "chatgpt"])
    parser.add_argument("--model")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--login", action="store_true", help="Continue with ChatGPT")
    actions.add_argument("--add-account", action="store_true")
    actions.add_argument("--accounts", action="store_true")
    actions.add_argument("--account", help="Select a saved ChatGPT client ID")
    actions.add_argument("--logout", action="store_true")
    actions.add_argument("--models", action="store_true")
    parser.add_argument("--no-memory", action="store_true", help="Do not read or write long-term memory")
    args = parser.parse_args([] if argv is None else argv)
    from zanbot.chatgpt_auth import AuthError, accounts, login, logout
    from filelock import Timeout

    try:
        if args.login or args.add_account:
            login(new=args.add_account)
            return 0
        if args.accounts or args.account:
            accounts(args.account)
            return 0
        if args.logout:
            logout()
            return 0
        if args.models:
            from zanbot.chatgpt import list_models

            for model in list_models():
                print(f"{model['slug']} | {model['display_name']}")
            return 0
        config = load_config(args.provider, args.model)
        if config.provider == "chatgpt":
            accounts()
    except (ValueError, AuthError, OSError, Timeout) as exc:
        print(f"Configuration error: {exc}")
        return 1
    store = None
    if not args.no_memory:
        try:
            store = MemoryStore()
        except (sqlite3.Error, OSError):
            print("Warning: Memory storage is unavailable. Continuing without memory.")
    messages = []
    print("Zanbot is ready. Use /clear to clear the conversation or /exit to quit.")
    print("Memory: /memory to list, /remember <text> to add, /forget <id> to delete.")
    try:
        while True:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue
            if user_input.lower() == "/exit":
                break
            if user_input.lower() == "/clear":
                messages.clear()
                config = replace(config, session_id="zanbot-" + uuid.uuid4().hex)
                print("Conversation cleared.")
                continue
            if handle_memory_command(store, user_input, config.session_id):
                continue
            system = recalled_system_prompt(store)
            try:
                print("\nZanbot: ", end="", flush=True)
                _, truncated = run_turn(messages, user_input, config, on_text=print_chunk, system=system)
            except APIError as exc:
                print(f"\nRequest failed: {exc}\nThis turn was not saved. Any text shown above may be incomplete. Please try again.")
                continue
            print()
            if truncated:
                print('(The response reached the output limit. You can type "continue".)')
    except (EOFError, KeyboardInterrupt):
        pass
    print("\nGoodbye!")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
