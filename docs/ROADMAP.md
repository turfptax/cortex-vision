# cortex-vision — Roadmap

> **Status as of 2026-05-06:** v0.4.0 shipped. Phases 0-6 backend complete. End-to-end validated on real hardware. The remaining work is frontend polish on the cortex-desktop side and a handful of stretch items.

The architecture is a **sidecar service** ([DISTRIBUTION.md](DISTRIBUTION.md)) — cortex-vision is its own `.exe`, cortex-desktop proxies to it.

---

## Phase 0 — Scaffolding ✅ DONE (v0.1.0)

Repo installable, sidecar runs, design locked in.

- ✅ Repo initialized + pushed to `turfptax/cortex-vision`
- ✅ `pyproject.toml` with extras (cpu/gpu/asr/cloud/dev/build)
- ✅ Pydantic schemas: `VideoSession`, `SceneEntry`, `TranscriptEntry`
- ✅ SQLite schema with idempotent migrations
- ✅ FastAPI sidecar (`cortex_vision/server.py`) with health/version/manifest
- ✅ `python -m cortex_vision serve` entry point
- ✅ `plugin.json` + PyInstaller spec
- ✅ `SessionManager` CRUD with status state machine
- ✅ Tests: schemas, DB, session manager

**On the cortex-desktop side (their work):**
- ✅ `services/plugin_manager.py` — sidecar lifecycle
- ✅ `routers/video.py` — HTTP proxy
- ✅ `routers/plugins.py` — admin API
- ✅ `components/settings/PluginsTab.tsx` — install/restart/uninstall UI
- ✅ Page gating: 'video' nav item hidden when plugin not running
- ✅ `POST /api/plugins/dev-register` — agent-friendly dev registration

---

## Phase 1 — File / URL batch ✅ DONE (v0.1.0)

Batch pipeline against URLs (yt-dlp) or local files.

- ✅ Port `VideoIndex/lib/downloader.py` → `cortex_vision/capture/ytdlp.py`
- ✅ Port `VideoIndex/lib/scene_extractor.py` → `cortex_vision/detection/batch_extractor.py` (with single-shot fallback)
- ✅ `cortex_vision/pipeline/batch.py` — full orchestrator
- ✅ `cortex_vision/description/lmstudio_client.py` — `chat_with_images()` against any OpenAI-compatible vision endpoint
- ✅ `cortex_vision/description/narrative.py` — LLM rollup with deterministic fallback
- ✅ `POST /api/video/jobs` wired to spawn the pipeline as a BackgroundTask
- ✅ `GET /api/video/sessions/{id}` returns hydrated `VideoSession`

**On the cortex-desktop side:**
- ✅ `FileMode.tsx` — paste URL → poll → render
- ✅ `SessionList.tsx` — history browser
- ✅ `lib/videoApi.ts` — typed proxy client
- ✅ `useVideoJob.ts` — polling hook with exponential backoff

---

## Phase 2 — Overseer bridge ✅ MOSTLY DONE

Completed sessions enrich the overseer's memory graph.

- ✅ Filter params on `/sessions` (`?status=`, `?pushed=`, `?mode=`)
- ✅ `POST /sessions/{id}/mark-pushed` endpoint
- ✅ cortex-desktop bridge polls every 30s and pushes via `pi_client`
- ⏳ Optional: per-FileMode "Push to overseer" toggle (currently push-all)
- ⏳ Optional: project_id picker

---

## Phase 3 — Video Journal mode ✅ DONE (v0.3.4)

Browser-recorded screen + mic, processed via batch pipeline.

- ✅ `POST /api/video/jobs/upload` — multipart upload with size cap, atomic write
- ✅ `use_local_file()` made idempotent for files already in session_dir
- ✅ Pipeline detects `kind=upload` and resolves to `<session_dir>/source.<ext>`

**On the cortex-desktop side:**
- ✅ `JournalMode.tsx` — getDisplayMedia + getUserMedia + MediaRecorder + upload
- ✅ Bridge attaches journal sessions to today's overseer journal entry

---

## Phase 4 — Live OBS mode ✅ DONE (v0.3.0 → v0.4.0)

Real-time screen capture with scene detection + describer + audio.

### Video capture (v0.3.0 - v0.3.3)

- ✅ Port `VisualFast/capture.py` → `cortex_vision/capture/camera.py`
- ✅ Port `VisualFast/scene_detector.py` → `cortex_vision/detection/live_detector.py` (3-method, burst capture, dark-frame filter, single-offset edge case)
- ✅ Port `VisualFast/pipeline.py` → `cortex_vision/pipeline/live.py` (4-thread orchestrator)
- ✅ HTTP endpoints: `POST /live/start`, `/stop`, `GET /status`, `/cameras`
- ✅ WebSocket endpoint: `WS /live/ws`
- ✅ Non-invasive camera enumeration via pygrabber (avoids cv2 SEH crashes)
- ✅ Uniform timestamp_wall + elapsed_s on every WS event (frontend rendering safety)
- ✅ `stats.frames` field name matches frontend contract

### Bug fixes (v0.3.4)

- ✅ Live "Stopping..." stuck forever — WS handler drains queued events including the terminal `stopped`
- ✅ Live describer timeout reduced to 30s so Stop responds promptly
- ✅ Journal upload `FileNotFoundError` — pipeline reads canonical session_dir path

### Audio capture (v0.4.0)

