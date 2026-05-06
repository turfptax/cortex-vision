"""Batch pipeline tests — Phase 1.

Strategy: monkeypatch the stages (download, scene extract, describe, narrate)
with deterministic fakes so we can exercise the orchestrator's state-machine
behavior without needing yt-dlp, ffmpeg, OpenCV, PySceneDetect, or LM Studio.

The actual pipeline against a real video is exercised manually via
    POST /api/video/jobs
when the cortex-vision sidecar is running with LM Studio reachable. CI smoke
coverage stays here.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from cortex_vision import run_batch_pipeline
from cortex_vision.detection.batch_extractor import ExtractedScene
from cortex_vision.pipeline.session_manager import SessionManager


def _fake_extracted_scenes(frames_dir: Path, n: int = 3) -> list[ExtractedScene]:
    """Build N fake ExtractedScene objects with on-disk placeholder JPEGs
    so the pipeline's keyframe paths point to real files."""
    out = []
    for i in range(n):
        scene_dir = frames_dir / str(i)
        scene_dir.mkdir(parents=True, exist_ok=True)
        kf_path = scene_dir / "0.jpg"
        kf_path.write_bytes(b"\xFF\xD8\xFF\xE0fake-jpeg")  # JPEG magic, fake data
        out.append(
            ExtractedScene(
                index=i,
                start_s=i * 5.0,
                end_s=(i + 1) * 5.0,
                duration_s=5.0,
                keyframe_paths=[str(kf_path)],
                brightness=128.0,                       # mid-range, won't be skipped
                trigger_method="scenedetect",
            )
        )
    return out


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the artifacts dir + DB at a tmp path so tests don't touch APPDATA."""
    artifacts = tmp_path / "video"
    artifacts.mkdir()
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: artifacts / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: artifacts / "sessions",
    )
    return artifacts


def test_batch_pipeline_happy_path(isolated_db, tmp_path):
    """Full pipeline with all stages mocked — verifies state transitions and
    that scenes + narrative get persisted."""
    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    fake_meta = {
        "file_path": str(tmp_path / "fake.mp4"),
        "title": "Test Video",
        "duration_s": 15.0,
        "platform": "url",
    }

    def fake_download(url, session_dir, **kwargs):
        Path(session_dir).mkdir(parents=True, exist_ok=True)
        return fake_meta

    def fake_extract(video_path, frames_dir, **kwargs):
        return _fake_extracted_scenes(Path(frames_dir), n=3)

    def fake_describe(*args, **kwargs):
        return "A scene happens here."

    def fake_narrate(*args, **kwargs):
        return "The video shows three scenes in sequence."

    with patch("cortex_vision.pipeline.batch.download_to_session", fake_download), \
         patch("cortex_vision.pipeline.batch.extract_scenes", fake_extract), \
         patch("cortex_vision.pipeline.batch.chat_with_images", fake_describe), \
         patch("cortex_vision.pipeline.batch.roll_up", fake_narrate):
        result = run_batch_pipeline(session.id)

    assert result.status == "complete"
    assert len(result.scenes) == 3
    assert all(s.description == "A scene happens here." for s in result.scenes)
    assert result.narrative == "The video shows three scenes in sequence."
    assert result.duration_s is not None
    assert result.error is None


def test_batch_pipeline_records_error_status(isolated_db):
    """If a stage raises, session ends in 'error' state with the message captured."""
    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    def fake_download(url, session_dir, **kwargs):
        raise RuntimeError("network broke")

    with patch("cortex_vision.pipeline.batch.download_to_session", fake_download):
        result = run_batch_pipeline(session.id)

    assert result.status == "error"
    assert "RuntimeError" in result.error
    assert "network broke" in result.error


def test_batch_pipeline_describer_unavailable_continues(isolated_db, tmp_path):
    """If LLM is down for per-scene description, scenes still get recorded
    with empty descriptions and the narrative falls back to concat."""
    from cortex_vision.description.lmstudio_client import LMStudioUnavailable

    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    fake_meta = {
        "file_path": str(tmp_path / "fake.mp4"),
        "title": "Test",
        "duration_s": 10.0,
        "platform": "url",
    }

    def fake_describe(*args, **kwargs):
        raise LMStudioUnavailable("server down")

    def fake_narrate(*args, **kwargs):
        raise LMStudioUnavailable("server still down")

    with patch("cortex_vision.pipeline.batch.download_to_session",
               lambda *a, **kw: fake_meta), \
         patch("cortex_vision.pipeline.batch.extract_scenes",
               lambda *a, **kw: _fake_extracted_scenes(Path(kw["frames_dir"]), n=2)), \
         patch("cortex_vision.pipeline.batch.chat_with_images", fake_describe), \
         patch("cortex_vision.pipeline.batch.roll_up", fake_narrate):
        result = run_batch_pipeline(session.id)

    # Pipeline should complete despite LLM being unreachable
    assert result.status == "complete"
    assert len(result.scenes) == 2
    # Descriptions are empty; describer_model annotates why
    assert all(s.description == "" for s in result.scenes)
    assert all("unavailable" in s.describer_model for s in result.scenes)
    # Narrative falls back to deterministic concat (which is empty here since
    # all descriptions are empty)
    assert result.narrative is not None


def test_batch_pipeline_no_scenes_extracted_errors(isolated_db, tmp_path):
    """An unreadable / empty video produces zero scenes -> session errors."""
    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    fake_meta = {"file_path": str(tmp_path / "x.mp4"), "title": "", "duration_s": 0,
                 "platform": "url"}

    with patch("cortex_vision.pipeline.batch.download_to_session",
               lambda *a, **kw: fake_meta), \
         patch("cortex_vision.pipeline.batch.extract_scenes",
               lambda *a, **kw: []):
        result = run_batch_pipeline(session.id)

    assert result.status == "error"
    assert "no usable scenes" in result.error.lower()


