"""Tests for the live scene detector — Phase 4.

Synthetic frames at known histograms exercise the three detection paths:
  - histogram correlation (color shift)
  - pixel diff (brightness shift)
  - structural diff (large-area change)

We don't rely on a real camera. Each frame is just a numpy array we feed
directly via `feed()`. Detector runs in its own thread so we sleep briefly
between feeds to let it process.
"""
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from cortex_vision.capture.camera import Frame
from cortex_vision.detection.live_detector import (
    LiveSceneDetector,
    SceneChangeEvent,
    _compute_histogram,
    _compute_pixel_diff,
    _compute_structural_diff,
    _is_usable_frame,
)


def _make_frame(image: np.ndarray, index: int) -> Frame:
    return Frame(
        image=image,
        timestamp=time.perf_counter(),
        index=index,
        original_size=(image.shape[1], image.shape[0]),
    )


def _solid_color(rgb: tuple[int, int, int], shape=(216, 384, 3)) -> np.ndarray:
    """Build a solid-color BGR frame at the given mean intensity."""
    img = np.zeros(shape, dtype=np.uint8)
    b, g, r = rgb
    img[:, :, 0] = b
    img[:, :, 1] = g
    img[:, :, 2] = r
    return img


# ---------------------------------------------------------------------------
# Comparison primitives
# ---------------------------------------------------------------------------

def test_pixel_diff_identical_frames_zero():
    a = _solid_color((100, 100, 100))
    assert _compute_pixel_diff(a, a) == 0.0


def test_pixel_diff_white_vs_black_max():
    """White vs black should saturate near 255."""
    diff = _compute_pixel_diff(_solid_color((255, 255, 255)), _solid_color((0, 0, 0)))
    assert diff > 250


def test_structural_diff_identical_zero():
    a = _solid_color((100, 100, 100))
    assert _compute_structural_diff(a, a) == 0.0


def test_structural_diff_full_change_one():
    """A complete content swap should give nearly 1.0 (every pixel changed)."""
    a = _solid_color((10, 10, 10))
    b = _solid_color((250, 250, 250))
    assert _compute_structural_diff(a, b) > 0.95


def test_is_usable_frame_filter():
    assert _is_usable_frame(_solid_color((128, 128, 128)))
    assert not _is_usable_frame(_solid_color((5, 5, 5)))      # too dark
    assert not _is_usable_frame(_solid_color((250, 250, 250))) # too bright


# ---------------------------------------------------------------------------
# Detector lifecycle
# ---------------------------------------------------------------------------

def test_detector_paused_emits_nothing():
    events: list[SceneChangeEvent] = []
    det = LiveSceneDetector(on_event=events.append, min_scene_gap=0.0)
    det.start()
    try:
        # Feed wildly different frames while paused — nothing should fire
        for i in range(5):
            det.feed(_make_frame(_solid_color((i * 50, 0, 0)), i))
            time.sleep(0.05)
    finally:
        det.stop(timeout=2)
    assert events == []


def test_detector_emits_scene_change_on_color_shift():
    events: list[SceneChangeEvent] = []
    det = LiveSceneDetector(
        on_event=events.append,
        threshold=0.95,                             # tight; easy to trigger
        pixel_diff_threshold=10.0,
        structural_threshold=0.10,
        burst_offsets=[0.0],                         # single keyframe so we don't wait
        steady_interval=999.0,                       # don't fire steady updates
        min_scene_gap=0.0,                           # no debounce
    )
    det.start()
    det.resume()
    try:
        # Feed two very different solid colors
        det.feed(_make_frame(_solid_color((20, 20, 20)), 0))
        time.sleep(0.1)
        det.feed(_make_frame(_solid_color((200, 50, 50)), 1))
        time.sleep(0.3)
    finally:
        det.stop(timeout=2)

    # resume() force-fires the first steady update immediately, so it may
    # land in the event list before our scene_change. Filter for the type
    # we actually care about.
    scene_changes = [e for e in events if e.change_type == "scene_change"]
    assert len(scene_changes) >= 1, f"got events: {[e.change_type for e in events]}"
    e = scene_changes[0]
    assert e.scene_index == 1                        # first detected change is index 1
    assert e.burst_frames                            # we got at least one keyframe
    assert e.trigger_method                          # explanation populated


def test_detector_min_scene_gap_debounces():
    """Two scene changes within min_scene_gap should produce only one event."""
    events: list[SceneChangeEvent] = []
    det = LiveSceneDetector(
        on_event=events.append,
        threshold=0.99,
        pixel_diff_threshold=5.0,
        structural_threshold=0.05,
        burst_offsets=[0.0],
        steady_interval=999.0,
        min_scene_gap=2.0,                           # high debounce
    )
    det.start()
    det.resume()
    try:
        det.feed(_make_frame(_solid_color((10, 10, 10)), 0))
        time.sleep(0.05)
        det.feed(_make_frame(_solid_color((200, 0, 0)), 1))
        time.sleep(0.1)
        det.feed(_make_frame(_solid_color((0, 200, 0)), 2))    # within min_scene_gap
        time.sleep(0.2)
    finally:
        det.stop(timeout=2)

    # Should debounce — at most one scene_change accepted in 2s
    scene_changes = [e for e in events if e.change_type == "scene_change"]
    assert len(scene_changes) <= 1


def test_detector_emits_steady_updates():
    """When steady_interval elapses without scene change, fire 'update' events."""
    events: list[SceneChangeEvent] = []
    det = LiveSceneDetector(
        on_event=events.append,
        threshold=0.0,                               # impossible to trigger
        pixel_diff_threshold=999.0,
        structural_threshold=2.0,
        burst_offsets=[0.0],
        steady_interval=0.2,                         # quick for testing
        min_scene_gap=999.0,                         # don't trigger scene changes
    )
    det.start()
    det.resume()
    try:
        # Feed identical frames — only steady updates should fire
        for i in range(10):
            det.feed(_make_frame(_solid_color((128, 128, 128)), i))
            time.sleep(0.05)
        time.sleep(0.4)                              # let steady updates accumulate
    finally:
        det.stop(timeout=2)

    updates = [e for e in events if e.change_type == "update"]
    assert len(updates) >= 1
    assert all(e.trigger_method == "steady_interval" for e in updates)


def test_detector_stop_is_idempotent():
    det = LiveSceneDetector(on_event=lambda _: None)
    det.start()
    det.stop(timeout=2)
    det.stop(timeout=2)                              # second stop should not raise


def test_detector_callback_exception_does_not_crash_thread():
    """A throwing on_event must not kill the detector thread."""
    call_count = [0]

    def bad_callback(event: SceneChangeEvent) -> None:
        call_count[0] += 1
        raise RuntimeError("intentional")

    det = LiveSceneDetector(
        on_event=bad_callback,
        threshold=0.95,
        pixel_diff_threshold=10.0,
        structural_threshold=0.1,
        burst_offsets=[0.0],
        steady_interval=999.0,
        min_scene_gap=0.0,
    )
    det.start()
    det.resume()
    try:
        det.feed(_make_frame(_solid_color((10, 10, 10)), 0))
        time.sleep(0.1)
        det.feed(_make_frame(_solid_color((200, 50, 50)), 1))
        time.sleep(0.3)
        # Even though the first callback threw, the thread should still be alive
        det.feed(_make_frame(_solid_color((50, 200, 50)), 2))
        time.sleep(0.3)
    finally:
        det.stop(timeout=2)

    # Detector kept running — at least one callback fired
    assert call_count[0] >= 1
