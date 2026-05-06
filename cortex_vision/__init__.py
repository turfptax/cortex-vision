"""cortex-vision — video understanding pipeline for the Cortex ecosystem.

Three modes:
    - live:    OBS Virtual Camera + scene detection + real-time description
    - file:    yt-dlp / local file -> scenes -> description -> narrative
    - journal: screen + mic recording -> same as file mode

All three produce a VideoSession with scene keyframes, descriptions,
optional audio transcript, and a narrative rollup.

This package runs as a sidecar HTTP service. See server.py for the entry point.

See docs/DESIGN.md for the architecture rationale and docs/DISTRIBUTION.md for
the install/update mechanism.
"""

__version__ = "0.3.2"

# Core schemas — exported so cortex-desktop tooling and tests can import them
# even though the desktop runtime communicates over HTTP, not direct import.
from cortex_vision.models.schemas import (
    VideoSession,
    SceneEntry,
    TranscriptEntry,
)
from cortex_vision.pipeline.session_manager import (
    SessionManager,
    SessionTransitionError,
)
from cortex_vision.pipeline.batch import run_batch_pipeline

__all__ = [
    "__version__",
    "VideoSession",
    "SceneEntry",
    "TranscriptEntry",
    "SessionManager",
    "SessionTransitionError",
    "run_batch_pipeline",
]
