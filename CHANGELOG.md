# Changelog

All notable changes to cortex-vision will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] — 2026-05-07

### Fixed — three bugs surfaced by real-world testing on a second machine

#### whisper-cli native crash on GPU init (the cascade root cause)

whisper.cpp's default builds attempt CUDA initialization via ggml's GPU backend selector. On machines with mismatched CUDA toolkits, no compatible GPU, or hybrid Optimus-style setups, the GPU init crashes the subprocess natively with `STATUS_FATAL_USER_CALLBACK_EXCEPTION` (exit code `0xC000041D` / `3221225501`) — no graceful failure, no traceback in stderr.

Fix: pass `-ng` (no-gpu) to whisper-cli unconditionally. CPU mode is real-time on modern cores, accuracy is identical, and we avoid every flavor of CUDA-mismatch crash. Whether the machine has a working GPU or not, we get the same fast path.

This single bug had cascading effects: while `subprocess.run(whisper-cli)` blocked the asyncio event loop for ~37s, the bundle stopped responding to `/health` checks, the team's plugin manager (with `restart_on_crash: true`) thought the bundle was dead and force-restarted it, which triggered orphan-session cleanup and marked the still-running session as `error`. Fixing the GPU crash collapses the whole cascade.

Regression test pinned in `tests/test_whisper_cpp.py::test_transcribe_via_whisper_cpp_passes_no_gpu_flag`.

#### `AudioCapture` `paInvalidChannelCount` (-9998)

Different audio hardware reports `max_output_channels` wildly — 8 for 7.1 surround, 16 for some pro audio cards, 6 for 5.1, 2 for stereo. The reported value isn't always a valid channel count for the device's current share-mode mix format. v0.4.0 passed it directly to `InputStream`, which raised `paInvalidChannelCount` on devices where the share-mode format only accepts a different count.

Fix: `AudioCapture.open()` now tries a small list of channel counts in order — native first, then `2`, then `1` — and uses the first that opens successfully. The resampler is constructed AFTER the actual count is known.

#### `live_stop` no longer blocks on whisper

`POST /api/video/live/stop` previously waited for the entire post-process (whisper transcription + segment bucketing + persistence) before returning, sometimes 30-60 seconds for long recordings. While blocked, the bundle couldn't respond to other requests including `/health` — exactly the watchdog-trigger condition above.

Fix: the endpoint now returns 200 immediately after capturing the session snapshot, with the actual `stop()` call running in a `BackgroundTask`. Frontend should treat the 200 as "stop signal accepted, cleanup in progress" and watch the WebSocket for `transcribing` / `transcribed` / `stopped` terminal events.

### Tests — 197 passing (1 new regression test)

- `test_transcribe_via_whisper_cpp_passes_no_gpu_flag` — pins `-ng` in the whisper invocation

## [0.4.0] — 2026-05-06

### Added — desktop audio capture in live mode + post-stop transcription

Live mode now captures audio alongside video. User picks at session start: **Desktop audio** (WASAPI loopback on the default Windows output — captures the system mix exactly as you hear it, no VB-Audio CABLE needed) or a specific **microphone**. Audio is recorded to `<session_dir>/audio.wav` continuously; on Stop, whisper.cpp transcribes the full recording and buckets segments per scene.

#### New module: `cortex_vision/audio/loopback.py`

- `AudioCapture` — sounddevice-backed continuous WAV writer. 16 kHz mono output. Internal linear resampler handles native-rate (e.g. 48 kHz stereo) → 16 kHz mono on the fly. Periodic RMS callback at ~10 Hz for the live audio level meter.
- `list_input_devices()` — returns the default WASAPI output (as a loopback target with sentinel `index=-1`) plus all real input devices. Powers the audio source dropdown.
- `_resolve_device()` — translates user-facing spec (None / "desktop" / int / name substring) into sounddevice index + loopback flag.

#### Live pipeline integration (`cortex_vision/pipeline/live.py`)

