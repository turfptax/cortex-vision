# cortex-vision — Progress Journal

A narrative record of what was built, why, and what was learned. Written for future contributors who want to understand how the project got here, plus retrospective value for the original author. Updated as a single document at major milestones; for per-release detail see [`CHANGELOG.md`](../CHANGELOG.md).

---

## Day 1 (2026-05-05 → 2026-05-06) — From design to v0.4.0

Started the day with cortex-vision being design + scaffolding. Ended with v0.4.0 published, 196 tests passing, end-to-end "live screen capture + system audio transcription" working on real hardware on Windows.

### What got shipped

**14 releases over the day** (counting only those that hit the GitHub release workflow):

| Tag | Theme |
|---|---|
| v0.1.0 | Phase 0/1: scaffolding + file/URL batch pipeline |
| v0.2.0 | Phase 5: PyInstaller bundle + GitHub release infrastructure |
| v0.3.0 | Phase 4: live OBS mode (video) + WebSocket events |
| v0.3.1 | Camera enumeration SEH-crash fix (pygrabber) |
| v0.3.2 | WS event timestamp uniformity |
| v0.3.3 | `stats.frames` field name fix |
| v0.3.4 | Journal upload bug + live stop hang + HTML export |
| v0.3.5 | Whisper.cpp auto-detection (incomplete — only checked one path) |
| v0.4.0 | Whisper.cpp detection complete + live mode audio capture + post-stop transcription |

Plus the corresponding cortex-desktop releases (the team shipped v0.18.0-dev.1 through dev.7 alongside, integrating each cortex-vision release into the Hub UI).

### The pattern that emerged

Each release surfaced exactly one bug class, in sequence — and each bug was masked by the previous one until it was fixed. The most instructive sequence:

```
v0.3.0:  Live tab loads → bundle dies silently
         (cv2.VideoCapture SEH crash during enumeration)
            ↓ fix in v0.3.1: switch to pygrabber for non-invasive listing
v0.3.1:  Live tab loads → click Start → GUI blanks
         (frontend crashes rendering events with missing timestamp_wall)
            ↓ fix in v0.3.2: inject baseline fields on every WS event
v0.3.2:  Click Start → GUI still blanks (same console error)
         (stats.frame_count vs stats.frames field name drift)
            ↓ fix in v0.3.3: rename to match documented contract
v0.3.3:  Click Start → IT WORKS
```

Three releases, three one-line backend bugs, each invisible until the previous was peeled off. Total backend code change for the trilogy: ~50 lines + tests. Total time spent: ~3 hours of pair-debugging via DevTools console + curl + log inspection.

**Lesson learned** (documented in CHANGELOG and the team relay notes): a single render error in `LiveMode.tsx` unmounts the entire component, which closes the WebSocket subscription and looks to the user like the bundle crashed. **A React error boundary around `<LiveMode>` would have made all three of these debug-from-a-printout instead of debug-from-DevTools.** Filed as a follow-up for the cortex-desktop team.

### Architectural decisions

The big design choices, locked in early and held throughout:

#### Sidecar service, not embedded

cortex-desktop ships as a frozen PyInstaller `.exe`. There's no `pip` inside the bundle, no writable site-packages — end users can't install Python packages into it. So cortex-vision is **its own** `.exe`, running as a local FastAPI service on `localhost:8004`. cortex-desktop's `routers/video.py` is a thin HTTP proxy.

This decision turned out to be load-bearing for everything else:

- Updates ship independently (50 MB cortex-desktop unaffected by 85 MB cortex-vision releases)
- Crashes don't cascade (a CUDA error in cortex-vision can't take down the system tray)
- Same pattern cortex-desktop already uses for cortex-core on the Pi — the integration approach was familiar to the team
- Plugin manager pattern unblocks future plugins without architectural changes

#### Open source, no licensing tier

Cortex is public; gating one plugin would create awkward asymmetry. v1 ships with no license check. If commercialization ever matters, the plugin manager can support gated download URLs without requiring sidecar-side changes.

