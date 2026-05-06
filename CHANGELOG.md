# Changelog

All notable changes to cortex-vision will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-05-06

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
