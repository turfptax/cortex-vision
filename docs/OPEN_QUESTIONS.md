# cortex-vision — Open Questions

Decisions that need to be made before / during early phases. Most have been answered as of the architecture lock-in; the rest have recommended defaults.

---

## RESOLVED

### 1. Repo visibility — RESOLVED: public

Public on GitHub at `turfptax/cortex-vision`. Matches the rest of the Cortex ecosystem.

### 2. License model — RESOLVED: open source, no premium tier

cortex-vision is MIT-licensed and free to install / run / modify, matching the rest of Cortex. No license server, no entitlement check, no paid tier. The plugin manager pattern in [DISTRIBUTION.md](DISTRIBUTION.md) supports gating in the future if a different plugin needs it.

### 3. GPU expectation — RESOLVED: lazy model weights, two bundles

- Two PyInstaller bundles published per release: `cortex-vision-X-windows-cpu.zip` (~500 MB) and `cortex-vision-X-windows-gpu.zip` (~2.5 GB)
- User picks at install time (or auto-detect via `nvidia-smi`)
- Model weights (SmolVLM, DINOv2, Parakeet, YOLO) are NOT bundled — they download lazily on first use to `%APPDATA%/Cortex/video/weights/`
- This keeps initial install ~1 GB smaller and makes weight swaps trivial

### 4. Distribution mechanism — RESOLVED: sidecar service + plugin manager

cortex-vision is its own PyInstaller .exe running on `localhost:8004`. cortex-desktop proxies. cortex-desktop's plugin manager (new component) handles install/update/uninstall via GitHub releases. See [DISTRIBUTION.md](DISTRIBUTION.md).

---

## STANDING

### 5. Audio transcription provider — RESOLVED: env-var-driven provider chain

**Shipped in Phase 6.** Audio transcription is opt-in per session via the `transcribe_audio` flag on `POST /api/video/jobs` and `POST /api/video/jobs/upload`. Provider auto-detected at runtime via env vars:

| Env var                        | Provider                                    |
|--------------------------------|---------------------------------------------|
| `CORTEX_VISION_WHISPER_URL`    | LM Studio Whisper (or any OpenAI-compat)    |
| `CORTEX_VISION_WHISPER_KEY`    | Auth key (LM Studio doesn't enforce)        |
| `CORTEX_VISION_WHISPER_MODEL`  | Default model id (default: `whisper-1`)     |
| `OPENAI_API_KEY`               | Fallback to OpenAI's hosted Whisper API     |

If neither is set, `transcribe_audio=True` is silently ignored — the pipeline still completes with descriptions and narrative, just without `spoken_text`. ffmpeg must also be on PATH for audio extraction; missing-ffmpeg is detected and skipped non-fatally.

**Local Parakeet-TDT** (the original `[asr]` extra) is still deferred — the OpenAI-compat HTTP path covers the same use case via LM Studio loaded with a Whisper model. If Parakeet adds clear value (lower latency, GPU-only flow), it can be added as another provider in `cortex_vision/audio/transcribe.py` without touching the pipeline.

### 6. Storage location — default: APPDATA

**Default:** `%APPDATA%/Cortex/video/` on Windows, `~/.local/share/cortex/video/` on Unix. Consistent with cortex-desktop. Not user-configurable in v1; settings option added in Phase 6 if requested.

Disk budget for a typical user: ~160 MB / hour of live capture. Auto-cleanup setting added in Phase 6 ("Delete sessions older than N days").

### 7. Live capture — default: support both OBS and native

**Default:** OBS Virtual Camera is the recommended path (gives the user crop/window-select/privacy-blur for free). Native Windows screen capture via `mss` is the fallback for users who don't run OBS — ~50 lines, no setup required.

Settings UI lists capture devices including:
- `OBS Virtual Camera` (recommended)
- `Primary monitor (native)`
- `Webcam: <name>`
- `Capture card: <name>`

### 8. Journal mode entry point — confirm during Phase 3

Before Phase 3, look at `cortex-desktop/hub/frontend/src/components/overseer/` and decide:
- Does overseer's existing journal flow have a UI button we extend? Add "Record video" next to it.
- Or is the existing journal flow text-only? Add a separate "Video journal" tab in cortex-vision's page; bridge attaches results to today's overseer journal entry.

Either way works; the bridge logic is the same.

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
