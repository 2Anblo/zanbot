"""A chat session independent of any user interface. Frontends render the events it emits."""

from dataclasses import dataclass, replace
from datetime import date
import json
import queue
import sqlite3
import threading
import uuid

from zanbot.agent import NoHooks, effects_note, run_turn
from zanbot.commands import dispatch
from zanbot.llm import APIError
from zanbot.prompts import build_system_prompt


@dataclass(frozen=True)
class ReplyStarted:
    pass


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReplyFinished:
    text: str
    truncated: bool


@dataclass(frozen=True)
class ReplyFailed:
    message: str
    cancelled: bool = False


class ReplyCancelled(Exception):
    """Raised inside an abandoned reply thread to stop it at its next text chunk."""


@dataclass(frozen=True)
class Info:
    lines: tuple


@dataclass(frozen=True)
class MemoryNotice:
    text: str


@dataclass(frozen=True)
class Cleared:
    pass


@dataclass(frozen=True)
class ExitRequested:
    pass


@dataclass(frozen=True)
class StatusChanged:
    """Something shown in a status bar changed, possibly from the memory worker thread."""


@dataclass(frozen=True)
class PermissionRequest:
    """A tool call with side effects, waiting for the user's answer: "y", "n", or "a" (always, this session)."""
    tool: str
    arguments: str  # JSON, shortened for display


@dataclass(frozen=True)
class Status:
    provider: str
    model: str
    memories: int | None  # None when memory is off
    auto_memory: bool
    saving: bool


