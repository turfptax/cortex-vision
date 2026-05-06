# cortex-vision — Roadmap

Six phases. Each ends with something working end-to-end. Estimated ~1 focused evening per phase except where noted. Order matters — each phase depends on the previous.

The architecture is **sidecar service** ([DISTRIBUTION.md](DISTRIBUTION.md)) — cortex-vision is its own .exe, cortex-desktop proxies to it. Phase 5 is the build/release work that turns the running Python sidecar into a distributed `.exe`.

---

## Phase 0 — scaffolding (½ evening)

**Goal:** repo is installable, sidecar runs, design is locked in.

### cortex-vision side

- [ ] Resolve [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) (most already have defaults)
- [ ] Initialize git, push to GitHub as `turfptax/cortex-vision`
- [x] `pyproject.toml` finalized with cpu/gpu/asr/cloud/dev/build extras
- [x] `cortex_vision/models/schemas.py` — Pydantic models
- [x] `cortex_vision/storage/db.py` — SQLite schema + idempotent migrations
- [x] `cortex_vision/server.py` — FastAPI sidecar with health, version, manifest endpoints
- [x] `cortex_vision/__main__.py` — `python -m cortex_vision serve` entry point
- [x] `plugin.json` — bundled manifest
- [x] `cortex-vision.spec` — PyInstaller spec stub
- [ ] `cortex_vision/pipeline/session_manager.py` — fill in CRUD methods (currently raise NotImplementedError)
- [x] Unit tests for schemas + DB — 8 tests passing

### cortex-desktop side

- [ ] Add `services/plugin_manager.py` — spawn/health/lifecycle of plugin sidecars; reads/writes `%APPDATA%/Cortex/plugins/registry.json`
- [ ] Add `routers/video.py` — HTTP proxy to `localhost:8004` (no business logic)
- [ ] Add `components/settings/PluginsTab.tsx` — list installed plugins with status dots, install/update/uninstall buttons (Phase 0 just lists, Phase 5 wires actual install)
- [ ] Wire 'video' into `App.tsx` Page union (initially gated on plugin registry presence)

**Done when:**
- `python -m cortex_vision serve` starts the sidecar; `curl http://127.0.0.1:8004/api/video/health` returns `{"status":"ok",...}`
- cortex-desktop's `routers/video.py` proxies and `GET /api/video/sessions` returns `[]`
- Plugins tab in cortex-desktop Settings shows "Cortex Vision: running" with a green dot

---

## Phase 1 — Mode 2 (file / URL) batch pipeline (1 evening)

**Goal:** smallest path that proves the vision loop. Pure batch, no live, no audio.

### cortex-vision (DONE — backend shipped)

- [x] Port `VideoIndex/lib/downloader.py` → `cortex_vision/capture/ytdlp.py`
- [x] Port `VideoIndex/lib/scene_extractor.py` → `cortex_vision/detection/batch_extractor.py` (keeps single-shot fallback)
- [x] Build `cortex_vision/pipeline/batch.py` — full orchestrator, no FAISS/SSCD/DINOv2
- [x] Build `cortex_vision/description/lmstudio_client.py` — `chat_with_images()` against any OpenAI-compatible vision endpoint
- [x] Build `cortex_vision/description/narrative.py` — LLM rollup with deterministic fallback
- [x] Fill in `cortex_vision/pipeline/session_manager.py` — full CRUD + status state machine
- [x] Wire `POST /api/video/jobs` in `server.py` to spawn the pipeline as a BackgroundTask
- [x] `GET /api/video/sessions/{id}` returns hydrated VideoSession with scenes + transcript
- [x] 44 tests passing (5 pipeline + 14 SessionManager + 7 narrative + 7 ytdlp + 8 schema/db + 3 db)

### cortex-desktop (TODO — frontend pending)

- [ ] `components/video/FileMode.tsx` — paste URL → POST /api/video/jobs → poll session → render
- [ ] `components/video/SceneTimeline.tsx` — shared scene grid (reused later by JournalMode + LiveMode)
- [ ] `components/video/NarrativePanel.tsx` — narrative display
- [ ] `lib/videoApi.ts` — typed proxy client matching the locked endpoint shapes

**Done when:** paste a TikTok or YouTube URL into the Hub UI, get back N scenes with descriptions and a narrative paragraph, all stored in the SQLite session DB inside the sidecar's `%APPDATA%/Cortex/video/`.

---

## Phase 2 — overseer push for batch (½ evening)

**Goal:** completed sessions enrich the overseer.

