# cortex-vision — Data Model

## Pydantic schemas

Defined in `cortex_vision/models/schemas.py`. These are the contract between the pipeline, the storage layer, and the FastAPI router.

```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

class TranscriptEntry(BaseModel):
    """One audio chunk's transcription."""
    timestamp: datetime
    text: str
    duration_s: float
    latency_ms: int = 0
    rms: float = 0.0          # for silence-filter audit
    chunk_index: int = 0      # ordering tiebreaker

class SceneEntry(BaseModel):
    """One detected scene with description and metadata."""
    index: int
    start_s: float
    end_s: float
    keyframe_paths: list[str] = Field(default_factory=list)  # 1-3 jpegs
    description: str = ""
    describer_model: str = ""                                # e.g. "lmstudio:smolvlm2-2.2b"
    spoken_text: str | None = None                           # transcript window
    objects: list[str] = Field(default_factory=list)         # YOLO detections (live only)
    similarity: float = 1.0                                  # detector value at trigger
    trigger_method: str = "scheduled"                        # histogram | pixel | structural | scheduled

class VideoSession(BaseModel):
    """A complete video processing run. The unit of work and persistence."""
    id: str                                                  # uuid
    mode: Literal["live", "file", "journal"]
    source: dict                                             # {url, file, capture_device, ...}
    status: Literal[
        "queued", "capturing", "processing",
        "describing", "narrating", "complete", "error"
    ] = "queued"
    project_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: float | None = None
    scenes: list[SceneEntry] = Field(default_factory=list)
    narrative: str | None = None
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    pushed_to_overseer: bool = False
    error: str | None = None
    progress: dict = Field(default_factory=dict)             # {current_scene: 4, total_scenes: 12}
```

## SQLite schema

`%APPDATA%/Cortex/video/sessions.db` (Windows) or `~/.local/share/cortex/video/sessions.db` (Unix). Tables defined in `cortex_vision/storage/db.py` with idempotent migrations (PRAGMA table_info checks before each ALTER, same pattern as VideoIndex's `lib/catalog.py`).

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,                 -- live | file | journal
    source_json     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    project_id      TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_s      REAL,
    narrative       TEXT,
    pushed_to_overseer INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    progress_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project_id ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_mode_status ON sessions(mode, status);

CREATE TABLE IF NOT EXISTS scenes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    scene_index     INTEGER NOT NULL,
    start_s         REAL NOT NULL,
    end_s           REAL NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    describer_model TEXT NOT NULL DEFAULT '',
    spoken_text     TEXT,
    objects_json    TEXT,
    similarity      REAL NOT NULL DEFAULT 1.0,
    trigger_method  TEXT NOT NULL DEFAULT 'scheduled',
    keyframes_json  TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, scene_index)
);

CREATE INDEX IF NOT EXISTS idx_scenes_session_id ON scenes(session_id);

CREATE TABLE IF NOT EXISTS transcripts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    text            TEXT NOT NULL,
    duration_s      REAL NOT NULL,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    rms             REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_transcripts_session_id ON transcripts(session_id);
```

Why split scenes / transcripts into their own tables instead of stuffing JSON into sessions? Querying. "Show me every scene from this week tagged to project X" wants a table scan, not 200 JSON parses. SQLite handles 100k scene rows trivially.

## Filesystem layout

`%APPDATA%/Cortex/video/` is the root.

```
%APPDATA%/Cortex/video/
├── sessions.db                              # SQLite from above
├── sessions/
│   └── <session_id>/                        # one dir per session
│       ├── source.<ext>                     # downloaded video (Mode 2) or recording (Mode 3)
│       ├── audio.wav                        # extracted/captured audio (optional)
│       ├── frames/
│       │   ├── 0/                           # scene 0
│       │   │   ├── 0.jpg
│       │   │   ├── 1.jpg
│       │   │   └── 2.jpg
│       │   ├── 1/                           # scene 1
│       │   │   └── ...
│       │   └── ...
│       └── meta.json                        # full VideoSession dump (mirrors SQLite, for portability)
└── tmp/                                     # in-progress recordings before they get a session id
```

Why also write `meta.json` if SQLite has it? Two reasons:
1. **Portability:** copying a session dir to another machine just works — no DB migration.
2. **Recovery:** if the SQLite file gets corrupted, we can rebuild from the `meta.json` files.

## Source dict by mode

The `source` field is mode-dependent. Convention:

```python
# Mode: file (URL via yt-dlp)
{"kind": "url", "url": "https://www.tiktok.com/@..."}

# Mode: file or journal (uploaded blob from browser MediaRecorder)
{"kind": "upload", "filename": "journal-2026-05-05.webm"}

# Mode: live (Phase 4)
{"kind": "obs_camera", "device": "OBS Virtual Camera", "resolution": [384, 216]}
{"kind": "capture_card", "device": "Elgato HD60", "resolution": [1280, 720]}
```

The `kind` field discriminates; the rest is mode-specific. Validated by Pydantic via a discriminated union if it's worth the boilerplate (probably not at v1).

### Journal mode strategy

Phase 3 journal mode uses **client-side recording** via the browser's
`getDisplayMedia()` + `getUserMedia()` APIs. The recorded blob is uploaded to
`POST /api/video/jobs/upload`, which writes it to
`<session_id>/source.<ext>` and runs the same Phase 1 batch pipeline against
it. No server-side ffmpeg needed for journal mode (that's reserved for Phase 4
live capture).

This is why `source.kind == "upload"` and `source.kind == "screen_recording"`
collapse into a single shape — the recording happened on the client, the
server just sees the resulting file.

## Status transitions

Valid transitions (enforced in `SessionManager.update_status()`):

```
queued ──> capturing ──> processing ──> describing ──> narrating ──> complete
   │           │             │              │              │
   └───────────┴─────────────┴──────────────┴──────────────┴────> error
```

Any state can transition to `error`. Once `complete` or `error`, no further transitions.

For live mode, `capturing` is the long-lived state — describing/narrating happen incrementally. The terminal state is set when the user clicks Stop or the process is killed.

## Overseer push contract

When pushing to overseer, this is the shape that goes through the bridge:

```python
# Per scene (live mode)
{
    "text": "[14:23] working in VS Code, scrolling video plugin design doc, terminal visible bottom-right\nSpoken: I think we should split this into a separate package",
    "tags": ["video", "session:abc-123", "mode:live"],
    "project_id": "cortex-vision",
    "attachment_path": "C:/Users/.../sessions/abc-123/frames/4/0.jpg",
    "timestamp": "2026-05-05T14:23:01",
}

# Per session segment (live, every 5 min)
{
    "text": "<narrative paragraph>",
    "scene_count": 12,
    "thumbnails": ["C:/.../frames/0/0.jpg", "C:/.../frames/1/0.jpg", ...],
    "project_id": "cortex-vision",
    "tags": ["video-rollup", "session:abc-123"],
}

# Per completed batch (file mode)
{
    "text": "<full narrative>",
    "scene_count": 28,
    "thumbnails": [...],
    "project_id": null,
    "tags": ["video-batch", "source:https://www.tiktok.com/@..."],
}
```

The bridge in cortex-desktop adapts these to whatever shape `pi_client.send_note()` and `pi_client.send_session_segment()` accept.
