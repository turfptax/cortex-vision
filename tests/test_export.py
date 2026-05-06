"""Tests for the HTML export endpoint."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )
    return tmp_path


def test_export_404_for_unknown_session(isolated):
    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.get("/api/video/sessions/abc-not-real/export.html")
    assert r.status_code == 404


def test_export_renders_self_contained_html(isolated):
    """The exported HTML should:
      - have a sensible filename in Content-Disposition
      - embed thumbnails as base64 (no external image refs)
      - include the narrative + per-scene descriptions"""
    from cortex_vision.models.schemas import SceneEntry, VideoSession
    from cortex_vision.pipeline.session_manager import SessionManager
    from cortex_vision.storage import db as db_module

    sm = SessionManager()
    s = sm.create(
        mode="file",
        source={"kind": "url", "url": "https://example.com/test.mp4"},
    )

    # Write a real keyframe file so the export can base64-encode it
    session_dir = db_module.default_artifacts_dir() / s.id
    frame_dir = session_dir / "frames" / "0"
    frame_dir.mkdir(parents=True, exist_ok=True)
    keyframe_path = frame_dir / "0.jpg"
    keyframe_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data" + b"\x00" * 50)

    sm.append_scene(s.id, SceneEntry(
        index=0,
        start_s=0.0,
        end_s=5.0,
        keyframe_paths=[str(keyframe_path)],
        description="A test scene about quantum kittens.",
        describer_model="lmstudio:test",
        trigger_method="scenedetect",
    ))
    sm.set_narrative(s.id, "The video is about quantum kittens, in detail.")
    sm.update_status(s.id, "capturing")
    sm.update_status(s.id, "describing")
    sm.update_status(s.id, "narrating")
    sm.update_status(s.id, "complete")

    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.get(f"/api/video/sessions/{s.id}/export.html")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert f"cortex-vision-{s.id[:8]}.html" in r.headers["content-disposition"]

    body = r.text
    # Self-contained: no <img src="/api/...">, only data URLs
    assert 'src="data:image/jpeg;base64,' in body
    assert 'src="/api/' not in body
    # Content present
    assert "quantum kittens" in body
    assert "Narrative" in body
    assert "1 scenes" in body or "Scenes (1)" in body


def test_export_handles_missing_keyframe_file(isolated):
    """If the keyframe file is missing on disk (manual cleanup, etc.),
    we still produce HTML with a placeholder rather than crashing."""
    from cortex_vision.models.schemas import SceneEntry
    from cortex_vision.pipeline.session_manager import SessionManager
    from cortex_vision.server import app

    sm = SessionManager()
    s = sm.create(mode="file", source={"url": "https://x"})
    sm.append_scene(s.id, SceneEntry(
        index=0,
        start_s=0,
        end_s=1,
        keyframe_paths=["/nonexistent/path.jpg"],   # file doesn't exist
        description="Fine description though",
    ))

    with TestClient(app) as c:
        r = c.get(f"/api/video/sessions/{s.id}/export.html")

    assert r.status_code == 200
    assert "keyframe missing" in r.text
    assert "Fine description though" in r.text


def test_export_handles_session_with_no_scenes(isolated):
    """A session that errored before producing scenes still exports cleanly."""
    from cortex_vision.pipeline.session_manager import SessionManager
    from cortex_vision.server import app

    sm = SessionManager()
    s = sm.create(mode="file", source={"url": "https://x"})
    sm.update_status(s.id, "error", error="something broke")

    with TestClient(app) as c:
        r = c.get(f"/api/video/sessions/{s.id}/export.html")

    assert r.status_code == 200
    assert "No scenes captured" in r.text
