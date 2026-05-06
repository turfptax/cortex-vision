"""HTTP sidecar service entry point for cortex-vision.

This is what gets bundled into cortex-vision.exe via PyInstaller. cortex-desktop
spawns this on app startup and proxies /api/video/* requests to it.

Usage:
    python -m cortex_vision serve
    python -m cortex_vision serve --port 8004 --host 127.0.0.1
    cortex-vision serve                          # via pip-installed entry point
    cortex-vision.exe                            # the PyInstaller bundle

The default host is 127.0.0.1 (localhost-only). Set CORTEX_VISION_HOST=0.0.0.0
or pass --host 0.0.0.0 if you want to run the sidecar on a separate machine
(e.g. a GPU desktop) and have cortex-desktop on a thin laptop proxy to it.

Phase 0: returns 501 NotImplemented for the heavy endpoints. Health, version,
and session listing work — that's enough for cortex-desktop to verify the
sidecar is alive and to render an empty session list.
"""
from __future__ import annotations

import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from cortex_vision import __version__
from cortex_vision import config as _cfg
from cortex_vision import logs as _logs
from cortex_vision.capture.ytdlp import VIDEO_EXTS
from cortex_vision.models.schemas import VideoMode, VideoSession
from cortex_vision.pipeline.batch import run_batch_pipeline
from cortex_vision.pipeline.session_manager import SessionManager
from cortex_vision.storage import db as db_module

logger = logging.getLogger("cortex_vision.server")


# ---------------------------------------------------------------------------
# Lifespan: schema migration on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — runs once on startup, once on shutdown."""
    # Install log capture FIRST so we record everything from here on.
    _logs.install()

    db_path = db_module.init_schema()
    logger.info("Sessions DB ready at %s", db_path)
    app.state.db_path = db_path

    # Resilience: clean up sessions that were left in non-terminal states
    # (last process crash, kill, or OS shutdown). Auto-resume isn't attempted
    # — see SessionManager.cleanup_orphaned_sessions for rationale.
    sm = SessionManager(db_path)
    orphans = sm.cleanup_orphaned_sessions()
    if orphans:
        logger.warning(
            "Cleaned up %d orphaned session(s) from previous run: %s",
            len(orphans), ", ".join(orphans[:5]) + ("..." if len(orphans) > 5 else ""),
        )

    # Singleton live-pipeline manager (Phase 4). Created lazily on import to
    # avoid pulling cv2 / numpy into the import graph for users who only do
    # batch work via /api/video/jobs.
    from cortex_vision.pipeline.live import LivePipelineManager
    app.state.live_manager = LivePipelineManager()

    yield

    # Stop any running live session on shutdown
    if hasattr(app.state, "live_manager"):
        try:
            app.state.live_manager.stop()
        except Exception:                                # noqa: BLE001
            logger.exception("live_manager.stop() raised during shutdown")
    logger.info("Shutting down")


