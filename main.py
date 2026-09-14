from zanbot.agent import run_turn
from zanbot.config import load_config
from zanbot.llm import APIError


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
                text, truncated = run_turn(messages, user_input, config)
            except APIError as exc:
                print(f"Request failed: {exc}\nThis turn was not saved. Please try again.")
                continue
            print(f"\nZanbot: {text}")
            if truncated:
                print('(The response reached the output limit. You can type "continue".)')
    except (EOFError, KeyboardInterrupt):
        pass
    print("\nGoodbye!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
