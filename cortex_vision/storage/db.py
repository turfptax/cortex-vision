"""SQLite schema + idempotent migrations.

Pattern lifted from VideoIndex/ai-video-index/lib/catalog.py: each schema
change is wrapped in a PRAGMA table_info check so existing DBs upgrade in
place without losing data.

Default DB location: %APPDATA%/Cortex/video/sessions.db (Windows) or
~/.local/share/cortex/video/sessions.db (Unix).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def default_db_path() -> Path:
    """OS-appropriate location for sessions.db."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "Cortex" / "video"
    else:
        base = Path.home() / ".local" / "share" / "cortex" / "video"
    base.mkdir(parents=True, exist_ok=True)
    return base / "sessions.db"


def default_artifacts_dir() -> Path:
    """Root directory for per-session artifacts (frames, recordings, audio)."""
    return default_db_path().parent / "sessions"


_SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id              TEXT PRIMARY KEY,
        mode            TEXT NOT NULL,
        source_json     TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'queued',
        project_id      TEXT,
        started_at      TEXT NOT NULL,
        ended_at        TEXT,
        duration_s      REAL,
        narrative       TEXT,
        pushed_to_overseer INTEGER NOT NULL DEFAULT 0,
        error           TEXT,
        progress_json   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_project_id ON sessions(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_mode_status ON sessions(mode, status)",
    """
    CREATE TABLE IF NOT EXISTS scenes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL,
        scene_index     INTEGER NOT NULL,
        start_s         REAL NOT NULL,
        end_s           REAL NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        describer_model TEXT NOT NULL DEFAULT '',
        spoken_text     TEXT,
        objects_json    TEXT,
        similarity      REAL NOT NULL DEFAULT 1.0,
        trigger_method  TEXT NOT NULL DEFAULT 'scheduled',
        keyframes_json  TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        UNIQUE (session_id, scene_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scenes_session_id ON scenes(session_id)",
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL,
        chunk_index     INTEGER NOT NULL,
        timestamp       TEXT NOT NULL,
        text            TEXT NOT NULL,
        duration_s      REAL NOT NULL,
        latency_ms      INTEGER NOT NULL DEFAULT 0,
        rms             REAL NOT NULL DEFAULT 0.0,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        UNIQUE (session_id, chunk_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_transcripts_session_id ON transcripts(session_id)",
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    """Idempotent ALTER TABLE ADD COLUMN."""
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_schema(db_path: Path | None = None) -> Path:
    """Create or migrate the schema. Idempotent — safe to call every startup.

    Returns the path to the DB file.
    """
    db_path = db_path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(stmt)
        # Future migrations go here, e.g.:
        # _add_column_if_missing(conn, "sessions", "tags_json", "TEXT DEFAULT '[]'")
        conn.commit()

    return db_path


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection with sane defaults (foreign keys on, row factory)."""
    db_path = db_path or default_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