- ✅ `cortex_vision/audio/loopback.py` — WASAPI loopback (desktop audio) + mic input
- ✅ Per-session `audio_source` config (None / "desktop" / int / device-name substring)
- ✅ Continuous recording to `<session_dir>/audio.wav` at 16 kHz mono
- ✅ Live `audio_level` WS events at ~10 Hz for the meter
- ✅ Post-stop transcription via existing whisper.cpp chain
- ✅ Per-scene `spoken_text` populated via segment bucketing
- ✅ `GET /api/video/live/audio-devices` for the picker
- ✅ Sounddevice + portaudio bundled in PyInstaller spec

**On the cortex-desktop side (TODO for next dev release):**
- ⏳ Audio source dropdown in LiveMode picker
- ⏳ "Transcribe audio" checkbox in LiveMode
- ⏳ Audio level meter (vertical/horizontal bar) updating from `audio_level` events
- ⏳ "Transcribing..." status during post-process

---

## Phase 5 — PyInstaller bundle + GitHub release ✅ DONE (v0.2.0)

Real installable `.exe` distributed via GitHub releases.

- ✅ `cortex-vision.spec` with all hidden imports + collect_all for cv2, scenedetect, yt-dlp extractors, pygrabber, comtypes, sounddevice
- ✅ CI workflow `.github/workflows/release.yml` — builds + smoke-tests on tag push
- ✅ Smoke test polls bundle on 127.0.0.1 (IPv4 — `localhost` resolves IPv6 first on Windows runners)
- ✅ SHA256 checksum computed + attached to release
- ✅ First release `v0.2.0` published, then v0.3.0/0.3.1/0.3.2/0.3.3/0.3.4/0.3.5/0.4.0
- ✅ cortex-desktop's plugin manager Install button works against real GitHub releases
- ✅ Update flow: download → SHA256 verify → swap → restart → rollback on failure
- ✅ Uninstall flow

---

## Phase 6 — Audio transcription ✅ DONE (v0.3.5 + v0.4.0)

Optional ffmpeg + Whisper transcription wired into all three modes.

- ✅ `cortex_vision/audio/ffmpeg_extract.py` — extract audio track from any video file
- ✅ `cortex_vision/audio/transcribe.py` — three-path Whisper provider chain
  - Explicit `CORTEX_VISION_WHISPER_URL` (LM Studio Whisper, custom server)
  - cortex-desktop's bundled whisper.cpp (auto-detected)
  - OpenAI cloud Whisper API
- ✅ whisper.cpp detection (v0.3.5 → v0.4.0): added cortex-desktop install paths (`<install>\_internal\backend\bin\`) — the actual location whisper-cli ships in
- ✅ Per-scene `spoken_text` via `bucket_segments_by_scene()`
- ✅ `transcribe_audio` flag on all three modes
- ✅ Live mode post-stop transcription (v0.4.0)
- ✅ Diagnostics endpoint reports active provider with paths

---

## Polish (queued, not yet shipped)

These are nice-to-haves that don't gate the v1 demo experience.

### cortex-vision side

- [ ] Pygrabber UTF-16 decode bug (`EÆgato` → `Elgato` — cosmetic)
- [ ] `meta.json` sidecar per session (portability — currently SQLite is the only metadata source)
- [ ] WebSocket-based live transcription (alternative to post-stop) — only if there's demand
- [ ] Browser file upload via drag-drop in FileMode (currently URL-only)
- [ ] CLI mode for headless processing: `cortex-vision process <url> --out report.html`
- [ ] Auto-cleanup setting: delete sessions older than N days

### cortex-desktop side

- [ ] **Configure UI** — form against `GET/PUT /api/video/config` + `POST /config/test` for the describer URL/model/key + transcribe URL/model + LM Studio "Discover" button using `/api/video/lmstudio/scan`
- [ ] **View Logs panel** — read-only log tail using `GET /api/video/logs`, with debug-mode toggle via `POST /api/video/logs/level`
- [ ] **LiveMode audio controls** — see Phase 4 TODO list above
- [ ] **React error boundary on LiveMode** — would have prevented v0.3.0/0.3.1/0.3.2 cascading-crash bugs
- [ ] **Auto-restart-on-crash** for plugin manager — currently a dead sidecar stays dead

---

## Stretch (out of scope for v1)

These are tracked here so we don't accidentally let them creep into earlier phases:

- Cross-video deduplication (use VideoIndex's FAISS layer if needed)
- Multi-camera live mode (e.g. webcam + screen simultaneously)
- VLM fine-tuning pipeline (capture → annotate → train) — could connect to `cortex-pet-training`
- Real-time face anonymization for shared screen recording
- MCP tool surface in `cortex-mcp` so Claude Code can summarize videos
- Code signing for the `.exe` (defer until stable user base)
- Linux / macOS bundles
- Multi-user support (sessions table needs `user_id`)
- Whisper model picker UI (currently auto-picks largest available)

---

## Phase ordering rationale (kept for retrospective value)

The actual shipping order matched the design:

1. Phase 0 (scaffolding) before everything else — primitives + state machine
2. Mode 2 (batch) before Mode 1 (live) because batch is debuggable; live is timing-dependent
3. Mode 3 (journal) reuses Mode 2's orchestrator — cheap once batch works
4. Mode 1 (live) layered video first, then audio (Phase 4 + Phase 6)
5. Phase 5 (PyInstaller) before polish so real-world install bugs surface early
6. Phase 6 (transcription) after Phase 5 so the bundled `.exe` was already validated

The bug-finding pattern was instructive: each release surfaced exactly one bug class, in sequence — cv2 SEH crash, missing event fields, field-name mismatch — each masked by the previous one. Bugs in front-of-the-frontend cascade until the frontend has an error boundary; we documented this for the team.
