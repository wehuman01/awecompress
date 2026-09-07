"""SQLite store for frozen summaries: one row per session key, plus an
append-only event log for `awecompress status`.

WAL mode so `status` can read while the proxy writes. Writes are single-row
and rare (once per span growth), so one shared connection behind a thread
lock is enough — no pool, no ORM.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionRecord:
    key: str
    upto: int
    prefix_hash: str
    summary: str
    saved_tokens: int = 0   # cumulative estimate across all extensions
    calls: int = 0          # summary LLM calls made for this session
    updated_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    key         TEXT PRIMARY KEY,
    upto        INTEGER NOT NULL,
    prefix_hash TEXT NOT NULL,
    summary     TEXT NOT NULL,
    saved_tokens INTEGER NOT NULL DEFAULT 0,
    calls       INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    session    TEXT NOT NULL,
    action     TEXT NOT NULL,
    span_tokens INTEGER NOT NULL,
    summary_tokens INTEGER NOT NULL,
    model      TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    def __init__(self, path: "str | Path"):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def get(self, key: str) -> "SessionRecord | None":
        with self._lock:
            row = self._db.execute(
                "SELECT key, upto, prefix_hash, summary, saved_tokens, calls, updated_at"
                " FROM sessions WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return SessionRecord(row["key"], row["upto"], row["prefix_hash"],
                             row["summary"], row["saved_tokens"], row["calls"],
                             row["updated_at"])

    def put(self, record: SessionRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions"
                " (key, upto, prefix_hash, summary, saved_tokens, calls, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.key, record.upto, record.prefix_hash, record.summary,
                 record.saved_tokens, record.calls, record.updated_at))
            self._db.commit()

    def log_event(self, session: str, action: str, span_tokens: int,
                  summary_tokens: int, model: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, session, action, span_tokens, summary_tokens, model)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), session, action, span_tokens, summary_tokens, model))
            self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            sessions = self._db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            calls = self._db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            saved = self._db.execute(
                "SELECT COALESCE(SUM(saved_tokens), 0) AS n FROM sessions").fetchone()["n"]
        return {"sessions": sessions, "calls": calls, "saved_tokens": saved}

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions")
            self._db.execute("DELETE FROM events")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
