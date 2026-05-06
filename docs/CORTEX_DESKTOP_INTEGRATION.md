# cortex-desktop Integration — Current State

> **Audience:** the cortex-desktop frontend + plugin-manager team.
> **Last updated:** 2026-05-06 against cortex-vision v0.4.0.
> **Supersedes the original [HANDOFF.md](../HANDOFF.md)** for current contract details. HANDOFF.md is preserved for the Phase 0 narrative; consult this doc for what's implemented today.

This document lists everything cortex-desktop should consume from cortex-vision and everything that's still pending on the cortex-desktop side. It's the operational reference — pair with [`README.md`](../README.md) for the per-endpoint detail.

---

## What's already integrated (cortex-desktop dev releases)

| Release | What landed |
|---|---|
| **v0.18.0-dev.1** | Plugin sidecar harness — `services/plugin_manager.py`, `routers/video.py` proxy, `routers/plugins.py` admin API, `PluginsTab.tsx`, page gating |
| **v0.18.0-dev.2** | `POST /api/plugins/dev-register` — agent-friendly dev registration |
| **v0.18.0-dev.3** | `FileMode.tsx`, `SessionList.tsx`, `videoApi.ts`, `useVideoJob.ts` |
| **v0.18.0-dev.4** | `JournalMode.tsx` — getDisplayMedia + getUserMedia + MediaRecorder + upload |
| **v0.18.0-dev.5** | `LiveMode.tsx` + WebSocket pass-through in proxy |
| **v0.18.0-dev.6** | Overseer bridge (polling) + Transcribe audio toggle on FileMode |
| **v0.18.0-dev.7** | LiveMode picker + camera enumeration fixes |

---

## What's pending on the cortex-desktop side

### High priority — unblocks the v0.4.0 audio feature

#### LiveMode audio controls

cortex-vision v0.4.0 ships desktop audio capture + post-stop transcription, but the LiveMode UI doesn't yet expose the controls. End users currently have to drive the API with curl.

**Required additions to `LiveMode.tsx` start form:**

1. Audio source dropdown (next to camera picker)
   - Populate via `GET /api/video/live/audio-devices`
   - First entry is "Desktop audio (...)" with `is_default_output_loopback: true` — this should be the default selection
   - Subsequent entries are real input devices (mics, line-ins, capture cards)
   - Plus a "(no audio)" / "None" option that maps to `audio_source: null`

2. "Transcribe audio" checkbox
   - Defaults to false (matches FileMode UX)
   - Disabled / hidden when audio source is "(none)"

3. Audio level meter
   - Listen for `{"type": "audio_level", "rms": float, "peak": float}` events on the live WebSocket
   - Emitted at ~10 Hz with `rms` and `peak` both in `[0, 1]`
   - Render as a vertical or horizontal bar; map RMS to fill height/width
   - Optionally use `peak` for a hold-line

4. "Transcribing..." status during post-process
   - After Stop click, wait for `{"type": "transcribing", "audio_duration_s": float}` event before transitioning to "complete"
   - Final state arrives as `{"type": "transcribed", "segment_count": int, "scenes_with_audio": int}`
   - Failure: `{"type": "transcribe_skipped", "reason": str}` or `{"type": "transcribe_failed", "message": str}` — show as warning rather than error (session itself is still complete)

5. Wire `audio_source` + `transcribe_audio` into the `POST /api/video/live/start` request body:

   ```ts
   {
     camera_index: number,
     resolution: [number, number],
     audio_source: number | string | null,
     transcribe_audio: boolean,
     // ... existing thresholds ...
   }
   ```

   Where `audio_source` is:
   - `null` (or absent) → no audio capture, video only
   - `"desktop"` (string) → WASAPI loopback on default Windows output
   - `<int>` → sounddevice input device index
   - `<string>` → substring match on device name (case-insensitive)

#### Configure UI

The settings endpoints (`GET/PUT /api/video/config`, `POST /api/video/config/test`, `GET /api/video/lmstudio/scan`) have been live since v0.3.0 but no UI consumes them. Right now the user has to either set env vars via setx (and restart cortex-desktop) or PUT the config via PowerShell.

**Suggested form in Settings → Plugins → Cortex Vision → Configure:**

```
Vision describer
  URL:    [____________________] [Discover ▾] [Test]
  Model:  [smolvlm2-2.2b-instruct  ▾]    (populated from Test response)
  API key: [••••••••••••] [Show]

Audio transcription (optional)
  URL:    [____________________] [Test]
  Model:  [whisper-1            ▾]
  API key: [••••••••••••] [Show]

  Note: Cortex auto-detects whisper.cpp installed by Cortex Hub at
  C:\Program Files (x86)\CortexHub\_internal\backend\bin\. No config
  needed if you've used Cortex Hub's voice journal feature.

Live mode defaults
  Scene threshold: [0.85    ]
  Pixel diff:      [25.0    ]
  Structural:      [0.15    ]

[Save] [Reset to defaults]
```

