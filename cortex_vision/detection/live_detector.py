"""Real-time scene change detection (Phase 4).

Adapted from VisualFast/scene_detector.py with the following simplifications
for cortex-vision:

  - No describer baked in. The detector emits scene-change events; the live
    pipeline calls the describer in a worker thread so detection latency
    stays under 50 ms regardless of LLM response time.
  - No in-memory transcript. Persistence happens via SessionManager.
  - No YOLO worker integration. cortex-vision skips YOLO by design.
  - No JPEG encoding. Detector hands raw BGR ndarrays to the pipeline; the
    pipeline writes them to disk as keyframes only when needed.

Detection uses three complementary methods, ANY of which can trigger:

  1. HSV histogram correlation (catches color distribution shifts)
  2. Mean absolute pixel difference (catches brightness changes)
  3. Structural difference (% pixels that changed > threshold; robust to noise)

After a trigger, a burst of frames is captured at offsets (default 0s, 0.3s,
0.8s) so transitions can settle before the describer sees them. Between
scene changes, steady-state samples fire every `steady_interval` seconds
so long static scenes (a paused tutorial, a slide deck) still get periodic
descriptions.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from cortex_vision.capture.camera import Frame


# ---------------------------------------------------------------------------
# Event shape — what the detector emits via on_event callback
# ---------------------------------------------------------------------------

@dataclass
class SceneChangeEvent:
    """Emitted when the detector decides a scene has changed (or that it's
    time for a steady-state update)."""
    scene_index: int
    change_type: str                            # "scene_change" | "update"
    timestamp_wall: float                       # time.time()
    timestamp_perf: float                       # time.perf_counter()
    burst_frames: list[np.ndarray] = field(default_factory=list)
    similarity: float = 1.0
    pixel_diff: float = 0.0
    structural_diff: float = 0.0
    trigger_method: str = ""
    brightness_per_frame: list[float] = field(default_factory=list)
    resolution: tuple[int, int] = (0, 0)        # (width, height)


SceneCallback = Callable[[SceneChangeEvent], None]


# ---------------------------------------------------------------------------
# Frame comparison primitives (lifted verbatim from VisualFast)
# ---------------------------------------------------------------------------

def _compute_histogram(frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def _compute_pixel_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(a, b)))


def _compute_structural_diff(a: np.ndarray, b: np.ndarray) -> float:
    diff = cv2.absdiff(a, b)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY) if len(diff.shape) == 3 else diff
    changed = int(np.sum(gray > 30))
    total = gray.shape[0] * gray.shape[1]
    return float(changed / total) if total else 0.0


def _is_usable_frame(frame: np.ndarray, min_brightness: float = 15.0) -> bool:
    """Reject near-black or near-white frames (transitions, fades)."""
    mean = float(np.mean(frame))
    return min_brightness < mean < 245.0


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class LiveSceneDetector:
    """Threaded real-time scene detector.

    Owns its own daemon thread. The pipeline feeds frames via `feed()`; the
    detector compares them to the previous frame using three methods and
    emits SceneChangeEvent when any method's threshold is exceeded.

    Pause/resume supported. Settings updatable at runtime.
    """

    def __init__(
        self,
        on_event: SceneCallback,
        threshold: float = 0.85,
        pixel_diff_threshold: float = 25.0,
        structural_threshold: float = 0.15,
        burst_offsets: list[float] | None = None,
        steady_interval: float = 30.0,
        min_scene_gap: float = 3.0,
    ) -> None:
        self.on_event = on_event
        self.threshold = threshold
        self.pixel_diff_threshold = pixel_diff_threshold
        self.structural_threshold = structural_threshold
        self.burst_offsets = burst_offsets or [0.0, 0.3, 0.8]
        self.steady_interval = steady_interval
        self.min_scene_gap = min_scene_gap

        # Internal state
        self._prev_hist: np.ndarray | None = None
        self._prev_frame: np.ndarray | None = None
        self._last_scene_time: float = 0.0
        self._last_steady_time: float = 0.0
        self._scene_index: int = 0
        self._burst_pending: list[float] = []
        self._burst_frames: list[np.ndarray] = []
        self._in_burst: bool = False
        self._trigger_method: str = ""

        # Concurrency
        self._frame_lock = threading.Lock()
        self._latest_frame: Frame | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = True

        # Stats — read-only snapshot for /status endpoint
        self.stats: dict = {
            "scene_changes": 0,
            "updates": 0,
            "current_scene": 0,
            "last_similarity": 1.0,
            "last_pixel_diff": 0.0,
            "last_structural_diff": 0.0,
            "in_burst": False,
            "paused": True,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the detection daemon thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cortex-vision-detector")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to exit and wait."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def resume(self) -> None:
        """Unpause. First steady update fires immediately (forced timing)."""
        self.paused = False
        self.stats["paused"] = False
        # Set last_steady_time so (now - _last_steady_time) >= steady_interval
        # is True on the next iteration. Without this the user waits up to
        # `steady_interval` seconds before seeing any output.
        self._last_steady_time = time.perf_counter() - self.steady_interval

    def pause(self) -> None:
        """Pause detection — frames are still received but ignored."""
        self.paused = True
        self._in_burst = False
        self._burst_pending = []
        self._burst_frames = []
        self.stats["paused"] = True
        self.stats["in_burst"] = False

    # ------------------------------------------------------------------
    # Frame intake
    # ------------------------------------------------------------------

    def feed(self, frame: Frame) -> None:
        """Update the latest-frame reference. Called on every capture tick."""
        with self._frame_lock:
            self._latest_frame = frame

    def update_settings(self, **kwargs) -> None:
        """Live-tune detection thresholds without restarting."""
        for k, v in kwargs.items():
            if k in (
                "threshold", "pixel_diff_threshold", "structural_threshold",
                "min_scene_gap", "steady_interval",
            ) and isinstance(v, (int, float)):
                setattr(self, k, float(v))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        last_index = -1
        while not self._stop.is_set():
            if self.paused:
                time.sleep(0.05)
                continue

            with self._frame_lock:
                frame = self._latest_frame

            if frame is None or frame.index == last_index:
                time.sleep(0.01)
                continue
            last_index = frame.index
            now = time.perf_counter()
            wall_now = time.time()

            # Compare against previous frame
            hist = _compute_histogram(frame.image)
            similarity = 1.0
            pixel_diff = 0.0
            structural_diff = 0.0
            if self._prev_hist is not None:
                similarity = float(cv2.compareHist(self._prev_hist, hist, cv2.HISTCMP_CORREL))
            if self._prev_frame is not None:
                pixel_diff = _compute_pixel_diff(self._prev_frame, frame.image)
                structural_diff = _compute_structural_diff(self._prev_frame, frame.image)

            self._prev_hist = hist
            self._prev_frame = frame.image.copy()
            self.stats["last_similarity"] = round(similarity, 4)
            self.stats["last_pixel_diff"] = round(pixel_diff, 1)
            self.stats["last_structural_diff"] = round(structural_diff, 4)

            # Scene-change trigger logic — any of the three methods firing,
            # gated by min_scene_gap to debounce
            time_since_last = now - self._last_scene_time
            hist_trigger = similarity < self.threshold
            pixel_trigger = pixel_diff > self.pixel_diff_threshold
            struct_trigger = structural_diff > self.structural_threshold

            if (hist_trigger or pixel_trigger or struct_trigger) and \
                    time_since_last > self.min_scene_gap:
                self._begin_burst(
                    frame, now, similarity, pixel_diff, structural_diff,
                    hist_trigger, pixel_trigger, struct_trigger,
                )
                continue

            # Burst capture — collecting frames at the configured offsets
            if self._in_burst and self._burst_pending:
                if now >= self._burst_pending[0]:
                    if _is_usable_frame(frame.image):
                        self._burst_frames.append(frame.image.copy())
                    self._burst_pending.pop(0)
                    if not self._burst_pending:
                        self._finalize_burst(frame, similarity, wall_now, now)
                continue

            # Steady-state sampling for long static scenes
            if not self._in_burst and \
                    (now - self._last_steady_time) >= self.steady_interval:
                self._last_steady_time = now
                self._emit_steady(frame, similarity, wall_now, now)

    def _begin_burst(
        self,
        frame: Frame,
        now: float,
        similarity: float,
        pixel_diff: float,
        structural_diff: float,
        hist_trigger: bool,
        pixel_trigger: bool,
        struct_trigger: bool,
    ) -> None:
        """Start a new burst capture. If we were already in a burst, abort it."""
        reasons: list[str] = []
        if hist_trigger:
            reasons.append(f"histogram={similarity:.3f}<{self.threshold}")
        if pixel_trigger:
            reasons.append(f"pixel_diff={pixel_diff:.1f}>{self.pixel_diff_threshold}")
        if struct_trigger:
            reasons.append(f"structural={structural_diff:.3f}>{self.structural_threshold}")

        self._scene_index += 1
        self._last_scene_time = now
        self.stats["scene_changes"] += 1
        self.stats["current_scene"] = self._scene_index
        self._trigger_method = ", ".join(reasons)

        self._in_burst = True
        self.stats["in_burst"] = True
        self._burst_frames = [frame.image.copy()] if _is_usable_frame(frame.image) else []
        self._burst_pending = [now + offset for offset in self.burst_offsets[1:]]

        # Edge case: single-offset burst (e.g. burst_offsets=[0.0]) leaves
        # _burst_pending empty. Without an explicit finalize here, the next
        # iteration's `if self._in_burst and self._burst_pending:` check is
        # False and the burst would hang forever. Resolve by finalizing now.
        if not self._burst_pending:
            self._finalize_burst(frame, similarity, time.time(), now)

    def _finalize_burst(
        self,
        frame: Frame,
        similarity: float,
        wall_now: float,
        now: float,
    ) -> None:
        """End-of-burst handler. Build a SceneChangeEvent and emit."""
        self._in_burst = False
        self.stats["in_burst"] = False
        # Fallback: if every burst frame was dark, take the current frame so
        # we're not left empty
        if not self._burst_frames:
            self._burst_frames = [frame.image.copy()]

        h, w = self._burst_frames[0].shape[:2]
        event = SceneChangeEvent(
            scene_index=self._scene_index,
            change_type="scene_change",
            timestamp_wall=wall_now,
            timestamp_perf=now,
            burst_frames=list(self._burst_frames),
            similarity=similarity,
            trigger_method=self._trigger_method,
            brightness_per_frame=[float(np.mean(f)) for f in self._burst_frames],
            resolution=(w, h),
        )
        self._burst_frames = []
        try:
            self.on_event(event)
        except Exception:
            # Don't let a callback crash kill the detector thread
            import logging
            logging.getLogger("cortex_vision.live_detector").exception(
                "on_event callback raised — continuing"
            )
        self._last_steady_time = now

    def _emit_steady(
        self,
        frame: Frame,
        similarity: float,
        wall_now: float,
        now: float,
    ) -> None:
        """Periodic update for static scenes."""
        self.stats["updates"] += 1
        h, w = frame.image.shape[:2]
        event = SceneChangeEvent(
            scene_index=self._scene_index,
            change_type="update",
            timestamp_wall=wall_now,
            timestamp_perf=now,
            burst_frames=[frame.image.copy()],
            similarity=similarity,
            trigger_method="steady_interval",
            brightness_per_frame=[float(np.mean(frame.image))],
            resolution=(w, h),
        )
        try:
            self.on_event(event)
        except Exception:
            import logging
            logging.getLogger("cortex_vision.live_detector").exception(
                "on_event callback raised — continuing"
            )
