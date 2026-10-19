import argparse
import sqlite3

from zanbot.cli import PlainTerminal
from zanbot.config import load_config
from zanbot.memory import MemoryStore
from zanbot.memory_extract import MemoryWorker


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
    worker = MemoryWorker(store) if store is not None else None
    return PlainTerminal().run(config, store, worker)


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
