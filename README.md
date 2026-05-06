# cortex-vision

Real-time and batch video understanding pipeline for the [Cortex](https://github.com/turfptax) AI companion ecosystem. Provides scene detection, per-scene description (via local or cloud vision LLMs), audio transcription, and narrative rollup. Plugs into `cortex-desktop` as a sidecar service and feeds the overseer's interpretive memory graph with visual context.

**Status: design / scaffolding.** Phase 0 of the [roadmap](docs/ROADMAP.md). The package is installable, the sidecar HTTP service runs, and schemas + storage are real. Pipeline implementations land in Phase 1.

## What it does

Three modes, one pipeline:

| Mode    | Input                          | Use case                                                |
|---------|--------------------------------|---------------------------------------------------------|
| Live    | OBS Virtual Camera / webcam    | Watches the user's screen, narrates work in progress    |
| File    | Local file or URL (yt-dlp)     | Process a recorded video into scenes + narrative        |
| Journal | Screen + mic recording         | Video diary — produces transcript + scene summary       |

All three produce the same output: a `VideoSession` with scene keyframes, per-scene descriptions, optional audio transcript, and a final narrative rollup. Sessions can be pushed to the Cortex overseer to enrich daily/weekly project rollups with visual context.

## How it fits into the Cortex ecosystem

cortex-vision is a **sidecar service**: its own PyInstaller-bundled `.exe` running on `localhost:8004`. cortex-desktop's plugin manager spawns it on app startup and proxies `/api/video/*` requests through to it.

```
+-------------------------+              +-----------------------------+
|  cortex-desktop.exe     |   HTTP       |  cortex-vision.exe          |
|  port 8003              +------------->+  port 8004                  |
|  routers/video.py       |              |  cortex_vision/server.py    |
|     proxies to ->       |              |  full vision pipeline       |
|  services/                              +-----------------------------+
|    plugin_manager.py    +---------- spawns ---------+
+-------------------------+
```

Why a sidecar instead of embedded? cortex-desktop ships as a frozen PyInstaller bundle with no `pip` and no writable site-packages. End users cannot install Python packages into it at runtime. The sidecar service model is how OBS plugins, VS Code extensions, and Cortex's own connection to cortex-core (the Pi) all work. See [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) for the full story.

## Source material

The pipeline distills proven primitives from two sandbox projects:

- **[VisualFast](../../VisualFast)** — real-time live-stream prototype (OBS capture, 3-method scene detection, threaded model workers, WASAPI audio)
- **[VideoIndex](../../VideoIndex)** — batch-ingest prototype (yt-dlp downloader, PySceneDetect with single-shot fallback, sighting metadata)

See [docs/SOURCES.md](docs/SOURCES.md) for the file-by-file port mapping.

## Documentation

| Document                                  | Purpose                                                          |
|-------------------------------------------|------------------------------------------------------------------|
| [HANDOFF.md](HANDOFF.md)                  | **Start here if you're on the cortex-desktop team** — Phase 0 work brief with acceptance criteria |
| [docs/DESIGN.md](docs/DESIGN.md)          | Architecture, sidecar rationale, mode behaviors                  |
| [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) | PyInstaller spec, GitHub release flow, install/update mechanism |
| [docs/ROADMAP.md](docs/ROADMAP.md)        | Phased build plan with done-when criteria                        |
| [docs/INTEGRATION.md](docs/INTEGRATION.md)| How cortex-desktop wires this in (proxy + plugin manager)        |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md)  | Pydantic schemas + SQLite layout + filesystem artifacts          |
| [docs/SOURCES.md](docs/SOURCES.md)        | File-by-file port map from VisualFast / VideoIndex               |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | Resolved decisions + remaining defaults                    |

## Quick start

### Run the sidecar (today, Phase 0)

```powershell
cd C:\dev\ttx\Cortex\cortex-vision
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
python -m cortex_vision serve
```

That starts the HTTP service on `http://127.0.0.1:8004`. Verify:

```powershell
curl http://127.0.0.1:8004/api/video/health
# {"status":"ok","version":"0.1.0","db_path":"C:/Users/.../AppData/Roaming/Cortex/video/sessions.db"}
```

### Run the tests

```powershell
pytest
```

### As an end-user plugin (Phase 5+)

Once Phase 5 ships, end users won't run any of the above. They'll install via cortex-desktop's Plugins tab, which downloads and manages the bundled `cortex-vision.exe` for them.

## License

MIT, matching the rest of the Cortex ecosystem. Free to install, run, and modify.
