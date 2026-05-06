"""Tests for cortex_vision.audio — Phase 6.

Covers:
  - ffmpeg_extract: subprocess wiring, error paths, ffmpeg-missing detection
  - transcribe: provider resolution, response parsing, per-scene bucketing
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cortex_vision.audio.ffmpeg_extract import (
    FfmpegError,
    extract_audio_track,
    ffmpeg_available,
    has_audio_track,
)
from cortex_vision.audio.transcribe import (
    TranscriptSegment,
    TranscriptionResult,
    WhisperUnavailable,
    _parse_http_response as _parse_response,         # renamed in v0.3.5
    _resolve_endpoint,
    bucket_segments_by_scene,
    is_configured,
)


# ---------------------------------------------------------------------------
# ffmpeg_extract
# ---------------------------------------------------------------------------

def test_ffmpeg_available_returns_bool():
    """Just exercise the path — actual value depends on the host's PATH."""
    assert isinstance(ffmpeg_available(), bool)


def test_extract_audio_missing_source(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        extract_audio_track(
            tmp_path / "does-not-exist.mp4",
            tmp_path / "out.wav",
        )


def test_extract_audio_no_ffmpeg(tmp_path, monkeypatch):
    """When ffmpeg isn't on PATH, raise FfmpegError with a helpful message."""
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: False
    )
    with pytest.raises(FfmpegError, match="ffmpeg not found"):
        extract_audio_track(src, tmp_path / "out.wav")


def test_extract_audio_subprocess_args(tmp_path, monkeypatch):
    """Verify we invoke ffmpeg with the right args for 16kHz mono WAV."""
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")
    dest = tmp_path / "out.wav"

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Simulate ffmpeg producing the output file
        Path(cmd[-1]).write_bytes(b"RIFF" + b"\x00" * 100)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr("subprocess.run", fake_run)

    out = extract_audio_track(src, dest, sample_rate=16000)

    assert out == dest
    assert dest.exists()

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"
    assert "-vn" in cmd                                  # no video
    assert str(src) in cmd
    assert str(dest) in cmd


def test_extract_audio_nonzero_exit(tmp_path, monkeypatch):
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")

    class FailedRun:
        returncode = 1
        stdout = ""
        stderr = "Invalid data found when processing input"

    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FailedRun())

    with pytest.raises(FfmpegError, match="exited 1"):
        extract_audio_track(src, tmp_path / "out.wav")


def test_extract_audio_empty_output(tmp_path, monkeypatch):
    """ffmpeg returned 0 but produced no output (silent video, no audio track)."""
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")

    class OkRun:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: OkRun())

    with pytest.raises(FfmpegError, match="produced no output"):
        extract_audio_track(src, tmp_path / "out.wav")


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

def test_resolve_endpoint_prefers_lmstudio(monkeypatch):
    monkeypatch.setenv("CORTEX_VISION_WHISPER_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-be-used")
    # Disable whisper.cpp detection by pointing APPDATA at an empty dir
    monkeypatch.setenv("APPDATA", str(__import__("tempfile").mkdtemp()))
    ep = _resolve_endpoint()
    # Renamed in v0.3.5 from "lmstudio" to "lmstudio_compat" for clarity
    assert ep.name == "lmstudio_compat"
    assert ep.url == "http://localhost:1234/v1/audio/transcriptions"


