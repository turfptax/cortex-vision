# Handoff — cortex-desktop integration for cortex-vision

**Audience:** The team working on `cortex-desktop`.
**Goal:** Make `cortex-desktop` aware of plugin sidecars so the upcoming `cortex-vision` plugin (and future ones) can be installed, started, monitored, and proxied through to.
**Scope:** Phase 0 of the cortex-vision roadmap — the smallest set of changes that lets a video plugin show up in the UI and respond to requests.
**Estimated effort:** ~3-5 focused days for Phase 0 cortex-desktop work. Phase 1+ work is on the cortex-vision side and unblocks once Phase 0 lands.

---

## TL;DR

We're adding video processing (live screen capture, recorded video summarization, video journals) as a **sidecar service plugin**. The plugin runs as its own `cortex-vision.exe` on `localhost:8004`. cortex-desktop needs three things:

1. A **proxy router** at `/api/video/*` that forwards to the sidecar (~80 lines)
2. A **plugin manager service** that handles install/start/stop/health/update for sidecar plugins (~400 lines)
3. A **Plugins tab** in Settings that lists installed plugins and offers install/update buttons

That's it for Phase 0. Pipeline implementation, UI for video modes, and overseer integration come in later phases on the cortex-vision side.

---

## Why a sidecar (and not just embedded)

cortex-desktop ships as a frozen PyInstaller bundle. There's no `pip` inside the .exe, no writable site-packages, no way for end users to install Python packages into it at runtime. So vision (and any future heavy plugin) has to live in its own process.

This is the same model cortex-desktop already uses for cortex-core on the Pi (HTTP at `:8420`). We're just adding a localhost peer at `:8004`. Building a proper plugin manager now also unblocks every future plugin — they all use the same pattern.

Key benefits:
- cortex-desktop updates stay ~50 MB; vision updates are independent
- A CUDA crash in vision can't take down the system tray
- Power users can run vision on a remote GPU machine and proxy to it
- No PyInstaller dependency tangle between cortex-desktop and vision deps (torch, opencv, etc.)

Full rationale: [docs/DESIGN.md](docs/DESIGN.md) and [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md).

---

## What you need to build (Phase 0)

### 1. `hub/backend/routers/video.py` — the proxy

A new router that forwards every `/api/video/*` request to the sidecar. No business logic.

**Behavior:**
- All HTTP methods (GET/POST/PUT/DELETE/PATCH) supported via `@router.api_route(..., methods=[...])`
- Forwards path, headers (minus `Host`), query params, and body verbatim to `http://<plugin.host>:<plugin.port>/api/video/<path>`
- Returns the upstream response with same status + headers + body
- WebSocket support deferred to Phase 4 (live mode); document the gap
- If the sidecar isn't running or doesn't respond, return **503** with a structured error body that the frontend can render as "click here to install":
  ```json
  {
    "error": "plugin_not_running",
    "plugin": "cortex-vision",
    "install_url": "/settings/plugins"
  }
  ```

