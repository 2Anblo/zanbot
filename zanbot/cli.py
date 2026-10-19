"""Terminal frontend: read input, pass it to the session, and print the events."""

from zanbot.commands import WAIT_SECONDS
from zanbot.session import (ChatSession, Cleared, ExitRequested, Info, MemoryNotice, ReplyFailed,
                            ReplyFinished, ReplyStarted, TextDelta)


class PlainTerminal:
    """The input()-based frontend, also used for pipes and tests."""

    def __init__(self):
        self.exit_requested = False

    def render(self, event):
        if isinstance(event, ReplyStarted):
            print("\nZanbot: ", end="", flush=True)
        elif isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ReplyFinished):
            print()
            if event.truncated:
                print('(The response reached the output limit. You can type "continue".)')
        elif isinstance(event, ReplyFailed):
            print(f"\nRequest failed: {event.message}\n"
                  "This turn was not saved. Any text shown above may be incomplete. Please try again.")
        elif isinstance(event, Info):
            for line in event.lines:
                print(line)
        elif isinstance(event, MemoryNotice):
            print(event.text)
        elif isinstance(event, Cleared):
            print("Conversation cleared.")
        elif isinstance(event, ExitRequested):
            self.exit_requested = True

    def run(self, config, store=None, worker=None):
        session = ChatSession(config, self.render, store, worker)
        print("Zanbot is ready. Type /help for commands, /clear to start over, or /exit to quit.")
        if store is not None:
            print("Memory: saved automatically. /memory to list, /forget <id> to delete, /memory off to pause.")
        try:
            while not self.exit_requested:
                # The worker runs in the background, so show its notices between inputs.
                session.flush_notices()
                text = input("\nYou: ")
                session.flush_notices()
                session.submit(text)
        except (EOFError, KeyboardInterrupt):
            pass
        session.close(WAIT_SECONDS)
        print("\nGoodbye!")
        return 0