def test_batch_pipeline_audio_transcription_buckets_per_scene(isolated_db, tmp_path):
    """transcribe_audio=True extracts audio, transcribes, and assigns spoken_text
    per scene by start time."""
    from cortex_vision.audio.transcribe import (
        TranscriptSegment,
        TranscriptionResult,
    )
    from cortex_vision.pipeline.session_manager import SessionManager

    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    fake_meta = {
        "file_path": str(tmp_path / "fake.mp4"),
        "title": "Has Audio",
        "duration_s": 15.0,
        "platform": "url",
    }

    def fake_extract_scenes(video_path, frames_dir, **kw):
        # Three scenes with windows we can target with transcript segments
        scenes = _fake_extracted_scenes(Path(frames_dir), n=3)
        scenes[0].start_s, scenes[0].end_s = 0.0, 5.0
        scenes[1].start_s, scenes[1].end_s = 5.0, 10.0
        scenes[2].start_s, scenes[2].end_s = 10.0, 15.0
        return scenes

    fake_transcript = TranscriptionResult(
        full_text="Scene one talk. Scene two talk. Scene three talk.",
        segments=[
            TranscriptSegment(start_s=1.0, end_s=2.0, text="Scene one talk."),
            TranscriptSegment(start_s=6.0, end_s=7.0, text="Scene two talk."),
            TranscriptSegment(start_s=11.0, end_s=12.0, text="Scene three talk."),
        ],
        language="en",
        provider="openai",
        model="whisper-1",
    )

    with patch("cortex_vision.pipeline.batch.download_to_session", lambda *a, **kw: fake_meta), \
         patch("cortex_vision.pipeline.batch.extract_scenes", fake_extract_scenes), \
         patch("cortex_vision.pipeline.batch.chat_with_images", lambda *a, **kw: "scene desc"), \
         patch("cortex_vision.pipeline.batch.roll_up", lambda *a, **kw: "narrative"), \
         patch("cortex_vision.pipeline.batch.ffmpeg_available", lambda: True), \
         patch("cortex_vision.pipeline.batch.whisper_configured", lambda: True), \
         patch("cortex_vision.pipeline.batch.extract_audio_track",
               lambda *a, **kw: tmp_path / "audio.wav"), \
         patch("cortex_vision.pipeline.batch.transcribe_file", lambda *a, **kw: fake_transcript):
        result = run_batch_pipeline(session.id, transcribe_audio=True)

    assert result.status == "complete"
    assert len(result.scenes) == 3
    assert result.scenes[0].spoken_text == "Scene one talk."
    assert result.scenes[1].spoken_text == "Scene two talk."
    assert result.scenes[2].spoken_text == "Scene three talk."

    # Transcript segments persisted as TranscriptEntry rows
    assert len(result.transcript) == 3
    assert {t.text for t in result.transcript} == {
        "Scene one talk.", "Scene two talk.", "Scene three talk.",
    }


def test_batch_pipeline_audio_skipped_when_ffmpeg_missing(isolated_db, tmp_path):
    """transcribe_audio=True with ffmpeg missing should skip silently — pipeline still completes."""
    from cortex_vision.pipeline.session_manager import SessionManager

    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    fake_meta = {"file_path": str(tmp_path / "fake.mp4"), "title": "X",
                 "duration_s": 5.0, "platform": "url"}

    with patch("cortex_vision.pipeline.batch.download_to_session", lambda *a, **kw: fake_meta), \
         patch("cortex_vision.pipeline.batch.extract_scenes",
               lambda *a, **kw: _fake_extracted_scenes(Path(kw["frames_dir"]), n=2)), \
         patch("cortex_vision.pipeline.batch.chat_with_images", lambda *a, **kw: "desc"), \
         patch("cortex_vision.pipeline.batch.roll_up", lambda *a, **kw: "narrative"), \
         patch("cortex_vision.pipeline.batch.ffmpeg_available", lambda: False):
        result = run_batch_pipeline(session.id, transcribe_audio=True)

    assert result.status == "complete"
    # Scenes should have no spoken_text since transcription was skipped
    assert all(s.spoken_text in (None, "") for s in result.scenes)
    assert result.transcript == []


def test_batch_pipeline_progress_callback_invoked(isolated_db, tmp_path):
    """on_progress should be called with stage events as we go."""
    sm = SessionManager()
    session = sm.create(mode="file", source={"kind": "url", "url": "https://x/v.mp4"})

    events: list[tuple[str, dict]] = []

    fake_meta = {"file_path": str(tmp_path / "fake.mp4"), "title": "X",
                 "duration_s": 5.0, "platform": "url"}

    with patch("cortex_vision.pipeline.batch.download_to_session",
               lambda *a, **kw: fake_meta), \
         patch("cortex_vision.pipeline.batch.extract_scenes",
               lambda *a, **kw: _fake_extracted_scenes(Path(kw["frames_dir"]), n=2)), \
         patch("cortex_vision.pipeline.batch.chat_with_images",
               lambda *a, **kw: "ok"), \
         patch("cortex_vision.pipeline.batch.roll_up",
               lambda *a, **kw: "narrative"):
        run_batch_pipeline(session.id, on_progress=lambda stage, payload: events.append((stage, payload)))

    stages = [s for s, _ in events]
    # Should hit each major stage in order
    assert "capturing" in stages
    assert "processing" in stages
    assert "describing" in stages
    assert "narrating" in stages
    assert "complete" in stages
    # Should emit one scene_described event per scene
    described = [s for s, _ in events if s == "scene_described"]
    assert len(described) == 2
