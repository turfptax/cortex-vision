"""Tests for cortex_vision.logs ring buffer + the /api/video/logs endpoints."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_logs_buffer():
    """Each test starts with a fresh buffer + INFO level."""
    from cortex_vision import logs as _logs
    _logs.clear()
    _logs.set_level("INFO")
    yield
    _logs.clear()
    _logs.set_level("INFO")


# ---------------------------------------------------------------------------
# Module-level
# ---------------------------------------------------------------------------

def test_install_is_idempotent():
    from cortex_vision import logs as _logs

    root = logging.getLogger()
    before = len(root.handlers)
    _logs.install()
    _logs.install()
    _logs.install()
    after = len(root.handlers)
    # Only one buffer handler regardless of how many times install() is called
    assert after - before <= 1


def test_buffer_captures_log_lines():
    from cortex_vision import logs as _logs

    _logs.install()
    test_logger = logging.getLogger("cortex_vision.test")
    test_logger.warning("hello buffer")
    test_logger.error("something bad")

    lines = _logs.get_recent(lines=10)
    text = "\n".join(lines)
    assert "hello buffer" in text
    assert "something bad" in text


def test_get_recent_filter_by_level():
    from cortex_vision import logs as _logs

    _logs.install()
    test_logger = logging.getLogger("cortex_vision.test")
    test_logger.info("an info")
    test_logger.warning("a warning")
    test_logger.error("an error")

    warnings_and_above = "\n".join(_logs.get_recent(level="warning"))
    assert "an info" not in warnings_and_above
    assert "a warning" in warnings_and_above
    assert "an error" in warnings_and_above

    errors_only = "\n".join(_logs.get_recent(level="error"))
    assert "a warning" not in errors_only
    assert "an error" in errors_only


def test_set_level_changes_root_logger():
    from cortex_vision import logs as _logs

    _logs.set_level("debug")
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    assert _logs.current_level() == "DEBUG"

    _logs.set_level("WARNING")
    assert _logs.current_level() == "WARNING"


def test_set_level_rejects_unknown():
    from cortex_vision import logs as _logs

    with pytest.raises(ValueError):
        _logs.set_level("LOUD")


def test_buffer_is_bounded():
    """Old lines drop off the front when maxlen is exceeded — no unbounded
    growth even under heavy logging."""
    from cortex_vision import logs as _logs

    _logs.install()
    test_logger = logging.getLogger("cortex_vision.test")
    for i in range(3000):                                  # > 2000 maxlen
        test_logger.info("line %d", i)

    assert _logs.total_buffered() <= 2000
    # Most recent lines win
    lines = _logs.get_recent(lines=10)
    assert "line 2999" in "\n".join(lines)
    assert "line 0" not in "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def test_get_logs_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )

    from cortex_vision.server import app

    with TestClient(app) as c:
        # Generate something to capture
        logging.getLogger("cortex_vision.test").warning("integration warning")
        r = c.get("/api/video/logs?lines=50")
        assert r.status_code == 200
        body = r.json()
        assert "lines" in body
        assert "current_level" in body
        assert "buffered" in body
        text = "\n".join(body["lines"])
        assert "integration warning" in text


def test_get_logs_with_level_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )

    from cortex_vision.server import app

    with TestClient(app) as c:
        log = logging.getLogger("cortex_vision.test")
        log.info("info line")
        log.error("error line")
        r = c.get("/api/video/logs?level=error")
        body = r.json()
        text = "\n".join(body["lines"])
        assert "error line" in text
        assert "info line" not in text


def test_get_logs_invalid_level_400(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )

    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.get("/api/video/logs?level=FATAL")
        assert r.status_code == 400


def test_set_log_level_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )

    from cortex_vision.server import app

    with TestClient(app) as c:
        r = c.post("/api/video/logs/level", json={"level": "debug"})
        assert r.status_code == 200
        assert r.json()["level"] == "DEBUG"
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG

        # Restore for other tests
        c.post("/api/video/logs/level", json={"level": "info"})


def test_clear_logs_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: tmp_path / "sessions",
    )

    from cortex_vision.server import app

    with TestClient(app) as c:
        logging.getLogger("cortex_vision.test").warning("about to clear")
        r = c.delete("/api/video/logs")
        assert r.status_code == 200
        assert r.json()["cleared"] >= 1

        # Buffer should be empty after clear
        r2 = c.get("/api/video/logs")
        text = "\n".join(r2.json()["lines"])
        assert "about to clear" not in text
