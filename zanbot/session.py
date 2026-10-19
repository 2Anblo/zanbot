"""A chat session independent of any user interface. Frontends render the events it emits."""

from dataclasses import dataclass, replace
from datetime import date
import sqlite3
import uuid

from zanbot.agent import run_turn
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
class Status:
    provider: str
    model: str
    memories: int | None  # None when memory is off
    auto_memory: bool
    saving: bool


class ChatSession:
    """Own the history, memory, and commands for one conversation."""

    def __init__(self, config, emit, store=None, worker=None):
        self.config = config
        self.emit = emit
        self.store = store
        self.worker = worker
        self.messages = []

    def info(self, *lines):
        if lines:
            self.emit(Info(lines))

    def submit(self, text):
        """Handle one line of input: a slash command or a message for the model."""
        if text.startswith("/"):
            dispatch(self, text.strip())
            return
        text = text.strip()
        if not text:
            return
        system = self.system_prompt()
        self.emit(ReplyStarted())
        try:
            answer, truncated = run_turn(self.messages, text, self.config,
                                         on_text=lambda chunk: self.emit(TextDelta(chunk)), system=system)
        except APIError as exc:
            self.emit(ReplyFailed(str(exc)))
            return
        self.emit(ReplyFinished(answer, truncated))
        if self.worker is not None:
            self.worker.submit(self.config, text, answer)
            self.emit(StatusChanged())

    def system_prompt(self):
        memories = []
        if self.store is not None:
            try:
                memories = self.store.recall()
            except sqlite3.Error:
                self.info("Warning: Could not read memories for this turn.")
        return build_system_prompt(memories, date.today())

    def clear(self):
        self.messages.clear()
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