- [ ] `cortex-desktop/hub/backend/services/video_overseer_bridge.py` — `push_session_to_overseer(session_id)` (lives in cortex-desktop because it needs `pi_client`)
- [ ] cortex-vision emits a `POST /api/overseer-push-request` event back to cortex-desktop on session completion (or cortex-desktop polls and detects new completed sessions)
- [ ] "Push to overseer" toggle in `FileMode.tsx`
- [ ] Project dropdown (read existing projects from Pi via `pi_client.list_projects()`)

**Done when:** processing a video creates overseer notes that show up in the overseer page, tagged with the chosen project.

---

## Phase 3 — Mode 3 (video journal) (1 evening)

**Goal:** record screen + mic, run batch pipeline, attach to overseer journal.

**Strategy decision:** journal mode uses **client-side recording** via the browser's `getDisplayMedia()` + `getUserMedia()` APIs. The recorded blob uploads to cortex-vision via `POST /api/video/jobs/upload` and the existing Phase 1 batch pipeline runs against it. No server-side ffmpeg in Phase 3 — that's reserved for Phase 4's continuous live capture from OBS.

### cortex-vision (DONE — backend shipped)

- [x] `POST /api/video/jobs/upload` — multipart upload endpoint with size cap, extension allowlist, streaming write
- [x] `use_local_file()` made idempotent for files already in session_dir (so the upload writes directly + pipeline picks up without re-copying)
- [x] `python-multipart` added to core deps for FastAPI UploadFile support
- [x] 8 new tests covering upload happy path, validation, mode/project_id threading, idempotent local file (52 tests total now passing)

### cortex-vision (DONE in Phase 6 — audio shipped)

- [x] `cortex_vision/audio/ffmpeg_extract.py` — extract audio track to 16 kHz mono WAV via ffmpeg subprocess; graceful fallback if ffmpeg missing
- [x] `cortex_vision/audio/transcribe.py` — Whisper provider chain (CORTEX_VISION_WHISPER_URL → OPENAI_API_KEY), per-scene segment bucketing
- [x] `transcribe_audio=True` flag wired through `POST /api/video/jobs` and `/upload` to `run_batch_pipeline()`
- [x] Per-scene `spoken_text` populated when transcription succeeds; full transcript persisted to session
- [x] 20+ new tests covering ffmpeg subprocess wiring, provider resolution, response parsing, per-scene bucketing, batch pipeline integration with audio enabled / ffmpeg missing

### cortex-vision (still deferred)

- [ ] `cortex_vision/capture/screen_recorder.py` — server-side ffmpeg recording (only if a future use case needs it; journal mode uses browser MediaRecorder)
- [ ] `cortex_vision/capture/file.py` — frame iterator (only if Phase 4 live mode wants ffmpeg-driven capture instead of OpenCV)

### cortex-desktop (TODO — frontend pending)

- [ ] `components/video/JournalMode.tsx` — getDisplayMedia + getUserMedia + MediaRecorder + upload to `POST /api/video/jobs/upload`
- [ ] Bridge mode: when `mode=journal`, attach scenes to today's overseer journal entry rather than creating a new session
- [ ] "Record" button with permission flow + timer + stop → upload progress

**Done when:** record a 60s journal of yourself working, get scenes pushed to today's overseer journal, viewable in the overseer page.

---

## Phase 4 — Mode 1 (live OBS) (2 evenings)

**Goal:** continuous screen watching, real-time overseer enrichment. The big prize, but only worth it after batch is proven.

### cortex-vision (DONE — backend shipped)

- [x] Port `VisualFast/capture.py` → `cortex_vision/capture/camera.py` (OBS Virtual Camera + capture card + native webcam, with `find_cameras()` and `describe_cameras()`)
- [x] Port `VisualFast/scene_detector.py` → `cortex_vision/detection/live_detector.py` (3-method, burst capture, dark-frame filter, callback-based events, single-offset edge case fixed)
- [x] Build `cortex_vision/pipeline/live.py` — 4-thread orchestrator (capture / detector / describer worker / stats emitter), `LivePipelineManager` singleton
- [x] HTTP endpoints: `POST /api/video/live/start`, `/stop`, `GET /status`, `GET /cameras`
- [x] WebSocket endpoint: `WS /api/video/live/ws` (single subscriber)
- [x] 20 new tests (11 detector + 9 pipeline + endpoints) — 72 tests passing total

### cortex-vision (deferred to Phase 6)

- [ ] Port `VisualFast/audio.py` → `cortex_vision/audio/loopback.py` + `cortex_vision/audio/parakeet.py`
- [ ] Sidecar resilience: rehydrate live session state from SQLite on respawn (currently a crashed sidecar leaves the session in `capturing` state until manually transitioned)

