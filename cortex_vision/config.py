"""Persistent runtime configuration for cortex-vision.

The cortex-desktop UI calls GET/PUT /api/video/config to read and update
this file. Settings persist across reboots, plugin updates, and machine
restores (the file lives in %APPDATA%/Cortex/video/, separate from the
plugin install directory at %APPDATA%/Cortex/plugins/cortex-vision/).

Resolution order (highest priority first):

    1. Explicit per-request overrides (function arguments, request bodies)
    2. The config file at %APPDATA%/Cortex/video/config.json
    3. Environment variables (CORTEX_VISION_LLM_URL, etc.)
    4. Built-in defaults

The config file is **authoritative** when set: a non-empty value in the file
overrides any env var. This means the UI is the trusted source once the user
configures via the form. An empty / null value in the file falls through to
the env var fallback (preserves the dev-mode escape hatch).

Atomic writes: PUT writes to config.json.tmp, then rename. Eliminates the
"corrupt half-written file after crash" failure mode.
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from cortex_vision.storage import db as db_module


_LOCK = threading.Lock()
_REDACTED = "***"


# ---------------------------------------------------------------------------
# Defaults — used when neither the config file nor env vars provide a value
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "describer": {
        "url": "http://localhost:1234/v1",
        "model": "",
        "api_key": "",
    },
    "transcribe": {
        # Empty url means "no transcription provider configured" — pipeline
        # gracefully skips audio if not set
        "url": "",
        "model": "whisper-1",
        "api_key": "",
    },
    "live": {
        "default_resolution": [384, 216],
        "default_threshold": 0.85,
        "default_pixel_diff_threshold": 25.0,
        "default_structural_threshold": 0.15,
        "default_steady_interval": 30.0,
        "default_min_scene_gap": 3.0,
    },
}


# ---------------------------------------------------------------------------
# File location
# ---------------------------------------------------------------------------

def config_path() -> Path:
    """Where the config file lives on disk. Same directory as sessions.db so
    backup tools that grab one grab the other."""
    return db_module.default_db_path().parent / "config.json"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load_raw_config() -> dict[str, Any]:
    """Read config.json verbatim, without filling defaults. Returns {} if
    missing or corrupt. Used by the resolution helpers so they can tell
    "user didn't set this" from "user set this to the default value"."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_config() -> dict[str, Any]:
    """Read config.json with defaults filled in for missing keys.

    Used by GET /api/video/config so the UI always gets a complete shape.
    Resolution helpers (get_describer_config etc.) use load_raw_config()
    instead so they can fall through to env vars when fields are unset.
    """
    return _merge_with_defaults(load_raw_config())


def _merge_with_defaults(user: dict[str, Any]) -> dict[str, Any]:
    """Fill missing keys with defaults so callers can always do `cfg["x"]["y"]`
    without KeyError. Top-level + one nesting deep is enough for our schema."""
    out = deepcopy(_DEFAULT_CONFIG)
    for section, vals in (user or {}).items():
        if section not in out or not isinstance(vals, dict):
            continue
        for k, v in vals.items():
            out[section][k] = v
    return out


# ---------------------------------------------------------------------------
# Write (atomic)
# ---------------------------------------------------------------------------

def save_config(cfg: dict[str, Any]) -> None:
    """Atomically write the config to disk. Uses a temp-file-then-rename
    pattern so a crash mid-write can't leave a corrupt config.json."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")

    with _LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)            # atomic on Windows + Unix


# ---------------------------------------------------------------------------
# Resolution helpers — config file > env var > default
# ---------------------------------------------------------------------------

def _resolve(file_value: Any, env_var: str | None, default: Any) -> Any:
    """File wins if truthy. Env var wins if file is empty/null. Default last."""
    if file_value not in (None, "", []):
        return file_value
    if env_var:
        env_value = os.environ.get(env_var, "").strip()
        if env_value:
            return env_value
    return default


def get_describer_config() -> dict[str, Any]:
    """Resolved describer settings. Used by lmstudio_client.py."""
    cfg = load_raw_config()
    block = cfg.get("describer", {}) or {}
    return {
        "url": _resolve(
            block.get("url"),
            "CORTEX_VISION_LLM_URL",
            _DEFAULT_CONFIG["describer"]["url"],
        ),
        "model": _resolve(
            block.get("model"),
            "CORTEX_VISION_LLM_MODEL",
            "",
        ),
        "api_key": _resolve(
            block.get("api_key"),
            "CORTEX_VISION_LLM_KEY",
            "lm-studio",
        ),
    }


def get_transcribe_config() -> dict[str, Any]:
    """Resolved transcription settings. Used by audio/transcribe.py."""
    cfg = load_raw_config()
    block = cfg.get("transcribe", {}) or {}
    # The transcribe module additionally accepts OPENAI_API_KEY as a
    # fallback path — that's handled there, not here.
    return {
        "url": _resolve(
            block.get("url"),
            "CORTEX_VISION_WHISPER_URL",
            "",
        ),
        "model": _resolve(
            block.get("model"),
            "CORTEX_VISION_WHISPER_MODEL",
            "whisper-1",
        ),
        "api_key": _resolve(
            block.get("api_key"),
            "CORTEX_VISION_WHISPER_KEY",
            "lm-studio",
        ),
    }


def get_live_config() -> dict[str, Any]:
    """Resolved live-mode defaults."""
    cfg = load_raw_config()
    block = cfg.get("live", {}) or {}
    defaults = _DEFAULT_CONFIG["live"]
    return {
        "default_resolution": block.get("default_resolution") or defaults["default_resolution"],
        "default_threshold": block.get("default_threshold") or defaults["default_threshold"],
        "default_pixel_diff_threshold": block.get("default_pixel_diff_threshold") or defaults["default_pixel_diff_threshold"],
        "default_structural_threshold": block.get("default_structural_threshold") or defaults["default_structural_threshold"],
        "default_steady_interval": block.get("default_steady_interval") or defaults["default_steady_interval"],
        "default_min_scene_gap": block.get("default_min_scene_gap") or defaults["default_min_scene_gap"],
    }


# ---------------------------------------------------------------------------
# Redaction — never return secrets to the API
# ---------------------------------------------------------------------------

def redact(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with API keys masked.

    Empty / unset api_key -> empty string. Non-empty -> "***". This way the UI
    can render "API key configured: yes/no" without ever seeing the value.
    """
    out = deepcopy(cfg)
    for section in ("describer", "transcribe"):
        if section in out and isinstance(out[section], dict):
            key = out[section].get("api_key")
            if key and key not in (_REDACTED, "lm-studio"):
                # "lm-studio" is the LM Studio default placeholder, not a real secret
                out[section]["api_key"] = _REDACTED
    return out


def merge_for_save(submitted: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """Merge a PUT payload with the existing on-disk config.

    If the user submitted "***" for an api_key, we preserve the existing
    value (the UI shows "***" as a placeholder for "key is set, don't show
    it" — submitting it back means "leave as-is").

    If they submitted "" / null, we clear it.
    Anything else, we save it.
    """
    out = deepcopy(existing)
    for section, vals in (submitted or {}).items():
        if section not in out:
            out[section] = {}
        if not isinstance(vals, dict):
            continue
        for k, v in vals.items():
            if k == "api_key" and v == _REDACTED:
                # Preserve existing key — user kept the placeholder
                continue
            out[section][k] = v
    return out
