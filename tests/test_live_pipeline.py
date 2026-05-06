"""Tests for the live pipeline orchestrator and the FastAPI live endpoints.

We mock cv2.VideoCapture so tests don't need an actual camera. The fake
emits a sequence of solid-color frames designed to trigger scene changes.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fake VideoCapture — a class that quacks like cv2.VideoCapture
# ---------------------------------------------------------------------------

class FakeCV2Capture:
    """Yields a deterministic sequence of frames. Used to mock cv2.VideoCapture."""

    def __init__(self, *frames: np.ndarray):
        self._frames = list(frames)
        self._idx = 0
        self._opened = True

    def isOpened(self) -> bool:
        return self._opened

    def read(self):
        if not self._opened or self._idx >= len(self._frames):
            return False, None
        f = self._frames[self._idx]
        self._idx = (self._idx + 1) % len(self._frames)        # loop
        return True, f

    def get(self, prop):
        # CAP_PROP_FRAME_WIDTH, _HEIGHT, _FPS — return plausible values
        if prop == 3:                                          # FRAME_WIDTH
            return self._frames[0].shape[1] if self._frames else 0
        if prop == 4:                                          # FRAME_HEIGHT
            return self._frames[0].shape[0] if self._frames else 0
        if prop == 5:                                          # FPS
            return 30.0
        return 0.0

    def release(self):
        self._opened = False


def _solid(color: tuple[int, int, int], shape=(216, 384, 3)) -> np.ndarray:
    img = np.zeros(shape, dtype=np.uint8)
    img[..., 0] = color[0]
    img[..., 1] = color[1]
    img[..., 2] = color[2]
    return img


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
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


@pytest.fixture
def fake_cv2_open(monkeypatch):
    """Replace cv2.VideoCapture with a fixture that returns a FakeCV2Capture
    of two alternating colors — guaranteed to trigger scene changes."""
    fake = FakeCV2Capture(
        _solid((20, 20, 20)),
        _solid((220, 60, 60)),
        _solid((20, 20, 20)),
        _solid((220, 60, 60)),
    )
    monkeypatch.setattr("cv2.VideoCapture", lambda *a, **kw: fake)
    return fake


@pytest.fixture
def stub_describer(monkeypatch):
    """Replace the LM Studio call so the pipeline runs without a server."""
    calls: list[dict] = []

    def fake_chat(text, image_paths, system, model, max_tokens, temperature):
        calls.append({"text": text, "image_paths": list(image_paths), "model": model})
        return f"Stubbed description ({len(image_paths)} keyframes)"

    monkeypatch.setattr(
        "cortex_vision.pipeline.live.chat_with_images", fake_chat
    )
    return calls


# ---------------------------------------------------------------------------
# LivePipeline directly
# ---------------------------------------------------------------------------

def test_live_pipeline_start_and_stop(isolated_db, fake_cv2_open, stub_describer):
    from cortex_vision.pipeline.live import LivePipeline, LivePipelineConfig
    from cortex_vision.pipeline.session_manager import SessionManager

    sm = SessionManager()
    session = sm.create(
        mode="live",
        source={"kind": "obs_camera", "device": "index:0", "resolution": [384, 216]},
    )

    config = LivePipelineConfig(
        session_id=session.id,
        camera_index=0,
        resolution=(384, 216),
        threshold=0.95,
        pixel_diff_threshold=10.0,
        structural_threshold=0.1,
        steady_interval=999.0,
        min_scene_gap=0.0,
    )

    pipeline = LivePipeline(config)
    pipeline.start()
    try:
        assert pipeline.is_running
        # Let the pipeline run long enough to detect at least one scene change
        time.sleep(1.5)
    finally:
        pipeline.stop(timeout=3)

    assert not pipeline.is_running

    # Session moved to 'complete'
    final = sm.get(session.id)
    assert final.status == "complete"

    # We should have at least one scene with a description
    assert len(final.scenes) >= 1
    described = [s for s in final.scenes if s.description]
    assert any("Stubbed description" in s.description for s in described)


def test_live_pipeline_emits_started_and_stopped_events(isolated_db, fake_cv2_open, stub_describer):
    from cortex_vision.pipeline.live import LivePipeline, LivePipelineConfig
    from cortex_vision.pipeline.session_manager import SessionManager

    sm = SessionManager()
    session = sm.create(mode="live", source={"kind": "obs_camera", "device": "index:0"})

    config = LivePipelineConfig(
        session_id=session.id,
        steady_interval=999.0,
        min_scene_gap=999.0,                        # don't trigger scene changes
    )
    pipeline = LivePipeline(config)
    pipeline.start()
    time.sleep(0.3)
    pipeline.stop(timeout=3)

    events: list[dict] = []
    while True:
        e = pipeline.get_event(timeout=0.05)
        if e is None:
            break
        events.append(e)

    types = [e["type"] for e in events]
    assert "started" in types
    assert "stopped" in types
    # Find the started event and check it has the expected shape
    started = next(e for e in events if e["type"] == "started")
    assert started["session_id"] == session.id
    assert started["camera_index"] == 0


def test_live_pipeline_camera_open_failure_raises(isolated_db, monkeypatch):
    """If cv2 can't open the camera, start() raises a clean RuntimeError."""
    from cortex_vision.pipeline.live import LivePipeline, LivePipelineConfig
    from cortex_vision.pipeline.session_manager import SessionManager

    closed = MagicMock()
    closed.isOpened.return_value = False
    monkeypatch.setattr("cv2.VideoCapture", lambda *a, **kw: closed)

    sm = SessionManager()
    session = sm.create(mode="live", source={"kind": "obs_camera", "device": "index:99"})
    pipeline = LivePipeline(LivePipelineConfig(session_id=session.id, camera_index=99))

    with pytest.raises(RuntimeError, match="Could not open camera"):
        pipeline.start()


