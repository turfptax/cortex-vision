"""Smoke tests for the yt-dlp wrapper. No actual downloads — those would
require network access and aren't appropriate for CI. Tests cover the helpers
that don't need network: platform detection, date parsing, local-file path."""
from pathlib import Path

import pytest

from cortex_vision.capture.ytdlp import (
    _parse_upload_date,
    _platform_from_url,
    use_local_file,
)


def test_platform_from_url_known_hosts():
    assert _platform_from_url("https://www.tiktok.com/@u/video/123") == "tiktok"
    assert _platform_from_url("https://youtube.com/watch?v=x") == "youtube"
    assert _platform_from_url("https://youtu.be/x") == "youtube"
    assert _platform_from_url("https://reddit.com/r/foo") == "reddit"
    assert _platform_from_url("https://v.redd.it/abc") == "reddit"
    assert _platform_from_url("https://vimeo.com/123") == "vimeo"
    assert _platform_from_url("https://twitter.com/u/status/1") == "twitter"
    assert _platform_from_url("https://x.com/u/status/1") == "twitter"
    assert _platform_from_url("https://instagram.com/p/abc") == "instagram"


def test_platform_from_url_unknown():
    assert _platform_from_url("https://random.example.com/video.mp4") == "url"


def test_parse_upload_date_valid():
    assert _parse_upload_date("20260101") == "2026-01-01"
    assert _parse_upload_date("19991231") == "1999-12-31"


def test_parse_upload_date_invalid():
    assert _parse_upload_date(None) is None
    assert _parse_upload_date("") is None
    assert _parse_upload_date("not-a-date") is None
    assert _parse_upload_date("2026") is None       # too short
    assert _parse_upload_date("202612345") is None  # too long


def test_use_local_file_creates_session_artifact(tmp_path: Path):
    # Create a fake video file
    src = tmp_path / "my-clip.mp4"
    src.write_bytes(b"fake video content")

    session_dir = tmp_path / "session-xyz"
    meta = use_local_file(str(src), session_dir=session_dir)

    assert meta["platform"] == "file"
    assert meta["title"] == "my-clip"
    assert meta["source_url"].startswith("file://")
    assert Path(meta["file_path"]).exists()
    assert Path(meta["file_path"]).suffix == ".mp4"


def test_use_local_file_rejects_non_video(tmp_path: Path):
    src = tmp_path / "not-a-video.txt"
    src.write_text("hello")

    with pytest.raises(ValueError, match="Unsupported"):
        use_local_file(str(src), session_dir=tmp_path / "session")


def test_use_local_file_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        use_local_file(str(tmp_path / "missing.mp4"), session_dir=tmp_path)