The "Discover" button calls `GET /api/video/lmstudio/scan?hints=<known-host>` and shows the reachable URLs in a dropdown. Save → `PUT /api/video/config`. No restart needed — config is read on every request.

API key handling: GET returns `"***"` for keys that are set, `""` for unset. When the user submits PUT with `"***"`, we preserve the existing key (don't clobber). With a new value, we save it. With `""`, we clear it.

#### View Logs panel

The endpoints for log access have been live since v0.3.0. No UI consumes them.

**Suggested addition in Plugins tab → Cortex Vision detail view:**

```
[View Logs ▾]  [Debug mode: Off ▼]  [Clear logs]

(scrollable code-formatted panel)
22:12:24 INFO    cortex_vision.pipeline.live  session=... started camera=7
22:12:24 INFO    httpx  HTTP Request: POST http://10.0.0.102:1234/v1/...
...
```

- "View Logs" button → `GET /api/video/logs?lines=300`
- "Debug mode" toggle → `POST /api/video/logs/level` with `{level: "debug"}` or `{level: "info"}`
- "Clear logs" button → `DELETE /api/video/logs`
- Auto-refresh every 2-3s when panel is open

This eliminates the "open PowerShell, dig into APPDATA, run Get-Content" loop for users debugging their own installs.

### Medium priority — robustness

#### React error boundary on LiveMode

Three crashes during the v0.3.0-v0.3.3 cycle were each "single render error in LiveMode unmounts the entire component, taking down the WS subscription, looking like a backend crash." All three were one-line backend bugs that took ~30 minutes each to identify only because the failure mode obscured the root cause.

**Suggested wrap:**

```tsx
<ErrorBoundary fallback={<LiveModeError onRetry={resetSession} />}>
  <LiveMode />
</ErrorBoundary>
```

The fallback renders something like:

```
⚠ Live view rendering hit an error: Cannot read properties of undefined
  (reading 'frames')

  [Retry]   [Stop session]   [Open browser console]
```

This keeps the WS alive (so events keep flowing into a state we just can't render correctly) and shows the user actionable info instead of a blank panel. Same pattern would benefit JournalMode.

#### Auto-restart-on-crash for plugin manager

Currently if the cortex-vision sidecar process dies, the plugin manager marks it Stopped and waits for the user to manually click Restart. A `restart_on_crash: true` flag in `registry.json` (default true) would respawn the process within ~5s of detecting it dead.

#### Plugin install/uninstall hardening

The `WinError 5: Access denied` we hit during reinstall was caused by:

1. Uninstall removed the registry entry first, then attempted file delete — but a stale process held DLLs open
2. Subsequent Install tried to rename `cortex-vision/` → `cortex-vision.bak/`, the `.bak` already existed from the previous failed install, conflict

**Suggested fixes in `services/plugin_manager.py`:**

- `uninstall()` should verify file deletion success before flipping the registry to "uninstalled" (atomic-ish)
- `install()` should clean up any stale `*.bak/` directories before attempting the rename
- Both should retry-with-sleep on Windows file-lock errors (typical 200-500ms window after process exit while the OS releases handles)

### Low priority — polish

#### LM Studio "Discover" hint propagation

cortex-desktop's `harness init` already scans the LAN for LM Studio. When the user opens the Configure form, that already-known URL should be passed as a hint to `/api/video/lmstudio/scan?hints=<url>` so it appears in the Discover dropdown without re-probing.

#### Dev-mode plugin label

The Plugins tab currently shows `cortex-vision · vdev · dev` for dev-mode plugins (registered via `dev-register`). Worth surfacing a small badge: "Dev mode — managed externally" so the user knows why the Restart button is disabled. v0.18.0-dev.7 already handles the restart 409 gracefully; this is just label polish.

---

## Endpoint contract (current state — v0.4.0)

All endpoints under `/api/video/*`, accessible via the proxy at `localhost:8003` or directly at `localhost:8004`.

### Stable API surface (relied on by the frontend)

| Method | Path | Status | Purpose |
|---|---|---|---|
| `GET` | `/health` | ✅ stable | Liveness probe |
| `GET` | `/version` | ✅ stable | Version + package name |
| `GET` | `/manifest` | ✅ stable | Plugin manifest (matches plugin.json) |
| `GET` | `/diagnostics` | ✅ stable | Operational snapshot |
| `GET` | `/sessions` | ✅ stable | List w/ filters: `?limit=`, `?mode=`, `?status=`, `?pushed=` |
| `GET` | `/sessions/{id}` | ✅ stable | Hydrated session (scenes + transcript) |
| `POST` | `/sessions/{id}/mark-pushed` | ✅ stable | For overseer bridge |
| `GET` | `/sessions/{id}/export.html` | ✅ v0.3.4 | Self-contained HTML report |
| `GET` | `/jobs/{id}/frame/{scene}/{frame}` | ✅ stable | Raw JPEG keyframe |
| `POST` | `/jobs` | ✅ stable | Batch job from URL or local file |
| `POST` | `/jobs/upload` | ✅ stable | Multipart upload (Journal mode) |
| `GET` | `/live/cameras` | ✅ stable | Available video devices |
| `GET` | `/live/audio-devices` | ✅ v0.4.0 | Available audio sources (NEW) |
| `POST` | `/live/start` | ✅ stable | Now accepts `audio_source` + `transcribe_audio` (v0.4.0) |
| `POST` | `/live/stop` | ✅ stable | Triggers post-process transcription if enabled |
| `GET` | `/live/status` | ✅ stable | Current session snapshot |
| `WS` | `/live/ws` | ✅ stable | Real-time event stream |
| `GET` | `/config` | ✅ v0.2.0 | Current config (api keys redacted) |
| `PUT` | `/config` | ✅ v0.2.0 | Update config (atomic write) |
| `POST` | `/config/test` | ✅ v0.2.0 | Test connectivity for proposed values |
| `GET` | `/lmstudio/scan` | ✅ v0.3.0 | Discover OpenAI-compat servers |
| `GET` | `/logs` | ✅ v0.3.0 | Recent log lines |
| `POST` | `/logs/level` | ✅ v0.3.0 | Bump log level (debug/info/warning/error) |
| `DELETE` | `/logs` | ✅ v0.3.0 | Clear ring buffer |

### WebSocket event types

Every event includes baseline timing: `timestamp_wall: float` (Unix seconds) and `elapsed_s: float` (seconds since session start). Plus type-specific fields:

| Type | Fields | When |
|---|---|---|
| `started` | session_id, camera_index, resolution, native_resolution, native_fps | At session start |
| `scene` | scene_index, change_type ("scene_change"\|"update"), thumbnail_url, trigger_method, similarity | When detector fires |
| `described` | scene_index, description, describer_model | When describer worker completes |
| `stats` | fps, frames, scene_count, describer_queue_depth, ... | Every 1 second |
| `audio_level` | rms, peak (both [0, 1]) | Every ~100ms (v0.4.0, only if audio_source != null) |
| `transcribing` | audio_duration_s | After Stop, while whisper runs (v0.4.0) |
| `transcribed` | provider, model, segment_count, scenes_with_audio, char_count | When post-process completes (v0.4.0) |
| `transcribe_skipped` | reason ("no_whisper_provider", "ffmpeg_missing", ...) | Graceful skip (v0.4.0) |
| `transcribe_failed` | message | Post-process error (v0.4.0) |
| `stopped` | session_id, scene_count, duration_s, audio_recorded, audio_duration_s | At session end |
| `error` | subsystem, message | On internal error (capture, audio, etc.) |

### Stable contract pinning

Tests in `tests/test_live_pipeline.py::test_stats_event_field_names_match_frontend_contract` and `test_live_pipeline_every_event_carries_baseline_timing` enforce:

- Every event has `timestamp_wall` + `elapsed_s` baseline
- Stats events include `fps`, `frames`, `scene_count`, `elapsed_s` (the four LiveMode renders)

Field-name renames now require coordinated frontend ship + cortex-vision test update.

---

## How to test cross-repo changes

```powershell
# Full integration smoke test — exercise every mode

# 1. Verify v0.4.0 + whisper.cpp detection
Invoke-RestMethod http://localhost:8003/api/video/version
Invoke-RestMethod http://localhost:8003/api/video/diagnostics |
    Select-Object -ExpandProperty transcribe | ConvertTo-Json

# 2. Process Video mode
$body = @{
    source = "https://www.tiktok.com/@example/video/123"
    mode = "file"
    transcribe_audio = $true
} | ConvertTo-Json
Invoke-RestMethod http://localhost:8003/api/video/jobs -Method POST `
    -Body $body -ContentType "application/json"

# 3. Live mode with audio (the v0.4.0 hot path)
$body = @{
    camera_index = 7
    audio_source = "desktop"
    transcribe_audio = $true
} | ConvertTo-Json
$result = Invoke-RestMethod http://localhost:8003/api/video/live/start -Method POST `
    -Body $body -ContentType "application/json"

# ... record stuff ...

Invoke-RestMethod http://localhost:8003/api/video/live/stop -Method POST

# 4. View results
$id = $result.session_id
Invoke-WebRequest "http://localhost:8003/api/video/sessions/$id/export.html" `
    -OutFile "$env:USERPROFILE\Desktop\test.html"
Start-Process "$env:USERPROFILE\Desktop\test.html"
```

If all four steps produce non-empty descriptions + transcripts, the cross-repo integration is healthy.

---

## Questions, contract changes, or breakage

If something in this doc looks wrong against current behavior, it's a bug. Either:

- The doc is stale (file an issue against cortex-vision)
- cortex-vision drifted from the documented contract (file a regression test against cortex-vision and the contract test will catch the drift)

Cross-repo coordination has worked best when both sides treat documented contracts as binding and verify via the `/diagnostics` + `/logs` endpoints rather than discussion.
