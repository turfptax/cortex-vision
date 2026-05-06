# cortex-vision — Open Questions

> **Status as of 2026-05-06 (post v0.4.0):** All 7 original questions resolved. 4 strategic items remain (not blockers). This document is preserved as a record of how the design decisions were made, not as an active decision queue.

Decisions that needed to be made before / during early phases.

---

## RESOLVED

### 1. Repo visibility — RESOLVED: public

Public on GitHub at `turfptax/cortex-vision`. Matches the rest of the Cortex ecosystem.

### 2. License model — RESOLVED: open source, no premium tier

cortex-vision is MIT-licensed and free to install / run / modify, matching the rest of Cortex. No license server, no entitlement check, no paid tier. The plugin manager pattern in [DISTRIBUTION.md](DISTRIBUTION.md) supports gating in the future if a different plugin needs it.

### 3. GPU expectation — RESOLVED: CPU-only bundle, no local model loading

**As shipped (different from original plan):** v0.4.0 ships a single CPU bundle (~85 MB) and **does not load any local vision/ASR models**. All vision describer calls go through LM Studio (OpenAI-compatible HTTP); all transcription goes through cortex-desktop's bundled whisper.cpp (CPU-only, real-time on most machines).

This turned out simpler than the originally-planned dual CPU/GPU bundles with lazy model downloads. We never needed local SmolVLM/DINOv2/Parakeet because LM Studio + whisper.cpp cover the same use cases. Could revisit if a future feature truly needs in-process model loading.

### 4. Distribution mechanism — RESOLVED: sidecar service + plugin manager

cortex-vision is its own PyInstaller .exe running on `localhost:8004`. cortex-desktop proxies. cortex-desktop's plugin manager (new component) handles install/update/uninstall via GitHub releases. See [DISTRIBUTION.md](DISTRIBUTION.md).

---

## STANDING

### 5. Audio transcription provider — RESOLVED: three-path chain with whisper.cpp auto-detection

**Shipped in Phase 6 + v0.4.0.** Audio transcription resolves in this priority:

1. Explicit URL config (env var `CORTEX_VISION_WHISPER_URL` or via `PUT /api/video/config`)
2. cortex-desktop's bundled `whisper-cli.exe` — auto-detected at the canonical install paths (`<ProgramFiles*>\CortexHub\_internal\backend\bin\`)
3. `OPENAI_API_KEY` for cloud Whisper

The whisper.cpp auto-detection is the dominant path in practice — users who install Cortex Hub get free local transcription with zero cortex-vision config. We just read the binary cortex-desktop ships with. Zero coupling between sidecar and host's HTTP API; only shared file conventions.

**Local Parakeet-TDT** (the original `[asr]` extra) is permanently deferred — whisper.cpp via cortex-desktop covers the local-CPU use case at comparable latency.

### 6. Storage location — default: APPDATA

**Default:** `%APPDATA%/Cortex/video/` on Windows, `~/.local/share/cortex/video/` on Unix. Consistent with cortex-desktop. Not user-configurable in v1; settings option added in Phase 6 if requested.

Disk budget for a typical user: ~160 MB / hour of live capture. Auto-cleanup setting added in Phase 6 ("Delete sessions older than N days").

### 7. Live capture — RESOLVED: OBS-only via DirectShow enumeration

**As shipped:** OBS Virtual Camera (and other DirectShow devices like webcams, capture cards, DroidCam, Meta Quest cameras) are enumerated non-invasively via pygrabber. No native `mss` fallback in v0.4.0 — turned out users who want live screen capture already run OBS, and OBS Virtual Camera works cleanly for everything we need.

The picker shows real device names (e.g. "OBS Virtual Camera", "Pixel 10a (Windows Virtual Camera)") instead of numeric indices — the cortex-desktop frontend's `pickDefaultCamera()` heuristic correctly auto-selects OBS by name match.

### 8. Journal mode entry point — RESOLVED: separate Video Journal tab

**As shipped:** Video Journal is its own tab in cortex-desktop's Video page, separate from the overseer's text-based journal. The post-process pipeline runs and the cortex-desktop bridge attaches the resulting scenes/transcript to today's overseer journal entry on completion. Best of both worlds — dedicated video journal UX plus integration with the existing journal model.

---

## STRATEGIC (not v1 blockers)

### A. MCP surface

Should `cortex-vision` expose itself via MCP so Claude Code can call "process this video" as a tool? Easy to add via cortex-mcp once the FastAPI router exists. Not v1.

### B. Multi-user

cortex-desktop is single-user. cortex-vision inherits that. If multi-user becomes a thing, sessions table needs a `user_id` column — easy via the idempotent migration pattern.

### C. Encryption at rest

Screen recordings and audio are sensitive. Should `%APPDATA%/Cortex/video/` be encrypted? Default no (Windows BitLocker handles it for most users). Settings toggle in Phase 6: "Encrypt session artifacts."

### D. Sharing / export

Does a session need a "share" or "export" button? E.g. dump narrative + thumbnails as a markdown report. Easy in Phase 6.

### E. Bundle code signing

Current plan: skip for v1, accept Windows SmartScreen warning ("Run anyway"). Revisit when there's an installed user base — code signing is ~$300/year and only meaningfully helps after the first ~100 downloads anyway.

### F. Plugin SDK

If cortex-vision ends up being plugin #1 of many, do we extract a `cortex-plugin-sdk` package with the manifest schema, lifespan handler patterns, and proxy helpers? Probably yes after plugin #2 ships, not before. Avoid premature abstraction.
