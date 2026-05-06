# cortex-vision — cortex-desktop Integration

How the desktop app wires up the vision plugin via the sidecar service model. Read alongside [DESIGN.md](DESIGN.md) and [DISTRIBUTION.md](DISTRIBUTION.md).

## Architecture recap

cortex-vision runs as a separate process (`cortex-vision.exe`) on `localhost:8004`. cortex-desktop's `routers/video.py` is an HTTP proxy. cortex-desktop never imports `cortex_vision` directly — it can't, because it's a frozen PyInstaller bundle without `pip`.

```
+-------------------------+              +-----------------------------+
|  cortex-desktop.exe     |   HTTP       |  cortex-vision.exe          |
|  port 8003              +------------->+  port 8004                  |
|                         |              |                             |
|  routers/video.py       |              |  cortex_vision/server.py    |
|     proxies to ->       |              |                             |
|                         |              +-----------------------------+
|  services/                                          ^
|    plugin_manager.py    +---------- spawns ---------+
+-------------------------+
```

## Files added to cortex-desktop

```
cortex-desktop/
  hub/backend/
    routers/
      video.py                       # NEW — HTTP proxy, ~80 lines
    services/
      plugin_manager.py              # NEW — sidecar lifecycle (install/start/stop/health)
      video_overseer_bridge.py       # NEW — converts SceneEntry -> overseer note/session (Phase 2)
  hub/frontend/src/
    components/
      settings/
        PluginsTab.tsx               # NEW — install/update/uninstall plugins UI
      video/
        VideoPage.tsx                # NEW — page entry, tab bar
        LiveMode.tsx                 # NEW — Phase 4
        FileMode.tsx                 # NEW — Phase 1
        JournalMode.tsx              # NEW — Phase 3
        SessionList.tsx              # NEW — past sessions browser
        SceneTimeline.tsx            # NEW — shared scene grid component
        NarrativePanel.tsx           # NEW — shared narrative display
    lib/
      videoApi.ts                    # NEW — typed proxy client
```

## Files modified in cortex-desktop

```
hub/backend/main.py                  # +1 line: include video router; +plugin_manager startup
hub/backend/config.py                # +plugin registry path setting
hub/frontend/src/App.tsx             # +'video' to Page union (gated on plugin registry)
hub/frontend/src/components/Layout.tsx  # +1 nav item (gated)
hub/frontend/src/components/settings/SettingsPage.tsx  # +Plugins subtab
```

No changes to cortex-desktop's `pyproject.toml` — we are NOT pip-depending on cortex-vision. The sidecar talks over HTTP only.

## The proxy router

`routers/video.py` is the entire integration surface from cortex-desktop's side:

```python
"""Video plugin proxy — forwards /api/video/* to the cortex-vision sidecar."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx

from services.plugin_manager import get_plugin

router = APIRouter()
PLUGIN_ID = "cortex-vision"


def _sidecar_base() -> str:
    plugin = get_plugin(PLUGIN_ID)
    if not plugin or not plugin.is_running:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "plugin_not_running",
                "plugin": PLUGIN_ID,
                "message": "Cortex Vision plugin is not installed or not running.",
                "install_url": "/settings/plugins",
            },
        )
    return f"http://{plugin.host}:{plugin.port}"


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(full_path: str, request: Request) -> StreamingResponse:
    """Forward any /api/video/* request to the sidecar verbatim."""
    base = _sidecar_base()
    url = f"{base}/api/video/{full_path}"

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=2.0)) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
                params=request.query_params,
            )
        except httpx.ConnectError:
            raise HTTPException(503, "Cortex Vision sidecar is not responding.")

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
```

That's it — no business logic. Adding a new endpoint to cortex-vision automatically becomes available to the frontend with zero changes here.

### WebSocket pass-through (Phase 4)

Phase 4 introduces `WS /api/video/live/ws` — a one-way event stream from the
sidecar to the browser. The HTTP-only proxy above won't carry WebSocket
traffic. Add a sibling handler:

```python
import asyncio
import websockets
from fastapi import WebSocket, WebSocketDisconnect

@router.websocket("/live/ws")
async def proxy_live_ws(client_ws: WebSocket) -> None:
    """Bridge a browser WebSocket to the sidecar's live event stream."""
    plugin = get_plugin(PLUGIN_ID)
    if not plugin or not plugin.is_running:
        await client_ws.close(code=1011, reason="Cortex Vision plugin not running")
        return

    upstream_url = f"ws://{plugin.host}:{plugin.port}/api/video/live/ws"
    await client_ws.accept()

    try:
        async with websockets.connect(upstream_url) as upstream:
            async def upstream_to_client():
                async for msg in upstream:
                    await client_ws.send_text(msg)

            async def client_to_upstream():
                while True:
                    msg = await client_ws.receive_text()
                    await upstream.send(msg)

            await asyncio.gather(
                upstream_to_client(),
                client_to_upstream(),
                return_exceptions=True,
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        await client_ws.close(code=1011, reason="upstream sidecar error")
```

