"""Terminal frontends: read input, pass it to the session, and print the events."""

import sys
import threading

from zanbot.commands import WAIT_SECONDS, complete
from zanbot.session import (ChatSession, Cleared, ExitRequested, Info, MemoryNotice, ReplyFailed,
                            ReplyFinished, ReplyStarted, StatusChanged, TextDelta)


def print_banner(store):
    print("Zanbot is ready. Type /help for commands, /clear to start over, or /exit to quit.")
    if store is not None:
        print("Memory: saved automatically. /memory to list, /forget <id> to delete, /memory off to pause.")


class PlainTerminal:
    """The input()-based frontend, used for pipes, unsupported terminals, --plain, and tests."""

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
        print_banner(store)
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


VK_RETURN = 13


def windows_console_input():
    """Read the Windows console in legacy mode, where key events keep their modifier state.

    prompt_toolkit prefers VT input on Windows 10+, but there Shift+Enter arrives as a plain
    carriage return, identical to Enter. Legacy key records report Shift and Ctrl, so this reader
    turns Shift+Enter into Meta+Enter; prompt_toolkit already turns Ctrl+Enter into Meta+Ctrl+J.
    """
    from prompt_toolkit.input.win32 import ConsoleInputReader, Win32Input
    from prompt_toolkit.key_binding import KeyPress
    from prompt_toolkit.keys import Keys

    class ModifierAwareReader(ConsoleInputReader):
        def _event_to_key_presses(self, ev):
            presses = super()._event_to_key_presses(ev)
            if (ev.VirtualKeyCode == VK_RETURN and ev.ControlKeyState & self.SHIFT_PRESSED
                    and [press.key for press in presses] == [Keys.ControlM]):
                return [KeyPress(Keys.Escape, ""), presses[0]]
            return presses

    console = Win32Input()
    console.console_input_reader.close()
    console.console_input_reader = ModifierAwareReader()
    console._use_virtual_terminal_input = False  # Also keeps raw_mode() out of VT input.
    return console


def format_status(status):
    parts = [status.provider, status.model]
    if status.memories is None:
        parts.append("memory off")
    else:
        parts.append(f"{status.memories} memories")
        parts.append("auto-memory " + ("on" if status.auto_memory else "paused"))
        if status.saving:
            parts.append("saving…")
    return " " + " · ".join(parts) + "    /help for commands"


class InteractiveTerminal(PlainTerminal):
    """prompt_toolkit frontend: command completion, a status bar, history, and live memory notices."""

    def __init__(self, history_path=None, input=None, output=None):
        super().__init__()
        self.history_path = history_path
        self.input = input
        self.output = output
        self.session = None
        self.status_text = ""
        self.replying = False
        self.held = []
        self.lock = threading.Lock()

    def render(self, event):
        # Memory events can arrive on the worker thread. patch_stdout() prints them above the
        # prompt, but a notice in the middle of a streaming answer would split it, so hold those.
        if isinstance(event, MemoryNotice):
            with self.lock:
                if self.replying:
                    self.held.append(event)
                    return
            print(event.text)
            return
        if isinstance(event, StatusChanged):
            self.refresh_status()
            return
        if isinstance(event, ReplyStarted):
            with self.lock:
                self.replying = True
        super().render(event)
        if isinstance(event, (ReplyFinished, ReplyFailed)):
            with self.lock:
                self.replying = False
                held, self.held = self.held, []
            for notice in held:
                print(notice.text)

    def refresh_status(self):
        # Cache the text: the toolbar redraws on every keystroke, and status() reads the database.
        self.status_text = format_status(self.session.status())
        from prompt_toolkit.application import get_app_or_none

        app = get_app_or_none()
        if app is not None:
            app.invalidate()

    def build_prompt(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.application import get_app
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.application.current import get_app_session
        from prompt_toolkit.key_binding import KeyBindings

        # Create the output now: on an unsupported console this raises here, inside run()'s
        # fallback, rather than later in patch_stdout() or prompt().
        self.output = self.output or get_app_session().output
        if self.input is None and sys.platform == "win32":
            self.input = windows_console_input()
        session = self.session

        class SlashCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                start, suggestions = complete(session, text)
                for suggestion in suggestions:
                    yield Completion(suggestion.text, start_position=start - len(text),
                                     display_meta=suggestion.meta)

        bindings = KeyBindings()

        # On Windows, windows_console_input() reports Shift+Enter as Meta+Enter and Ctrl+Enter as
        # Meta+Ctrl+J. Other terminals can be set to send Escape+Enter for Shift+Enter. Ctrl+J is a
        # fallback, and is what Ctrl+Enter sends in VT input mode.
        @bindings.add("escape", "enter")
        @bindings.add("escape", "c-j")
        @bindings.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        history = InMemoryHistory()
        if self.history_path is not None:
            try:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(self.history_path))
            except OSError:
                print("Warning: Could not open the input history file. History is kept for this session only.")
        return PromptSession(
            history=history,
            completer=SlashCompleter(),
            # Popups only for commands, so they never interrupt ordinary (IME) typing.
            complete_while_typing=Condition(lambda: get_app().current_buffer.text.startswith("/")),
            bottom_toolbar=lambda: self.status_text,
            key_bindings=bindings,
            input=self.input,
            output=self.output,
        )

    def run(self, config, store=None, worker=None):
        self.session = ChatSession(config, self.render, store, worker)
        try:
            prompt = self.build_prompt()
        except Exception:  # For example, no Windows console buffer under mintty or an IDE pane.
            print("This terminal does not support interactive input; using plain input instead.")
            return PlainTerminal().run(config, store, worker)
        from prompt_toolkit.patch_stdout import patch_stdout

        self.session.live_notices()
        self.refresh_status()
        print_banner(store)
        with patch_stdout(raw=True):
            try:
                while not self.exit_requested:
                    text = prompt.prompt("\nYou: ")
                    self.session.submit(text)
                    self.refresh_status()
            except (EOFError, KeyboardInterrupt):
                pass
            self.session.close(WAIT_SECONDS)
        print("\nGoodbye!")
        return 0