app = FastAPI(
    title="cortex-vision",
    version=__version__,
    description=(
        "Video understanding sidecar for the Cortex AI companion ecosystem. "
        "Spawned by cortex-desktop; proxied via /api/video/*."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health / version — used by cortex-desktop's plugin manager
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    db_path: str


@app.get("/api/video/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Readiness probe. cortex-desktop polls this after spawning the sidecar."""
    return HealthResponse(
        status="ok",
        version=__version__,
        db_path=str(app.state.db_path),
    )


@app.get("/api/video/version")
async def version() -> dict:
    """Used by the plugin manager to compare against the latest GitHub release."""
    return {"version": __version__, "package": "cortex-vision"}


# ---------------------------------------------------------------------------
# Sessions — list and fetch
# ---------------------------------------------------------------------------

@app.get("/api/video/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=500),
    mode: VideoMode | None = None,
    status: str | None = Query(None, description="Filter by session status"),
    pushed: bool | None = Query(
        None,
        description="Filter by pushed_to_overseer flag. "
        "Common bridge query: status=complete&pushed=false",
    ),
) -> list[dict]:
    """Most-recent-first list of sessions (without scenes/transcript)."""
    sm = SessionManager(app.state.db_path)
    # Cast status to the Literal type — Pydantic-side validation already
    # ran via FastAPI; accept any string and let SQLite return [] for typos
    sessions = sm.list(
        limit=limit,
        mode=mode,
        status=status,                                 # type: ignore[arg-type]
        pushed=pushed,
    )
    return [s.model_dump(mode="json") for s in sessions]


@app.get("/api/video/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """Fetch one session fully hydrated with its scenes and transcript."""
    sm = SessionManager(app.state.db_path)
    session = sm.get(session_id)
    if not session:
        raise HTTPException(404, f"Session {session_id} not found")
    return session.model_dump(mode="json")


@app.post("/api/video/sessions/{session_id}/mark-pushed", status_code=204)
async def mark_pushed(session_id: str) -> None:
    """Flip pushed_to_overseer flag. Called by cortex-desktop's bridge after
    a successful overseer push."""
    sm = SessionManager(app.state.db_path)
    if not sm.get(session_id):
        raise HTTPException(404, f"Session {session_id} not found")
    sm.mark_pushed_to_overseer(session_id)


# ---------------------------------------------------------------------------
# Jobs — create / get / fetch frames (stubs for Phase 0)
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    source: str                                  # URL or local file path
    mode: Literal["file", "journal"] = "file"
    project_id: str | None = None
    push_to_overseer: bool = False
    transcribe_audio: bool = False
    keyframes_per_scene: int = 1
    describer_model: str | None = None
    narrative_model: str | None = None


@app.post("/api/video/jobs", status_code=202)
async def create_job(
    req: CreateJobRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Create a batch processing job and kick it off in the background.

    Returns immediately with the session_id. Poll GET /api/video/sessions/{id}
    to track progress (status field + progress.current_scene / total_scenes).
    """
    sm = SessionManager(app.state.db_path)

    # Build the source dict in the canonical shape per DATA_MODEL.md
    if req.source.startswith(("http://", "https://")):
        source_dict = {"kind": "url", "url": req.source}
    else:
        source_dict = {"kind": "upload", "file": req.source}

    session = sm.create(
        mode=req.mode,
        source=source_dict,
        project_id=req.project_id,
    )

    # Run the pipeline asynchronously. BackgroundTasks runs after the response
    # is sent, so the client gets the session_id immediately.
    background_tasks.add_task(
        run_batch_pipeline,
        session.id,
        req.keyframes_per_scene,
        req.describer_model,
        req.narrative_model,
        req.transcribe_audio,
    )

    return {
        "session_id": session.id,
        "status": session.status,
        "poll_url": f"/api/video/sessions/{session.id}",
    }


@app.get("/api/video/jobs/{session_id}/frame/{scene_index}/{frame_index}")
async def get_frame(session_id: str, scene_index: int, frame_index: int):
    """Return a raw JPEG keyframe."""
    artifacts = db_module.default_artifacts_dir()
    frame_path = artifacts / session_id / "frames" / str(scene_index) / f"{frame_index}.jpg"
    if not frame_path.exists():
        raise HTTPException(404, "Frame not found")
    return FileResponse(frame_path, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Upload endpoint — for browser-recorded video journals (Phase 3)
# ---------------------------------------------------------------------------

# 2 GB max upload — generous bound for screen recordings. Browser MediaRecorder
# at 1080p / VP9 produces ~5-10 MB per minute; 2 GB ~= 3+ hours.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


@app.post("/api/video/jobs/upload", status_code=202)
async def upload_and_create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mode: Literal["file", "journal"] = Form("journal"),
    project_id: str | None = Form(None),
    push_to_overseer: bool = Form(False),
    transcribe_audio: bool = Form(False),
    keyframes_per_scene: int = Form(1),
    describer_model: str | None = Form(None),
    narrative_model: str | None = Form(None),
) -> dict:
    """Accept a video file upload and kick off a batch processing job.

    Used primarily by the journal mode: cortex-desktop's frontend captures the
    user's screen + mic via browser MediaRecorder, then POSTs the resulting
    blob here. Same pipeline as POST /api/video/jobs, just sourced from an
    upload instead of a URL.

    Returns 202 with the session_id immediately; the pipeline runs in the
    background. Poll GET /api/video/sessions/{id} for status.
    """
    # 1. Validate the upload
    if not file.filename:
        raise HTTPException(400, "filename required")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in VIDEO_EXTS:
        raise HTTPException(
            400,
            f"Unsupported video extension: {suffix!r}. "
            f"Expected one of: {sorted(VIDEO_EXTS)}",
        )

    # 2. Create the session up front so we have an id for the artifact path
    sm = SessionManager(app.state.db_path)
    artifacts_root = db_module.default_artifacts_dir()
    session = sm.create(
        mode=mode,
        source={"kind": "upload", "filename": file.filename},
        project_id=project_id,
    )

    # 3. Stream the upload to disk at the canonical session-source path
    session_dir = artifacts_root / session.id
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / f"source{suffix}"

    bytes_written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1 << 16)        # 64 KB
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"Upload exceeds maximum size ({_MAX_UPLOAD_BYTES} bytes)",
                    )
                out.write(chunk)
    except HTTPException:
        # Clean up partial file + cancel session before propagating
        if dest.exists():
            dest.unlink(missing_ok=True)
        sm.update_status(session.id, "error", error="upload exceeded size limit")
        raise

    # 4. Kick off the pipeline in the background. Pipeline's use_local_file
    #    branch will detect the file is already at session_dir/source.<ext>
    #    (idempotent path) and proceed straight to scene extraction.
    background_tasks.add_task(
        run_batch_pipeline,
        session.id,
        keyframes_per_scene,
        describer_model,
        narrative_model,
        transcribe_audio,
    )

    return {
        "session_id": session.id,
        "status": session.status,
        "bytes_uploaded": bytes_written,
        "poll_url": f"/api/video/sessions/{session.id}",
    }


# ---------------------------------------------------------------------------
# Configuration — read/write %APPDATA%/Cortex/video/config.json
# ---------------------------------------------------------------------------

class ConfigSection(BaseModel):
    """One section of the config (describer / transcribe / live).

    All fields optional so the UI can submit partial updates. Submit
    api_key="***" to preserve the existing key without showing it.
    """
    url: str | None = None
    model: str | None = None
    api_key: str | None = None
    # live-mode fields
    default_resolution: list[int] | None = None
    default_threshold: float | None = None
    default_pixel_diff_threshold: float | None = None
    default_structural_threshold: float | None = None
    default_steady_interval: float | None = None
    default_min_scene_gap: float | None = None


class ConfigUpdate(BaseModel):
    describer: ConfigSection | None = None
    transcribe: ConfigSection | None = None
    live: ConfigSection | None = None


@app.get("/api/video/config")
async def get_config() -> dict:
    """Return the current configuration.

    API keys are redacted: an empty string for "not set", "***" for
    "set but hidden". The UI shows "Configured" / "Not configured" based on
    the redacted value, never sees the actual secret.
    """
    cfg = _cfg.load_config()
    redacted = _cfg.redact(cfg)
    redacted["config_path"] = str(_cfg.config_path())
    return redacted


@app.put("/api/video/config")
async def update_config(updates: ConfigUpdate) -> dict:
    """Update the configuration. Atomically writes config.json.

    For api_key fields: submit "***" to preserve the existing key (the UI
    sends this back when the user didn't touch the masked field), submit a
    new value to replace, submit "" / null to clear.

    The change takes effect immediately — describer/transcribe/live config
    are read on every request, no sidecar restart needed.
    """
    submitted = updates.model_dump(exclude_none=True)
    existing = _cfg.load_config()
    merged = _cfg.merge_for_save(submitted, existing)
    _cfg.save_config(merged)
    out = _cfg.redact(merged)
    out["config_path"] = str(_cfg.config_path())
    return out


@app.post("/api/video/config/test")
async def test_config(updates: ConfigUpdate) -> dict:
    """Test connectivity for the submitted values WITHOUT saving them.

    Useful for the UI's "Test connection" button — user types a URL, clicks
    Test, sees whether it's reachable, then either saves or revises.

    Tests are best-effort: 5s timeout, never raises. Returns a structured
    result the UI can render as a status badge.
    """
    out: dict[str, Any] = {}

    if updates.describer is not None:
        out["describer"] = await _probe_openai_compat(
            url=(updates.describer.url or "").strip(),
            api_key=(updates.describer.api_key or "").strip(),
            label="describer",
        )

    if updates.transcribe is not None:
        out["transcribe"] = await _probe_openai_compat(
            url=(updates.transcribe.url or "").strip(),
            api_key=(updates.transcribe.api_key or "").strip(),
            label="transcribe",
        )

    return out


async def _probe_openai_compat(
    url: str, api_key: str, label: str
) -> dict[str, Any]:
    """Hit GET /models on an OpenAI-compatible server. Returns status + model list."""
    if not url:
        return {"reachable": False, "error": "no URL provided"}

    import httpx
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/models", headers=headers)
    except httpx.RequestError as e:
        return {"reachable": False, "error": f"connection failed: {e}"}

    if r.status_code >= 400:
        return {
            "reachable": False,
            "error": f"HTTP {r.status_code}: {r.text[:200]}",
        }

    try:
        data = r.json()
        items = data.get("data", []) if isinstance(data, dict) else []
        models = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        return {
            "reachable": True,
            "available_models": models[:50],          # cap so OpenAI's full catalog doesn't overflow
            "model_count": len(models),
        }
    except (ValueError, AttributeError) as e:
        return {"reachable": True, "error": f"parsed badly: {e}"}


# ---------------------------------------------------------------------------
# Logs — in-memory ring buffer + debug-level toggle
# ---------------------------------------------------------------------------

_LOG_LEVEL_VALUES = ("debug", "info", "warning", "error", "critical")


@app.get("/api/video/logs")
async def get_logs(
    lines: int = Query(200, ge=1, le=2000),
    level: str | None = Query(
        None,
        description="Minimum level filter — debug | info | warning | error | critical",
    ),
) -> dict:
    """Return recent sidecar log lines from the in-memory ring buffer.

    Used by cortex-desktop's Plugins tab "View Logs" button so the user can
    see what's happening without leaving the Hub. No file paths to memorize.
    """
    if level and level.lower() not in _LOG_LEVEL_VALUES:
        raise HTTPException(
            400, f"level must be one of {list(_LOG_LEVEL_VALUES)}"
        )
    return {
        "lines": _logs.get_recent(lines=lines, level=level),
        "current_level": _logs.current_level(),
        "buffered": _logs.total_buffered(),
    }


class LogLevelUpdate(BaseModel):
    level: Literal["debug", "info", "warning", "error", "critical"]


@app.post("/api/video/logs/level")
async def set_log_level(req: LogLevelUpdate) -> dict:
    """Bump the runtime log level — typically to `debug` when capturing
    diagnostic output for a support ticket. Doesn't persist; the level
    resets to INFO on next bundle restart.
    """
    canonical = _logs.set_level(req.level)
    logger.info("log level changed to %s", canonical)
    return {"level": canonical}


@app.delete("/api/video/logs")
async def clear_logs() -> dict:
    """Empty the ring buffer. Useful before reproducing a bug so the
    captured logs are scoped to just the repro window."""
    before = _logs.total_buffered()
    _logs.clear()
    return {"cleared": before}


# ---------------------------------------------------------------------------
# LM Studio discovery — scan likely candidate URLs
# ---------------------------------------------------------------------------

# Common URLs we probe automatically. Anything else the user provides as
# `hint` parameters gets added on top.
_DEFAULT_LMSTUDIO_CANDIDATES = (
    "http://localhost:1234/v1",
    "http://127.0.0.1:1234/v1",
    "http://localhost:11434/v1",      # Ollama default port
    "http://127.0.0.1:11434/v1",
)


@app.get("/api/video/lmstudio/scan")
async def scan_lmstudio(
    hints: list[str] = Query(
        default=[],
        description="Additional URLs to probe (e.g. cortex-desktop's known LM Studio host). "
                    "Accepts full URL, host:port, or bare host (assumes :1234/v1).",
    ),
    timeout: float = Query(2.0, ge=0.5, le=10.0),
) -> dict:
    """Probe a list of likely LM Studio (or any OpenAI-compatible) servers
    and return the reachable ones with their model lists.

    The cortex-desktop Settings UI uses this to populate a "Discover LM Studio"
    dropdown next to the URL field — user clicks Scan, picks from the list,
    saves. Beats memorizing or copy-pasting URLs.

    Probes default localhost candidates plus any `hints` provided by the
    caller (cortex-desktop knows about its own LM Studio from the Hub's
    network scan; passing that as a hint surfaces it here too).
    """
    candidates: list[str] = list(_DEFAULT_LMSTUDIO_CANDIDATES)
    for h in hints:
        candidates.append(_normalize_lmstudio_url(h))
    # Dedupe while preserving order
    seen: set[str] = set()
    unique = [c for c in candidates if not (c in seen or seen.add(c))]

    import asyncio
    probes = await asyncio.gather(
        *[_probe_one_lmstudio(url, timeout) for url in unique],
        return_exceptions=False,
    )
    return {
        "candidates": probes,
        "reachable_count": sum(1 for p in probes if p["reachable"]),
    }


def _normalize_lmstudio_url(value: str) -> str:
    """Turn 'host', 'host:port', or 'http://host:port' into a full v1 URL."""
    s = value.strip()
    if not s.startswith("http"):
        s = f"http://{s}"
    # If no port given, append :1234
    from urllib.parse import urlparse
    parsed = urlparse(s)
    if not parsed.port:
        s = f"{parsed.scheme}://{parsed.hostname}:1234"
    if "/v1" not in s:
        s = s.rstrip("/") + "/v1"
    return s


async def _probe_one_lmstudio(url: str, timeout: float) -> dict[str, Any]:
    """Single-URL probe. Returns reachability + models on success, error on failure."""
    import httpx

    out: dict[str, Any] = {"url": url, "reachable": False}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{url.rstrip('/')}/models")
        if r.status_code >= 400:
            out["error"] = f"HTTP {r.status_code}"
            return out
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        items = data.get("data", []) if isinstance(data, dict) else []
        models = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        out.update({
            "reachable": True,
            "models": models,
            "model_count": len(models),
            # Try to identify the server type from typical model id patterns
            "likely_server": _guess_server_type(models),
        })
    except httpx.RequestError as e:
        out["error"] = type(e).__name__
    except (ValueError, KeyError) as e:
        out["error"] = f"parse error: {e}"
    return out


def _guess_server_type(models: list[str]) -> str | None:
    """Heuristic guess at server type from the loaded models."""
    if not models:
        return None
    joined = " ".join(models).lower()
    if any(k in joined for k in ("smolvlm", "llava", "qwen", "lmstudio")):
        return "lm-studio"
    if any(k in joined for k in ("llama3.2", "qwen2", "phi3")) and "ollama" in joined:
        return "ollama"
    return "openai-compatible"


# ---------------------------------------------------------------------------
# Live mode — Phase 4
# ---------------------------------------------------------------------------

class LiveStartRequest(BaseModel):
    camera_index: int = 0
    resolution: list[int] = [384, 216]              # [width, height]
    project_id: str | None = None
    threshold: float = 0.85
    pixel_diff_threshold: float = 25.0
    structural_threshold: float = 0.15
    steady_interval: float = 30.0
    min_scene_gap: float = 3.0
    describer_model: str | None = None


@app.get("/api/video/live/cameras")
async def live_cameras() -> dict:
    """List available camera devices (OBS Virtual Camera, webcams, etc.).

    Each entry includes index, native resolution, and native fps so the UI
    can let the user pick before starting a session.
    """
    from cortex_vision.capture.camera import describe_cameras
    return {"cameras": describe_cameras()}


@app.post("/api/video/live/start", status_code=202)
async def live_start(req: LiveStartRequest) -> dict:
    """Start a live capture session.

    Only one live session can run per cortex-vision process. Returns 409 if
    one is already active — call /stop first or query /status.
    """
    from cortex_vision.pipeline.live import LivePipelineConfig

    sm = SessionManager(app.state.db_path)
    session = sm.create(
        mode="live",
        source={
            "kind": "obs_camera",
            "device": f"index:{req.camera_index}",
            "resolution": req.resolution,
        },
        project_id=req.project_id,
    )

    config = LivePipelineConfig(
        session_id=session.id,
        camera_index=req.camera_index,
        resolution=tuple(req.resolution[:2]),       # type: ignore[arg-type]
        threshold=req.threshold,
        pixel_diff_threshold=req.pixel_diff_threshold,
        structural_threshold=req.structural_threshold,
        steady_interval=req.steady_interval,
        min_scene_gap=req.min_scene_gap,
        describer_model=req.describer_model,
    )

    try:
        pipeline = app.state.live_manager.start(config)
    except RuntimeError as e:
        # Either "already running" or "could not open camera"
        sm.update_status(session.id, "error", error=str(e))
        if "already running" in str(e):
            raise HTTPException(409, str(e)) from e
        raise HTTPException(503, str(e)) from e

    return {
        "session_id": session.id,
        "status": "capturing",
        "ws_url": "/api/video/live/ws",
        "stop_url": "/api/video/live/stop",
        "config": {
            "camera_index": pipeline.config.camera_index,
            "resolution": list(pipeline.config.resolution),
        },
    }


@app.post("/api/video/live/stop", status_code=200)
async def live_stop() -> dict:
    """Stop the active live session. 404 if no session is running."""
    final = app.state.live_manager.stop()
    if final is None:
        raise HTTPException(404, "No active live session")
    return {"stopped": True, "final_status": final}


@app.get("/api/video/live/status")
async def live_status() -> dict:
    """Snapshot of the active live session, or {is_running: false} if none."""
    snap = app.state.live_manager.status()
    if snap is None:
        return {"is_running": False}
    return snap


@app.websocket("/api/video/live/ws")
async def live_ws(websocket: WebSocket) -> None:
    """Stream live events to a single connected consumer.

    Multi-subscriber is not supported in v1. If you need fan-out, the
    cortex-desktop proxy can broadcast to multiple browser clients while
    holding a single upstream WS connection here.

    Event protocol: see cortex_vision/pipeline/live.py module docstring.
    """
    import asyncio
    await websocket.accept()

    pipeline = app.state.live_manager.get_active()
    if pipeline is None:
        await websocket.send_json({
            "type": "error",
            "message": "no active live session — start one via POST /api/video/live/start",
        })
        await websocket.close()
        return

    loop = asyncio.get_event_loop()
    try:
        while True:
            # Pull the next event from the pipeline's thread-safe queue.
            # `get_event` blocks up to 0.5s; offload to a thread so we don't
            # block the event loop.
            event = await loop.run_in_executor(None, pipeline.get_event, 0.5)
            if event is None:
                # No event this tick — check if the pipeline is still running
                if not pipeline.is_running:
                    break
                continue
            await websocket.send_json(event)
            if event.get("type") == "stopped":
                break
    except WebSocketDisconnect:
        # Client went away — that's fine; pipeline keeps running
        logger.info("live WS client disconnected")
    except Exception:                                       # noqa: BLE001
        logger.exception("live WS handler crashed")
        try:
            await websocket.close()
        except Exception:                                    # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Plugin manifest — cortex-desktop reads this on install
# ---------------------------------------------------------------------------

@app.get("/api/video/diagnostics")
async def diagnostics() -> dict:
    """Operational snapshot — what's configured, what's reachable, what's broken.

    Used by the Plugins tab to render a "Cortex Vision health" detail view
    and by support tickets ("paste me your /diagnostics output"). Doesn't
    expose any secrets — API keys are reported as a boolean only.
    """
    from cortex_vision.audio.ffmpeg_extract import ffmpeg_available
    from cortex_vision.audio.transcribe import is_configured as whisper_configured
    from cortex_vision.description.lmstudio_client import (
        _api_key,
        _base_url,
        _default_model,
        health_check as llm_health,
    )

    # LLM (vision describer + narrative)
    llm_url = _base_url()
    llm_model = _default_model()
    llm_reachable = llm_health(timeout=2.0)

    # Whisper (audio transcription) — resolved through config now
    transcribe_cfg = _cfg.get_transcribe_config()
    whisper_url = transcribe_cfg.get("url") or ""
    whisper_configured_flag = whisper_configured()
    whisper_provider = (
        "lmstudio_compat" if whisper_url
        else "openai" if os.environ.get("OPENAI_API_KEY")
        else None
    )

    # Session counts by status (one query)
    counts: dict[str, int] = {}
    with db_module.connect(app.state.db_path) as conn:
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM sessions GROUP BY status"
        ):
            counts[row["status"]] = row["n"]

    # Disk usage of session artifacts — sum of file sizes under sessions dir
    artifacts_root = db_module.default_artifacts_dir()
    total_bytes = 0
    session_dir_count = 0
    if artifacts_root.exists():
        for session_subdir in artifacts_root.iterdir():
            if not session_subdir.is_dir():
                continue
            session_dir_count += 1
            for f in session_subdir.rglob("*"):
                if f.is_file():
                    try:
                        total_bytes += f.stat().st_size
                    except OSError:
                        pass

    return {
        "version": __version__,
        "db_path": str(app.state.db_path),
        "artifacts_root": str(artifacts_root),
        "describer": {
            "provider": "lmstudio_compat",
            "url": llm_url,
            "model": llm_model,
            "api_key_configured": bool(_api_key()) and _api_key() != "lm-studio",
            "reachable": llm_reachable,
        },
        "transcribe": {
            "provider": whisper_provider,
            "url": whisper_url or None,
            "configured": whisper_configured_flag,
            "ffmpeg_available": ffmpeg_available(),
        },
        "live": {
            "active_session_id": (
                app.state.live_manager.get_active().config.session_id
                if app.state.live_manager.get_active()
                else None
            ),
        },
        "sessions": {
            "by_status": counts,
            "total": sum(counts.values()),
        },
        "storage": {
            "session_dirs": session_dir_count,
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 1),
        },
    }


