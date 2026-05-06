"""Pydantic schemas for cortex-vision.

These are the contract between pipeline, storage, and API. See
docs/DATA_MODEL.md for the full description.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


VideoMode = Literal["live", "file", "journal"]
SessionStatus = Literal[
    "queued",
    "capturing",
    "processing",
    "describing",
    "narrating",
    "complete",
    "error",
]


class TranscriptEntry(BaseModel):
    """One audio chunk's transcription.

    Live mode: produced by Parakeet/Whisper as 3-second chunks stream in.
    Batch mode: produced by transcribing the audio track extracted via ffmpeg.
    """

    timestamp: datetime
    text: str
    duration_s: float
    latency_ms: int = 0
    rms: float = 0.0
    chunk_index: int = 0


class SceneEntry(BaseModel):
    """One detected scene with description and metadata.

    A scene spans `start_s`..`end_s` in the source video. We capture 1-3
    keyframes per scene (more for live mode's burst capture, just one for
    batch mode at the scene midpoint).
    """

    index: int
    start_s: float
    end_s: float
    keyframe_paths: list[str] = Field(default_factory=list)
    description: str = ""
    describer_model: str = ""
    spoken_text: str | None = None
    objects: list[str] = Field(default_factory=list)
    similarity: float = 1.0
    trigger_method: str = "scheduled"


class VideoSession(BaseModel):
    """A complete video processing run.

    The unit of work and persistence. One session corresponds to one row in
    the `sessions` table and one directory under
    %APPDATA%/Cortex/video/sessions/<session_id>/.
    """

    id: str
    mode: VideoMode
    source: dict
    status: SessionStatus = "queued"
    project_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: float | None = None
    scenes: list[SceneEntry] = Field(default_factory=list)
    narrative: str | None = None
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    pushed_to_overseer: bool = False
    error: str | None = None
    progress: dict = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """True if the session has reached a final state."""
        return self.status in ("complete", "error")

    @property
    def duration_or_zero(self) -> float:
        """Duration in seconds, or 0 if not yet computed."""
        return self.duration_s or 0.0
