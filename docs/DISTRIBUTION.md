# cortex-vision — Distribution & Update Model

How the bits get to the user's machine and how updates flow. Read alongside [DESIGN.md](DESIGN.md) and [INTEGRATION.md](INTEGRATION.md).

## Architecture: sidecar service

cortex-desktop ships as a frozen PyInstaller `.exe`. It cannot `pip install` plugins at runtime. So cortex-vision is **its own** PyInstaller `.exe`, running as a local FastAPI service on `localhost:8004`. cortex-desktop's `routers/video.py` is a thin HTTP proxy.

```
+-------------------------+              +-----------------------------+
|  cortex-desktop.exe     |   HTTP       |  cortex-vision.exe          |
|  (frozen, ~50 MB)       +------------->+  (frozen, ~500 MB CPU       |
|  port 8003              |              |   or ~2.5 GB GPU)           |
|                         |              |  port 8004                  |
|  routers/video.py       |              |                             |
|     (proxies to ->)     |              |  FastAPI app (server.py)    |
|                         |              |  full vision pipeline       |
|  services/              |              +-----------------------------+
|    plugin_manager.py    |                          ^
|     (lifecycle)         |                          | spawns at startup,
|                         +--------------------------+ kills at shutdown
+-------------------------+
```

This is the same pattern cortex-desktop already uses for cortex-core (the Pi at `:8420`). Adding a localhost peer at `:8004` is structurally identical.

## Why a sidecar (and not embedded)

| Concern                     | Embedded build           | Sidecar service              |
|-----------------------------|--------------------------|------------------------------|
| cortex-desktop update size  | 2 GB Pro / 50 MB Free    | 50 MB (always)               |
| cortex-vision update size   | 2 GB (full re-download)  | ~500 MB (independent)        |
| Crash blast radius          | takes down system tray   | sidecar dies, tray fine      |
| Process isolation           | none                     | full                         |
| Run on second machine       | no                       | yes — point proxy at remote IP |
| Adding a 2nd plugin later   | another monolithic merge | identical pattern, decoupled |
| Code signing                | one binary               | two — cost doubles           |

The signing-cost item is the only real downside, and we punt code signing to a later phase regardless.

## Plugin manifest

Every plugin ships a `plugin.json` at the root of its repo and bundles it into the `.exe`. cortex-desktop's plugin manager reads this to wire the plugin into the UI.

The current manifest is at [`/plugin.json`](../plugin.json). Key fields:

```json
{
  "id": "cortex-vision",
  "version": "0.1.0",
  "executable": { "windows": "cortex-vision.exe" },
  "default_port": 8004,
  "api_prefix": "/api/video",
  "health_endpoint": "/api/video/health",
  "manifest_endpoint": "/api/video/manifest",
  "ui": { "page_id": "video", "label": "Video" },
  "github_repo": "turfptax/cortex-vision",
  "release_assets": {
    "windows-cpu": "cortex-vision-{version}-windows-cpu.zip",
    "windows-gpu": "cortex-vision-{version}-windows-gpu.zip"
  }
}
```

The same manifest is also served at runtime via `GET /api/video/manifest` so cortex-desktop can introspect a running plugin without re-reading files on disk.

## Build pipeline

Two bundles per release, produced by GitHub Actions:

| Bundle                                          | Size  | Use                                                      |
|-------------------------------------------------|-------|----------------------------------------------------------|
| `cortex-vision-X.Y.Z-windows-cpu.zip`           | ~500 MB | No GPU. Cloud describers (LM Studio remote, OpenRouter) only |
| `cortex-vision-X.Y.Z-windows-gpu.zip`           | ~2.5 GB | Local GPU describers + Parakeet ASR                      |

Build commands:

```powershell
# CPU bundle
python -m venv .venv-cpu
.\.venv-cpu\Scripts\activate
pip install -e .[build,cpu]
pyinstaller cortex-vision.spec --noconfirm
Compress-Archive -Path dist/cortex-vision/* -Destination cortex-vision-0.1.0-windows-cpu.zip

# GPU bundle
python -m venv .venv-gpu
.\.venv-gpu\Scripts\activate
pip install -e .[build,gpu]
pyinstaller cortex-vision.spec --noconfirm -- --gpu
Compress-Archive -Path dist/cortex-vision/* -Destination cortex-vision-0.1.0-windows-gpu.zip
```

