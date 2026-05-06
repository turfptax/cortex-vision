# cortex-vision — Source Map

File-by-file mapping from the prototype repos (VisualFast, VideoIndex) into cortex-vision modules. Use this when actually doing the ports during Phase 1 and Phase 4.

Status legend:
- **PORT** — copy with minimal changes (rename imports, drop dead code)
- **ADAPT** — port with structural changes (split, merge, refactor)
- **NEW** — write from scratch
- **SKIP** — explicitly out of scope

## Capture (`cortex_vision/capture/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `camera.py`              | PORT   | `VisualFast/capture.py`                     | OBS Virtual Camera + webcam + capture card. Drops the FastAPI streaming bits |
| `file.py`                | NEW    | —                                           | ffmpeg-based frame iterator for batch mode. Yields `(frame_index, timestamp_s, np.ndarray)` |
| `ytdlp.py`               | PORT   | `VideoIndex/ai-video-index/lib/downloader.py` | yt-dlp wrapper, cookie support, cascading format selector, VIDEO_EXTS allowlist |
| `screen_recorder.py`     | NEW    | —                                           | ffmpeg-based screen + mic recorder for journal mode |

## Detection (`cortex_vision/detection/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `live_detector.py`       | PORT   | `VisualFast/scene_detector.py`              | 3-method detection (histogram + pixel diff + structural), burst capture, dark-frame filter, steady update |
| `batch_extractor.py`     | PORT   | `VideoIndex/ai-video-index/lib/scene_extractor.py` | PySceneDetect ContentDetector + single-shot fallback for cut-less videos |

## Description (`cortex_vision/description/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `lmstudio_client.py`     | ADAPT  | `cortex-desktop/hub/backend/services/lmstudio.py` + `VideoIndex/ai-video-index/harness/llm.py::chat_with_images()` | Wraps the desktop service, but the underlying `chat_with_images()` lives in cortex-desktop so other features can use it |
| `describer_factory.py`   | ADAPT  | `VisualFast/server.py` (the `start_pipeline` describer setup) | Auto-detect vision vs text models by name. Returns a callable `describe(frames, objects) -> str` |
| `yolo_worker.py`         | PORT   | `VisualFast/models.py::YOLOModel`           | Optional, off by default. Saves ~150 MB if user doesn't enable |
| `openrouter.py`          | PORT   | `VisualFast/models.py::OpenRouterModel`     | Cloud describer for users without local GPU |
| `narrative.py`           | NEW    | —                                           | Single LLM call: feed scene descriptions chronologically, get coherent paragraph(s) |

## Audio (`cortex_vision/audio/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `loopback.py`            | PORT   | `VisualFast/audio.py::AudioCapture`         | WASAPI loopback via sounddevice. Live mode only |
| `ffmpeg_extract.py`      | NEW    | —                                           | `ffmpeg -i input.mp4 -ar 16000 -ac 1 audio.wav`. Used by Mode 2 + 3 |
| `parakeet.py`            | PORT   | `VisualFast/audio.py::AudioWorker` (the Parakeet half) | NVIDIA NeMo Parakeet-TDT 0.6B. Optional via `[asr]` extra |

## Pipeline (`cortex_vision/pipeline/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `live.py`                | PORT   | `VisualFast/pipeline.py`                    | Threaded orchestrator: capture loop, model workers, scene detector callback, audio feed |
| `batch.py`               | ADAPT  | `VideoIndex/ai-video-index/plugins/ingest/plugin.py` | The keyframe + describe loop, **minus** the FAISS/SSCD/DINOv2 fingerprinting calls |
| `session_manager.py`     | NEW    | —                                           | CRUD over the SQLite schema. Used by both live and batch |

## Models (`cortex_vision/models/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `schemas.py`             | NEW    | —                                           | Pydantic models. Already drafted in this scaffold |

## Storage (`cortex_vision/storage/`)

| Target file              | Status | Source                                      | Notes |
|--------------------------|--------|---------------------------------------------|-------|
| `db.py`                  | NEW    | —                                           | SQLite schema + idempotent migrations. Already drafted |
| `artifacts.py`           | NEW    | —                                           | Helpers for `frames/<scene>/<frame>.jpg` paths, atomic write, cleanup |

## Skip — explicitly out of scope for v1

These exist in the source repos but are **not** ported. Mentioned here so we don't accidentally drag them in:

| Source                                                      | Why skipped                                              |
|-------------------------------------------------------------|----------------------------------------------------------|
| `VideoIndex/ai-video-index/lib/fingerprint.py`              | SSCD + DINOv2 + pHash. Cross-video dedup not needed |
| `VideoIndex/ai-video-index/lib/catalog.py` (FAISS half)     | FAISS indices for similarity search. Not needed |
| `VideoIndex/ai-video-index/lib/matcher.py`                  | Match aggregation. Not needed |
| `VideoIndex/ai-video-index/lib/reddit_client.py`            | Reddit-specific |
| `VideoIndex/ai-video-index/plugins/batch_ingest_reddit/*`   | rUFOs DB processing. VideoIndex's own thing |
| `VideoIndex/ai-video-index/plugins/reddit_watch/*`          | PRAW polling. VideoIndex's own thing |
| `VideoIndex/ai-video-index/plugins/match/*`                 | Catalog match queries. Not needed |
| `VideoIndex/ai-video-index/plugins/compare/*`               | Two-video head-to-head. Not needed |
| `VideoIndex/ai-video-index/harness/*`                       | Whole harness CLI/MCP/GUI framework. We use FastAPI in cortex-desktop instead |
| `VisualFast/server.py` (FastAPI parts)                      | We replace with cortex-desktop's `routers/video.py` |
| `VisualFast/static/index.html`                              | We replace with React components in cortex-desktop |
| `VisualFast/main.py`, `cli.py`                              | CLI subsumed by Phase 5's `cortex-vision` script |
| `VisualFast/start.bat`                                      | Windows launcher. cortex-desktop already has its own |

## Order of port operations (Phase 1)

When you actually start coding Phase 1, do them in this order to minimize broken-state time:

1. `models/schemas.py` (already drafted)
2. `storage/db.py` (already drafted) + `storage/artifacts.py`
3. `pipeline/session_manager.py` (CRUD, no pipeline calls yet)
4. `capture/ytdlp.py` (port, runs on its own)
5. `detection/batch_extractor.py` (port, runs on its own)
6. cortex-desktop's `services/lmstudio.py` extension with `chat_with_images()`
7. `description/lmstudio_client.py` + `description/narrative.py`
8. `pipeline/batch.py` — pulls items 4-7 together
9. cortex-desktop's `routers/video.py`
10. cortex-desktop's `components/video/FileMode.tsx`

Each item should be testable in isolation before the next. If item 5 produces garbage scene boundaries on a test video, fix it before item 6 has to consume them.
