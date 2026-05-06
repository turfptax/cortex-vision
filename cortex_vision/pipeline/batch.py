"""Batch pipeline orchestrator — files and URLs end-to-end.

Glues together the four major stages:

    1. Capture     — download URL via yt-dlp OR symlink local file
    2. Segment     — PySceneDetect + single-shot fallback
    3. Describe    — vision LLM per scene
    4. Narrate     — text LLM rolls scene descriptions into narrative

Each stage updates the session's status and progress in SQLite so the
FastAPI layer can stream live progress to cortex-desktop.

Failure handling:
    - LLM unreachable for per-scene description -> scene gets empty description
      but pipeline continues (we still got keyframes + boundaries)
    - LLM unreachable for narrative rollup -> falls back to deterministic concat
    - Single scene fails -> recorded with empty description, continue
    - Pipeline-level fatal (download fails, video unreadable) -> session marked
      "error" with the exception text, not propagated up

Usage from FastAPI:

    @app.post("/api/video/jobs")
    async def create_job(req):
        session = SessionManager().create(mode="file", source={"url": req.source})
        background_tasks.add_task(run_batch_pipeline, session.id)
        return {"session_id": session.id, "status": "queued"}
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from datetime import datetime, timezone

from cortex_vision.audio.ffmpeg_extract import (
    FfmpegError,
    extract_audio_track,
    ffmpeg_available,
)
from cortex_vision.audio.transcribe import (
    WhisperUnavailable,
    bucket_segments_by_scene,
    is_configured as whisper_configured,
    transcribe_file,
)
from cortex_vision.capture.ytdlp import download_to_session, use_local_file
from cortex_vision.description.lmstudio_client import (
    LMStudioUnavailable,
    chat_with_images,
)
from cortex_vision.description.narrative import (
    SCENE_DESCRIBER_SYSTEM,
    build_scene_describer_prompt,
    fallback_rollup,
    roll_up,
)
from cortex_vision.detection.batch_extractor import (
    ExtractedScene,
    extract_scenes,
)
from cortex_vision.models.schemas import (
    SceneEntry,
    TranscriptEntry,
    VideoSession,
)
from cortex_vision.pipeline.session_manager import SessionManager
from cortex_vision.storage import db as db_module

logger = logging.getLogger("cortex_vision.pipeline.batch")


# Brightness range outside which we skip the describer call. Very dark or
# fully white frames waste a vision model call (transitions, intermissions).
_BRIGHTNESS_MIN = 15.0
_BRIGHTNESS_MAX = 245.0

# How many keyframes we capture per scene for the describer. 1 = midpoint.
# Phase 6 setting: "Keyframes per scene" (1-3) in the cortex-desktop UI.
_DEFAULT_KEYFRAMES_PER_SCENE = 1


# Optional progress callback — lets the FastAPI layer or tests observe each
# stage transition without us needing to bolt eventing onto SessionManager.
ProgressCallback = Callable[[str, dict], None]


def run_batch_pipeline(
    session_id: str,
    keyframes_per_scene: int = _DEFAULT_KEYFRAMES_PER_SCENE,
    describer_model: str | None = None,
    narrative_model: str | None = None,
    transcribe_audio: bool = False,
    on_progress: ProgressCallback | None = None,
) -> VideoSession:
    """Run the full batch pipeline against an existing session.

    The session must have been created (status=queued) by the caller — typically
    via SessionManager.create() in the API handler. This function picks it up
    and drives it to status=complete or status=error.

    Returns the final hydrated session. Does NOT raise on pipeline errors —
    those get recorded in the session's `error` field.
    """
    sm = SessionManager()
    session = sm.get(session_id)
    if not session:
        raise KeyError(f"Session {session_id} not found")

    artifacts_root = db_module.default_artifacts_dir()
    session_dir = artifacts_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = session_dir / "frames"

    def _emit(stage: str, payload: dict | None = None) -> None:
        if on_progress:
            try:
                on_progress(stage, payload or {})
            except Exception:                       # noqa: BLE001
                logger.exception("on_progress callback raised; ignoring")

    try:
        # ------------------------------------------------------------------
        # 1. CAPTURE
        # ------------------------------------------------------------------
        sm.update_status(session_id, "capturing")
        _emit("capturing", {"session_id": session_id})

        source = session.source
        if source.get("url") or source.get("kind") == "url":
            url = source.get("url") or source["url"]
            cookies_from_browser = source.get("cookies_from_browser", "")
            cookies_file = source.get("cookies_file", "")
            meta = download_to_session(
                url,
                session_dir=session_dir,
                cookies_from_browser=cookies_from_browser,
                cookies_file=cookies_file,
            )
        elif source.get("file") or source.get("kind") == "upload":
            file_path = source.get("file") or source.get("filename")
            meta = use_local_file(file_path, session_dir=session_dir)
        else:
            raise ValueError(
                f"Unsupported source shape: {source!r}. "
                f"Expected {{url: ...}} or {{file: ...}}."
            )

        logger.info(
            "session=%s captured: title=%r duration=%.1fs platform=%s",
            session_id, meta.get("title"), meta.get("duration_s", 0), meta.get("platform"),
        )

        # ------------------------------------------------------------------
        # 2. SEGMENT
        # ------------------------------------------------------------------
        sm.update_status(session_id, "processing")
        _emit("processing", {"session_id": session_id})

        extracted = extract_scenes(
            video_path=meta["file_path"],
            frames_dir=frames_dir,
            keyframes_per_scene=keyframes_per_scene,
        )

        if not extracted:
            sm.update_status(
                session_id, "error",
                error="No usable scenes extracted (video may be empty or corrupt)",
            )
            return sm.get(session_id)  # type: ignore[return-value]

        total = len(extracted)
        sm.update_progress(session_id, {"total_scenes": total, "current_scene": 0})
        _emit("scenes_extracted", {"total": total, "trigger": extracted[0].trigger_method})

        # ------------------------------------------------------------------
        # 2.5. AUDIO TRANSCRIPTION (optional)
        # ------------------------------------------------------------------
        # Per-scene spoken_text is filled in here so the describer pass below
        # can include it as context. Failure is non-fatal: the pipeline
        # continues to describe + narrate without audio if anything fails.
        spoken_per_scene: list[str] = ["" for _ in extracted]
        if transcribe_audio:
            spoken_per_scene = _maybe_transcribe(
                session_id=session_id,
                video_path=meta["file_path"],
                session_dir=session_dir,
                extracted=extracted,
                sm=sm,
                emit=_emit,
            )

        # ------------------------------------------------------------------
        # 3. DESCRIBE
        # ------------------------------------------------------------------
        sm.update_status(session_id, "describing")
        _emit("describing", {"total": total})

        described_scenes: list[SceneEntry] = []
        used_model = describer_model or os.environ.get("CORTEX_VISION_LLM_MODEL", "")
        describer_label = f"lmstudio:{used_model}" if used_model else "lmstudio:default"

        for i, scene in enumerate(extracted):
            description, model_used = _describe_scene(
                scene, model=describer_model, fallback_label=describer_label
            )
            entry = _scene_to_entry(scene, description=description, model=model_used)
            entry.spoken_text = spoken_per_scene[i] or None
            sm.append_scene(session_id, entry)
            described_scenes.append(entry)
            sm.update_progress(
                session_id,
                {
                    "total_scenes": total,
                    "current_scene": i + 1,
                    "last_description": description[:120],
                },
            )
            _emit("scene_described", {"index": i, "total": total, "description": description})

        # ------------------------------------------------------------------
        # 4. NARRATE
        # ------------------------------------------------------------------
        sm.update_status(session_id, "narrating")
        _emit("narrating", {"scene_count": len(described_scenes)})

        descriptions = [s.description for s in described_scenes]
        try:
            narrative = roll_up(
                scene_descriptions=descriptions,
                title=meta.get("title"),
                duration_s=meta.get("duration_s"),
                model=narrative_model,
            )
        except LMStudioUnavailable as e:
            logger.warning(
                "session=%s narrative LLM unavailable, using fallback concat: %s",
                session_id, e,
            )
            narrative = fallback_rollup(descriptions)

        sm.set_narrative(session_id, narrative)
        _emit("narrative_complete", {"length": len(narrative)})

        # ------------------------------------------------------------------
        # 5. DONE
        # ------------------------------------------------------------------
        sm.update_status(session_id, "complete")
        _emit("complete", {"session_id": session_id})

        final = sm.get(session_id)
        assert final is not None
        return final

    except Exception as e:                          # noqa: BLE001
        logger.exception("session=%s pipeline failed", session_id)
        try:
            sm.update_status(session_id, "error", error=f"{type(e).__name__}: {e}")
        except Exception:                           # noqa: BLE001
            logger.exception("session=%s also failed to write error status", session_id)
        result = sm.get(session_id)
        if result is None:
            raise
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _describe_scene(
    scene: ExtractedScene,
    model: str | None,
    fallback_label: str,
) -> tuple[str, str]:
    """Run the vision describer on one scene's keyframes.

    Returns (description, describer_model_label). Returns empty description
    if the scene is too dark/bright to bother describing, or if the LLM
    server is unreachable.
    """
    # Skip darks/transitions — these waste describer calls
    if not (_BRIGHTNESS_MIN < scene.brightness < _BRIGHTNESS_MAX):
        return ("", fallback_label + ":skipped-brightness")

    if not scene.keyframe_paths:
        return ("", fallback_label + ":no-keyframes")

    user_text = build_scene_describer_prompt(
        scene_index=scene.index,
        duration_s=scene.duration_s,
        keyframe_count=len(scene.keyframe_paths),
    )

    try:
        description = chat_with_images(
            text=user_text,
            image_paths=scene.keyframe_paths,
            system=SCENE_DESCRIBER_SYSTEM,
            model=model,
            max_tokens=400,
            temperature=0.2,
        )
    except LMStudioUnavailable as e:
        logger.warning("scene %d describe failed: %s", scene.index, e)
        return ("", fallback_label + ":unavailable")
    except Exception as e:                           # noqa: BLE001
        logger.warning("scene %d describe raised %s: %s", scene.index, type(e).__name__, e)
        return ("", fallback_label + ":error")

    return (description.strip(), fallback_label)


def _scene_to_entry(
    scene: ExtractedScene,
    description: str,
    model: str,
) -> SceneEntry:
    """Convert an ExtractedScene (capture-layer struct) to a SceneEntry
    (persistence-layer schema)."""
    return SceneEntry(
        index=scene.index,
        start_s=scene.start_s,
        end_s=scene.end_s,
        keyframe_paths=list(scene.keyframe_paths),
        description=description,
        describer_model=model,
        trigger_method=scene.trigger_method,
        similarity=1.0,
    )


def _maybe_transcribe(
    session_id: str,
    video_path: str,
    session_dir,
    extracted: list[ExtractedScene],
    sm: SessionManager,
    emit,
) -> list[str]:
    """Run audio extraction + transcription. Returns one spoken_text string
    per scene (parallel to `extracted`). Empty strings if anything fails.

    Side effects:
      - writes session_dir/audio.wav
      - persists every transcript segment to the session via append_transcript
    """
    spoken_per_scene = ["" for _ in extracted]

    # Pre-flight: skip without raising if ffmpeg is missing or no Whisper
    if not ffmpeg_available():
        logger.info(
            "session=%s transcribe_audio=True but ffmpeg not on PATH — skipping",
            session_id,
        )
        emit("transcribe_skipped", {"reason": "ffmpeg_missing"})
        return spoken_per_scene
    if not whisper_configured():
        logger.info(
            "session=%s transcribe_audio=True but no Whisper provider configured — skipping",
            session_id,
        )
        emit("transcribe_skipped", {"reason": "no_whisper_provider"})
        return spoken_per_scene

    # Extract the audio track (skip silently if extraction fails)
    audio_path = session_dir / "audio.wav"
    try:
        extract_audio_track(video_path, audio_path)
    except (FfmpegError, FileNotFoundError) as e:
        logger.warning(
            "session=%s audio extraction failed (%s) — skipping transcription",
            session_id, e,
        )
        emit("transcribe_skipped", {"reason": f"extract_failed: {e}"})
        return spoken_per_scene

    emit("audio_extracted", {"path": str(audio_path)})

    # Transcribe — failure here is also non-fatal
    try:
        result = transcribe_file(audio_path)
    except (WhisperUnavailable, FileNotFoundError) as e:
        logger.warning(
            "session=%s transcription failed (%s) — continuing without spoken_text",
            session_id, e,
        )
        emit("transcribe_skipped", {"reason": f"transcribe_failed: {e}"})
        return spoken_per_scene

    emit(
        "audio_transcribed",
        {
            "provider": result.provider,
            "model": result.model,
            "segment_count": len(result.segments),
            "char_count": len(result.full_text),
        },
    )

    # Persist transcript segments to the session
    started_at = datetime.now(timezone.utc)
    for i, seg in enumerate(result.segments):
        try:
            sm.append_transcript(
                session_id,
                TranscriptEntry(
                    timestamp=started_at,
                    text=seg.text,
                    duration_s=max(0.0, seg.end_s - seg.start_s),
                    chunk_index=i,
                ),
            )
        except Exception:                                # noqa: BLE001
            logger.exception("session=%s could not persist transcript chunk %d", session_id, i)

    # Bucket segments per scene by start time
    scene_windows = [(s.start_s, s.end_s) for s in extracted]
    spoken_per_scene = bucket_segments_by_scene(result.segments, scene_windows)
    return spoken_per_scene