### cortex-desktop (TODO)

- [ ] **WebSocket pass-through in `routers/video.py`** — bridge `WS /api/video/live/ws` between browser and sidecar (see `INTEGRATION.md`)
- [ ] `components/video/LiveMode.tsx` — start button, camera picker, live thumbnails, scene count + FPS, stop button
- [ ] Tray menu items in `cortex_desktop/tray.py`: "Start watching screen / Stop / Open Hub"
- [ ] 5-min rollup loop in `services/video_overseer_bridge.py` (Phase 2 work — push session segments to overseer via `pi_client`)

**Done when:** click "Start watching" in tray, work for 10 minutes, see overseer's daily rollup include visual context tagged to the active project.

---

## Phase 5 — PyInstaller bundle + GitHub release (1 evening)

**Goal:** real installable .exe.

- [ ] Refine `cortex-vision.spec` with all hidden imports, bundle test passes
- [ ] CI workflow `.github/workflows/build.yml` — builds CPU + GPU bundles on tag push
- [ ] SHA256 checksum step in CI; published in release notes
- [ ] First release `v0.1.0` published with `cortex-vision-0.1.0-windows-cpu.zip` + `cortex-vision-0.1.0-windows-gpu.zip`
- [ ] cortex-desktop `services/plugin_manager.py` install flow works against real GitHub releases (download → SHA256 verify → extract → spawn → health check)
- [ ] Update flow works (download new → graceful shutdown → swap → restart → rollback on failure)
- [ ] Uninstall flow works
- [ ] Manual smoke test on a clean Windows machine without dev environment

**Done when:** a fresh cortex-desktop install on a fresh Windows VM can install cortex-vision via the Plugins tab and process a video end-to-end.

---

## Phase 6 — polish (1 evening)

**Goal:** ship-ready UX.

- [ ] Settings page additions: describer model picker, narrative model picker, audio device picker, frame skip, scene detection thresholds
- [ ] Describer hot-swap (lifted from `VisualFast/server.py`) — switch models mid-session
- [ ] Resume past session: if a live session crashes mid-day, recover state from SQLite on next launch
- [ ] CLI mode for headless batch processing: `cortex-vision process <url> --out report.md`
- [ ] Auto-cleanup setting: delete sessions older than N days
- [ ] Remote sidecar UI: "Run vision plugin: Local | Remote (host:port)" in settings
- [ ] README screenshots, install instructions, video walk-through

**Done when:** v1.0 release-ready.

---

## Stretch (out of scope for v1)

These are tracked here so we don't accidentally let them creep into earlier phases:

- Cross-video deduplication (use VideoIndex's FAISS layer if needed)
- Multi-camera live mode (e.g. webcam + screen simultaneously)
- VLM fine-tuning pipeline (capture → annotate → train) — could connect to `cortex-pet-training`
- Real-time face anonymization for shared screen recording
- Browser extension for in-tab video processing
- MCP tool surface in `cortex-mcp` so Claude Code can summarize videos
- Code signing for the .exe (defer until stable user base)
- Linux / macOS bundles
- Multi-user support (sessions table needs `user_id`)

---

## Phase ordering rationale

Mode 2 (batch) before Mode 1 (live) is intentional:

1. **Batch is debuggable.** A bug in the describer or narrative pass shows up the same way every time. Live mode's bugs are timing-dependent — much harder to chase before you've validated the components individually.
2. **Batch validates the data model.** If `SceneEntry` doesn't capture what we need from a 30-minute YouTube video, it sure won't capture what we need from 8 hours of live screen capture.
3. **Batch is the journal pipeline.** Mode 3 reuses Mode 2's orchestrator. Building Mode 1 first means writing pipeline code twice.
4. **Live mode has a higher floor.** Audio loopback, threaded orchestration, WebSocket streaming, tray integration — all interleaved. Worth saving for after the basics work.

PyInstaller bundling (Phase 5) before polish (Phase 6) is intentional:

1. **Real-world testing requires a real bundle.** Bugs in PyInstaller, lazy weight downloads, and Windows-specific quirks only show up in the bundled .exe.
2. **The plugin manager UX needs the bundle.** Until Phase 5, the Plugins tab is testing against a dev `python -m cortex_vision serve`. Phase 5 lets us test the full install/update/uninstall flow.
3. **Polish work depends on bundle feedback.** Settings UI choices (e.g. "auto-cleanup" thresholds) should be informed by how big the bundle and weights actually get on real installs.