def test_resolve_endpoint_openai_fallback(monkeypatch):
    monkeypatch.delenv("CORTEX_VISION_WHISPER_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    # Disable whisper.cpp detection by pointing APPDATA at an empty dir
    monkeypatch.setenv("APPDATA", str(__import__("tempfile").mkdtemp()))
    ep = _resolve_endpoint()
    assert ep.name == "openai"
    assert ep.url.endswith("/audio/transcriptions")
    assert ep.api_key == "sk-fake"


def test_resolve_endpoint_no_provider_raises(monkeypatch):
    monkeypatch.delenv("CORTEX_VISION_WHISPER_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("APPDATA", str(__import__("tempfile").mkdtemp()))
    with pytest.raises(WhisperUnavailable):
        _resolve_endpoint()


def test_is_configured(monkeypatch):
    monkeypatch.delenv("CORTEX_VISION_WHISPER_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Empty APPDATA disables the v0.3.5 whisper.cpp detection path
    monkeypatch.setenv("APPDATA", str(__import__("tempfile").mkdtemp()))
    assert is_configured() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert is_configured() is True


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _fake_endpoint():
    from cortex_vision.audio.transcribe import _HttpEndpoint
    return _HttpEndpoint(
        name="openai",
        url="https://api.openai.com/v1/audio/transcriptions",
        api_key="sk-fake",
        default_model="whisper-1",
    )


def test_parse_response_with_segments():
    payload = {
        "text": "Hello world. This is a test.",
        "language": "en",
        "segments": [
            {"start": 0.0, "end": 1.5, "text": " Hello world."},
            {"start": 1.5, "end": 3.0, "text": " This is a test."},
        ],
    }
    result = _parse_response(payload, _fake_endpoint())
    assert result.full_text == "Hello world. This is a test."
    assert result.language == "en"
    assert len(result.segments) == 2
    assert result.segments[0].text == "Hello world."         # leading space stripped
    assert result.segments[0].start_s == 0.0
    assert result.segments[1].start_s == 1.5


def test_parse_response_no_segments_synthesizes_one():
    """Older Whisper builds may return only `text`. We fall back to a single
    timestamp-less segment so the caller can still attach the transcript."""
    payload = {"text": "Just plain text response."}
    result = _parse_response(payload, _fake_endpoint())
    assert len(result.segments) == 1
    assert result.segments[0].text == "Just plain text response."


def test_parse_response_skips_malformed_segments():
    """Bad segments shouldn't crash the parser — they should be dropped."""
    payload = {
        "text": "ok",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "good"},
            {"start": "not a number", "end": 2.0, "text": "bad"},
            {"text": "missing timestamps"},
        ],
    }
    result = _parse_response(payload, _fake_endpoint())
    assert len(result.segments) == 1
    assert result.segments[0].text == "good"


# ---------------------------------------------------------------------------
# Per-scene bucketing
# ---------------------------------------------------------------------------

def test_bucket_segments_basic():
    """Segments get assigned to the scene whose window contains their start."""
    segments = [
        TranscriptSegment(start_s=0.5, end_s=1.5, text="first scene speech"),
        TranscriptSegment(start_s=3.0, end_s=4.0, text="second scene speech"),
        TranscriptSegment(start_s=4.2, end_s=5.0, text="more second scene"),
    ]
    scene_windows = [(0.0, 2.0), (2.0, 5.0), (5.0, 8.0)]

    out = bucket_segments_by_scene(segments, scene_windows)
    assert out[0] == "first scene speech"
    assert out[1] == "second scene speech more second scene"
    assert out[2] == ""                                       # no segments


def test_bucket_segments_at_boundary():
    """A segment starting exactly at a scene boundary goes to the LATER scene
    (per the [start, end) half-open interval convention)."""
    segments = [
        TranscriptSegment(start_s=2.0, end_s=2.5, text="boundary"),
    ]
    scene_windows = [(0.0, 2.0), (2.0, 4.0)]
    out = bucket_segments_by_scene(segments, scene_windows)
    assert out[0] == ""
    assert out[1] == "boundary"


def test_bucket_segments_drops_out_of_bounds():
    """Segments outside any scene window are dropped."""
    segments = [
        TranscriptSegment(start_s=-1.0, end_s=0.0, text="before"),
        TranscriptSegment(start_s=10.0, end_s=11.0, text="after"),
        TranscriptSegment(start_s=1.0, end_s=2.0, text="inside"),
    ]
    scene_windows = [(0.0, 5.0)]
    out = bucket_segments_by_scene(segments, scene_windows)
    assert out == ["inside"]


def test_bucket_segments_skips_empty_text():
    segments = [
        TranscriptSegment(start_s=1.0, end_s=2.0, text=""),
        TranscriptSegment(start_s=2.0, end_s=3.0, text="real"),
    ]
    out = bucket_segments_by_scene(segments, [(0.0, 5.0)])
    assert out == ["real"]


# ---------------------------------------------------------------------------
# has_audio_track
# ---------------------------------------------------------------------------

def test_has_audio_track_no_ffprobe(tmp_path, monkeypatch):
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert has_audio_track(src) is False


def test_has_audio_track_with_audio(tmp_path, monkeypatch):
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffprobe")

    class R:
        returncode = 0
        stdout = "audio\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: R())
    assert has_audio_track(src) is True


def test_has_audio_track_no_audio(tmp_path, monkeypatch):
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffprobe")

    class R:
        returncode = 0
        stdout = ""                                       # empty = no audio stream
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: R())
    assert has_audio_track(src) is False
