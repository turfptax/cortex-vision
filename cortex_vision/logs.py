"""In-memory log capture for the cortex-vision sidecar.

A bounded ring buffer attached to the root logger captures recent log lines
so they're available via GET /api/video/logs without the user needing to
know the file path. Same pattern cortex-desktop's Hub uses for its own logs.

Two consumer paths:
    1. cortex-desktop's Plugins tab "View Logs" button -> GET /api/video/logs
    2. End user troubleshooting -> POST /api/video/logs/level to bump to DEBUG
       temporarily, reproduce the issue, GET /api/video/logs to see it

Capacity: 2000 lines. At ~100 chars/line that's ~200 KB resident — negligible
compared to anything else in this process. Old lines drop off the front.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Literal


# Ring buffer — module-level so all loggers share it
_BUFFER: deque[str] = deque(maxlen=2000)
_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


class _BufferHandler(logging.Handler):
    """Push formatted log lines into the in-memory ring buffer.

    Never raises — logging from inside a logging handler would be a bad time.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append(self.format(record))
        except Exception:                                # noqa: BLE001
            pass


def install() -> None:
    """Attach the buffer handler to the root logger.

    Idempotent — calling twice doesn't double-attach. Call once during
    server lifespan startup.
    """
    root = logging.getLogger()
    if any(isinstance(h, _BufferHandler) for h in root.handlers):
        return
    handler = _BufferHandler()
    handler.setFormatter(_FORMATTER)
    root.addHandler(handler)


def get_recent(
    lines: int = 200,
    level: Literal["debug", "info", "warning", "error", "critical"] | None = None,
) -> list[str]:
    """Return the most recent N log lines, optionally filtered by min level.

    Filtering is by string match on the formatted line (we record formatted
    strings, not raw records, to keep the buffer tiny). The format includes
    `<timestamp> <LEVEL>` so we can match the literal level token.
    """
    buf = list(_BUFFER)
    if level:
        wanted = _levels_at_or_above(level.upper())
        buf = [line for line in buf if any(f" {lvl}" in line for lvl in wanted)]
    return buf[-lines:]


def total_buffered() -> int:
    """Current ring-buffer occupancy. Capped at maxlen."""
    return len(_BUFFER)


def clear() -> None:
    """Empty the ring buffer. Useful before reproducing a bug."""
    _BUFFER.clear()


def set_level(level: str) -> str:
    """Set the root logger's effective level. Returns the canonical level name.

    Valid: debug, info, warning, error, critical.
    Idempotent — re-setting to the current level is a no-op.
    """
    canonical = level.upper().strip()
    if canonical not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError(f"Unknown log level: {level!r}")
    logging.getLogger().setLevel(canonical)
    return canonical


def current_level() -> str:
    """Effective level of the root logger."""
    return logging.getLevelName(logging.getLogger().getEffectiveLevel())


# ---------------------------------------------------------------------------
# Internal — level filtering
# ---------------------------------------------------------------------------

_LEVEL_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _levels_at_or_above(level: str) -> list[str]:
    if level not in _LEVEL_ORDER:
        return list(_LEVEL_ORDER)
    idx = _LEVEL_ORDER.index(level)
    return list(_LEVEL_ORDER[idx:])
