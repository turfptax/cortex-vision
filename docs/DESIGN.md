# cortex-vision — Design

## Purpose

Process video — live screen capture, recorded files/URLs, or video journal recordings — into structured scene-by-scene memory that the Cortex overseer can ingest. Provide visual context for the user's day in the same way the overseer already provides session/journal context for their writing and chat.

## Architecture: sidecar service

cortex-vision is **its own** PyInstaller-bundled `.exe` running as a local FastAPI service on `localhost:8004`. cortex-desktop's `routers/video.py` is a thin HTTP proxy that forwards `/api/video/*` to the sidecar.

This is necessary, not optional. cortex-desktop ships as a frozen PyInstaller bundle with no `pip` and no writable site-packages. End users cannot `pip install cortex-vision` — that only works for developers running from source. The sidecar model is how OBS plugins, VS Code extensions, and (already) cortex-desktop's connection to cortex-core on the Pi all work.

```
+-------------------------+              +-----------------------------+
|  cortex-desktop.exe     |   HTTP       |  cortex-vision.exe          |
|  port 8003              +------------->+  port 8004                  |
|                         |              |                             |
|  routers/video.py       |              |  cortex_vision/server.py    |
|     proxies to ->       |              |  full vision pipeline       |
|                         |              |                             |
|  services/              |              +-----------------------------+
|    plugin_manager.py    |                          ^
|     spawns / monitors   +--------------------------+
+-------------------------+
```

See [DISTRIBUTION.md](DISTRIBUTION.md) for the full install/update mechanism.

### Why separate sidecar

| Concern                       | Single embedded build      | Sidecar service           |
|-------------------------------|----------------------------|---------------------------|
| cortex-desktop update size    | 2 GB                       | stays ~50 MB              |
| Vision update size            | 2 GB full re-download      | ~500 MB independent       |
| Crash isolation               | takes down tray            | sidecar dies, tray fine   |
| Run on remote GPU machine     | no                         | yes — proxy to remote IP  |
| Adding plugin #2 later        | monolithic merge each time | same pattern, decoupled   |
| Code signing                  | 1 binary                   | 2 binaries (deferred)     |

## Three modes, one pipeline

```
                 +----------------------------------------+
                 |             cortex_vision              |
                 |             (in sidecar)               |
                 |                                        |
  Live (OBS)  ---> capture --> scene detect --> describe -+
                 |                                     |  |
  File / URL  ---> download --> segment   --> describe -+  |
                 |                                     |  |
  Journal     ---> record   --> segment   --> describe -+  |
                 |                                     |  |
                 |                                     v  |
                 |                       narrative rollup |
                 |                                     |  |
                 |                                     v  |
                 |     SessionManager --> SQLite + JSON sidecars
                 +----------------------------------------+
                                       |
                                       v   HTTP proxy via cortex-desktop
                       overseer bridge --> Pi notes / sessions
```

### Mode 1 — Live (OBS Virtual Camera)

Long-running pipeline. User clicks "Start watching screen" in the system tray or Hub UI. cortex-vision opens OBS Virtual Camera, runs continuous scene detection, sends each detected scene's keyframes to a vision model, accumulates audio transcript from WASAPI loopback, and emits structured events:

- **On scene change** → write `SceneEntry` to SQLite, push a one-line note to overseer: `[14:23] working in VS Code, scrolling cortex-vision design doc, terminal visible bottom-right`
- **Every 5 minutes** → batch the window's scenes + transcript into an overseer session segment with a Sonnet-written narrative
- **On stop** → finalize, write final narrative

Reuses VisualFast wholesale: live orchestrator, 3-method scene detector (histogram + pixel diff + structural diff), capture loop, audio loopback.

### Mode 2 — File / URL (batch)

Smallest path. User drops a file or pastes a URL. Run download → segment → describe each scene → narrative pass → done. No streaming, no audio loopback (use ffmpeg to extract the audio track for transcription if requested). Reuses VideoIndex wholesale: yt-dlp wrapper, PySceneDetect with single-shot fallback, keyframe extraction (minus the FAISS/SSCD/DINOv2 fingerprinting — out of scope for cortex-vision).