Both zips published to `github.com/turfptax/cortex-vision/releases/v0.1.0`. cortex-desktop's plugin manager picks the right one based on:

1. User's choice (radio button at install time: "CPU" / "GPU")
2. Auto-detected: if `nvidia-smi` exists, suggest GPU; otherwise CPU

### Lazy model weights

The bundle **does not include** model weights. SmolVLM, DINOv2, Parakeet, YOLO — all download on first use to `%APPDATA%/Cortex/video/weights/` via HuggingFace's transformers cache. This:

- Keeps the .exe ~1 GB smaller
- Avoids stale model weights pinned to a release
- Lets users swap models via the settings UI without re-downloading the .exe

First-run UX: when the user kicks off their first describe operation, cortex-desktop shows a "Downloading SmolVLM weights (350 MB)..." progress bar. One-time cost. Subsequent runs are instant.

## Install flow (from end-user perspective)

```
1. User opens cortex-desktop
2. Clicks Settings -> Plugins
3. Sees "Cortex Vision" with "Install" button
4. Clicks Install
   -> dialog: "CPU (small, cloud-only) | GPU (large, local NVIDIA)"
   -> user picks GPU
5. Plugin manager:
   a. Hits github.com/turfptax/cortex-vision/releases/latest (GitHub API)
   b. Downloads cortex-vision-0.1.0-windows-gpu.zip to %TEMP%
   c. SHA256 verify against published checksum (in release notes)
   d. Extract to %APPDATA%/Cortex/plugins/cortex-vision/
   e. Read plugin.json from extracted dir
   f. Add entry to %APPDATA%/Cortex/plugins/registry.json
   g. Spawn cortex-vision.exe as subprocess
   h. Poll http://127.0.0.1:8004/api/video/health for up to 30s
6. On success: Video tab appears in cortex-desktop nav
7. User can navigate to Video tab; requests proxy to localhost:8004
```

Total time on a 100 Mbps connection: ~3 minutes for GPU bundle.

## Update flow

Plugin manager runs a background check daily (or on demand from the UI):

```
1. GET https://api.github.com/repos/turfptax/cortex-vision/releases/latest
2. Compare tag_name against installed plugin.json.version
3. If newer: show "Update available (0.1.1)" badge in Plugins tab
4. User clicks Update:
   a. Download new .zip
   b. SHA256 verify
   c. Send SIGTERM to running cortex-vision.exe (graceful shutdown via lifespan handler)
   d. Wait for process exit (5s timeout, then SIGKILL)
   e. Backup current %APPDATA%/Cortex/plugins/cortex-vision/ to .bak
   f. Extract new bundle over the old location
   g. Spawn new cortex-vision.exe
   h. Poll health endpoint
5. On success: delete .bak. Toast: "Cortex Vision updated to 0.1.1"
6. On failure: restore from .bak. Toast: "Update failed, rolled back"
```

The `.bak` rollback handles bad updates without leaving the user broken. Same pattern as Steam, Chrome, etc.

## Uninstall flow

```
1. User clicks Uninstall in Plugins tab
2. Plugin manager:
   a. Confirm dialog: "Remove Cortex Vision? Your sessions and weights will be preserved."
   b. SIGTERM the running cortex-vision.exe
   c. Wait for exit
   d. Delete %APPDATA%/Cortex/plugins/cortex-vision/
   e. Remove entry from registry.json
3. Video tab disappears from nav

Sessions DB and weights cache are NOT deleted (user data invariant).
A separate "Delete all data" button purges %APPDATA%/Cortex/video/.
```

## Plugin registry (cortex-desktop side)

`%APPDATA%/Cortex/plugins/registry.json` tracks installed plugins:

