import argparse
import uuid
from dataclasses import replace

from zanbot.agent import run_turn
from zanbot.config import load_config
from zanbot.llm import APIError


def print_chunk(text):
    print(text, end="", flush=True)


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
    messages = []
    print("Zanbot is ready. Use /clear to clear the conversation or /exit to quit.")
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
            try:
                print("\nZanbot: ", end="", flush=True)
                _, truncated = run_turn(messages, user_input, config, on_text=print_chunk)
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
