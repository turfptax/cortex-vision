"""Tests for the non-invasive camera enumeration (v0.3.1).

The bug we're guarding against: cv2's CAP_DSHOW probing has historically
SEH-crashed the entire bundle on Windows when certain virtual cameras
are in odd states (DroidCam offline, OBS Virtual Camera mid-init, etc.).
The fix uses pygrabber's ICreateDevEnum to LIST devices without OPENING
them. We test that:

  1. When pygrabber works, we use its results and never call cv2
  2. When pygrabber is missing/fails, we fall back to the cv2 probe
  3. The cv2 probe is the only path that can crash, and it's the fallback
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Path A: pygrabber works
# ---------------------------------------------------------------------------

def test_describe_uses_pygrabber_when_available():
    """If pygrabber returns devices, we never touch cv2."""
    from cortex_vision.capture import camera

    fake_graph_class = MagicMock()
    fake_graph_class.return_value.get_input_devices.return_value = [
        "Integrated Camera",
        "OBS Virtual Camera",
        "DroidCam Source",
    ]

    fake_module = MagicMock()
    fake_module.FilterGraph = fake_graph_class

    cv2_called = MagicMock()

    with patch.dict(
        "sys.modules",
        {"pygrabber.dshow_graph": fake_module},
    ), patch("cortex_vision.capture.camera.cv2.VideoCapture", cv2_called):
        result = camera.describe_cameras()

    assert len(result) == 3
    assert result[0] == {"index": 0, "name": "Integrated Camera"}
    assert result[1] == {"index": 1, "name": "OBS Virtual Camera"}
    assert result[2] == {"index": 2, "name": "DroidCam Source"}

    # Critical: cv2.VideoCapture must NOT have been called — that's the
    # whole point of switching to pygrabber. If this assertion fails,
    # we're still SEH-crash-prone.
    cv2_called.assert_not_called()


def test_describe_returns_empty_when_no_devices():
    """Pygrabber returning [] is a valid response (no cameras attached)."""
    from cortex_vision.capture import camera

    fake_graph_class = MagicMock()
    fake_graph_class.return_value.get_input_devices.return_value = []

    fake_module = MagicMock()
    fake_module.FilterGraph = fake_graph_class

    with patch.dict("sys.modules", {"pygrabber.dshow_graph": fake_module}):
        result = camera.describe_cameras()

    assert result == []


# ---------------------------------------------------------------------------
# Path B: pygrabber unavailable -> fallback to cv2 probe
# ---------------------------------------------------------------------------

def test_describe_falls_back_to_cv2_when_pygrabber_missing():
    """On non-Windows or in environments where pygrabber didn't install,
    the import fails and we fall back to the legacy cv2 probe."""
    from cortex_vision.capture import camera

    # Simulate ImportError by making pygrabber's import bomb
    def boom_import(*args, **kw):
        raise ImportError("no pygrabber here")

    # Mock cv2 to return one fake device at index 1
    fake_cap = MagicMock()
    fake_cap.isOpened.side_effect = lambda: True            # Pretend index 0/1/2 all open
    fake_cap.get.side_effect = lambda prop: {
        3: 1280,                                            # FRAME_WIDTH
        4: 720,                                             # FRAME_HEIGHT
        5: 30.0,                                            # FPS
    }.get(prop, 0)

    with patch("cortex_vision.capture.camera._enumerate_via_pygrabber", lambda: None), \
         patch("cortex_vision.capture.camera.cv2.VideoCapture", lambda *a, **kw: fake_cap):
        result = camera.describe_cameras(max_check=2)

    # Should fall through to cv2 probe and return resolution data
    assert len(result) == 2
    assert result[0]["native_resolution"] == [1280, 720]
    assert "name" not in result[0]                          # pygrabber path adds name; fallback doesn't


def test_describe_handles_pygrabber_runtime_error():
    """COM init can fail even when pygrabber imports — e.g. running as a
    Windows service. We treat that as 'unavailable' and fall through."""
    from cortex_vision.capture import camera

    fake_graph_class = MagicMock(side_effect=RuntimeError("COM init failed"))
    fake_module = MagicMock()
    fake_module.FilterGraph = fake_graph_class

    fake_cap = MagicMock()
    fake_cap.isOpened.return_value = False                  # No cv2 cameras either

    with patch.dict("sys.modules", {"pygrabber.dshow_graph": fake_module}), \
         patch("cortex_vision.capture.camera.cv2.VideoCapture", lambda *a, **kw: fake_cap):
        result = camera.describe_cameras(max_check=2)

    assert result == []                                     # graceful empty, not a crash


# ---------------------------------------------------------------------------
# find_cameras() — wrapper around describe_cameras
# ---------------------------------------------------------------------------

def test_find_cameras_returns_indices_only():
    from cortex_vision.capture import camera

    fake_graph_class = MagicMock()
    fake_graph_class.return_value.get_input_devices.return_value = [
        "Cam A", "Cam B",
    ]
    fake_module = MagicMock()
    fake_module.FilterGraph = fake_graph_class

    with patch.dict("sys.modules", {"pygrabber.dshow_graph": fake_module}):
        indices = camera.find_cameras()

    assert indices == [0, 1]


# ---------------------------------------------------------------------------
# Integration with the /api/video/live/cameras endpoint
# ---------------------------------------------------------------------------

def test_endpoint_returns_pygrabber_format(tmp_path, monkeypatch):
    """Smoke test of the actual HTTP endpoint with mocked pygrabber."""
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )

    fake_graph_class = MagicMock()
    fake_graph_class.return_value.get_input_devices.return_value = [
        "OBS Virtual Camera",
    ]
    fake_module = MagicMock()
    fake_module.FilterGraph = fake_graph_class

    from fastapi.testclient import TestClient
    from cortex_vision.server import app

    with patch.dict("sys.modules", {"pygrabber.dshow_graph": fake_module}):
        with TestClient(app) as c:
            r = c.get("/api/video/live/cameras")
    assert r.status_code == 200
    cams = r.json()["cameras"]
    assert len(cams) == 1
    assert cams[0]["name"] == "OBS Virtual Camera"
    assert cams[0]["index"] == 0