Add `websockets>=12` to cortex-desktop's `pyproject.toml` if it isn't already
there (it's transitively present via `uvicorn[standard]`, but the explicit
import is cleaner).

The sidecar's WS protocol — what flows through this bridge — is documented
in `cortex_vision/pipeline/live.py`'s module docstring. Event types include
`started`, `scene`, `described`, `stats`, `stopped`, `error`. Frontend
should treat unknown types as forward-compatible and ignore them.

## The plugin manager

`services/plugin_manager.py` handles sidecar lifecycle. Methods:

```python
class PluginManager:
    def list_installed(self) -> list[InstalledPlugin]: ...
    def install(self, plugin_id: str, variant: str = "auto", version: str = "latest") -> InstalledPlugin: ...
    def update(self, plugin_id: str) -> InstalledPlugin: ...
    def uninstall(self, plugin_id: str, keep_user_data: bool = True) -> None: ...
    def start(self, plugin_id: str) -> None: ...
    def stop(self, plugin_id: str, graceful: bool = True) -> None: ...
    def health(self, plugin_id: str) -> PluginHealth: ...
    def check_updates(self, plugin_id: str | None = None) -> dict[str, str | None]: ...
```

Reads/writes `%APPDATA%/Cortex/plugins/registry.json`. Each entry tracks install dir, version, port, process handle, last health check.

On cortex-desktop startup:
1. `PluginManager.__init__()` reads `registry.json`
2. For each entry with `auto_start: true`, spawns `<install_dir>/<executable>`
3. Polls health endpoint until ready (or marks unhealthy after 30s)
4. cortex-desktop's frontend reads the plugin list to know which nav items to render

