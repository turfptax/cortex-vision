"""Smoke tests for SQLite schema setup — Phase 0."""
from pathlib import Path

from cortex_vision.storage import db


def test_init_schema_creates_tables(tmp_path: Path):
    db_file = tmp_path / "test.db"
    out = db.init_schema(db_file)
    assert out == db_file
    assert db_file.exists()

    with db.connect(db_file) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row["name"] for row in cur.fetchall()]
    assert "sessions" in tables
    assert "scenes" in tables
    assert "transcripts" in tables


def test_init_schema_is_idempotent(tmp_path: Path):
    db_file = tmp_path / "test.db"
    db.init_schema(db_file)
    db.init_schema(db_file)  # should not raise
    db.init_schema(db_file)


def test_foreign_key_cascade(tmp_path: Path):
    """Deleting a session should remove its scenes and transcripts."""
    db_file = tmp_path / "test.db"
    db.init_schema(db_file)

    with db.connect(db_file) as conn:
        conn.execute(
            """INSERT INTO sessions (id, mode, source_json, started_at)
               VALUES ('s1', 'file', '{}', '2026-01-01T00:00:00')"""
        )
        conn.execute(
            """INSERT INTO scenes (session_id, scene_index, start_s, end_s)
               VALUES ('s1', 0, 0.0, 1.0)"""
        )
        conn.execute(
            """INSERT INTO transcripts (session_id, chunk_index, timestamp, text, duration_s)
               VALUES ('s1', 0, '2026-01-01T00:00:00', 'hi', 1.0)"""
        )

    with db.connect(db_file) as conn:
        conn.execute("DELETE FROM sessions WHERE id = 's1'")

    with db.connect(db_file) as conn:
        scenes = conn.execute("SELECT COUNT(*) AS n FROM scenes").fetchone()
        transcripts = conn.execute("SELECT COUNT(*) AS n FROM transcripts").fetchone()

    assert scenes["n"] == 0
    assert transcripts["n"] == 0
