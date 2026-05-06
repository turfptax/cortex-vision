"""yt-dlp wrapper — download a video from a URL into a session's artifact dir.

Adapted from VideoIndex/ai-video-index/lib/downloader.py. Differences:
  - Targets cortex-vision's session-based layout (sessions/<id>/source.<ext>)
    rather than VideoIndex's catalog-based layout
  - Returns a flat dict matching the SourceMeta shape we feed into batch.py
  - No SHA256 (cortex-vision doesn't dedup; that was VideoIndex's concern)

Usage:
    from cortex_vision.capture.ytdlp import download_to_session
    meta = download_to_session("https://...", session_dir=Path(".../sessions/abc"))
    # meta = {"file_path": "...", "title": "...", "duration_s": 12.4,
    #         "platform": "tiktok", "uploader": "...", ...}
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv"}


def _platform_from_url(url: str) -> str:
    """Best-effort guess at the source platform for sighting metadata."""
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        return "tiktok"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "reddit.com" in host or "v.redd.it" in host:
        return "reddit"
    if "vimeo.com" in host:
        return "vimeo"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    if "instagram.com" in host:
        return "instagram"
    return "url"


def _parse_upload_date(yyyymmdd: str | None) -> str | None:
    """yt-dlp gives upload_date as YYYYMMDD string. Return ISO date or None."""
    if not yyyymmdd or not yyyymmdd.isdigit() or len(yyyymmdd) != 8:
        return None
    try:
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    except Exception:
        return None


def download_to_session(
    url: str,
    session_dir: Path,
    quality: str = "720p",
    cookies_from_browser: str = "",
    cookies_file: str = "",
) -> dict[str, Any]:
    """Download `url` into `session_dir/source.<ext>` and return metadata.

    Args:
        url: yt-dlp compatible URL (YouTube, TikTok, direct file, etc.)
        session_dir: per-session artifact directory, e.g.
            ``%APPDATA%/Cortex/video/sessions/<session_id>/``
        quality: max video height, e.g. "720p", "480p", "1080p"
        cookies_from_browser: optional browser name to pull cookies from
            ("chrome" / "firefox" / "edge" / "brave" / ...) for gated content
        cookies_file: optional path to a Netscape-format cookies.txt file

    Returns:
        dict with keys: file_path, title, duration_s, source_url, platform,
        uploader, upload_date, posted_at, thumbnail_url, raw

    Raises:
        RuntimeError: download succeeded but no video file was produced
        yt_dlp.DownloadError: download itself failed (caller decides what to do)
    """
    import yt_dlp

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    # Cascading format selector — prefer height-capped video+audio merged into
    # mp4, fall back to "best" for platforms that don't expose split streams
    # (e.g. TikTok always returns a single muxed file).
    height = int(quality.replace("p", ""))
    opts: dict[str, Any] = {
        "format": (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/"
            f"best"
        ),
        "outtmpl": str(session_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "writeinfojson": True,
        "quiet": True,                  # surface progress via the FastAPI layer instead
        "no_warnings": True,
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    elif cookies_file:
        opts["cookiefile"] = cookies_file

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Find the actual video file (filter out .info.json, .webp, .vtt, etc.)
    candidates = [
        p for p in session_dir.glob("source.*")
        if p.suffix.lower() in VIDEO_EXTS
    ]
    if not candidates:
        raise RuntimeError(
            f"yt-dlp finished but no video file found in {session_dir}"
        )
    candidates.sort(key=lambda p: 0 if p.suffix.lower() == ".mp4" else 1)
    file_path = candidates[0]

    upload_date = _parse_upload_date(info.get("upload_date"))
    posted_at = upload_date + "T00:00:00Z" if upload_date else None

    return {
        "file_path": str(file_path),
        "title": info.get("title", ""),
        "duration_s": float(info.get("duration") or 0.0),
        "source_url": url,
        "platform": _platform_from_url(url),
        "uploader": info.get("uploader", "") or info.get("channel", ""),
        "upload_date": upload_date,
        "posted_at": posted_at,
        "thumbnail_url": info.get("thumbnail", ""),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "extractor": info.get("extractor", ""),
        "ingested_at": datetime.now().isoformat(),
        # Stash the raw info dict for downstream consumers that want fields
        # we didn't lift into the canonical shape.
        "raw": {
            k: info.get(k)
            for k in (
                "id", "webpage_url", "ext", "fps", "width", "height",
                "filesize_approx", "tags", "description",
            )
            if info.get(k) is not None
        },
    }


def use_local_file(file_path: str, session_dir: Path) -> dict[str, Any]:
    """Treat a local file as the session's source — no download needed.

    Three cases handled:
      1. File is OUTSIDE session_dir → symlink into session_dir/source.<ext>
         (or copy if symlink unavailable on this platform/permissions)
      2. File is ALREADY in session_dir as source.<ext> → no-op (used by the
         upload endpoint, which writes directly to that path)
      3. File doesn't exist or has unsupported extension → raise

    Idempotent: safe to call multiple times against the same session.
    """
    src = Path(file_path).resolve()
    if not src.exists():
        raise FileNotFoundError(file_path)
    if src.suffix.lower() not in VIDEO_EXTS:
        raise ValueError(
            f"Unsupported video extension: {src.suffix!r}. "
            f"Expected one of: {sorted(VIDEO_EXTS)}"
        )

    session_dir = Path(session_dir).resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / f"source{src.suffix.lower()}"

    # Case 2: file is already at the canonical location. No-op.
    if dest.exists() and dest.resolve() == src:
        pass
    # Case 1: file is elsewhere. Bring it under the session dir.
    elif not dest.exists():
        try:
            dest.symlink_to(src)
        except (OSError, NotImplementedError):
            import shutil
            shutil.copy2(src, dest)

    return {
        "file_path": str(dest),
        "title": src.stem,
        "duration_s": 0.0,                 # filled in by scene extractor
        "source_url": f"file://{src}",
        "platform": "file",
        "uploader": "",
        "upload_date": None,
        "posted_at": None,
        "thumbnail_url": "",
        "view_count": None,
        "like_count": None,
        "extractor": "local",
        "ingested_at": datetime.now().isoformat(),
        "raw": {},
    }