On cortex-desktop shutdown:
1. SIGTERM each running plugin (graceful — sidecar's lifespan handler closes DB cleanly)
2. Wait up to 5s for exit
3. SIGKILL any stragglers

## The Plugins tab UI

`components/settings/PluginsTab.tsx` is the user-facing surface for plugin lifecycle:

```
+-----------------------------------------------------------------+
| Plugins                                                         |
+-----------------------------------------------------------------+
|                                                                 |
|  [Cortex Vision]    [v0.1.0]  [GPU bundle]  [Running] [green]   |
|     Process videos, watch your screen live, video journals.     |
|     [Update available: v0.1.1]  [Update] [Uninstall] [Restart]  |
|                                                                 |
|  [Cortex Whisper]   Not installed                               |
|     Audio-only transcription plugin (placeholder)               |
|     [Install]                                                   |
|                                                                 |
+-----------------------------------------------------------------+
```

Wired to:
- `GET /api/plugins` — list installed
- `POST /api/plugins/install` — `{plugin_id, variant, version}`
- `POST /api/plugins/{id}/update` — pull latest
- `DELETE /api/plugins/{id}` — uninstall
- `POST /api/plugins/{id}/restart` — stop + start

These endpoints live in cortex-desktop, not cortex-vision (they manage cortex-vision, they don't run inside it).

## The overseer bridge

The bridge lives in cortex-desktop because it needs `pi_client` to push to the Pi. cortex-vision emits structured `SceneEntry` data over HTTP; cortex-desktop converts and forwards.

Two integration patterns:

**Polling (Phase 2 default):** cortex-desktop polls `GET /api/video/sessions?status=complete&pushed=false` every 30s. For each unpushed session, fetches it, converts to overseer notes/sessions via `pi_client`, then `POST /api/video/sessions/{id}/mark-pushed` to flip the flag.

**Push (Phase 4 for live mode):** cortex-vision opens an outbound WebSocket back to `cortex-desktop://localhost:8003/api/video/events`. On each scene change, the sidecar sends a JSON message; cortex-desktop's bridge picks it up and pushes to overseer in near-real-time.

The polling pattern is simpler and works for batch + journal modes. Live mode benefits from push to keep latency low between "scene changes" and "appears in overseer."

```python
# services/video_overseer_bridge.py — polling implementation
async def poll_and_push():
    """Background task — runs in cortex-desktop's main event loop."""
    while True:
        async with httpx.AsyncClient() as client:
            sessions = await client.get(
                "http://localhost:8004/api/video/sessions?status=complete&pushed=false"
            )
            for s in sessions.json():
                full = await client.get(f"http://localhost:8004/api/video/sessions/{s['id']}")
                _push_session_to_overseer(full.json())
                await client.post(
                    f"http://localhost:8004/api/video/sessions/{s['id']}/mark-pushed"
                )
        await asyncio.sleep(30)


def _push_session_to_overseer(session: dict) -> None:
    if session["mode"] == "journal":
        pi_client.append_to_journal(
            scenes=session["scenes"],
            transcript=session["transcript"],
            narrative=session["narrative"],
        )
    else:
        pi_client.send_session_segment(
            text=session["narrative"] or "",
            scene_count=len(session["scenes"]),
            thumbnails=[s["keyframe_paths"][0] for s in session["scenes"][:6]],
            project_id=session.get("project_id"),
            tags=["video", f"mode:{session['mode']}"],
        )
```

## Frontend page wiring

`VideoPage.tsx` is a tab bar over the three modes:

```tsx
import { useState } from 'react'
import { LiveMode } from './LiveMode'
import { FileMode } from './FileMode'
import { JournalMode } from './JournalMode'
import { SessionList } from './SessionList'

type VideoTab = 'live' | 'file' | 'journal' | 'history'

export function VideoPage() {
  const [tab, setTab] = useState<VideoTab>('file')
  return (
    <div>
      <nav className="tabs">
        <button onClick={() => setTab('live')}>Live (OBS)</button>
        <button onClick={() => setTab('file')}>Process video</button>
        <button onClick={() => setTab('journal')}>Video journal</button>
        <button onClick={() => setTab('history')}>History</button>
      </nav>
      {tab === 'live' && <LiveMode />}
      {tab === 'file' && <FileMode />}
      {tab === 'journal' && <JournalMode />}
      {tab === 'history' && <SessionList />}
    </div>
  )
}
```

`App.tsx` modifications — the page is gated on the plugin being installed:

```tsx
import { useInstalledPlugins } from './hooks/useInstalledPlugins'
import { VideoPage } from './components/video/VideoPage'

export type Page = 'chat' | 'pi' | 'data' | 'overseer' | 'video' | 'settings'

function App() {
  const { plugins } = useInstalledPlugins()
  const videoInstalled = plugins.some(p => p.id === 'cortex-vision' && p.running)
  // ...
  {page === 'video' && (videoInstalled ? <VideoPage /> : <PluginInstallCTA pluginId="cortex-vision" />)}
}
```

## Dev mode setup

Both repos cloned as siblings under `C:\dev\ttx\Cortex\`:

```powershell
# Terminal 1 — cortex-vision sidecar
cd C:\dev\ttx\Cortex\cortex-vision
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev,cpu]
python -m cortex_vision serve --port 8004

# Terminal 2 — cortex-desktop
cd C:\dev\ttx\Cortex\cortex-desktop
.\.venv\Scripts\activate
python -m cortex_desktop
```

cortex-desktop's plugin manager detects there's no installed bundle but a process is responding on the configured port (8004) — treats it as `auto_start: false` external dev instance, proxies normally, and shows a "Dev mode" badge in the Plugins tab.

To skip the plugin manager entirely during development, edit `%APPDATA%/Cortex/plugins/registry.json`:

```json
{
  "schema_version": 1,
  "plugins": {
    "cortex-vision": {
      "version": "dev",
      "host": "127.0.0.1",
      "port": 8004,
      "auto_start": false,
      "executable": null
    }
  }
}
```

## Testing the integration

After Phase 0:

```powershell
# Terminal 1
cd C:\dev\ttx\Cortex\cortex-vision
python -m cortex_vision serve

# Terminal 2 (other shell)
curl http://localhost:8004/api/video/health
# {"status":"ok","version":"0.1.0","db_path":"..."}

curl http://localhost:8004/api/video/manifest
# {"id":"cortex-vision","name":"Cortex Vision",...}

curl http://localhost:8004/api/video/sessions
# []
```

After Phase 0 + cortex-desktop proxy:

```powershell
# Terminal 1: cortex-vision running on :8004
# Terminal 2: cortex-desktop running on :8003
curl http://localhost:8003/api/video/health
# {"status":"ok",...}    <- proxied through
```

After Phase 1:

```powershell
curl -X POST http://localhost:8003/api/video/jobs `
  -H "Content-Type: application/json" `
  -d '{"source":"https://www.tiktok.com/@user/video/123","mode":"file"}'
# {"session_id":"abc-...","status":"queued"}
```