```json
{
  "schema_version": 1,
  "plugins": {
    "cortex-vision": {
      "version": "0.1.0",
      "variant": "gpu",
      "install_dir": "C:\\Users\\you\\AppData\\Roaming\\Cortex\\plugins\\cortex-vision",
      "executable": "cortex-vision.exe",
      "port": 8004,
      "auto_start": true,
      "installed_at": "2026-05-05T14:23:01Z",
      "last_health_check": "2026-05-05T15:00:00Z"
    }
  }
}
```

cortex-desktop reads this on startup, spawns each `auto_start: true` plugin, and registers proxy routes per `api_prefix`.

## Port allocation

To avoid conflicts when multiple plugins are installed:

| Service                       | Port  |
|-------------------------------|-------|
| cortex-desktop (Hub UI)       | 8003  |
| cortex-vision (default)       | 8004  |
| Future plugin slot            | 8005  |
| Future plugin slot            | 8006  |
| ...                           | ...   |
| cortex-core (Pi, remote)      | 8420  |

The plugin manager reads `default_port` from the plugin's manifest. If that port is taken by another plugin or system process, it picks the next free port in 8004-8099 and writes the assigned port to `registry.json`. The proxy in cortex-desktop reads from the registry, so dynamic port assignment is transparent to the user.

## Running the sidecar on a different machine

This falls out for free. User edits `registry.json` to point at a remote host:

```json
{
  "cortex-vision": {
    "host": "10.0.0.42",
    "port": 8004,
    "auto_start": false
  }
}
```

`auto_start: false` tells cortex-desktop not to spawn a local process. The proxy now hits `http://10.0.0.42:8004`. The user runs `cortex-vision.exe` themselves on the remote machine (with `--host 0.0.0.0`). Use case: thin laptop runs cortex-desktop, GPU desktop runs cortex-vision.

In a Phase 6 polish, this becomes a settings UI option: "Run vision plugin: Local | Remote (host:port)".

## Dev mode

For developers running both repos from source:

```powershell
# Terminal 1 — cortex-vision sidecar
cd C:\dev\ttx\Cortex\cortex-vision
.\.venv\Scripts\activate
pip install -e .[dev]
python -m cortex_vision serve --port 8004

# Terminal 2 — cortex-desktop
cd C:\dev\ttx\Cortex\cortex-desktop
.\.venv\Scripts\activate
python -m cortex_desktop
```

cortex-desktop's plugin manager detects there's no installed bundle for cortex-vision but a process is already responding on the configured port — treats it as "external dev instance" and proxies normally. Same registry.json, just `auto_start: false` and the developer manages the process.

## CI: release checklist

When cutting a release tag:

1. [ ] Bump version in `pyproject.toml`, `cortex_vision/__init__.py`, `plugin.json`
2. [ ] Update `CHANGELOG.md`
3. [ ] `pytest` passes
4. [ ] Tag commit `vX.Y.Z`, push
5. [ ] GitHub Actions builds CPU + GPU bundles, computes SHA256, attaches to release
6. [ ] Manual smoke test: install via cortex-desktop's plugin manager, run a test video through batch mode
7. [ ] Publish release notes including SHA256 sums

## Security posture

- **Default bind: 127.0.0.1.** Sidecar is not exposed to the network unless the user explicitly opts in via `--host 0.0.0.0`.
- **No auth between cortex-desktop and the sidecar.** Localhost is treated as trusted, same as cortex-desktop's own backend. If someone has localhost code execution, they already have everything.
- **SHA256 verification of downloaded zips.** Published in release notes; checked before extraction.
- **Code signing: deferred.** Costs $300+/year and Windows SmartScreen still complains for the first 100 downloads. Revisit when there's a stable user base. In the meantime, document the "More info → Run anyway" workaround.

## What's NOT in this design

Explicitly out of scope, listed so we don't accidentally creep them in:

- **License server / DRM.** Cortex is open / public domain. No entitlement check, no license keys. The download is free; running it is free.
- **Auto-update of cortex-desktop itself.** That's cortex-desktop's existing problem and is unaffected by this design.
- **Plugin sandboxing.** Plugins run with full user privileges. This is the same trust model as VS Code extensions and OBS plugins; appropriate for a single-user desktop tool.
- **Multi-arch builds.** Windows x64 only at v1. Linux/macOS later if there's demand.
