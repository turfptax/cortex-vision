"""Smoke tests for Pydantic schemas — Phase 0."""
from datetime import datetime, timezone

from cortex_vision.models.schemas import (
    SceneEntry,
    TranscriptEntry,
    VideoSession,
)


def test_video_session_minimal_construct():
    s = VideoSession(
        id="abc-123",
        mode="file",
        source={"kind": "url", "url": "https://example.com/v.mp4"},
        started_at=datetime.now(timezone.utc),
    )
    assert s.status == "queued"
    assert s.scenes == []
    assert s.transcript == []
    assert not s.is_terminal
    assert s.duration_or_zero == 0.0


def test_scene_entry_defaults():
    sc = SceneEntry(index=0, start_s=0.0, end_s=4.2)
    assert sc.description == ""
    assert sc.keyframe_paths == []
    assert sc.objects == []
    assert sc.similarity == 1.0
    assert sc.trigger_method == "scheduled"


def test_transcript_entry_construct():
    t = TranscriptEntry(
        timestamp=datetime.now(timezone.utc),
        text="hello world",
        duration_s=3.0,
    )
    assert t.text == "hello world"
    assert t.chunk_index == 0


def test_terminal_status():
    s = VideoSession(
        id="x",
        mode="file",
        source={},
        started_at=datetime.now(timezone.utc),
        status="complete",
    )
    assert s.is_terminal

    s2 = s.model_copy(update={"status": "error", "error": "boom"})
    assert s2.is_terminal


def test_round_trip_via_json():
    """Schemas should serialize and deserialize cleanly — needed for storage layer."""
    original = VideoSession(
        id="x",
        mode="live",
        source={"kind": "obs_camera", "device": "OBS Virtual Camera"},
        started_at=datetime.now(timezone.utc),
        scenes=[SceneEntry(index=0, start_s=0.0, end_s=2.0, description="title screen")],
    )
    payload = original.model_dump_json()
    rehydrated = VideoSession.model_validate_json(payload)
    assert rehydrated.id == original.id
    assert len(rehydrated.scenes) == 1
    assert rehydrated.scenes[0].description == "title screen"
