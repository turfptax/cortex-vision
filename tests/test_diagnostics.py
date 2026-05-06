"""Tests for /api/video/diagnostics — operational observability."""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Pin DB + artifacts to tmp so we don't pollute %APPDATA%.

    Also points APPDATA itself at a fresh tmp dir so v0.3.5's whisper.cpp
    auto-detection doesn't pick up a real install on the dev's machine
    and confuse the provider-resolution tests.
    """
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
    # Empty APPDATA -> no whisper.cpp detected -> tests get deterministic
    # provider behavior regardless of the dev's local install
    fake_appdata = tmp_path / "AppData"
    fake_appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    return artifacts


def test_diagnostics_smoke(isolated_db, monkeypatch):
    """Endpoint responds with the expected top-level keys regardless of
    provider availability."""
    # Strip env vars so providers report as not-configured
    for key in ("CORTEX_VISION_WHISPER_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: False
    )
    # Stub the LLM health check so we don't make a real request
    monkeypatch.setattr(
        "cortex_vision.description.lmstudio_client.health_check", lambda timeout=2.0: False
    )

    from cortex_vision.server import app
    with TestClient(app) as c:
        r = c.get("/api/video/diagnostics")
        assert r.status_code == 200
        body = r.json()

    # Top-level keys
    for key in ("version", "describer", "transcribe", "live", "sessions", "storage"):
        assert key in body, f"missing key: {key}"

    # Describer subtree
    assert body["describer"]["provider"] == "lmstudio_compat"
    assert body["describer"]["reachable"] is False         # we stubbed it false

    # Transcribe subtree
    assert body["transcribe"]["configured"] is False
    assert body["transcribe"]["provider"] is None
    assert body["transcribe"]["ffmpeg_available"] is False

    # No live session
    assert body["live"]["active_session_id"] is None

    # Counts default to 0 on a fresh DB
    assert body["sessions"]["total"] == 0


def test_diagnostics_counts_by_status(isolated_db, monkeypatch):
    """Status counts reflect what's in SQLite at the time of the call.

    Important: TestClient runs the FastAPI lifespan handler, which calls
    cleanup_orphaned_sessions() on startup — so any non-terminal sessions
    created before TestClient enters its context will be transitioned to
    'error'. That's the actual production behavior we want to verify.
    """
    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr(
        "cortex_vision.description.lmstudio_client.health_check", lambda timeout=2.0: True
    )

    from cortex_vision.pipeline.session_manager import SessionManager
    from cortex_vision.server import app

    sm = SessionManager()
    a = sm.create(mode="file", source={"url": "a"})        # queued
    b = sm.create(mode="file", source={"url": "b"})
    c = sm.create(mode="file", source={"url": "c"})
    sm.update_status(b.id, "capturing")
    sm.update_status(c.id, "capturing")
    sm.update_status(c.id, "describing")
    sm.update_status(c.id, "narrating")
    sm.update_status(c.id, "complete")

    # When TestClient starts, lifespan runs orphan cleanup: a (queued) and
    # b (capturing) get transitioned to error. c (complete) stays.
    with TestClient(app) as c_client:
        body = c_client.get("/api/video/diagnostics").json()

    assert body["sessions"]["total"] == 3
    by_status = body["sessions"]["by_status"]
    assert by_status.get("complete") == 1
    assert by_status.get("error") == 2                     # a and b cleaned up
    assert by_status.get("queued", 0) == 0                 # cleanup ran
    assert by_status.get("capturing", 0) == 0              # cleanup ran


def test_diagnostics_whisper_provider_reports_lmstudio(isolated_db, monkeypatch):
    monkeypatch.setenv("CORTEX_VISION_WHISPER_URL", "http://localhost:1234/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr(
        "cortex_vision.description.lmstudio_client.health_check", lambda timeout=2.0: True
    )

    from cortex_vision.server import app
    with TestClient(app) as c:
        body = c.get("/api/video/diagnostics").json()

    assert body["transcribe"]["provider"] == "lmstudio_compat"
    assert body["transcribe"]["configured"] is True
    assert body["transcribe"]["url"] == "http://localhost:1234/v1"


def test_diagnostics_whisper_provider_falls_back_to_openai(isolated_db, monkeypatch):
    monkeypatch.delenv("CORTEX_VISION_WHISPER_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr(
        "cortex_vision.description.lmstudio_client.health_check", lambda timeout=2.0: False
    )

    from cortex_vision.server import app
    with TestClient(app) as c:
        body = c.get("/api/video/diagnostics").json()

    assert body["transcribe"]["provider"] == "openai"
    assert body["transcribe"]["configured"] is True


def test_diagnostics_storage_reports_disk_usage(isolated_db, monkeypatch):
    """Drop a fake artifact and verify disk usage shows up."""
    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: False
    )
    monkeypatch.setattr(
        "cortex_vision.description.lmstudio_client.health_check", lambda timeout=2.0: False
    )

    sessions_dir = isolated_db / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    fake_session = sessions_dir / "abc-123"
    fake_session.mkdir()
    (fake_session / "source.mp4").write_bytes(b"\x00" * 12345)
    frames = fake_session / "frames" / "0"
    frames.mkdir(parents=True)
    (frames / "0.jpg").write_bytes(b"\x00" * 6789)

    from cortex_vision.server import app
    with TestClient(app) as c:
        body = c.get("/api/video/diagnostics").json()

    assert body["storage"]["session_dirs"] == 1
    assert body["storage"]["total_bytes"] == 12345 + 6789
    assert body["storage"]["total_mb"] >= 0


def test_diagnostics_does_not_leak_secrets(isolated_db, monkeypatch):
    """API keys must never appear in the response body."""
    secret = "sk-very-secret-key-do-not-leak-12345"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("CORTEX_VISION_LLM_KEY", "another-secret-key-67890")
    monkeypatch.setattr(
        "cortex_vision.audio.ffmpeg_extract.ffmpeg_available", lambda: True
    )
    monkeypatch.setattr(
        "cortex_vision.description.lmstudio_client.health_check", lambda timeout=2.0: True
    )

    from cortex_vision.server import app
    with TestClient(app) as c:
        body_text = c.get("/api/video/diagnostics").text

    assert secret not in body_text
    assert "another-secret-key-67890" not in body_text
