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

    EVERY event includes baseline timing fields:
        timestamp_wall  : float (Unix seconds, time.time())
        elapsed_s       : float (seconds since session start)

    Plus type-specific fields:

    {"type": "started",   timestamp_wall, elapsed_s, session_id, camera_index,
                          resolution, native_resolution, native_fps}
    {"type": "scene",     timestamp_wall, elapsed_s, scene_index,
                          change_type ("scene_change"|"update"), thumbnail_url,
                          trigger_method, similarity}
    {"type": "described", timestamp_wall, elapsed_s, scene_index, description,
                          describer_model}
    {"type": "stats",     timestamp_wall, elapsed_s, fps, frame_count,
                          scene_count, ...}
    {"type": "stopped",   timestamp_wall, elapsed_s, session_id, scene_count,
                          duration_s}
    {"type": "error",     timestamp_wall, elapsed_s, message}

The uniform baseline lets WebSocket consumers safely call `.toLocaleString()`
or similar formatting on any event without per-type undefined checks.
Consumers should treat unknown `type` values as forward-compatible and
ignore them.
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

from cortex_vision.audio.loopback import AudioCapture
from cortex_vision.audio.transcribe import (
    WhisperUnavailable,
    bucket_segments_by_scene,
    is_configured as whisper_configured,
    transcribe_file,
)
from cortex_vision.capture.camera import FrameCapture
from cortex_vision.description.lmstudio_client import (
    LMStudioUnavailable,
    chat_with_images,
)
from cortex_vision.models.schemas import TranscriptEntry
from datetime import datetime, timezone
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
    # ── Audio (v0.4.0) ───────────────────────────────────────────────
    # audio_source semantics:
    #   None     -> no audio capture (default, backward-compat)
    #   "desktop"-> WASAPI loopback on the default Windows output device
    #   int      -> sounddevice input device index (mic)
    #   str      -> substring match on device name
    audio_source: int | str | None = None
    # If True, run whisper.cpp on the captured audio after Stop and
    # bucket segments per scene. Requires audio_source != None.
    transcribe_audio: bool = False


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

        # Audio capture (v0.4.0). Optional — only spun up if config.audio_source
        # is set. Audio is recorded continuously to <session_dir>/audio.wav and
        # transcribed AFTER Stop (not in real time) for better quality.
        self._audio_capture: AudioCapture | None = None
        self._audio_path = self.session_dir / "audio.wav"

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

        # Audio capture (v0.4.0) — optional, opt-in via config.audio_source.
        # Failure here is non-fatal: we log and continue with video-only.
        if self.config.audio_source is not None:
            try:
                self._audio_capture = AudioCapture(
                    out_path=self._audio_path,
                    device=self.config.audio_source,
                    on_level=self._handle_audio_level,
                )
                self._audio_capture.open()
                logger.info(
                    "session=%s audio source=%s -> %s",
                    self.config.session_id, self.config.audio_source, self._audio_path,
                )
            except Exception as e:                       # noqa: BLE001
                logger.exception("session=%s audio capture failed", self.config.session_id)
                self._audio_capture = None
                self._emit({
                    "type": "error",
                    "subsystem": "audio",
                    "message": f"audio capture failed: {e}",
                })

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
        """Signal all threads to stop and join, then post-process audio.
        Idempotent."""
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

        # Stop audio capture and finalize the WAV file
        if self._audio_capture is not None:
            try:
                self._audio_capture.close()
                logger.info(
                    "session=%s audio captured %.1fs to %s",
                    self.config.session_id,
                    self._audio_capture.duration_s,
                    self._audio_path,
                )
            except Exception:                            # noqa: BLE001
                logger.exception("error closing audio capture")

        # Drain describer queue or wait for in-flight job to finish
        if self._describer_thread is not None:
            self._describer_thread.join(timeout=timeout)

        if self._stats_thread is not None:
            self._stats_thread.join(timeout=timeout)

        # Post-process transcription — runs synchronously after threads stop
        # but before we emit the terminal "stopped" event. Keeps the contract
        # simple: when the user sees "stopped", everything is persisted.
        if self.config.transcribe_audio and self._audio_path.exists():
            self._post_transcribe()

        # Move session to 'complete'
        try:
            self.sm.update_status(self.config.session_id, "complete")
        except Exception:                                # noqa: BLE001
            logger.exception("failed to mark live session complete")

        elapsed = time.perf_counter() - self._started_at if self._started_at else 0
        self._emit({
            "type": "stopped",
            "session_id": self.config.session_id,
            "scene_count": self._scene_count,
            "duration_s": round(elapsed, 1),
            "audio_recorded": self._audio_capture is not None,
            "audio_duration_s": (
                round(self._audio_capture.duration_s, 1)
                if self._audio_capture else 0.0
            ),
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
        """Snapshot for the /status endpoint and the periodic `stats` WS event.

        Field names here are part of the public contract documented at the top
        of this module. Don't rename without a frontend coordination — e.g.
        `frames` was previously called `frame_count` in the implementation
        but was always `frames` in the protocol docstring; the rename in
        v0.3.3 fixed that drift after the frontend hit the unmatched key.
        """
        elapsed = time.perf_counter() - self._started_at if self._started_at else 0
        detector_stats = self._detector.stats if self._detector else {}
        return {
            "session_id": self.config.session_id,
            "is_running": self.is_running,
            "elapsed_s": round(elapsed, 1),
            "frames": self._frame_count,                # public contract: "frames"
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
                # Live mode wants responsiveness — if LM Studio takes >30s
                # something is wrong (CUDA OOM, model swap, etc.). Shorter
                # timeout also bounds how long a Stop click waits for the
                # describer thread to finish its in-flight call.
                timeout=30.0,
            )
            return description.strip(), label
        except LMStudioUnavailable as e:
            logger.warning("scene %d describe failed: %s", scene_index, e)
            return ("", label + ":unavailable")
        except Exception as e:                           # noqa: BLE001
            logger.warning("scene %d describe raised %s", scene_index, type(e).__name__)
            return ("", label + ":error")

    # ------------------------------------------------------------------
    # Audio (v0.4.0)
    # ------------------------------------------------------------------

    def _handle_audio_level(self, rms: float, peak: float) -> None:
        """Called ~10 Hz from the sounddevice audio thread. Emits a level
        event for the UI meter. Both values are normalized [0, 1]."""
        self._emit({
            "type": "audio_level",
            "rms": round(rms, 4),
            "peak": round(peak, 4),
        })

    def _post_transcribe(self) -> None:
        """Run whisper.cpp on the captured audio.wav after Stop, bucket
        the resulting segments per scene, and persist them. Emits
        `transcribing` before and `transcribed` after.

        Failure here is non-fatal: the session still completes, just
        without transcript data. We log + emit an error event for the UI.
        """
        if not whisper_configured():
            logger.info(
                "session=%s transcribe_audio=True but no whisper provider "
                "configured — skipping post-transcribe",
                self.config.session_id,
            )
            self._emit({
                "type": "transcribe_skipped",
                "reason": "no_whisper_provider",
            })
            return

        self._emit({
            "type": "transcribing",
            "audio_duration_s": round(self._audio_capture.duration_s, 1)
                if self._audio_capture else 0.0,
        })

        try:
            result = transcribe_file(
                self._audio_path,
                # Allow up to 5 min processing time. whisper.cpp is roughly
                # real-time on CPU; a 30-min recording could take 5-30 min
                # depending on hardware. If the user wants more they can
                # bump the bundle's CORTEX_VISION_WHISPER_TIMEOUT later.
                timeout=600.0,
            )
        except (WhisperUnavailable, FileNotFoundError) as e:
            logger.warning(
                "session=%s transcription failed: %s",
                self.config.session_id, e,
            )
            self._emit({
                "type": "transcribe_failed",
                "message": str(e),
            })
            return

        # Persist transcript chunks
        started_at_dt = datetime.now(timezone.utc)
        for i, seg in enumerate(result.segments):
            try:
                self.sm.append_transcript(
                    self.config.session_id,
                    TranscriptEntry(
                        timestamp=started_at_dt,
                        text=seg.text,
                        duration_s=max(0.0, seg.end_s - seg.start_s),
                        chunk_index=i,
                    ),
                )
            except Exception:                            # noqa: BLE001
                logger.exception("failed to persist transcript chunk %d", i)

        # Bucket segments per scene by START time. Each scene's window is
        # [scene.start_s, scene.end_s) — for live mode end_s == start_s
        # (point-in-time events), so we extend each window to the next
        # scene's start so segments before the next scene change attribute
        # to the previous scene.
        scenes_in_db = self.sm.get(self.config.session_id)
        if scenes_in_db is None:
            return

        windows: list[tuple[float, float]] = []
        for i, sc in enumerate(scenes_in_db.scenes):
            start = sc.start_s
            end = (
                scenes_in_db.scenes[i + 1].start_s
                if i + 1 < len(scenes_in_db.scenes)
                else (self._audio_capture.duration_s if self._audio_capture else start + 600)
            )
            windows.append((start, end))

        per_scene = bucket_segments_by_scene(result.segments, windows)
        for sc, spoken in zip(scenes_in_db.scenes, per_scene):
            if spoken:
                # Update the scene's spoken_text via partial update
                # (the full re-write would clobber description/keyframes)
                with __import__("sqlite3").connect(self.sm.db_path) as conn:
                    conn.execute(
                        "UPDATE scenes SET spoken_text = ? WHERE session_id = ? AND scene_index = ?",
                        (spoken, self.config.session_id, sc.index),
                    )

        self._emit({
            "type": "transcribed",
            "provider": result.provider,
            "model": result.model,
            "segment_count": len(result.segments),
            "scenes_with_audio": sum(1 for x in per_scene if x),
            "char_count": len(result.full_text),
        })
        logger.info(
            "session=%s transcribed: %d segments, %d/%d scenes got spoken_text",
            self.config.session_id,
            len(result.segments),
            sum(1 for x in per_scene if x),
            len(per_scene),
        )

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
        """Push to the WS queue. Drops oldest if full.

        Injects baseline timing fields (`timestamp_wall`, `elapsed_s`) on
        EVERY event so WS consumers can format them without worrying about
        which event types include them. The caller-supplied event dict
        wins on conflicts so per-event timing (e.g. a scene's actual
        capture time) overrides the emit-time timestamp.
        """
        baseline = {
            "timestamp_wall": time.time(),
            "elapsed_s": (
                round(time.perf_counter() - self._started_at, 1)
                if self._started_at else 0.0
            ),
        }
        # event values win — caller can override timestamps with the actual
        # event time (e.g. scene events that happened earlier in the buffer)
        merged = {**baseline, **event}

        try:
            self._events.put_nowait(merged)
        except queue.Full:
            # Drop oldest, retry once
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(merged)
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
