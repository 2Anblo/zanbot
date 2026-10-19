"""Slash commands, registered once for dispatch, /help, and later completion."""

from dataclasses import dataclass
import difflib
import sqlite3

from zanbot.memory import KINDS, MemoryStoreError

WAIT_SECONDS = 10


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    help: str
    run: object  # run(session, rest)
    needs_memory: bool = False


def run_help(session, rest):
    width = max(len(f"{c.name} {c.args}".strip()) for c in COMMANDS)
    session.info(*[f"  {f'{c.name} {c.args}'.strip():<{width}}  {c.help}" for c in COMMANDS])


def run_clear(session, rest):
    session.clear()


def run_exit(session, rest):
    session.request_exit()


def run_memory(session, rest):
    worker = session.worker
    if rest.lower() in {"on", "off"}:
        worker.enabled = rest.lower() == "on"
        session.info("Automatic memory is " + ("on." if worker.enabled else "paused for this session."))
        return
    if rest:
        session.info("Usage: /memory [on|off]")
        return
    # List what the latest turns produced, not a snapshot from mid-extraction.
    if not worker.wait(WAIT_SECONDS):
        session.info("Memory extraction is still running; this list may be incomplete.")
    session.flush_notices()
    memories = session.store.active()
    if not memories:
        session.info("No memories saved yet. Use /remember <text> to add one.")
    session.info(*[f"#{m.id} [{m.kind}] {m.content}" + (f" (until {m.valid_until})" if m.valid_until else "")
                   for m in memories])


def run_remember(session, rest):
    kind, _, content = rest.partition(" ")
    if kind.lower() not in KINDS or not content.strip():
        kind, content = "note", rest
    memory_id = session.store.add(content, kind=kind.lower(), session_id=session.config.session_id)
    session.info(f"Remembered #{memory_id}.")


def run_forget(session, rest):
    if not rest.lstrip("#").isdigit():
        session.info("Usage: /forget <id>")
    elif session.store.forget(int(rest.lstrip("#"))):
        session.info("Forgotten.")
    else:
        session.info("No active memory has that ID.")


COMMANDS = (
    Command("/help", "", "Show all commands", run_help),
    Command("/clear", "", "Clear the conversation and start a new one", run_clear),
    Command("/memory", "[on|off]", "List memories, or pause/resume automatic memory", run_memory, True),
    Command("/remember", "[kind] <text>", "Save a memory (kind: " + ", ".join(KINDS) + ")", run_remember, True),
    Command("/forget", "<id>", "Delete a memory and erase its text", run_forget, True),
    Command("/exit", "", "Quit Zanbot", run_exit),
)
BY_NAME = {command.name: command for command in COMMANDS}


def dispatch(session, text):
    """Run one slash command line. Unknown names are reported, never sent to the model."""
    name, _, rest = text.partition(" ")
    name, rest = name.lower(), rest.strip()
    command = BY_NAME.get(name)
    if command is None:
        close = difflib.get_close_matches(name, BY_NAME, n=1)
        hint = f" Did you mean {close[0]}?" if close else ""
        session.info(f"Unknown command {name}.{hint} Type /help for all commands.",
                     "To send a message that starts with /, add a space before it.")
        return
    if command.needs_memory and session.store is None:
        session.info("Memory is off for this session.")
        return
    try:
        command.run(session, rest)
    except MemoryStoreError as exc:
        session.info(f"Memory not saved: {exc}")
    except sqlite3.Error:
        session.info("Memory storage failed. Please try again.")