class ChatSession:
    """Own the history, memory, and commands for one conversation."""

    def __init__(self, config, emit, store=None, worker=None, tools=None, ask=None):
        self.config = config
        self.emit = emit
        self.store = store
        self.worker = worker
        self.tools = tools
        # ask(PermissionRequest) -> "y" | "n" | "a", called on the main thread. None: the frontend
        # cannot ask (pipes, --plain), so calls with side effects are denied.
        self.ask = ask
        self.allowed_tools = set()  # Answered "a" (always) in this conversation.
        self.permission_requests = None  # The current reply's queue; see wait_for().
        self.messages = []
        # Side-effect calls from turns that failed or were cancelled, one list per turn.
        # A cancelled turn's thread may still add to its list after the turn is abandoned.
        self.unsaved_effects = []

    def info(self, *lines):
        if lines:
            self.emit(Info(lines))

    def submit(self, text):
        """Handle one line of input: a slash command or a message for the model.

        Ctrl+C (KeyboardInterrupt) cancels the current reply or command and returns normally.
        """
        try:
            if text.startswith("/"):
                dispatch(self, text.strip())
                return
            text = text.strip()
            if text:
                self.reply(text)
        except KeyboardInterrupt:
            self.info("Cancelled.")

    def active_tools(self):
        # Tools are Anthropic-only for now (roadmap N7).
        return self.tools if self.config.provider == "anthropic" else None

    def reply(self, text):
        system = self.system_prompt()
        tools = self.active_tools()
        # run_turn extends this copy only on success; it is adopted only if the turn was not cancelled.
        pending = list(self.messages)
        cancelled = threading.Event()
        outcome = {}
        earlier = [call for calls in self.unsaved_effects for call in calls]
        content = text
        if earlier:
            content = [{"type": "text", "text": effects_note(earlier)}, {"type": "text", "text": text}]
        effects = []
        self.unsaved_effects.append(effects)

        def on_text(chunk):
            if cancelled.is_set():
                raise ReplyCancelled()
            self.emit(TextDelta(chunk))

        # Each reply gets its own queue, so a cancelled reply's thread can never ask in a later turn.
        requests = self.permission_requests = queue.Queue()
        session = self

        class Hooks(NoHooks):
            def check_cancelled(self):
                if cancelled.is_set():
                    raise ReplyCancelled()

            def ask_permission(self, call):
                # Runs on the reply thread; the main thread asks the user in wait_for().
                if call["name"] in session.allowed_tools:
                    return True
                if session.ask is None:
                    session.info(f"({call['name']} was denied: this terminal cannot ask for permission. "
                                 "Run Zanbot without --plain to allow it.)")
                    return None
                pending = {"call": call, "answered": threading.Event(), "allowed": False}
                requests.put(pending)
                while not pending["answered"].wait(0.1):
                    if cancelled.is_set():
                        raise ReplyCancelled()
                if cancelled.is_set():
                    raise ReplyCancelled()
                return pending["allowed"]

        def work():
            try:
                outcome["result"] = run_turn(pending, content, self.config, on_text=on_text, system=system,
                                             tools=tools, hooks=Hooks(), effects=effects)
            except ReplyCancelled:
                pass
            except APIError as exc:
                outcome["error"] = exc
            except BaseException as exc:  # Re-raised on the calling thread below.
                outcome["crash"] = exc

        self.emit(ReplyStarted())
        # The request runs on its own thread: on Windows, Ctrl+C cannot interrupt a blocking socket
        # read, so a reply still waiting for its first token could not be cancelled otherwise.
        thread = threading.Thread(target=work, name="zanbot-reply", daemon=True)
        thread.start()
        try:
            self.wait_for(thread)
        except KeyboardInterrupt:
            # The abandoned thread stops at its next text chunk, or ends on its own timeout.
            cancelled.set()
            self.emit(ReplyFailed("Cancelled.", cancelled=True))
            return
        if "crash" in outcome:
            raise outcome["crash"]
        if "error" in outcome:
            self.emit(ReplyFailed(str(outcome["error"])))
            return
        answer, truncated = outcome["result"]
        self.messages[:] = pending
        # The note reached the model as part of a saved turn, so it is no longer needed.
        self.unsaved_effects = []
        self.emit(ReplyFinished(answer, truncated))
        if self.worker is not None:
            self.worker.submit(self.config, text, answer)
            self.emit(StatusChanged())

    def wait_for(self, thread):
        # Short joins keep the main thread responsive to Ctrl+C. Permission questions are asked
        # here, so the terminal is only ever read from the main thread.
        while thread.is_alive():
            thread.join(0.1)
            requests = self.permission_requests
            while requests is not None and not requests.empty():
                self.answer_permission(requests.get_nowait())

    def answer_permission(self, pending):
        call = pending["call"]
        arguments = json.dumps(call["input"], ensure_ascii=False)
        if len(arguments) > 300:
            arguments = arguments[:300] + "…"
        # Ctrl+C here propagates out of wait_for and cancels the turn; the reply thread sees it.
        answer = self.ask(PermissionRequest(call["name"], arguments))
        if answer == "a":
            self.allowed_tools.add(call["name"])
        pending["allowed"] = answer in ("y", "a")
        pending["answered"].set()

    def system_prompt(self):
        memories = []
        if self.store is not None:
            try:
                memories = self.store.recall()
            except sqlite3.Error:
                self.info("Warning: Could not read memories for this turn.")
        if self.store is None:
            memory = "off"
        else:
            memory = "on" if self.worker is not None and self.worker.enabled else "paused"
        if self.active_tools() is not None:
            tools = True
        else:
            tools = "no-tools" if self.tools is None else "provider"
        return build_system_prompt(memories, date.today(), memory, tools=tools)

    def clear(self):
        self.messages.clear()
        self.allowed_tools.clear()
        self.config = replace(self.config, session_id="zanbot-" + uuid.uuid4().hex)
        self.emit(Cleared())

    def request_exit(self):
        self.emit(ExitRequested())

    def live_notices(self):
        """Emit worker notices as they happen. Only for frontends whose emit is safe from another thread."""
        if self.worker is not None:
            self.worker.listener = self._on_worker_update

    def _on_worker_update(self, notices):
        for notice in notices:
            self.emit(MemoryNotice(notice))
        self.emit(StatusChanged())

    def status(self):
        memories = None
        if self.store is not None:
            try:
                memories = len(self.store.active())
            except sqlite3.Error:
                pass
        worker = self.worker
        return Status(self.config.provider, self.config.model, memories,
                      worker is not None and worker.enabled, worker is not None and worker.busy)

    def flush_notices(self):
        """Emit memory notices collected by the background worker since the last flush."""
        if self.worker is not None:
            for notice in self.worker.take_notices():
                self.emit(MemoryNotice(notice))

    def close(self, timeout):
        """Wait for pending memory extraction before the program exits."""
        if self.worker is None:
            return
        if not self.worker.wait(0):
            self.info("Saving memories from the latest turn...")
        if not self.worker.close(timeout):
            self.info("Memory extraction did not finish; the latest turn may not be remembered.")
        self.flush_notices()
