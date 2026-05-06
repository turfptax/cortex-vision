"""Extract an audio track from a video file using ffmpeg.

Used by Mode 2 (file/URL) and Mode 3 (journal upload) when the user opts
into audio transcription. Produces a 16 kHz mono WAV — the format every
ASR provider (Whisper, Parakeet, etc.) consumes natively.

ffmpeg is required at runtime. Bundle it with the PyInstaller build (Phase 5)
or document the install requirement for dev mode. Use ``ffmpeg_available()``
at the top of the pipeline to detect missing-ffmpeg and skip transcription
gracefully.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    """Raised when ffmpeg is missing or fails to run."""


def ffmpeg_available() -> bool:
    """Best-effort check for ffmpeg on PATH.

    Used by the pipeline to skip audio extraction when ffmpeg isn't installed
    rather than failing the whole job.
    """
    return shutil.which("ffmpeg") is not None


def extract_audio_track(
    video_path: str | Path,
    output_path: str | Path,
    sample_rate: int = 16000,
    timeout: float = 600.0,
) -> Path:
    """Extract the audio track from `video_path` to `output_path` as mono WAV.

    Args:
        video_path: source video file (any container ffmpeg can decode)
        output_path: destination .wav path; parent dir is created if missing
        sample_rate: target sample rate in Hz (default 16000 for Whisper)
        timeout: seconds before the ffmpeg subprocess is killed

    Returns:
        Path to the written WAV file.

    Raises:
        FileNotFoundError: source video doesn't exist
        FfmpegError: ffmpeg missing on PATH, returned non-zero, or timed out
    """
    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if not ffmpeg_available():
        raise FfmpegError(
            "ffmpeg not found on PATH. Install ffmpeg and ensure it's on PATH, "
            "or skip audio transcription (transcribe_audio=False)."
        )

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",                                # overwrite without prompting
        "-loglevel", "error",                # only print errors
        "-i", str(src),
        "-ar", str(sample_rate),             # resample
        "-ac", "1",                           # mono
        "-vn",                                # no video
        "-acodec", "pcm_s16le",              # 16-bit signed little-endian PCM
        str(dest),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise FfmpegError(
            f"ffmpeg timed out after {timeout}s extracting audio from {src.name}"
        ) from e
    except FileNotFoundError as e:
        # `ffmpeg` not on PATH despite ffmpeg_available() check (race condition)
        raise FfmpegError(f"ffmpeg invocation failed: {e}") from e

    if result.returncode != 0:
        raise FfmpegError(
            f"ffmpeg exited {result.returncode}: {result.stderr.strip()[:500]}"
        )

    if not dest.exists() or dest.stat().st_size == 0:
        raise FfmpegError(
            f"ffmpeg reported success but produced no output at {dest}. "
            f"Source may have no audio track."
        )

    return dest


def has_audio_track(video_path: str | Path, timeout: float = 30.0) -> bool:
    """Check whether `video_path` contains at least one audio stream.

    Uses ffprobe (ships with ffmpeg). Returns False if ffprobe is missing
    rather than raising — the caller can decide whether to attempt extraction
    anyway or skip silently.
    """
    if shutil.which("ffprobe") is None:
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    return result.returncode == 0 and result.stdout.strip() == "audio"
