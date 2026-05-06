"""Tests for cortex_vision.config and the /api/video/config endpoints."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config_path() and the artifacts dir at a tmp location so tests
    don't touch the user's real %APPDATA%."""
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "video" / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "video" / "sessions",
    )
    # Clear any env vars that might leak in from the dev environment
    for var in (
        "CORTEX_VISION_LLM_URL",
        "CORTEX_VISION_LLM_MODEL",
        "CORTEX_VISION_LLM_KEY",
        "CORTEX_VISION_WHISPER_URL",
        "CORTEX_VISION_WHISPER_MODEL",
        "CORTEX_VISION_WHISPER_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path / "video" / "config.json"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def test_load_config_returns_defaults_when_missing(isolated_config):
    from cortex_vision import config

    cfg = config.load_config()
    assert "describer" in cfg
    assert "transcribe" in cfg
    assert "live" in cfg
    assert cfg["describer"]["url"] == "http://localhost:1234/v1"


def test_save_and_load_round_trip(isolated_config):
    from cortex_vision import config

    config.save_config({
        "describer": {"url": "http://10.0.0.5:1234/v1", "model": "smolvlm", "api_key": ""},
        "transcribe": {"url": "", "model": "whisper-1", "api_key": ""},
        "live": {"default_threshold": 0.9},
    })

    cfg = config.load_config()
    assert cfg["describer"]["url"] == "http://10.0.0.5:1234/v1"
    assert cfg["describer"]["model"] == "smolvlm"
    assert cfg["live"]["default_threshold"] == 0.9
    # Defaults filled in for missing keys
    assert "default_resolution" in cfg["live"]


def test_load_config_handles_corrupt_file(isolated_config):
    from cortex_vision import config

    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("{not valid json", encoding="utf-8")

    # Should fall back to defaults silently, not raise
    cfg = config.load_config()
    assert cfg["describer"]["url"] == "http://localhost:1234/v1"


def test_save_is_atomic(isolated_config, monkeypatch):
    """If the rename step is interrupted, the destination file shouldn't be
    a half-written fragment."""
    from cortex_vision import config

    config.save_config({"describer": {"url": "http://valid:1234/v1"}})
    original = config.load_config()
    assert original["describer"]["url"] == "http://valid:1234/v1"

    # Now simulate a write that fails after the tmp file is created but
    # before the rename completes
    real_replace = __import__("os").replace
    def boom(*args, **kw):
        raise OSError("disk full")
    monkeypatch.setattr("os.replace", boom)

    with pytest.raises(OSError):
        config.save_config({"describer": {"url": "http://corrupted:1234/v1"}})

    monkeypatch.setattr("os.replace", real_replace)
    cfg = config.load_config()
    # Original config is intact
    assert cfg["describer"]["url"] == "http://valid:1234/v1"


# ---------------------------------------------------------------------------
# Resolution: file > env > default
# ---------------------------------------------------------------------------

def test_get_describer_config_uses_defaults_when_unset(isolated_config):
    from cortex_vision import config

    out = config.get_describer_config()
    assert out["url"] == "http://localhost:1234/v1"
    assert out["model"] == ""


def test_get_describer_config_env_var_fallback(isolated_config, monkeypatch):
    from cortex_vision import config

    # No config file, env vars set -> env wins
    monkeypatch.setenv("CORTEX_VISION_LLM_URL", "http://env:1234/v1")
    monkeypatch.setenv("CORTEX_VISION_LLM_MODEL", "env-model")

    out = config.get_describer_config()
    assert out["url"] == "http://env:1234/v1"
    assert out["model"] == "env-model"


def test_get_describer_config_file_overrides_env(isolated_config, monkeypatch):
    """Config file takes precedence over env vars when set — UI is authoritative."""
    from cortex_vision import config

    monkeypatch.setenv("CORTEX_VISION_LLM_URL", "http://env:1234/v1")
    config.save_config({
        "describer": {"url": "http://file:1234/v1", "model": "file-model"},
    })

    out = config.get_describer_config()
    assert out["url"] == "http://file:1234/v1"
    assert out["model"] == "file-model"


def test_empty_file_value_falls_through_to_env(isolated_config, monkeypatch):
    """If the user clears a field via the UI (saves empty), env var still wins."""
    from cortex_vision import config

    monkeypatch.setenv("CORTEX_VISION_LLM_URL", "http://env:1234/v1")
    config.save_config({"describer": {"url": "", "model": ""}})

    out = config.get_describer_config()
    assert out["url"] == "http://env:1234/v1"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_redact_masks_secrets(isolated_config):
    from cortex_vision import config

    cfg = {
        "describer": {"url": "http://x", "model": "m", "api_key": "sk-real-secret"},
        "transcribe": {"url": "http://y", "model": "whisper-1", "api_key": "another-secret"},
        "live": {"default_threshold": 0.85},
    }
    redacted = config.redact(cfg)

    assert redacted["describer"]["api_key"] == "***"
    assert redacted["transcribe"]["api_key"] == "***"
    assert redacted["describer"]["url"] == "http://x"  # url not redacted
    assert redacted["live"]["default_threshold"] == 0.85
    # Original not mutated
    assert cfg["describer"]["api_key"] == "sk-real-secret"


def test_redact_lm_studio_placeholder_is_not_a_secret(isolated_config):
    """LM Studio's default placeholder 'lm-studio' isn't a real secret —
    don't display it as ***."""
    from cortex_vision import config

    cfg = {"describer": {"api_key": "lm-studio"}, "transcribe": {}, "live": {}}
    redacted = config.redact(cfg)
    assert redacted["describer"]["api_key"] == "lm-studio"


def test_redact_empty_key_stays_empty(isolated_config):
    from cortex_vision import config

    cfg = {"describer": {"api_key": ""}, "transcribe": {}, "live": {}}
    redacted = config.redact(cfg)
    assert redacted["describer"]["api_key"] == ""


# ---------------------------------------------------------------------------
# merge_for_save: "***" preserves existing key
# ---------------------------------------------------------------------------

def test_merge_for_save_preserves_existing_key_when_redacted(isolated_config):
    """User submits api_key="***" — should keep the existing key, not set it to '***'."""
    from cortex_vision import config

    existing = {
        "describer": {"url": "http://x", "model": "m", "api_key": "sk-real"},
        "transcribe": {"url": "", "model": "whisper-1", "api_key": ""},
        "live": {},
    }
    submitted = {
        "describer": {"url": "http://NEW-URL", "api_key": "***"},
    }

    merged = config.merge_for_save(submitted, existing)
    assert merged["describer"]["url"] == "http://NEW-URL"
    assert merged["describer"]["api_key"] == "sk-real"          # preserved
    assert merged["describer"]["model"] == "m"                   # untouched


def test_merge_for_save_clears_key_when_empty(isolated_config):
    from cortex_vision import config

    existing = {
        "describer": {"url": "http://x", "api_key": "sk-real"},
        "transcribe": {}, "live": {},
    }
    submitted = {"describer": {"api_key": ""}}

    merged = config.merge_for_save(submitted, existing)
    assert merged["describer"]["api_key"] == ""


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def test_get_config_returns_redacted(isolated_config):
    from cortex_vision import config
    from cortex_vision.server import app

    config.save_config({
        "describer": {"url": "http://x", "model": "m", "api_key": "sk-secret"},
        "transcribe": {"url": "", "model": "whisper-1", "api_key": ""},
        "live": {},
    })

    with TestClient(app) as c:
        r = c.get("/api/video/config")
    assert r.status_code == 200
    body = r.json()
    assert body["describer"]["api_key"] == "***"
    assert body["describer"]["url"] == "http://x"
    assert "config_path" in body


def test_put_config_writes_to_file(isolated_config):
    from cortex_vision import config
    from cortex_vision.server import app

    payload = {
        "describer": {
            "url": "http://10.0.0.102:1234/v1",
            "model": "smolvlm2-2.2b-instruct",
            "api_key": "sk-fresh",
        },
    }

    with TestClient(app) as c:
        r = c.put("/api/video/config", json=payload)
    assert r.status_code == 200

    on_disk = config.load_config()
    assert on_disk["describer"]["url"] == "http://10.0.0.102:1234/v1"
    assert on_disk["describer"]["model"] == "smolvlm2-2.2b-instruct"
    assert on_disk["describer"]["api_key"] == "sk-fresh"


def test_put_config_redacted_key_preserves_existing(isolated_config):
    """The flow we care about: GET returns ***, user edits URL only, PUTs the
    full body back, original API key should NOT get clobbered to "***"."""
    from cortex_vision import config
    from cortex_vision.server import app

    config.save_config({
        "describer": {"url": "http://old", "model": "m", "api_key": "sk-real"},
        "transcribe": {}, "live": {},
    })

    payload = {
        "describer": {
            "url": "http://new",
            "model": "m",
            "api_key": "***",                          # user kept the masked field
        },
    }

    with TestClient(app) as c:
        c.put("/api/video/config", json=payload)

    on_disk = config.load_config()
    assert on_disk["describer"]["url"] == "http://new"
    assert on_disk["describer"]["api_key"] == "sk-real"


def test_put_config_partial_section(isolated_config):
    """Submitting only describer should NOT wipe transcribe / live."""
    from cortex_vision import config
    from cortex_vision.server import app

    config.save_config({
        "describer": {"url": "http://d"},
        "transcribe": {"url": "http://t"},
        "live": {"default_threshold": 0.9},
    })

    with TestClient(app) as c:
        c.put("/api/video/config", json={"describer": {"url": "http://NEW"}})

    on_disk = config.load_config()
    assert on_disk["describer"]["url"] == "http://NEW"
    assert on_disk["transcribe"]["url"] == "http://t"
    assert on_disk["live"]["default_threshold"] == 0.9


def test_test_config_unreachable_returns_structured_error(isolated_config):
    """POST /config/test for an unreachable URL returns reachable: false + error."""
    from cortex_vision.server import app

    payload = {"describer": {"url": "http://no-such-host-12345.invalid:9999/v1"}}

    with TestClient(app) as c:
        r = c.post("/api/video/config/test", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert "describer" in body
    assert body["describer"]["reachable"] is False
    assert "error" in body["describer"]


def test_test_config_no_url(isolated_config):
    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.post("/api/video/config/test", json={"describer": {"url": ""}})
    body = r.json()
    assert body["describer"]["reachable"] is False
    assert body["describer"]["error"] == "no URL provided"


def test_describer_uses_config_after_put(isolated_config):
    """End-to-end: PUT to /config, verify the LM Studio client picks up the
    new value on its next call."""
    from cortex_vision.description.lmstudio_client import _base_url
    from cortex_vision.server import app

    with TestClient(app) as c:
        c.put("/api/video/config", json={
            "describer": {"url": "http://patched.example:1234/v1"}
        })

    # Resolved URL should now reflect the saved config
    assert _base_url() == "http://patched.example:1234/v1"
