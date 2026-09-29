"""Registry of pipeline sessions (LangGraph threads) so the UI can list and resume them.

The graph's own checkpoints hold the state; this table only adds what the checkpointer does not
index: a human title, the last known stage, and which gate (if any) the run is waiting at.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idea_sessions (
    thread_id      TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    stage          TEXT NOT NULL,
    interrupted_at TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class SessionInfo:
    thread_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    stage: str
    interrupted_at: str | None


def _now() -> datetime:
    return datetime.now(UTC)


class SessionRegistry:
    """SQLite-backed list of sessions. The file (and its folder) is created on first use."""

    def __init__(self, db_path: Path, *, clock: Callable[[], datetime] = _now):
        self._db_path = Path(db_path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")  # the checkpointer shares this file
        connection.execute(_SCHEMA)
        return connection

    def upsert(self, thread_id: str, title: str, stage: str, interrupted_at: str | None = None) -> None:
        now = self._clock().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO idea_sessions (thread_id, title, stage, interrupted_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title = excluded.title,
                    stage = excluded.stage,
                    interrupted_at = excluded.interrupted_at,
                    updated_at = excluded.updated_at
                """,
                (thread_id, title, stage, interrupted_at, now, now),
            )

    def list(self, limit: int = 50) -> list[SessionInfo]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT thread_id, title, created_at, updated_at, stage, interrupted_at "
                "FROM idea_sessions ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            SessionInfo(
                thread_id=thread_id,
                title=title,
                created_at=datetime.fromisoformat(created),
                updated_at=datetime.fromisoformat(updated),
                stage=stage,
                interrupted_at=interrupted_at,
            )
            for thread_id, title, created, updated, stage, interrupted_at in rows
        ]

    def delete(self, thread_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM idea_sessions WHERE thread_id = ?", (thread_id,))
