# Changelog

All notable changes to cortex-vision will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.5] — 2026-05-06

### Added — local whisper.cpp transcription via cortex-desktop's install

Audio transcription now has a **three-path provider chain**, with the new path being **completely automatic** for users who already have cortex-desktop's overseer set up.

**Provider resolution (highest priority first):**

  1. `CORTEX_VISION_WHISPER_URL` / config-file URL — explicit OpenAI-compatible endpoint
  2. **whisper.cpp at `%APPDATA%\Cortex\whisper-cpp\`** *(NEW)* — auto-detected, uses cortex-desktop's installed binary + model directly via subprocess. Same files the overseer uses for voice journals
  3. `OPENAI_API_KEY` — cloud Whisper API fallback

**Why this matters:** users with cortex-desktop already have whisper.cpp set up for the overseer's voice journal feature. Now their `transcribe_audio: true` jobs use the same install — free local transcription with no extra config. Only users without cortex-desktop or with a custom setup need to configure anything.

### Implementation details

- New `find_whisper_cli()` and `find_whisper_model()` helpers detect cortex-desktop's install at the canonical `%APPDATA%\Cortex\whisper-cpp\whisper-cli.exe` and `%APPDATA%\Cortex\whisper-models\ggml-*.bin` paths. Same paths cortex-desktop's `routers/transcribe.py` treats as authoritative
- New `_LocalWhisperCpp` endpoint type alongside the existing `_HttpEndpoint`. `transcribe_file()` dispatches by type
- `_transcribe_via_whisper_cpp()` invokes `whisper-cli -m model -f wav.wav -oj -of base` via subprocess and parses the JSON output (millisecond offsets, different shape from OpenAI's `verbose_json`)
- Picks the highest-accuracy model when multiple are installed (`large-v3` > `large` > `medium` > ... > `base`)
- Zero coupling to cortex-desktop's API — we just read its binary + model files directly. If cortex-desktop isn't installed or whisper.cpp isn't set up, gracefully falls through to next path

### Diagnostics

`/api/video/diagnostics` now reports the **active** transcription provider with the relevant details:

```jsonc
"transcribe": {
    "configured": true,
    "provider": "whisper_cpp",   // or "lmstudio_compat" or "openai"
    "cli_path": "C:\\...\\whisper-cli.exe",
    "model_path": "C:\\...\\ggml-large-v3.bin",
    "model": "ggml-large-v3.bin",
    "ffmpeg_available": true
}
```

Users can hit this to confirm which path will be used before submitting a job.

### Tests — 15 new (178 total passing)

- Detection helpers: missing files, present files, model preference order, missing APPDATA
- Resolution priority: explicit URL beats whisper.cpp beats OpenAI
- Subprocess invocation: parses whisper.cpp JSON correctly, handles non-zero exit, handles no-output-file
- Diagnostics integration: reports the right provider info
- Backward-compat: existing OpenAI/LM Studio path still works

### Renamed (internal)

- `_Endpoint` → `_HttpEndpoint` to disambiguate from the new `_LocalWhisperCpp` type
- `_parse_response` → `_parse_http_response` (provider-specific now)
- HTTP provider name `"lmstudio"` → `"lmstudio_compat"` for clarity

These are internal symbols — public API (`transcribe_file`, `is_configured`, `bucket_segments_by_scene`, etc.) is unchanged.

## [0.3.4] — 2026-05-06

### Fixed

- **Video Journal `FileNotFoundError`** — upload-mode sessions stored only the browser-side filename in `source.filename`. The pipeline's `Path(filename).resolve()` then landed in the bundle's CWD instead of the session dir where the file was actually saved. Pipeline now looks in `<session_dir>/source.*` for upload sessions, finds the actual saved file, and proceeds.
- **Live mode "Stopping..." stuck forever** — race condition in the WebSocket handler. When `pipeline.is_running` flipped to false, the WS loop exited before draining the `stopped` event from the queue. Frontend never saw the terminal event, UI stuck on "Stopping...". Handler now drains all remaining queued events when the pipeline ends.
- **Live mode describer blocking Stop** — the describer thread used the default 120s httpx timeout for LM Studio calls. If Stop was clicked mid-describe, the thread join blocked for up to 120s before the `stopped` event could fire. Reduced live-mode describer timeout to 30s — bounds Stop responsiveness without compromising the typical 5-10s SmolVLM response window.

### Added

- **`GET /api/video/sessions/{id}/export.html`** — render a session as a self-contained shareable HTML report. Base64-embedded thumbnails (no external image refs), narrative + per-scene descriptions + transcript if any. Single file, drop into Slack/email/anywhere.
- 5 new tests covering upload path resolution, missing-source error path, HTML export self-containment, missing-keyframe placeholder, no-scenes export

## [0.3.3] — 2026-05-06

### Fixed — `stats.frames` field rename to match the documented contract

- `stats` event field renamed from `frame_count` to `frames` so it matches both the docstring at the top of `cortex_vision/pipeline/live.py` (always said `frames`) and what the cortex-desktop `LiveMode.tsx` actually reads on line 540: `stats.frames.toLocaleString()`. Implementation had drifted; the frontend correctly followed the documented contract; the resulting `undefined.toLocaleString()` was the actual cause of the LiveMode crash that v0.3.2 only partially fixed.
- Added a contract test (`test_stats_event_field_names_match_frontend_contract`) pinning the four field names LiveMode reads (`fps`, `frames`, `scene_count`, `elapsed_s`) so this can never silently drift again. Future renames require a coordinated frontend ship.

## [0.3.2] — 2026-05-06

### Fixed — uniform timestamp fields on every WebSocket event

- Live mode's `described`, `stopped`, and `error` events were missing the `timestamp_wall` and `elapsed_s` fields that `scene` and `stats` events carried. cortex-desktop's `LiveMode.tsx` formats event timestamps via `.toLocaleString()` and crashed with `Cannot read properties of undefined` whenever a `described` event arrived, taking the whole LiveMode component down (and closing the WS subscription with it). User-visible symptom: GUI blanks shortly after Start
- `LivePipeline._emit()` now injects `timestamp_wall` and `elapsed_s` baseline fields onto every event before queueing. Caller-supplied timing wins on conflicts so per-event timestamps (e.g. scene events that captured an earlier moment) override the emit-time clock
- New regression test asserts every event from a complete live session carries both fields with numeric values

### Note for cortex-desktop frontend

While this backend change closes the immediate crash, defense in depth is worth adding on your side too:

1. Optional chaining on every `.toLocaleString()` / `.toFixed()` call rendering event fields (`event.elapsed_s?.toLocaleString() ?? "—"`)
2. A React error boundary around `LiveMode` so a single render error doesn't unmount the component (and disconnect the WS) — show "Live view rendering failed; click to retry" instead

## [0.3.1] — 2026-05-06

### Fixed — fatal SEH crash during camera enumeration

- `describe_cameras()` no longer opens `cv2.VideoCapture` on each device index to read its resolution. That probe path SEH-crashed the entire bundle on Windows when a virtual camera (DroidCam offline, OBS Virtual Camera mid-init) was in an odd state — silent process death with no Python traceback
- New path uses `pygrabber`'s `FilterGraph.get_input_devices()`, which calls Windows' `ICreateDevEnum` directly to LIST devices by name without instantiating any of them. Safe regardless of camera state
- Returns `{index, name}` (e.g. `"OBS Virtual Camera"`) instead of `{index, native_resolution, native_fps}`. The cortex-desktop `pickDefaultCamera()` heuristic that checks `/obs/i` against the name field now actually works
- Falls back to the legacy cv2 probe on non-Windows or when pygrabber unavailable
- Bundle includes pygrabber + comtypes (Windows-only deps via `sys_platform == "win32"`)
- 6 new tests covering pygrabber path, fallback path, runtime errors, and endpoint integration

## [0.3.0] — 2026-05-06

### Added — in-app debug + LM Studio discovery

- `cortex_vision/logs.py` — bounded ring buffer (2000 lines) attached to root logger; same pattern cortex-desktop's Hub uses
- `GET /api/video/logs` — recent log lines with optional level filter (debug/info/warning/error/critical). Used by the Plugins tab "View Logs" button
- `POST /api/video/logs/level` — runtime debug toggle. Bump to DEBUG, reproduce, bump back. No restart needed
- `DELETE /api/video/logs` — clear the ring buffer (handy before a fresh repro)
- `GET /api/video/lmstudio/scan?hints=...` — probe likely OpenAI-compatible servers (localhost:1234, 127.0.0.1:1234, Ollama defaults, plus user-supplied hints) and report reachability + model lists. The cortex-desktop Configure form uses this for a "Discover LM Studio" dropdown
- 20 new tests covering ring buffer bounds, level filtering, level toggle, URL normalization, scan dedup, server-type heuristic

### Changed

- Server lifespan now installs the log capture handler before initializing the DB so all startup logs are captured

### Added — persistent configuration for end-user installs

- `cortex_vision/config.py` — read/write JSON config at `%APPDATA%/Cortex/video/config.json`
- `GET /api/video/config` — current config with API keys redacted as `***`
- `PUT /api/video/config` — atomic writes (tmp + rename), `***` preserves existing key
- `POST /api/video/config/test` — try connecting with proposed values without saving (5s probe of OpenAI-compatible `/models`)
- Resolution order: config file > env vars > defaults. UI is authoritative once a value is set; env vars stay as a power-user override
- 20 new tests covering load/save round-trip, atomic write rollback on failure, env-var fallback, redaction semantics, partial-section PUTs, redacted-key preservation

### Changed

- `lmstudio_client.py` and `audio/transcribe.py` now resolve URL/model/key via config first, env vars second
- `/api/video/diagnostics` reports `lmstudio_compat` consistently for both describer and transcribe providers
- `transcribe_audio: True` no longer requires `setx` — UI users can configure Whisper via the config form

### Removed nothing — backward compatible

- All existing env vars (`CORTEX_VISION_LLM_URL`, etc.) still work as before; they're just now overridden by config file values when those exist

## [0.1.0] — Phase 0–6 backend complete

- Initial design and scaffolding (Phase 0)
- Pydantic schemas: `VideoSession`, `SceneEntry`, `TranscriptEntry`
- SQLite schema with idempotent migrations
- FastAPI sidecar service (`cortex_vision/server.py`) — health, version, manifest, sessions endpoints
- `POST /api/video/jobs` — batch pipeline for URL / file (Phase 1)
- `POST /api/video/jobs/upload` — multipart upload for journal mode (Phase 3)
- Live mode: `POST /api/video/live/start`, `WS /api/video/live/ws` — OBS Virtual Camera + 3-method scene detection (Phase 4)
- Audio transcription: ffmpeg extract + Whisper provider chain (Phase 6)
- Filter params on `/sessions` for bridge polling
- Diagnostics endpoint, orphan session cleanup
- PyInstaller bundle producing `cortex-vision.exe` (Phase 5)
- 106 tests passing

### Phase plan

See [docs/ROADMAP.md](docs/ROADMAP.md) for the phased build plan.

---

## Conventions

Each release tag (`vX.Y.Z`) triggers a CI build that produces:
- `cortex-vision-X.Y.Z-windows-cpu.zip` (~500 MB)
- `cortex-vision-X.Y.Z-windows-gpu.zip` (~2.5 GB)

Both attached to the GitHub release with SHA256 checksums in the release notes.

cortex-desktop's plugin manager checks `https://api.github.com/repos/turfptax/cortex-vision/releases/latest` for updates.
