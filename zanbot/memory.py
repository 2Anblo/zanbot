"""Long-term memory stored locally in SQLite, outside the repository."""

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
import sqlite3

KINDS = ("profile", "preference", "project", "event", "relationship", "note")
MAX_CONTENT = 200
RECALL_BUDGET = 2000
# Likely credentials: provider key prefixes, JWTs, long hex or base64 runs.
SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{10,}|\b[0-9a-fA-F]{32,}\b|[A-Za-z0-9+/=_-]{40,}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id          INTEGER PRIMARY KEY,
  content     TEXT NOT NULL,
  keywords    TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  valid_until TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  source      TEXT NOT NULL,
  session_id  TEXT,
  replaces    INTEGER REFERENCES memories(id)
);
"""


class MemoryStoreError(Exception):
    """A memory input or storage error safe to display."""


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    kind: str
    valid_until: str | None
    updated_at: str


def memory_directory():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "zanbot"


def check_content(content):
    """Normalize one fact to a single line and reject unsafe input."""
    content = " ".join(content.split())
    if not content:
        raise MemoryStoreError("Memory content must not be empty.")
    if len(content) > MAX_CONTENT:
        raise MemoryStoreError(f"Memory content must be at most {MAX_CONTENT} characters.")
    if SECRET_PATTERN.search(content):
        raise MemoryStoreError("This looks like a password, key, or token. Memories never store secrets.")
    return content


class MemoryStore:
    """Open a fresh connection per operation so later background threads can share the store."""

    def __init__(self, path=None):
        if path is None:
            directory = memory_directory()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "memory.db"
        self.path = Path(path)
        with closing(self.connect()) as connection, connection:
            connection.executescript(SCHEMA)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def add(self, content, kind="note", source="manual", session_id=None, valid_until=None):
        if kind not in KINDS:
            raise MemoryStoreError("Memory kind must be one of: " + ", ".join(KINDS) + ".")
        content = check_content(content)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO memories (content, kind, valid_until, created_at, updated_at, source, session_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (content, kind, valid_until, now, now, source, session_id))
            return cursor.lastrowid

    def forget(self, memory_id):
        """Soft-delete and erase the text so forgetting is real."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE memories SET status = 'deleted', content = '', keywords = '', updated_at = ?"
                " WHERE id = ? AND status = 'active'", (now, memory_id))
            return cursor.rowcount == 1

    def active(self, today=None):
        """Return active, unexpired memories with profile first, then most recently updated."""
        today = (today or date.today()).isoformat()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id, content, kind, valid_until, updated_at FROM memories"
                " WHERE status = 'active' AND (valid_until IS NULL OR valid_until >= ?)"
                " ORDER BY kind != 'profile', updated_at DESC, id DESC", (today,)).fetchall()
        return [Memory(**row) for row in rows]

    def recall(self, budget=RECALL_BUDGET, today=None):
        """Select memories for the system prompt without exceeding the character budget."""
        selected, used = [], 0
        for memory in self.active(today):
            cost = len(memory.content) + len(memory.kind) + 5
            if used + cost > budget:
                continue
            selected.append(memory)
            used += cost
        return selected