- New `LivePipelineConfig.audio_source` (None / "desktop" / int / str) and `transcribe_audio: bool` fields
- Audio capture spawned alongside video capture in `start()`, finalized in `stop()`
- Audio-thread RMS callbacks emit `{"type": "audio_level", "rms": ..., "peak": ...}` events for the UI meter
- After all threads stop, if `transcribe_audio=True`, runs whisper.cpp on the recorded WAV via the existing v0.3.5 provider chain
- Transcript segments persisted to the session and bucketed per scene via `bucket_segments_by_scene()` — each scene's `spoken_text` field gets the audio that played during its time window

#### New WebSocket events

```jsonc
{"type": "audio_level", "rms": 0.045, "peak": 0.12}              // ~10 Hz
{"type": "transcribing", "audio_duration_s": 202.0}              // after Stop, whisper running
{"type": "transcribed", "provider": "whisper_cpp",
                        "segment_count": 47,
                        "scenes_with_audio": 12}                  // when post-process done
{"type": "transcribe_skipped", "reason": "no_whisper_provider"}  // graceful skip
{"type": "transcribe_failed", "message": "..."}                  // post-process failure
```

`stopped` event also gains `audio_recorded: bool` and `audio_duration_s: float`.

#### New endpoint

- `GET /api/video/live/audio-devices` — lists capture sources for the picker

#### `POST /api/video/live/start` accepts new fields

- `audio_source: int | str | null` (default null = video-only)
- `transcribe_audio: bool` (default false)

### Fixed — whisper-cli detection covers cortex-desktop's bundled install

v0.3.5 only checked `%APPDATA%\Cortex\whisper-cpp\whisper-cli.exe` for the binary, but cortex-desktop's official installer bundles whisper-cli inside its own PyInstaller install at `<install>\_internal\backend\bin\whisper-cli.exe`. `find_whisper_cli()` now searches a 4-tier order:

1. `CORTEX_VISION_WHISPER_CLI` env var (explicit override)
2. cortex-desktop install paths:
   - `%ProgramFiles(x86)%\CortexHub\_internal\backend\bin\whisper-cli.exe`
   - `%ProgramFiles%\CortexHub\_internal\backend\bin\whisper-cli.exe`
   - `%LOCALAPPDATA%\Programs\CortexHub\_internal\backend\bin\whisper-cli.exe`
3. `%APPDATA%\Cortex\whisper-cpp\whisper-cli.exe` (manual install fallback)
4. `shutil.which("whisper-cli")` (PATH lookup)

This was the actual reason "Transcribe audio" silently did nothing in v0.3.5 even when cortex-desktop's overseer had the model installed.

### Bundling

- `sounddevice>=0.4.6` moved from `[cpu]/[gpu]` extras to **core deps** so the PyInstaller bundle picks it up unconditionally
- Spec updated to collect_all `sounddevice` + `_sounddevice_data` (portaudio DLL ships inside the wheel)
- `_cffi_backend` added as explicit hidden import (sounddevice loads via cffi)

### Tests — 33 new (196 total passing)

- Resampler: passthrough, 48k→16k downsample, stereo→mono downmix, empty input
- Device resolution: None → loopback, int → input, string match → input or loopback by category
- list_input_devices: includes desktop loopback sentinel + real inputs, graceful empty when sounddevice missing
- AudioCapture lifecycle: writes valid 16 kHz mono WAV, idempotent close, open-failure cleanup
- find_whisper_cli: env override, ProgramFiles, ProgramFiles(x86), missing APPDATA path
- Audio devices endpoint integration

### What this changes for the user

Before v0.4.0: Live mode was video-only; "Transcribe audio" worked for File/Journal modes but only if whisper.cpp was at the manual-install path.

After v0.4.0: Live mode can record system audio (or a mic), and transcription auto-detects cortex-desktop's whisper.cpp install. End-to-end live mode with overseer-quality transcripts works without any CLI configuration.

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
