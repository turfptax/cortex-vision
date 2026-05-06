"""Audio: WASAPI loopback (live, Phase 4 stretch), ffmpeg track extraction
(batch + journal), OpenAI-compatible Whisper transcription with provider chain.

Phase 6 audio shipped:
  - ffmpeg_extract.extract_audio_track: video -> 16kHz mono WAV
  - transcribe.transcribe_file: WAV -> TranscriptionResult with segments
  - transcribe.bucket_segments_by_scene: assign segments to scene windows
"""

from cortex_vision.audio.ffmpeg_extract import (
    FfmpegError,
    extract_audio_track,
    ffmpeg_available,
    has_audio_track,
)
from cortex_vision.audio.transcribe import (
    TranscriptSegment,
    TranscriptionResult,
    WhisperUnavailable,
    bucket_segments_by_scene,
    is_configured,
    transcribe_file,
)

__all__ = [
    "FfmpegError",
    "extract_audio_track",
    "ffmpeg_available",
    "has_audio_track",
    "TranscriptSegment",
    "TranscriptionResult",
    "WhisperUnavailable",
    "bucket_segments_by_scene",
    "is_configured",
    "transcribe_file",
]
