"""Live capture orchestrator — Phase 4.

Drives the camera -> scene detector -> describer pipeline as a long-running
background process. One LivePipeline instance per active live session;
LivePipelineManager enforces the singleton invariant at the FastAPI layer.

Threads owned by a running LivePipeline:

  - capture     : reads frames from FrameCapture, feeds the detector
  - detector    : LiveSceneDetector's own thread (3-method comparison)
  - describer   : pulls scene events from a job queue, calls LM Studio,
                  writes descriptions back to SessionManager
  - stats       : emits a stats event every 1s to the WS queue

Event protocol (queued for WebSocket consumers):

    {"type": "started",   "session_id": ..., "camera_index": ..., "resolution": [...]}
    {"type": "scene",     "scene_index": 5, "change_type": "scene_change" | "update",
                          "thumbnail_url": ..., "trigger_method": ..., "similarity": ...}
    {"type": "described", "scene_index": 5, "description": "...", "describer_model": ...}
    {"type": "stats",     "fps": 25.3, "frames": 1234, "scene_count": 5,
                          "elapsed_s": 60.2, ...}
    {"type": "stopped",   "session_id": ..., "scene_count": 12, "duration_s": 600}
    {"type": "error",     "message": "..."}

WebSocket consumers should treat any future "type" values as forward-compatible
extensions and ignore unknown ones.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cortex_vision.capture.camera import FrameCapture
from cortex_vision.description.lmstudio_client import (
    LMStudioUnavailable,
    chat_with_images,
)
from cortex_vision.description.narrative import (
    SCENE_DESCRIBER_SYSTEM,
    build_scene_describer_prompt,
)
from cortex_vision.detection.live_detector import (
    LiveSceneDetector,
    SceneChangeEvent,
)
from cortex_vision.models.schemas import SceneEntry
from cortex_vision.pipeline.session_manager import SessionManager
from cortex_vision.storage import db as db_module

logger = logging.getLogger("cortex_vision.pipeline.live")


# Keep at most this many events in the WS buffer. If a slow consumer falls
# behind, oldest events get dropped — better than unbounded memory growth.
_EVENT_QUEUE_MAX = 1000

# Describer worker queue. Bounded so a slow LLM doesn't pile up scenes.
_DESCRIBER_QUEUE_MAX = 50


@dataclass
class LivePipelineConfig:
    """Configuration for one live session."""
    session_id: str
    camera_index: int = 0
    resolution: tuple[int, int] = (384, 216)
    threshold: float = 0.85
    pixel_diff_threshold: float = 25.0
    structural_threshold: float = 0.15
    steady_interval: float = 30.0
    min_scene_gap: float = 3.0
    describer_model: str | None = None
    keyframes_per_scene: int = 1                # for now we save 1 keyframe per scene


class LivePipeline:
    """One live capture session. See module docstring for thread layout."""

    def __init__(self, config: LivePipelineConfig) -> None:
        self.config = config
        self.sm = SessionManager()
        self.session_dir = db_module.default_artifacts_dir() / config.session_id
        self.frames_dir = self.session_dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)

        # Concurrency primitives
        self._stop = threading.Event()
        self._started_at: float = 0.0
        self._capture_thread: threading.Thread | None = None
        self._describer_thread: threading.Thread | None = None
        self._stats_thread: threading.Thread | None = None
        self._capture: FrameCapture | None = None
        self._detector: LiveSceneDetector | None = None

        # Job + event queues (thread-safe)
        self._describer_jobs: queue.Queue[tuple[int, list[str]]] = queue.Queue(
            maxsize=_DESCRIBER_QUEUE_MAX,
        )
        self._events: queue.Queue[dict] = queue.Queue(maxsize=_EVENT_QUEUE_MAX)

        # Live counters (read by /status endpoint)
        self._frame_count = 0
        self._scene_count = 0
        self._capture_fps = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the camera, spawn all threads, transition session to 'capturing'."""
        if self._started_at:
            raise RuntimeError("LivePipeline already started")

        self._capture = FrameCapture(
            camera_index=self.config.camera_index,
            resolution=self.config.resolution,
        )
        if not self._capture.open():
            raise RuntimeError(
                f"Could not open camera index {self.config.camera_index}. "
                f"Is OBS Virtual Camera running? Use GET /api/video/live/cameras to list."
            )

        self._detector = LiveSceneDetector(
            on_event=self._handle_scene_event,
            threshold=self.config.threshold,
            pixel_diff_threshold=self.config.pixel_diff_threshold,
            structural_threshold=self.config.structural_threshold,
            steady_interval=self.config.steady_interval,
            min_scene_gap=self.config.min_scene_gap,
        )

        # Move session into 'capturing' state
        self.sm.update_status(self.config.session_id, "capturing")
        self._started_at = time.perf_counter()

        # Launch threads
        self._detector.start()
        self._detector.resume()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="cortex-vision-capture"
        )
        self._describer_thread = threading.Thread(
            target=self._describer_loop, daemon=True, name="cortex-vision-describer"
        )
        self._stats_thread = threading.Thread(
            target=self._stats_loop, daemon=True, name="cortex-vision-stats"
        )
        self._capture_thread.start()
        self._describer_thread.start()
        self._stats_thread.start()

        self._emit({
            "type": "started",
            "session_id": self.config.session_id,
            "camera_index": self.config.camera_index,
            "resolution": list(self.config.resolution),
            "native_resolution": list(self._capture.native_resolution),
            "native_fps": self._capture.native_fps,
        })
        logger.info(
            "session=%s started camera=%d resolution=%s",
            self.config.session_id, self.config.camera_index, self.config.resolution,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal all threads to stop and join. Idempotent."""
        if self._stop.is_set():
            return
        self._stop.set()

        # Stop the detector first so it won't enqueue new describer jobs
        if self._detector is not None:
            self._detector.stop(timeout=timeout)

        # Wait for the capture loop to drop the camera
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=timeout)
        if self._capture is not None:
            self._capture.close()

        # Drain describer queue or wait for in-flight job to finish
        if self._describer_thread is not None:
            self._describer_thread.join(timeout=timeout)

        if self._stats_thread is not None:
            self._stats_thread.join(timeout=timeout)

        # Move session to 'complete'
        try:
            self.sm.update_status(self.config.session_id, "complete")
        except Exception:                                       # noqa: BLE001
            logger.exception("failed to mark live session complete")

        elapsed = time.perf_counter() - self._started_at if self._started_at else 0
        self._emit({
            "type": "stopped",
            "session_id": self.config.session_id,
            "scene_count": self._scene_count,
            "duration_s": round(elapsed, 1),
        })
        logger.info("session=%s stopped scenes=%d", self.config.session_id, self._scene_count)

    @property
    def is_running(self) -> bool:
        return self._started_at > 0 and not self._stop.is_set()

    # ------------------------------------------------------------------
    # Event consumer API (used by /api/video/live/ws)
    # ------------------------------------------------------------------

    def get_event(self, timeout: float = 0.5) -> dict | None:
        """Block up to `timeout` seconds for the next event. None on timeout."""
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def status(self) -> dict:
        """Snapshot for the /status endpoint."""
        elapsed = time.perf_counter() - self._started_at if self._started_at else 0
        detector_stats = self._detector.stats if self._detector else {}
        return {
            "session_id": self.config.session_id,
            "is_running": self.is_running,
            "elapsed_s": round(elapsed, 1),
            "frame_count": self._frame_count,
            "fps": round(self._capture_fps, 1),
            "scene_count": self._scene_count,
            "describer_queue_depth": self._describer_jobs.qsize(),
            "event_queue_depth": self._events.qsize(),
            "detector": detector_stats,
        }

    # ------------------------------------------------------------------
    # Capture loop (thread)
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Read frames continuously; feed each to the detector."""
        last_fps_calc = time.perf_counter()
        frames_since_calc = 0

        try:
            while not self._stop.is_set():
                if self._capture is None:
                    break
                frame = self._capture.read()
                if frame is None:
                    # Camera dropped. Exit cleanly.
                    self._emit({
                        "type": "error",
                        "message": "camera read failed — stopping live session",
                    })
                    break
                self._detector.feed(frame)               # type: ignore[union-attr]
                self._frame_count = frame.index + 1
                frames_since_calc += 1

                # Roll FPS once a second
                now = time.perf_counter()
                if now - last_fps_calc >= 1.0:
                    self._capture_fps = frames_since_calc / (now - last_fps_calc)
                    frames_since_calc = 0
                    last_fps_calc = now
        except Exception as e:                           # noqa: BLE001
            logger.exception("capture_loop crashed")
            self._emit({"type": "error", "message": f"capture loop crashed: {e}"})

    # ------------------------------------------------------------------
    # Scene-event handler (called from detector thread)
    # ------------------------------------------------------------------

    def _handle_scene_event(self, event: SceneChangeEvent) -> None:
        """Persist keyframes + scene row, emit WS event, queue describer job.

        Runs in the detector thread, so it must be fast (<50 ms) — all the
        slow work (LLM call) happens in the describer thread.
        """
        scene_dir = self.frames_dir / str(event.scene_index)
        scene_dir.mkdir(parents=True, exist_ok=True)

        # Save keyframes to disk
        keyframe_paths: list[str] = []
        for frame_idx, img in enumerate(event.burst_frames[: self.config.keyframes_per_scene]):
            out_path = scene_dir / f"{frame_idx}.jpg"
            cv2.imwrite(str(out_path), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            keyframe_paths.append(str(out_path))

        if not keyframe_paths:
            return

        # Append scene row with empty description (will be patched after describer runs)
        entry = SceneEntry(
            index=event.scene_index,
            start_s=event.timestamp_perf - self._started_at,
            end_s=event.timestamp_perf - self._started_at,           # live = single-instant
            keyframe_paths=keyframe_paths,
            description="",
            describer_model="pending",
            similarity=event.similarity,
            trigger_method=event.trigger_method or event.change_type,
        )
        try:
            self.sm.append_scene(self.config.session_id, entry)
        except Exception:                                # noqa: BLE001
            logger.exception("failed to persist scene %d", event.scene_index)

        self._scene_count = max(self._scene_count, event.scene_index)

        # Emit "scene" event to WS
        self._emit({
            "type": "scene",
            "scene_index": event.scene_index,
            "change_type": event.change_type,
            "trigger_method": event.trigger_method,
            "similarity": round(event.similarity, 4),
            "thumbnail_url": (
                f"/api/video/jobs/{self.config.session_id}/frame/"
                f"{event.scene_index}/0"
            ),
            "timestamp_wall": event.timestamp_wall,
            "elapsed_s": round(event.timestamp_perf - self._started_at, 1),
        })

        # Queue describer job (non-blocking; drop if queue is full)
        try:
            self._describer_jobs.put_nowait((event.scene_index, keyframe_paths))
        except queue.Full:
            logger.warning(
                "describer queue full (>%d) — skipping describe for scene %d",
                _DESCRIBER_QUEUE_MAX, event.scene_index,
            )

    # ------------------------------------------------------------------
    # Describer worker (thread)
    # ------------------------------------------------------------------

    def _describer_loop(self) -> None:
        """Pull queued scene jobs and run LM Studio describer on each."""
        while not self._stop.is_set():
            try:
                scene_index, keyframe_paths = self._describer_jobs.get(timeout=0.5)
            except queue.Empty:
                continue

            description, model_label = self._describe_one(scene_index, keyframe_paths)
            try:
                self.sm.update_scene_description(
                    self.config.session_id, scene_index, description, model_label,
                )
            except Exception:                            # noqa: BLE001
                logger.exception("scene %d: failed to write description", scene_index)
                continue

            self._emit({
                "type": "described",
                "scene_index": scene_index,
                "description": description,
                "describer_model": model_label,
            })

    def _describe_one(
        self, scene_index: int, keyframe_paths: list[str]
    ) -> tuple[str, str]:
        """Call LM Studio for one scene. Returns (description, model_label)."""
        used_model = self.config.describer_model or os.environ.get(
            "CORTEX_VISION_LLM_MODEL", ""
        )
        label = f"lmstudio:{used_model}" if used_model else "lmstudio:default"

        prompt = build_scene_describer_prompt(
            scene_index=scene_index,
            duration_s=0.0,                              # live scenes are point-in-time
            keyframe_count=len(keyframe_paths),
        )
        try:
            description = chat_with_images(
                text=prompt,
                image_paths=keyframe_paths,
                system=SCENE_DESCRIBER_SYSTEM,
                model=self.config.describer_model,
                max_tokens=400,
                temperature=0.2,
            )
            return description.strip(), label
        except LMStudioUnavailable as e:
            logger.warning("scene %d describe failed: %s", scene_index, e)
            return ("", label + ":unavailable")
        except Exception as e:                           # noqa: BLE001
            logger.warning("scene %d describe raised %s", scene_index, type(e).__name__)
            return ("", label + ":error")

    # ------------------------------------------------------------------
    # Stats emitter (thread)
    # ------------------------------------------------------------------

    def _stats_loop(self) -> None:
        """Emit a stats event every 1s for live UI updates."""
        while not self._stop.is_set():
            time.sleep(1.0)
            if self._stop.is_set():
                break
            self._emit({
                "type": "stats",
                **self.status(),
            })

    # ------------------------------------------------------------------
    # Internal — emit to WS queue with overflow handling
    # ------------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        """Push to the WS queue. Drops oldest if full."""
        try:
            self._events.put_nowait(event)
        except queue.Full:
            # Drop oldest, retry once
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass


# ---------------------------------------------------------------------------
# Singleton manager
# ---------------------------------------------------------------------------

class LivePipelineManager:
    """Enforces single-active-live-session at the process level.

    The FastAPI server holds one of these at app.state.live_manager. The
    /api/video/live/start endpoint creates a session via SessionManager,
    constructs a LivePipeline, and asks this manager to start it.

    Concurrency: simple lock around the slot. Live sessions are user-driven
    and serialized — there's no contention to optimize for.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: LivePipeline | None = None

    def start(self, config: LivePipelineConfig) -> LivePipeline:
        with self._lock:
            if self._active is not None and self._active.is_running:
                raise RuntimeError(
                    f"A live session is already running: {self._active.config.session_id}"
                )
            pipeline = LivePipeline(config)
            pipeline.start()
            self._active = pipeline
            return pipeline

    def stop(self) -> dict | None:
        """Stop the active session if any. Returns its final status."""
        with self._lock:
            if self._active is None:
                return None
            status = self._active.status()
            self._active.stop()
            self._active = None
            return status

    def get_active(self) -> LivePipeline | None:
        with self._lock:
            return self._active

    def status(self) -> dict | None:
        active = self.get_active()
        return active.status() if active else None