A working sample is in [docs/INTEGRATION.md § "The proxy router"](docs/INTEGRATION.md#the-proxy-router) — you can copy that almost verbatim.

**Mount in `hub/backend/main.py`:**

```python
from routers import video
app.include_router(video.router, prefix="/api/video", tags=["video"])
```

### 2. `hub/backend/services/plugin_manager.py` — the lifecycle service

This is the meat of Phase 0. Manages installed plugins: registry persistence, subprocess lifecycle, health polling, install/update/uninstall flows.

**Required class shape:**

```python
class PluginManager:
    def __init__(self, registry_path: Path | None = None) -> None: ...

    # Lifecycle
    def list_installed(self) -> list[InstalledPlugin]: ...
    def get(self, plugin_id: str) -> InstalledPlugin | None: ...
    def start(self, plugin_id: str) -> None: ...
    def stop(self, plugin_id: str, graceful: bool = True) -> None: ...
    def restart(self, plugin_id: str) -> None: ...

    # Health
    async def health(self, plugin_id: str) -> PluginHealth: ...
    async def health_loop(self) -> None: ...    # background task

    # Install / update / uninstall (Phase 5 finishes the GitHub release wiring;
    # Phase 0 just stub these or implement against a local zip path)
    async def install(self, plugin_id: str, variant: str = "auto",
                      version: str = "latest") -> InstalledPlugin: ...
    async def update(self, plugin_id: str) -> InstalledPlugin: ...
    def uninstall(self, plugin_id: str, keep_user_data: bool = True) -> None: ...
    async def check_updates(self, plugin_id: str | None = None) -> dict[str, str | None]: ...
```

**Registry file:** `%APPDATA%/Cortex/plugins/registry.json`

```json
{
  "schema_version": 1,
  "plugins": {
    "cortex-vision": {
      "version": "0.1.0",
      "variant": "gpu",
      "install_dir": "C:\\Users\\you\\AppData\\Roaming\\Cortex\\plugins\\cortex-vision",
      "executable": "cortex-vision.exe",
      "host": "127.0.0.1",
      "port": 8004,
      "auto_start": true,
      "installed_at": "2026-05-05T14:23:01Z",
      "last_health_check": "2026-05-05T15:00:00Z"
    }
  }
}
```

**Spawning behavior:**
- Use `subprocess.Popen` with `creationflags=CREATE_NO_WINDOW` on Windows (no console flicker)
- Capture stdout/stderr to `%APPDATA%/Cortex/plugins/<id>/logs/` (rotating file, 5 MB cap)
- Track the PID; on health-loop tick, verify the PID is still alive AND the health endpoint responds
- On graceful stop: send SIGTERM, wait 5s for exit, then SIGKILL stragglers
- Health endpoint URL comes from the plugin manifest (`health_endpoint` field)

**Startup hook in `hub/backend/main.py`:**

```python
from services.plugin_manager import PluginManager

@app.on_event("startup")
async def start_plugins():
    app.state.plugins = PluginManager()
    for plugin in app.state.plugins.list_installed():
        if plugin.auto_start:
            try:
                app.state.plugins.start(plugin.id)
            except Exception as e:
                logger.error("Failed to start plugin %s: %s", plugin.id, e)
    asyncio.create_task(app.state.plugins.health_loop())

@app.on_event("shutdown")
async def stop_plugins():
    if hasattr(app.state, "plugins"):
        for plugin in app.state.plugins.list_installed():
            app.state.plugins.stop(plugin.id, graceful=True)
```

**Process manager reuse:** if `services/process_manager.py` is suitable for plugin lifecycle, reuse it. If it's tied too closely to the LoRA training pipeline, plugin_manager can be its own thing — just don't reinvent if the existing one fits.

### 3. `hub/backend/routers/plugins.py` — plugin admin API

Endpoints the Plugins tab calls:

| Method | Path                                | Body / params                   | Returns                  |
|--------|-------------------------------------|--------------------------------|--------------------------|
| GET    | `/api/plugins`                      | —                              | `list[InstalledPlugin]`  |
| GET    | `/api/plugins/marketplace`          | —                              | `list[AvailablePlugin]`  |
| POST   | `/api/plugins/install`              | `{plugin_id, variant, version}`| `InstalledPlugin`        |
| POST   | `/api/plugins/{id}/update`          | —                              | `InstalledPlugin`        |
| DELETE | `/api/plugins/{id}`                 | `?keep_user_data=true`         | `{"ok": true}`           |
| POST   | `/api/plugins/{id}/restart`         | —                              | `InstalledPlugin`        |
| GET    | `/api/plugins/{id}/health`          | —                              | `PluginHealth`           |
| GET    | `/api/plugins/check-updates`        | —                              | `{plugin_id: latest_version}` |

For the **marketplace** in Phase 0: a static list hardcoded in cortex-desktop (just `cortex-vision` for now). Future: pull from a registry JSON hosted on GitHub Pages.

```python
MARKETPLACE = [
    {
        "id": "cortex-vision",
        "name": "Cortex Vision",
        "description": "Process videos, watch your screen live, record video journals.",
        "github_repo": "turfptax/cortex-vision",
        "manifest_url": "https://raw.githubusercontent.com/turfptax/cortex-vision/main/plugin.json",
    },
]
```

### 4. `hub/frontend/src/components/settings/PluginsTab.tsx` — the UI

A new subtab in the existing Settings page. Lists installed plugins (status dot, version, variant, action buttons) and available marketplace plugins (with Install button).

**Mockup:**

```
+-------------------------------------------------------------------+
| Settings > Plugins                                                |
+-------------------------------------------------------------------+
|                                                                   |
|  INSTALLED                                                        |
|                                                                   |
|  [Cortex Vision]    v0.1.0 GPU  [GREEN] Running                   |
|     Process videos, watch your screen live, record journals.      |
|     Last health: 2s ago                                           |
|     [ Update available: v0.1.1 ]  [Restart] [Uninstall]           |
|                                                                   |
|  AVAILABLE                                                        |
|                                                                   |
|  [Cortex Vision] (already installed)                              |
|                                                                   |
+-------------------------------------------------------------------+
```

For Phase 0: the Install button can be a stub that just shows "Coming in Phase 5" or accepts a local zip path for testing. Real GitHub-release installs land in Phase 5 once cortex-vision has a release published.

### 5. Wiring the Video page (gated)

In `App.tsx`:

```tsx
import { useInstalledPlugins } from './hooks/useInstalledPlugins'

export type Page = 'chat' | 'pi' | 'data' | 'overseer' | 'video' | 'settings'

const { plugins } = useInstalledPlugins()
const visionInstalled = plugins.find(p => p.id === 'cortex-vision')?.running

// nav: only show Video item if visionInstalled (or grey it out with a CTA)
```

The actual `VideoPage.tsx`, `LiveMode.tsx`, etc. components will be delivered as part of Phase 1+ on the cortex-vision side. **You don't need to write them in Phase 0.** Just leave the routing slot ready.

---

## What you should NOT do

These would conflict with the architecture. Please flag if any of them feel right and you think the design needs revisiting — better to discuss now than refactor later.

- **Don't `pip install cortex-vision` into the cortex-desktop bundle.** It defeats the entire architecture. The whole reason for the sidecar is that the frozen PyInstaller bundle can't host runtime plugins.
- **Don't add `import cortex_vision` anywhere in cortex-desktop's code.** The integration is pure HTTP. Direct imports would create a tight coupling that breaks the bundle.
- **Don't add license / entitlement checks to the Plugins tab or proxy.** cortex-vision is open source and free — no gating. The plugin manager pattern can support gating in the future for a different plugin if needed; we're not building it now.
- **Don't add other routes under `/api/video/*` in cortex-desktop.** That URL space is owned by the proxy. Anything cortex-desktop needs to expose about video should live at `/api/plugins/cortex-vision/...` or just be implicit in the proxy.
- **Don't claim port 8004 for anything else.** It's reserved for cortex-vision. Future plugins get 8005, 8006, etc. Plugin manager assigns dynamically if 8004 is taken (write the assigned port back into registry.json).
- **Don't make the Video tab visible when cortex-vision isn't installed.** Either hide it or grey it with a "click to install" CTA. A broken page when the plugin is missing is worse than no page.

---

## The contract — what cortex-vision exposes

Sidecar API (the proxy forwards to these unchanged). Already implemented in cortex-vision Phase 0:

| Endpoint                                                | Status        | Purpose |
|---------------------------------------------------------|---------------|---------|
| `GET /api/video/health`                                 | working       | Plugin manager polls this to verify the sidecar is up |
| `GET /api/video/version`                                | working       | Plugin manager compares against latest GitHub release |
| `GET /api/video/manifest`                               | working       | Self-description; mirrors plugin.json |
| `GET /api/video/sessions`                               | working       | Returns `[]` on fresh install; lists past sessions |
| `GET /api/video/sessions/{id}`                          | working       | One session row |
| `GET /api/video/jobs/{id}/frame/{scene}/{frame}`        | working       | Raw JPEG keyframe |
| `POST /api/video/jobs`                                  | 501 (Phase 1) | Create batch job |
| `POST /api/video/live/start`                            | 501 (Phase 4) | Start live capture |
| `POST /api/video/live/stop`                             | 501 (Phase 4) | Stop live capture |
| `WS /api/video/live/ws`                                 | 501 (Phase 4) | Live event stream |

Sample health response:

```bash
curl http://127.0.0.1:8004/api/video/health
# {
#   "status": "ok",
#   "version": "0.1.0",
#   "db_path": "C:/Users/you/AppData/Roaming/Cortex/video/sessions.db"
# }
```

Sample manifest response (matches `plugin.json` in the cortex-vision repo):

```bash
curl http://127.0.0.1:8004/api/video/manifest
# {
#   "id": "cortex-vision",
#   "name": "Cortex Vision",
#   "version": "0.1.0",
#   "api_prefix": "/api/video",
#   "default_port": 8004,
#   "ui": {"page_id": "video", "label": "Video", "tabs": [...]},
#   "capabilities": ["batch", "live", "journal"],
#   "github_repo": "turfptax/cortex-vision"
# }
```

---

## Acceptance criteria — Phase 0 done when

1. `cortex-vision` (running as `python -m cortex_vision serve` from a sibling clone) is registered in `%APPDATA%/Cortex/plugins/registry.json` as `auto_start: false` (dev mode).
2. cortex-desktop starts, plugin_manager sees the registry entry, polls health, marks it running.
3. `curl http://localhost:8003/api/plugins` returns the cortex-vision entry with status running.
4. `curl http://localhost:8003/api/video/health` proxies through and returns 200.
5. `curl http://localhost:8003/api/video/sessions` proxies through and returns `[]`.
6. Plugins tab in Settings shows "Cortex Vision: Running" with a green dot, version 0.1.0.
7. Stopping the sidecar (`Ctrl+C` in its terminal) makes the dot turn red within ~5s.
8. Restarting the sidecar makes it turn green again within ~5s.
9. With cortex-vision NOT running, hitting `/api/video/health` returns 503 with the structured error body.

Phase 5 will additionally require: real install (download zip from GitHub release, extract, spawn), real update (replace files, restart), real uninstall (stop, delete, deregister). Those are deferred — for Phase 0 just stub them or test against local zips.

---

## Testing it end-to-end (dev mode)

Two terminals:

```powershell
# Terminal 1 — cortex-vision sidecar
cd C:\dev\ttx\Cortex\cortex-vision
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
python -m cortex_vision serve --port 8004
# Should log: "cortex-vision 0.1.0 starting on http://127.0.0.1:8004"
```

```powershell
# Terminal 2 — cortex-desktop
cd C:\dev\ttx\Cortex\cortex-desktop
.\.venv\Scripts\activate
python -m cortex_desktop
```

Before starting cortex-desktop, hand-edit `%APPDATA%/Cortex/plugins/registry.json` to register the sidecar in dev mode:

```json
{
  "schema_version": 1,
  "plugins": {
    "cortex-vision": {
      "version": "dev",
      "variant": "dev",
      "install_dir": null,
      "executable": null,
      "host": "127.0.0.1",
      "port": 8004,
      "auto_start": false,
      "installed_at": "2026-05-05T00:00:00Z"
    }
  }
}
```

Then in browser at `http://localhost:8003`:
- Settings → Plugins → see "Cortex Vision (dev) — Running"
- Top nav → Video tab visible (or shows the install CTA if you keep gating strict)
- Visit `/api/video/health` — proxies through

---

## Heads up — what's coming after Phase 0

So you can plan and avoid painting yourself into a corner:

| Phase | Owner | What changes for cortex-desktop |
|-------|-------|--------------------------------|
| 1: Batch pipeline | cortex-vision | `POST /api/video/jobs` becomes real; need a `FileMode.tsx` component (will be delivered or worked on collaboratively) |
| 2: Overseer push | cortex-desktop | Add `services/video_overseer_bridge.py` — polls cortex-vision for completed sessions, pushes to overseer via existing `pi_client` |
| 3: Video journal | cortex-vision | New `JournalMode.tsx`; bridge handles attaching to existing journal |
| 4: Live OBS | cortex-vision | WebSocket support needed in proxy; tray menu items "Start/Stop watching screen"; LiveMode.tsx |
| 5: PyInstaller release | cortex-vision | First `.exe` published to GitHub releases; plugin_manager install/update flows must work end-to-end |
| 6: Polish | both | Settings UI for describer/audio/thresholds; remote sidecar option |

**Key deferred ask for Phase 4:** the proxy router needs WebSocket pass-through (`@router.websocket(...)` + bidirectional bridge). Worth knowing now so the proxy abstraction doesn't lock that out.

---

## Reference

In the cortex-vision repo (sibling clone at `C:\dev\ttx\Cortex\cortex-vision\`):

| Doc | When to read |
|-----|-------------|
| [docs/DESIGN.md](docs/DESIGN.md) | If you want the architecture rationale |
| [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) | If you're building the install/update flow (Phase 5) — has full sequences and error handling |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | The most directly relevant doc — has working code samples for `routers/video.py`, `services/plugin_manager.py` shape, `PluginsTab.tsx` mockup |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | If you need to render scene/transcript data in cortex-desktop UI components later |
| [docs/ROADMAP.md](docs/ROADMAP.md) | For the full phase plan |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | Decisions log; questions with answers |
| [plugin.json](plugin.json) | The plugin manifest the manager reads on install |
| [cortex_vision/server.py](cortex_vision/server.py) | The actual sidecar — useful if you want to see what you're proxying to |

The fastest way to get oriented: `INTEGRATION.md` then this handoff. Both should fit in ~30 minutes of reading.

---

## Questions / decisions for the team

A few things to confirm before kicking off — none are blockers but worth aligning on:

1. **Reuse vs new for `process_manager.py`.** Does the existing one fit plugin lifecycle, or should `plugin_manager.py` be its own thing? Either is fine; just don't reinvent if the existing one fits.
2. **Marketplace registry location.** Phase 0 hardcodes the marketplace list in cortex-desktop. Future: pull from a registry JSON hosted somewhere (GitHub Pages? raw.githubusercontent.com?). Not blocking but worth deciding before Phase 5.
3. **Health check cadence.** Default 5s. Worth making configurable (env var or settings)? Default is fine for v1.
4. **Plugin registry schema versioning.** I included `schema_version: 1` in the registry. If you change the schema later, please bump and add a migration. Same idempotent pattern cortex-vision uses for SQLite.
5. **Logging convention.** Match whatever cortex-desktop already does — likely `logger = logging.getLogger("cortex.hub.plugins")` and route into the existing `cortex-hub.log` rotating file.

Reply on the cortex-vision issue tracker or ping in the team channel — happy to clarify or rev the design if any of this looks off.

---

## Ticket-ready breakdown

For copying into your issue tracker:

```
[ ] [cortex-desktop] Add services/plugin_manager.py
    Implement PluginManager class per docs/INTEGRATION.md.
    Registry persistence at %APPDATA%/Cortex/plugins/registry.json.
    Spawn/health/stop lifecycle. Stub install/update/uninstall (real implementations land in Phase 5).
    Acceptance: PluginManager().list_installed() returns registered dev plugins.

[ ] [cortex-desktop] Add routers/video.py proxy
    Forward /api/video/* to plugin sidecar via httpx.
    Return 503 with structured error when sidecar unreachable.
    Mount in main.py.
    Acceptance: curl through cortex-desktop returns same response as direct curl to sidecar.

[ ] [cortex-desktop] Add routers/plugins.py admin API
    Endpoints per HANDOFF.md "plugin admin API" table.
    Marketplace list hardcoded for now.
    Acceptance: GET /api/plugins lists installed plugins.

[ ] [cortex-desktop] Add components/settings/PluginsTab.tsx
    List installed plugins with status dot, version, action buttons.
    Show available marketplace plugins.
    Install button stub OK for Phase 0.
    Acceptance: tab loads, shows registered plugins, status dot updates within 5s of plugin start/stop.

[ ] [cortex-desktop] Gate Video page in App.tsx + Layout.tsx
    'video' in Page union; nav item shown only when cortex-vision is registered.
    VideoPage.tsx component itself comes from cortex-vision Phase 1.
    Acceptance: Video nav item appears/disappears based on plugin state.

[ ] [cortex-desktop] Reserve port 8004 / dynamic port allocation
    Default port from plugin manifest. If taken, scan 8004-8099 for free.
    Write assigned port back to registry.json.
    Acceptance: starting cortex-desktop with port 8004 already in use still works.
```
