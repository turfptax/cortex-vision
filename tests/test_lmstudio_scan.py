"""Tests for the LM Studio scanner endpoint."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def test_normalize_bare_host():
    from cortex_vision.server import _normalize_lmstudio_url

    assert _normalize_lmstudio_url("10.0.0.5") == "http://10.0.0.5:1234/v1"


def test_normalize_host_port():
    from cortex_vision.server import _normalize_lmstudio_url

    assert _normalize_lmstudio_url("10.0.0.5:9999") == "http://10.0.0.5:9999/v1"


def test_normalize_full_url():
    from cortex_vision.server import _normalize_lmstudio_url

    assert (
        _normalize_lmstudio_url("http://10.0.0.5:1234/v1")
        == "http://10.0.0.5:1234/v1"
    )


def test_normalize_url_without_v1():
    from cortex_vision.server import _normalize_lmstudio_url

    assert (
        _normalize_lmstudio_url("http://10.0.0.5:1234")
        == "http://10.0.0.5:1234/v1"
    )


# ---------------------------------------------------------------------------
# Scan endpoint
# ---------------------------------------------------------------------------

def test_scan_returns_default_candidates(isolated):
    """With nothing reachable, the scan still returns the default candidate
    list — UI gets a clear "tried these, none worked" view."""
    from cortex_vision.server import app

    # Mock the per-URL probe so no real network calls
    async def fake_probe(url: str, timeout: float):
        return {"url": url, "reachable": False, "error": "ConnectError"}

    with patch("cortex_vision.server._probe_one_lmstudio", fake_probe):
        with TestClient(app) as c:
            r = c.get("/api/video/lmstudio/scan")
            assert r.status_code == 200
            body = r.json()

    assert "candidates" in body
    assert body["reachable_count"] == 0
    urls = [c["url"] for c in body["candidates"]]
    # Both default localhost candidates should have been probed
    assert any("localhost:1234" in u for u in urls)
    assert any("127.0.0.1:1234" in u for u in urls)


def test_scan_includes_hints(isolated):
    """User-supplied hint URLs should be probed alongside the defaults."""
    from cortex_vision.server import app

    async def fake_probe(url: str, timeout: float):
        return {"url": url, "reachable": False, "error": "x"}

    with patch("cortex_vision.server._probe_one_lmstudio", fake_probe):
        with TestClient(app) as c:
            r = c.get("/api/video/lmstudio/scan?hints=10.0.0.42&hints=192.168.1.5:1234")
            urls = [c["url"] for c in r.json()["candidates"]]

    assert any("10.0.0.42" in u for u in urls)
    assert any("192.168.1.5" in u for u in urls)


def test_scan_dedupes(isolated):
    """If a hint duplicates a default, the URL should only appear once."""
    from cortex_vision.server import app

    async def fake_probe(url: str, timeout: float):
        return {"url": url, "reachable": False, "error": "x"}

    with patch("cortex_vision.server._probe_one_lmstudio", fake_probe):
        with TestClient(app) as c:
            r = c.get("/api/video/lmstudio/scan?hints=localhost:1234")
            urls = [c["url"] for c in r.json()["candidates"]]

    # localhost:1234 should appear at most once even though it's both a
    # default candidate and a hint
    localhost_count = sum(1 for u in urls if u == "http://localhost:1234/v1")
    assert localhost_count == 1


def test_scan_reports_reachable_count(isolated):
    """Mock one URL as reachable, verify the count and model list propagate."""
    from cortex_vision.server import app

    async def fake_probe(url: str, timeout: float):
        if "10.0.0.99" in url:
            return {
                "url": url,
                "reachable": True,
                "models": ["smolvlm2-2.2b-instruct", "qwen2.5-7b"],
                "model_count": 2,
                "likely_server": "lm-studio",
            }
        return {"url": url, "reachable": False, "error": "ConnectError"}

    with patch("cortex_vision.server._probe_one_lmstudio", fake_probe):
        with TestClient(app) as c:
            r = c.get("/api/video/lmstudio/scan?hints=10.0.0.99")
            body = r.json()

    assert body["reachable_count"] == 1
    reachable = [c for c in body["candidates"] if c["reachable"]]
    assert len(reachable) == 1
    assert "smolvlm2-2.2b-instruct" in reachable[0]["models"]
    assert reachable[0]["likely_server"] == "lm-studio"


# ---------------------------------------------------------------------------
# Server-type guess
# ---------------------------------------------------------------------------

def test_guess_server_type():
    from cortex_vision.server import _guess_server_type

    assert _guess_server_type([]) is None
    assert _guess_server_type(["smolvlm2-2.2b-instruct"]) == "lm-studio"
    assert _guess_server_type(["llava-1.5-7b"]) == "lm-studio"
    assert _guess_server_type(["gpt-4o", "gpt-4o-mini"]) == "openai-compatible"