#### Lazy model weights

The `.exe` bundle does NOT include SmolVLM, DINOv2, Parakeet, or YOLO weights. They download on first use to `%APPDATA%\Cortex\video\weights\` via HuggingFace's transformers cache. Keeps the bundle ~3 GB smaller and lets users swap models without re-downloading the .exe. (Not yet exercised in practice — we currently delegate vision describer entirely to LM Studio, no local model loading at all.)

#### File-based config, UI-editable, env-var fallback

Config lives at `%APPDATA%\Cortex\video\config.json`. Resolution order: per-request override > config file > env var > built-in default. The UI is authoritative once a value is set; env vars stay as a power-user override.

This decision made the difference between "user has to setx env vars and restart cortex-desktop" and "user clicks Save in a UI form." End-user vs. developer ergonomics.

#### Three-path Whisper provider chain

Audio transcription auto-detects in this order:

1. Explicit URL config — for users who run their own LM Studio Whisper
2. cortex-desktop's bundled whisper.cpp at `<install>\_internal\backend\bin\` — the canonical path for the 99% case
3. OpenAI cloud Whisper API — for users who want fast cloud transcription

This means **a user who's already used the overseer's voice journal feature once gets free local transcription with zero cortex-vision config**. We just read the binary and model files cortex-desktop already has on disk. Zero coupling between sidecar and host's HTTP API — only shared file conventions.

#### Cross-repo coordination via shared file paths

When cortex-vision needs something from cortex-desktop (whisper-cli binary, model files, plugin registry state), we read it from a known path on disk rather than calling cortex-desktop's API. This keeps the architecture decoupled — cortex-vision works fine if cortex-desktop is offline, just falls back to other providers.

### The bug catalog (for posterity)

Ordered by which one bit hardest:

#### 1. cv2 + DirectShow camera enumeration SEH crash (v0.3.0 → v0.3.1)

`cv2.VideoCapture(i, CAP_DSHOW)` to probe each device's resolution caused a native access violation when DroidCam was offline or OBS Virtual Camera was mid-init. SEH crashes don't go through Python exception handlers — the whole interpreter dies silently. No traceback, no error message in sidecar.log past the four DSHOW warning lines.

**Fix:** non-invasive enumeration via `pygrabber.dshow_graph.FilterGraph.get_input_devices()`. Calls Windows `ICreateDevEnum` directly to LIST devices without instantiating any of them. Returns names too — better UX (`"OBS Virtual Camera"` vs. `"Camera 7"`).

**Generalization:** any time we touch the OS via cv2 / Windows APIs / DirectShow, we have to plan for SEH crashes. Use enumeration APIs that don't open devices when probing is the goal.

#### 2. WebSocket event timestamp inconsistency (v0.3.1 → v0.3.2)

The `scene` and `stats` events had `timestamp_wall` and `elapsed_s` fields. The `described`, `stopped`, and `error` events didn't. cortex-desktop's `LiveMode.tsx` rendered every event uniformly with `.toLocaleString()` — crashed on undefined when a `described` event arrived first.

**Fix:** wrap the emit layer to inject baseline timing fields onto every event before queueing. Documented the contract at the top of `live.py`. Added a regression test asserting all event types from a complete session carry the baseline.

**Generalization:** any time multiple event types share a renderer, the union shape needs to be a strict superset. Documented invariants help; regression tests enforce.

#### 3. `stats.frame_count` vs `stats.frames` field-name drift (v0.3.2 → v0.3.3)

The protocol docstring at the top of `live.py` said `frames`. The implementation said `frame_count`. The frontend correctly followed the docstring. We had a contract drift between docs and code with no test catching it.

**Fix:** rename the dict key, add a contract test pinning the four fields LiveMode actually reads from the stats event.

**Generalization:** docstrings without enforcement drift. Pin the contract with a test that fails when the implementation changes.

#### 4. Whisper-cli detection too narrow (v0.3.5 → v0.4.0)

v0.3.5 only checked `%APPDATA%\Cortex\whisper-cpp\whisper-cli.exe` for the binary. cortex-desktop's official installer drops it at `<install>\_internal\backend\bin\whisper-cli.exe` — a PyInstaller convention. Result: `transcribe.configured: false` even though the binary AND the model were both on disk. Pipeline silently skipped transcription.

**Fix:** v0.4.0 search order checks `CORTEX_VISION_WHISPER_CLI` env var, then ProgramFiles / ProgramFiles(x86) / LocalAppData/Programs CortexHub install paths, then the original APPDATA fallback, then `shutil.which`. Verified live on the user's machine.

**Generalization:** when integrating with another tool's install layout, search every plausible location, not just the canonical "should-be-there" path. Real installs deviate.

#### 5. Various smaller bugs

- Journal mode `FileNotFoundError` — upload endpoint stored `filename` (basename) but pipeline tried `Path(filename).resolve()` which landed in CWD. Fixed by having the pipeline detect upload-mode and look in `<session_dir>/source.*`.
- Live stop hanging — race condition where the WS handler exited before the terminal `stopped` event was drained from the queue. Fixed by draining queued events when `pipeline.is_running` flips false.
- Live describer 120s timeout — Stop blocked on the describer thread join while httpx waited for LM Studio. Reduced live-mode describer timeout to 30s.
- Plugin install Access Denied — Windows file lock from a previous sidecar process holding DLLs. Documented the manual cleanup recipe + filed as a follow-up for the team's plugin manager (retry loop + verify-then-flip-registry).

### Cross-team collaboration

cortex-vision is one half of the integration. The other half is cortex-desktop's Hub UI + plugin manager + overseer bridge. The cortex-desktop team shipped 7 dev releases (v0.18.0-dev.1 through dev.7) over the same day, each integrating one cortex-vision release.

Coordination worked via:

- **Locked endpoint contracts** in `docs/INTEGRATION.md` and `docs/HANDOFF.md` — once an endpoint shape was published, both sides built against it without further discussion
- **Telemetry-via-curl** for verification — they hit my endpoints, I hit theirs, contracts caught drift
- **Specific bug repros via DevTools** — when frontend bugs hit, sharing the JS console stack trace + WS Messages capture made diagnosis fast (5-10 minutes per bug)

The team independently caught and fixed several issues on their side that had cortex-vision dependencies:

- Phantom session attach on tab mount → moved to explicit Start
- Camera label format mismatch with new field shape → updated their type
- Error envelope unwrap (`parseStartError`) for nicer UX
- Plugin manager `dev_mode_no_restart` 409 — graceful handling of dev-mode plugins

### What's still queued

[ROADMAP.md](ROADMAP.md) tracks this in detail. The short list:

**Frontend work for the cortex-desktop team:**
- Configure UI for the describer + transcribe settings
- View Logs panel
- LiveMode audio controls (source dropdown, transcribe checkbox, level meter)
- React error boundary on LiveMode

**Optional cortex-vision polish:**
- pygrabber UTF-16 decode (cosmetic — `EÆgato` → `Elgato` in device names)
- `meta.json` sidecar per session
- CLI mode for headless processing

**Out of scope for v1:**
- Cross-video deduplication
- Multi-camera live mode
- Code signing
- Linux/macOS bundles

---

## Repository state at end of Day 1

| Metric | Value |
|---|---|
| Source LOC (cortex_vision/) | ~5,500 |
| Test LOC (tests/) | ~3,000 |
| Tests passing | 196 / 196 |
| Latest release | v0.4.0 |
| Bundle size (CPU) | ~85 MB |
| Endpoints | 24 |
| Documented modules | All public surface |
| Known bugs | None blocking; cosmetic only (`EÆgato`) |

**The whole "Cortex companion sees what's on your screen and describes it (with audio)" demo is genuinely working on a clean install.** That's what we set out to build.