### Mode 3 — Video journal

User clicks "Record video journal" → screen + mic captured for N minutes → on stop, runs the Mode 2 batch pipeline against the recorded file → produces a journal entry with thumbnails, transcript, and narrative.

The audio half is already working in overseer's existing journal feature; this mode adds the video half by running the batch pipeline on the screen recording. Output attaches to the existing journal data structure rather than creating a new one.

## Why the modes converge

All three modes produce the same `VideoSession` shape (see [DATA_MODEL.md](DATA_MODEL.md)). The differences are purely in capture and trigger:

- Live uses real-time scene detection (3-method, sub-second) with continuous output
- Batch uses PySceneDetect's `ContentDetector` (offline, more accurate) with all output at end
- Journal is batch over a recording the user just made

The describer, narrative rollup, audio transcription, and storage are identical across modes. This is what makes the architecture clean — every new feature added (new vision model, better narrative prompt, transcript correction) lights up all three modes at once.

## License model

cortex-vision is **open source / public domain** under MIT, matching the rest of the Cortex ecosystem. There is no premium tier, no license key, no entitlement check. The plugin is free to install, free to run, and free to modify.

This decision was made deliberately:
- The Cortex project itself is public; gating one plugin would create awkward asymmetry
- Runtime processing is local — there's no recurring server cost to amortize via licensing
- Open distribution maximizes the testing surface, contributor pool, and hackability

If a future plugin needs commercialization, the [DISTRIBUTION.md](DISTRIBUTION.md) plugin manager pattern can support it (gated download URL + license header sent during install). Not needed for v1.

## Overseer integration

This is what makes cortex-vision more than "another video tool." The overseer already produces gists/themes/episodes from session text. Adding timestamped scene descriptions tied to projects gives it visual context for everything.

Per-mode push policy:

| Mode    | What gets pushed                                            | When             |
|---------|-------------------------------------------------------------|------------------|
| Live    | One note per scene change + 5-min rollup as session segment | continuously     |
| File    | One session entry with full narrative + scene list          | on completion    |
| Journal | Append scenes to existing journal session                   | on completion    |

Project tagging is the lynchpin. If the user is "working on cortex-vision", every scene from live mode tags `project:cortex-vision`. The overseer's existing project rollup includes "what was on screen" alongside "what was committed" and "what was journaled" — free upgrade to per-project narrative output with no overseer code changes.

The bridge module lives in cortex-desktop, not cortex-vision, because pushing to overseer requires `pi_client` (which talks to the user's Pi). The vision sidecar emits structured `SceneEntry` data; cortex-desktop's bridge converts and forwards. See [INTEGRATION.md](INTEGRATION.md).

## Trade-offs noted in the design

- **Skip fingerprinting.** VideoIndex's SSCD/DINOv2/pHash/FAISS layer is for cross-video duplicate detection. cortex-vision processes one video at a time — adding fingerprinting would drag in 2 GB of model weights for a feature nobody asked for. If a future Cortex feature wants "did I already watch this video?", revisit.
- **Skip YOLO by default.** Vision LLMs handle static keyframes well. YOLO earned its keep in VisualFast as a faster pre-pass for live streams; in cortex-vision live mode it's optional (toggle in settings). Saves ~150 MB + a worker thread.
- **Audio is opt-in.** Not all video has meaningful audio. Live mode's audio loopback requires WASAPI; batch mode's `ffmpeg` audio extract is cheap but Parakeet ASR isn't. Default audio off; user opts in per session.
- **One vision model per session.** No "describer hot-swap mid-session" complexity. Pick at session start, lock in. (This was complexity in VisualFast that we don't need here.)
- **Lazy model weights.** SmolVLM, DINOv2, Parakeet, YOLO are NOT bundled in the .exe — they download to `%APPDATA%/Cortex/video/weights/` on first use. Keeps the .exe ~1 GB smaller and lets users swap models without re-downloading.

## Open design decisions

See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for unresolved items. Most have been answered as of the architecture lock-in.
