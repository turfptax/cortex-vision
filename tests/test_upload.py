"""Tests for the upload endpoint and idempotent use_local_file — Phase 3."""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cortex_vision.capture.ytdlp import use_local_file


# ---------------------------------------------------------------------------
# Idempotent use_local_file (the upload writes directly to session_dir)
# ---------------------------------------------------------------------------

def test_use_local_file_idempotent_when_already_in_session_dir(tmp_path: Path):
    """If the file is ALREADY at session_dir/source.<ext>, no-op cleanly."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    src = session_dir / "source.mp4"
    src.write_bytes(b"video content")

    # First call lands on the canonical path
    meta1 = use_local_file(str(src), session_dir=session_dir)
    assert meta1["file_path"] == str(src.resolve())

    # Second call against the same file — must not raise, must not duplicate
    meta2 = use_local_file(str(src), session_dir=session_dir)
    assert meta2["file_path"] == str(src.resolve())
    assert src.exists()
    assert src.read_bytes() == b"video content"           # unchanged


def test_use_local_file_external_still_brings_into_session(tmp_path: Path):
    """When the source is OUTSIDE session_dir, original symlink/copy behavior."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    external = tmp_path / "external.mp4"
    external.write_bytes(b"external video")

    meta = use_local_file(str(external), session_dir=session_dir)
    dest = Path(meta["file_path"])
    assert dest.exists()
    assert dest.parent.resolve() == session_dir.resolve()


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_isolated_db(tmp_path, monkeypatch):
    """Spin up a TestClient with the artifacts dir + DB pointed at tmp_path,
    and the batch pipeline stubbed so tests don't touch real LM Studio."""
    artifacts = tmp_path / "video"
    artifacts.mkdir()

    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: artifacts / "sessions.db",
    )
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_artifacts_dir",
        lambda: artifacts / "sessions",
    )

    # Stub the background pipeline so the upload test focuses on the endpoint
    with patch("cortex_vision.server.run_batch_pipeline") as mock_run:
        from cortex_vision.server import app
        with TestClient(app) as c:
            yield c, mock_run, artifacts


def test_upload_endpoint_accepts_video(client_with_isolated_db):
    c, mock_run, artifacts = client_with_isolated_db

    fake_blob = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200   # plausible mp4 magic
    files = {"file": ("journal.mp4", fake_blob, "video/mp4")}
    data = {"mode": "journal", "transcribe_audio": "false"}

    r = c.post("/api/video/jobs/upload", files=files, data=data)
    assert r.status_code == 202, r.text
    body = r.json()
    assert "session_id" in body
    assert body["status"] == "queued"
    assert body["bytes_uploaded"] == len(fake_blob)
    assert body["poll_url"].startswith("/api/video/sessions/")

    # File should be on disk at the canonical location
    session_id = body["session_id"]
    src = artifacts / "sessions" / session_id / "source.mp4"
    assert src.exists()
    assert src.read_bytes() == fake_blob

    # Pipeline should have been queued. TestClient runs BackgroundTasks
    # synchronously after the response, so by the time we get here the mock
    # has been called once with the session_id we just received.
    assert mock_run.call_count == 1
    args, _ = mock_run.call_args
    assert args[0] == session_id


def test_upload_endpoint_rejects_non_video(client_with_isolated_db):
    c, _, _ = client_with_isolated_db

    files = {"file": ("notes.txt", b"plain text", "text/plain")}
    r = c.post("/api/video/jobs/upload", files=files, data={"mode": "journal"})
    assert r.status_code == 400
    assert "Unsupported" in r.json()["detail"]


def test_upload_endpoint_rejects_no_filename(client_with_isolated_db):
    c, _, _ = client_with_isolated_db

    # FastAPI requires a filename for UploadFile; sending without one returns 422
    # Test the explicit 400 by sending an empty-name workaround
    files = {"file": ("", b"data", "video/mp4")}
    r = c.post("/api/video/jobs/upload", files=files)
    # Either 400 (our check) or 422 (FastAPI validation) — both reject correctly
    assert r.status_code in (400, 422)


def test_upload_creates_session_with_journal_mode(client_with_isolated_db):
    c, _, _ = client_with_isolated_db

    files = {"file": ("clip.mp4", b"\x00" * 100, "video/mp4")}
    r = c.post("/api/video/jobs/upload", files=files, data={"mode": "journal"})
    assert r.status_code == 202

    session_id = r.json()["session_id"]
    detail = c.get(f"/api/video/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["mode"] == "journal"
    assert detail.json()["source"]["kind"] == "upload"
    assert detail.json()["source"]["filename"] == "clip.mp4"


def test_upload_creates_session_with_file_mode(client_with_isolated_db):
    """mode=file is also valid (e.g. user picks a file via OS file dialog)."""
    c, _, _ = client_with_isolated_db

    files = {"file": ("video.mp4", b"\x00" * 100, "video/mp4")}
    r = c.post("/api/video/jobs/upload", files=files, data={"mode": "file"})
    assert r.status_code == 202

    session_id = r.json()["session_id"]
    detail = c.get(f"/api/video/sessions/{session_id}")
    assert detail.json()["mode"] == "file"


def test_upload_threads_project_id(client_with_isolated_db):
    c, _, _ = client_with_isolated_db

    files = {"file": ("clip.mp4", b"\x00" * 100, "video/mp4")}
    r = c.post(
        "/api/video/jobs/upload",
        files=files,
        data={"mode": "journal", "project_id": "cortex-vision"},
    )
    assert r.status_code == 202

    session_id = r.json()["session_id"]
    detail = c.get(f"/api/video/sessions/{session_id}")
    assert detail.json()["project_id"] == "cortex-vision"