@app.get("/api/video/manifest")
async def manifest() -> dict:
    """Self-description. Mirrors the plugin.json bundled in the .exe.

    cortex-desktop's plugin manager reads this to know:
      - what API prefix to proxy
      - what version is running
      - what nav entry to render
    """
    return {
        "id": "cortex-vision",
        "name": "Cortex Vision",
        "version": __version__,
        "api_prefix": "/api/video",
        "default_port": 8004,
        "ui": {
            "page_id": "video",
            "label": "Video",
            "icon": "video",
            "tabs": ["live", "file", "journal", "history"],
        },
        "capabilities": ["batch", "live", "journal"],
        "github_repo": "turfptax/cortex-vision",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DEFAULT_HOST = os.environ.get("CORTEX_VISION_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CORTEX_VISION_PORT", "8004"))


def main() -> None:
    """Run the sidecar service. Called by `python -m cortex_vision serve`."""
    parser = argparse.ArgumentParser(
        prog="cortex-vision-server",
        description="Sidecar HTTP service for cortex-desktop's video plugin",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Bind address (default: {DEFAULT_HOST}; env: CORTEX_VISION_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Bind port (default: {DEFAULT_PORT}; env: CORTEX_VISION_PORT)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("cortex-vision %s starting on http://%s:%d", __version__, args.host, args.port)

    import uvicorn

    uvicorn.run(
        "cortex_vision.server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )


if __name__ == "__main__":
    main()
