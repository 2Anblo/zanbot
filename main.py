from zanbot.agent import run_turn
from zanbot.config import load_config
from zanbot.llm import APIError


def print_chunk(text):
    print(text, end="", flush=True)


def main():
    try:
        config = load_config()
    except ValueError as exc:
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
    raise SystemExit(main())
