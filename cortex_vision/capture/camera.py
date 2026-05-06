"""Camera capture for live mode (Phase 4).

Adapted from VisualFast/capture.py. Reads frames from any OpenCV-compatible
device (OBS Virtual Camera, webcam, capture card) with optional downsampling
and a small ring buffer.

Usage:
    from cortex_vision.capture.camera import FrameCapture, find_cameras

    cams = find_cameras()                                   # [0, 1, 2]
    with FrameCapture(camera_index=cams[0], resolution=(384, 216)) as cap:
        while True:
            frame = cap.read()
            if frame is None:
                break
            # frame.image -> (216, 384, 3) BGR ndarray
            # frame.timestamp -> perf_counter when captured

OBS Virtual Camera registers as a regular DirectShow device on Windows
(``cv2.CAP_DSHOW``) and a v4l2 loopback device on Linux. cv2.VideoCapture
picks both up via the same numeric index — no special handling required.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np


# Resolution presets — same naming as VisualFast for parity. "fast" is good
# enough for scene detection; "medium" is the right tradeoff for per-scene
# vision describer keyframes; "detail" is for high-resolution capture cards.
RESOLUTION_TIERS = {
    "fast":   (192, 108),
    "medium": (384, 216),
    "detail": (768, 432),
}


@dataclass
class Frame:
    """A single captured video frame plus metadata."""
    image: np.ndarray                       # downsampled BGR, shape (H, W, 3)
    timestamp: float                        # time.perf_counter() at read time
    index: int                              # sequential counter
    original_size: tuple[int, int]          # native (width, height)


class FrameCapture:
    """OpenCV-backed frame reader with downsampling and a small ring buffer.

    Supports the context manager protocol — recommended for clean teardown:

        with FrameCapture(camera_index=0) as cap:
            frame = cap.read()

    On Windows, prefers DirectShow for lower latency. Falls back to the
    default backend on other platforms.
    """

    def __init__(
        self,
        camera_index: int = 0,
        resolution: tuple[int, int] = (384, 216),
        buffer_size: int = 60,
    ) -> None:
        self.camera_index = camera_index
        self.target_w, self.target_h = resolution
        self.buffer: deque[Frame] = deque(maxlen=buffer_size)
        self.cap: cv2.VideoCapture | None = None
        self.frame_count = 0
        self._running = False
        self.native_resolution: tuple[int, int] = (0, 0)
        self.native_fps: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> bool:
        """Open the camera device. Returns True on success."""
        # DirectShow first on Windows; fall back to default backend
        self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.camera_index)
        if not self.cap.isOpened():
            self.cap = None
            return False

        self.native_resolution = (
            int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        self.native_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self._running = True
        return True

    def close(self) -> None:
        """Release the camera. Idempotent."""
        self._running = False
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> FrameCapture:
        if not self.open():
            raise RuntimeError(
                f"Failed to open camera_index={self.camera_index}. "
                f"Use find_cameras() to list available devices."
            )
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(self) -> Frame | None:
        """Read one frame, downsample, append to ring buffer.

        Returns None if the camera is closed or the read failed.
        """
        if self.cap is None or not self._running:
            return None
        ret, raw = self.cap.read()
        if not ret:
            return None

        orig_h, orig_w = raw.shape[:2]
        small = cv2.resize(
            raw, (self.target_w, self.target_h), interpolation=cv2.INTER_AREA
        )
        frame = Frame(
            image=small,
            timestamp=time.perf_counter(),
            index=self.frame_count,
            original_size=(orig_w, orig_h),
        )
        self.buffer.append(frame)
        self.frame_count += 1
        return frame

    def latest(self) -> Frame | None:
        """Most recent frame in the ring buffer, or None if empty."""
        return self.buffer[-1] if self.buffer else None


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

# Why this is non-invasive: cv2.VideoCapture(i, CAP_DSHOW) actually OPENS the
# device to probe it. With some configurations of DroidCam, OBS Virtual
# Camera, or virtual cameras in unusual states, that open call triggers a
# native SEH crash inside cv2's DirectShow backend that takes down the whole
# Python process — no traceback, no graceful recovery.
#
# pygrabber's FilterGraph.get_input_devices() goes directly to the Windows
# DirectShow ICreateDevEnum API and lists device names WITHOUT instantiating
# them. It also gives us real names ("OBS Virtual Camera", "DroidCam Source")
# instead of numeric indices, which the cortex-desktop UI's pickDefault
# heuristic actually wants.
#
# We fall back to the cv2 probe only when pygrabber isn't available
# (non-Windows, import failed) — at which point the fallback is at least
# documented as risky.


def _enumerate_via_pygrabber() -> list[dict] | None:
    """Use Windows DirectShow's ICreateDevEnum to list devices by name.

    Returns None if pygrabber isn't installed or fails. Caller falls back
    to the cv2 probe path in that case.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph
    except Exception:                                       # noqa: BLE001
        return None

    try:
        graph = FilterGraph()
        names = graph.get_input_devices()
    except Exception:                                       # noqa: BLE001
        # COM init can fail in unusual environments (services, locked-down
        # accounts). Better to fall back than crash.
        return None

    return [
        {
            "index": i,
            "name": name,
        }
        for i, name in enumerate(names)
    ]


def _probe_via_cv2(max_check: int) -> list[dict]:
    """Legacy cv2 probe — opens each device briefly to read its resolution.

    Used as a fallback when pygrabber isn't available. Carries the SEH-crash
    risk documented above; the only mitigation is "don't call this on
    Windows when pygrabber is reachable."
    """
    out: list[dict] = []
    for i in range(max_check):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if not cap.isOpened():
            continue
        try:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            out.append({
                "index": i,
                "native_resolution": [w, h],
                "native_fps": round(fps, 1),
            })
        finally:
            cap.release()
    return out


def describe_cameras(max_check: int = 5) -> list[dict]:
    """Enumerate available video capture devices.

    Each entry: {index, name?, native_resolution?, native_fps?}.

    Path A (preferred, Windows): pygrabber's DirectShow enumeration. Returns
    {index, name} for every device the OS knows about. NEVER opens the
    device — safe even when DroidCam / OBS Virtual Camera are in weird
    states.

    Path B (fallback, non-Windows or pygrabber missing): cv2 probe.
    Returns {index, native_resolution, native_fps}. Opens each device
    briefly. Has historically crashed the bundle on Windows; only used
    when path A is unavailable.

    The UI consuming this should treat all fields as optional — only `index`
    is guaranteed. Resolution/fps come from path B; name comes from path A.
    """
    via_pygrabber = _enumerate_via_pygrabber()
    if via_pygrabber is not None:
        return via_pygrabber
    return _probe_via_cv2(max_check)


def find_cameras(max_check: int = 5) -> list[int]:
    """Indices only. Used by callers that don't need metadata."""
    return [c["index"] for c in describe_cameras(max_check)]
