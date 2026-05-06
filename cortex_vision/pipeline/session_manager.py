"""SessionManager — CRUD over the SQLite session schema.

This is the only module that talks to SQLite directly (besides storage/db.py
itself). All pipeline orchestrators and the FastAPI router go through here.

Status transitions enforced per DATA_MODEL.md:
    queued -> capturing -> processing -> describing -> narrating -> complete
       |          |             |             |             |
       +----------+-------------+-------------+-------------+--> error
Once `complete` or `error`, no further transitions.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cortex_vision.models.schemas import (
    SceneEntry,
    SessionStatus,
    TranscriptEntry,
    VideoMode,
    VideoSession,
)
from cortex_vision.storage import db as db_module


# Valid status transitions. Source -> set of valid destinations.
# Any state can go to "error". Terminal states have no outgoing transitions.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued":     {"capturing", "processing", "error"},
    "capturing":  {"processing", "describing", "complete", "error"},
    "processing": {"describing", "error"},
    "describing": {"narrating", "error"},
    "narrating":  {"complete", "error"},
    "complete":   set(),
    "error":      set(),
}


class SessionTransitionError(ValueError):
    """Raised when a status update would violate the state machine."""


class SessionManager:
    """CRUD facade over sessions.db.

    Usage:
        sm = SessionManager()                      # uses default DB path
        s = sm.create(mode="file", source={"url": "..."})
        sm.update_status(s.id, "describing")
        s = sm.get(s.id)
        sessions = sm.list(limit=20)
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_module.init_schema(db_path)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        mode: VideoMode,
        source: dict,
        project_id: str | None = None,
    ) -> VideoSession:
        """Create a new queued session."""
        session = VideoSession(
            id=str(uuid.uuid4()),
            mode=mode,
            source=source,
            status="queued",
            project_id=project_id,
            started_at=datetime.now(timezone.utc),
        )
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO sessions
                   (id, mode, source_json, status, project_id, started_at, progress_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id,
                    session.mode,
                    json.dumps(session.source),
                    session.status,
                    session.project_id,
                    session.started_at.isoformat(),
                    json.dumps(session.progress),
                ),
            )
        return session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> VideoSession | None:
        """Fetch one session by id, hydrated with scenes and transcript."""
        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None

            scenes = self._fetch_scenes(conn, session_id)
            transcript = self._fetch_transcript(conn, session_id)

        return self._row_to_session(row, scenes, transcript)

    def list(
        self,
        limit: int = 50,
        mode: VideoMode | None = None,
        status: SessionStatus | None = None,
        pushed: bool | None = None,
    ) -> list[VideoSession]:
        """Most-recent-first list of sessions with optional filters.

        Sessions in this list have empty scenes/transcript (call get() to
        hydrate). Keeps list endpoints fast.

        Filters:
            mode    — only sessions with this mode
            status  — only sessions in this status
            pushed  — only sessions where pushed_to_overseer matches (True/False)

        Common bridge query: status="complete", pushed=False to find sessions
        ready for overseer push.
        """
        sql = "SELECT * FROM sessions"
        params: list = []
        clauses: list[str] = []

        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if pushed is not None:
            clauses.append("pushed_to_overseer = ?")
            params.append(1 if pushed else 0)

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        with db_module.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        return [self._row_to_session(r, [], []) for r in rows]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_status(
        self,
        session_id: str,
        status: SessionStatus,
        error: str | None = None,
    ) -> None:
        """Move a session to a new status. Validates transitions per DATA_MODEL.md.

        If status == "complete" or "error", also writes ended_at and duration_s.
        """
        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status, started_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Session {session_id} not found")

            current = row["status"]
            if status not in _VALID_TRANSITIONS.get(current, set()):
                raise SessionTransitionError(
                    f"Invalid transition: {current} -> {status}"
                )

            if status in ("complete", "error"):
                ended_at = datetime.now(timezone.utc)
                started_at = datetime.fromisoformat(row["started_at"])
                duration = (ended_at - started_at).total_seconds()
                conn.execute(
                    """UPDATE sessions
                       SET status = ?, error = ?, ended_at = ?, duration_s = ?
                       WHERE id = ?""",
                    (status, error, ended_at.isoformat(), duration, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET status = ?, error = ? WHERE id = ?",
                    (status, error, session_id),
                )

    def update_progress(self, session_id: str, progress: dict) -> None:
        """Update the progress dict (e.g. {current_scene: 4, total_scenes: 12})."""
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET progress_json = ? WHERE id = ?",
                (json.dumps(progress), session_id),
            )

    def append_scene(self, session_id: str, scene: SceneEntry) -> None:
        """Persist one SceneEntry. Idempotent on (session_id, scene_index)."""
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO scenes
                   (session_id, scene_index, start_s, end_s, description,
                    describer_model, spoken_text, objects_json, similarity,
                    trigger_method, keyframes_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    scene.index,
                    scene.start_s,
                    scene.end_s,
                    scene.description,
                    scene.describer_model,
                    scene.spoken_text,
                    json.dumps(scene.objects),
                    scene.similarity,
                    scene.trigger_method,
                    json.dumps(scene.keyframe_paths),
                ),
            )

    def update_scene_description(
        self, session_id: str, scene_index: int, description: str, model: str
    ) -> None:
        """Patch just the description+model fields without rewriting the row."""
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE scenes SET description = ?, describer_model = ?
                   WHERE session_id = ? AND scene_index = ?""",
                (description, model, session_id, scene_index),
            )

    def append_transcript(self, session_id: str, entry: TranscriptEntry) -> None:
        """Persist one TranscriptEntry. Idempotent on (session_id, chunk_index)."""
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO transcripts
                   (session_id, chunk_index, timestamp, text, duration_s,
                    latency_ms, rms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    entry.chunk_index,
                    entry.timestamp.isoformat(),
                    entry.text,
                    entry.duration_s,
                    entry.latency_ms,
                    entry.rms,
                ),
            )

    def set_narrative(self, session_id: str, narrative: str) -> None:
        """Write the final narrative rollup."""
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET narrative = ? WHERE id = ?",
                (narrative, session_id),
            )

    def mark_pushed_to_overseer(self, session_id: str) -> None:
        """Flip the pushed_to_overseer flag once the bridge confirms."""
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET pushed_to_overseer = 1 WHERE id = ?",
                (session_id,),
            )

    # ------------------------------------------------------------------
    # Resilience — orphan cleanup on startup
    # ------------------------------------------------------------------

    def cleanup_orphaned_sessions(
        self,
        error_message: str = "Sidecar restarted — session was interrupted",
    ) -> list[str]:
        """Transition all non-terminal sessions to 'error' status.

        Called from the FastAPI lifespan on startup. If cortex-vision crashed
        or was killed mid-pipeline, those sessions sit in 'capturing' /
        'describing' / etc. forever — this cleans them up so the History UI
        doesn't show stale in-progress entries.

        Auto-resume is intentionally NOT attempted: live sessions can't be
        resumed (the camera state is gone) and batch sessions had their
        BackgroundTask reference dropped on process exit. Marking them
        'error' is the honest, predictable behavior. The user's UI shows
        the failure cleanly and they can resubmit if they want.

        Returns:
            List of session_ids that were transitioned. Empty list if no
            orphans were found (clean shutdown happens often enough that
            this is the common case).
        """
        non_terminal = ("queued", "capturing", "processing", "describing", "narrating")
        with db_module.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE status IN ({','.join('?' * len(non_terminal))})",
                non_terminal,
            ).fetchall()
            orphan_ids = [r["id"] for r in rows]

        for sid in orphan_ids:
            try:
                self.update_status(sid, "error", error=error_message)
            except Exception:                                # noqa: BLE001
                # Already-terminal sessions or invalid transitions get logged
                # but don't block the rest of cleanup
                import logging
                logging.getLogger("cortex_vision.session_manager").exception(
                    "could not transition orphan %s to error", sid
                )
        return orphan_ids

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_scenes(conn: sqlite3.Connection, session_id: str) -> list[SceneEntry]:
        rows = conn.execute(
            """SELECT * FROM scenes WHERE session_id = ?
               ORDER BY scene_index ASC""",
            (session_id,),
        ).fetchall()
        return [
            SceneEntry(
                index=r["scene_index"],
                start_s=r["start_s"],
                end_s=r["end_s"],
                description=r["description"] or "",
                describer_model=r["describer_model"] or "",
                spoken_text=r["spoken_text"],
                objects=json.loads(r["objects_json"]) if r["objects_json"] else [],
                similarity=r["similarity"],
                trigger_method=r["trigger_method"],
                keyframe_paths=json.loads(r["keyframes_json"]) if r["keyframes_json"] else [],
            )
            for r in rows
        ]

    @staticmethod
    def _fetch_transcript(
        conn: sqlite3.Connection, session_id: str
    ) -> list[TranscriptEntry]:
        rows = conn.execute(
            """SELECT * FROM transcripts WHERE session_id = ?
               ORDER BY chunk_index ASC""",
            (session_id,),
        ).fetchall()
        return [
            TranscriptEntry(
                timestamp=datetime.fromisoformat(r["timestamp"]),
                text=r["text"],
                duration_s=r["duration_s"],
                latency_ms=r["latency_ms"],
                rms=r["rms"],
                chunk_index=r["chunk_index"],
            )
            for r in rows
        ]

    @staticmethod
    def _row_to_session(
        row: sqlite3.Row,
        scenes: list[SceneEntry],
        transcript: list[TranscriptEntry],
    ) -> VideoSession:
        return VideoSession(
            id=row["id"],
            mode=row["mode"],
            source=json.loads(row["source_json"]),
            status=row["status"],
            project_id=row["project_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            duration_s=row["duration_s"],
            scenes=scenes,
            narrative=row["narrative"],
            transcript=transcript,
            pushed_to_overseer=bool(row["pushed_to_overseer"]),
            error=row["error"],
            progress=json.loads(row["progress_json"]) if row["progress_json"] else {},
        )