# ---------------------------------------------------------------------------
# LivePipelineManager singleton enforcement
# ---------------------------------------------------------------------------

def test_manager_rejects_concurrent_sessions(isolated_db, fake_cv2_open, stub_describer):
    from cortex_vision.pipeline.live import (
        LivePipelineConfig,
        LivePipelineManager,
    )
    from cortex_vision.pipeline.session_manager import SessionManager

    sm = SessionManager()
    s1 = sm.create(mode="live", source={"kind": "obs_camera", "device": "index:0"})
    s2 = sm.create(mode="live", source={"kind": "obs_camera", "device": "index:0"})

    mgr = LivePipelineManager()
    cfg1 = LivePipelineConfig(session_id=s1.id, steady_interval=999.0, min_scene_gap=999.0)
    cfg2 = LivePipelineConfig(session_id=s2.id, steady_interval=999.0, min_scene_gap=999.0)

    mgr.start(cfg1)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            mgr.start(cfg2)
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def test_live_status_when_idle(isolated_db, fake_cv2_open):
    """GET /live/status returns is_running=False when no session is active."""
    from fastapi.testclient import TestClient
    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.get("/api/video/live/status")
        assert r.status_code == 200
        assert r.json() == {"is_running": False}


def test_live_stop_when_idle_returns_404(isolated_db, fake_cv2_open):
    from fastapi.testclient import TestClient
    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.post("/api/video/live/stop")
        assert r.status_code == 404


def test_live_start_then_stop(isolated_db, fake_cv2_open, stub_describer):
    """End-to-end happy path through the HTTP layer."""
    from fastapi.testclient import TestClient
    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.post(
            "/api/video/live/start",
            json={
                "camera_index": 0,
                "resolution": [384, 216],
                "threshold": 0.95,
                "pixel_diff_threshold": 10.0,
                "structural_threshold": 0.1,
                "steady_interval": 999.0,
                "min_scene_gap": 0.0,
            },
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "capturing"
        assert body["ws_url"] == "/api/video/live/ws"
        session_id = body["session_id"]

        # Status now shows running
        r2 = c.get("/api/video/live/status")
        assert r2.status_code == 200
        snap = r2.json()
        assert snap["is_running"] is True
        assert snap["session_id"] == session_id

        # Stop it
        r3 = c.post("/api/video/live/stop")
        assert r3.status_code == 200
        assert r3.json()["stopped"] is True

        # Idle again
        r4 = c.get("/api/video/live/status")
        assert r4.json() == {"is_running": False}


def test_live_start_409_when_already_running(isolated_db, fake_cv2_open, stub_describer):
    from fastapi.testclient import TestClient
    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.post(
            "/api/video/live/start",
            json={"camera_index": 0, "steady_interval": 999.0, "min_scene_gap": 999.0},
        )
        assert r.status_code == 202

        try:
            r2 = c.post(
                "/api/video/live/start",
                json={"camera_index": 0, "steady_interval": 999.0, "min_scene_gap": 999.0},
            )
            assert r2.status_code == 409
            assert "already running" in r2.json()["detail"].lower()
        finally:
            c.post("/api/video/live/stop")


def test_live_cameras_endpoint(monkeypatch):
    """/live/cameras delegates to describe_cameras()."""
    from fastapi.testclient import TestClient
    from cortex_vision.server import app

    monkeypatch.setattr(
        "cortex_vision.capture.camera.describe_cameras",
        lambda *a, **kw: [
            {"index": 0, "native_resolution": [1920, 1080], "native_fps": 30.0},
            {"index": 2, "native_resolution": [1280, 720], "native_fps": 60.0},
        ],
    )

    with TestClient(app) as c:
        r = c.get("/api/video/live/cameras")
        assert r.status_code == 200
        cams = r.json()["cameras"]
        assert len(cams) == 2
        assert cams[0]["index"] == 0
