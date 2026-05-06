# cortex-vision

[![Release](https://img.shields.io/github/v/release/turfptax/cortex-vision)](https://github.com/turfptax/cortex-vision/releases/latest)
[![CI](https://github.com/turfptax/cortex-vision/actions/workflows/ci.yml/badge.svg)](https://github.com/turfptax/cortex-vision/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Real-time and batch video understanding pipeline for the [Cortex](https://github.com/turfptax) AI companion ecosystem. Plugs into [`cortex-desktop`](https://github.com/turfptax/cortex-desktop) as a sidecar service plugin and feeds the overseer's interpretive memory graph with visual + audio context.

**Status: shipping. v0.4.0 published. 196 tests passing. End-to-end working on Windows.**

## What it does

Three modes, one pipeline:

| Mode | Source | What you get |
|---|---|---|
| **Process video** | URL (yt-dlp) or local file | Scenes detected → keyframes captured → vision LLM describes each → narrative paragraph + (optional) audio transcript |
| **Video journal** | Browser screen + mic recording | Same pipeline against the recorded clip |
| **Live (OBS)** | OBS Virtual Camera or webcam + (optional) desktop audio or mic | Real-time scene detection → live thumbnails + descriptions over WebSocket → post-stop transcription |

All three produce a queryable `VideoSession` with scene keyframes, per-scene descriptions, optional spoken-text per scene, and a narrative rollup. Sessions can be pushed to the Cortex overseer to enrich daily/weekly project rollups with screen + audio context.

## Architecture

cortex-vision runs as **its own PyInstaller-bundled `.exe`** on `localhost:8004` (a plugin sidecar). cortex-desktop's plugin manager spawns it on app startup and proxies `/api/video/*` through to it.

```
+-------------------------+              +-----------------------------+
|  cortex-desktop.exe     |   HTTP/WS    |  cortex-vision.exe          |
|  port 8003              +------------->+  port 8004                  |
|  routers/video.py       |              |  cortex_vision/server.py    |
|     proxies to ->       |              |  full pipeline + threads    |
|  services/                              +-----------------------------+
|    plugin_manager.py    +---------- spawns ---------+
+-------------------------+
```

This separates concerns: vision deps (~85 MB bundle) don't bloat cortex-desktop, vision iterates fast, cortex-desktop stays stable. Same pattern cortex-desktop already uses for cortex-core on the Pi.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full architecture rationale and [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) for the install/update mechanism.

## Install

Cortex Hub (the desktop app) installs cortex-vision automatically once the plugin is added — Settings → Plugins → Install Cortex Vision pulls the latest GitHub release, verifies SHA256, extracts to `%APPDATA%\Cortex\plugins\cortex-vision\`, and spawns the bundled `.exe`. No terminal required.

For development:

```powershell
git clone https://github.com/turfptax/cortex-vision
cd cortex-vision
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev,build]

# Run the sidecar service
python -m cortex_vision serve
# Then verify:
curl http://127.0.0.1:8004/api/video/health
```

For the `.exe` bundle:

```powershell
.\build.ps1 -Test
# Output: dist\cortex-vision\cortex-vision.exe (~85 MB)
```

## Configuration

Out of the box: cortex-vision auto-detects what it needs.

- **Vision describer**: defaults to LM Studio at `http://localhost:1234/v1` — set the URL via the config UI in cortex-desktop, or via `PUT /api/video/config`, or via `setx CORTEX_VISION_LLM_URL`
- **Audio transcription**: auto-detects cortex-desktop's bundled whisper.cpp at `<install>\_internal\backend\bin\whisper-cli.exe` and the model at `%APPDATA%\Cortex\whisper-models\`. No config needed if the overseer's voice journal feature has been used once
- **Camera + audio devices**: enumerated non-invasively via Windows DirectShow + WASAPI; the picker shows real device names, not indices

Resolution order for every config value: per-request override > config file (`%APPDATA%\Cortex\video\config.json`) > env var > built-in default.

## API surface

All endpoints are at `/api/video/*` on port 8004 (or proxied through cortex-desktop on port 8003).

### Sessions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sessions` | List sessions (with optional `?status=`, `?mode=`, `?pushed=` filters) |
| `GET` | `/sessions/{id}` | Hydrated session with scenes + transcript |
| `POST` | `/sessions/{id}/mark-pushed` | Used by the cortex-desktop overseer bridge |
| `GET` | `/sessions/{id}/export.html` | Self-contained HTML report with embedded thumbnails |
| `GET` | `/jobs/{id}/frame/{scene}/{frame}` | Raw JPEG keyframe |

### Process video / Journal

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Create batch job from URL or local file path |
| `POST` | `/jobs/upload` | Multipart upload (used by Journal mode's browser MediaRecorder) |

### Live mode

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/live/cameras` | Available video capture devices (with names) |
| `GET` | `/live/audio-devices` | Available audio capture sources |
| `POST` | `/live/start` | Start capture (accepts camera, audio, threshold config) |
| `POST` | `/live/stop` | Stop + post-process transcription |
| `GET` | `/live/status` | Current session snapshot |
| `WS` | `/live/ws` | Real-time event stream (scenes, descriptions, audio levels, transcripts) |

### Configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/config` | Current config (API keys redacted as `***`) |
| `PUT` | `/config` | Update config atomically |
| `POST` | `/config/test` | Test connectivity for proposed values without saving |
| `GET` | `/lmstudio/scan` | Discover OpenAI-compatible servers on the LAN |

### Operational

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Sidecar liveness probe |
| `GET` | `/version` | Version + package name |
| `GET` | `/manifest` | Plugin manifest (matches `plugin.json`) |
| `GET` | `/diagnostics` | Snapshot of providers, sessions, storage |
| `GET` | `/logs` | Recent log lines from the in-memory ring buffer |
| `POST` | `/logs/level` | Bump log level for diagnosis (`debug` / `info` / etc.) |

## Documentation

| Document | Purpose |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture, sidecar rationale, mode behaviors |
| [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) | PyInstaller spec, GitHub release flow, install/update mechanism |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased build plan and current status |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Narrative of what's been built and key decisions |
| [docs/CORTEX_DESKTOP_INTEGRATION.md](docs/CORTEX_DESKTOP_INTEGRATION.md) | Current contract for the cortex-desktop frontend |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Pydantic schemas + SQLite layout |
| [docs/SOURCES.md](docs/SOURCES.md) | File-by-file port map from VisualFast / VideoIndex |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | Resolved decisions + remaining defaults |
| [HANDOFF.md](HANDOFF.md) | Original Phase 0 brief for the cortex-desktop team |
| [CHANGELOG.md](CHANGELOG.md) | Per-release detail |

## License

MIT, matching the rest of the Cortex ecosystem. Free to install, run, and modify.

## Acknowledgments

Code patterns ported from sister projects:

- [`VisualFast`](https://github.com/turfptax/visualfast-prototype) — real-time live-stream prototype (OBS capture, 3-method scene detection, threaded model workers, WASAPI audio)
- [`VideoIndex`](https://github.com/turfptax/videoindex-prototype) — batch-ingest prototype (yt-dlp downloader, PySceneDetect with single-shot fallback)

See [`docs/SOURCES.md`](docs/SOURCES.md) for the file-by-file port mapping.
