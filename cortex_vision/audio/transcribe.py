"""Audio transcription via OpenAI-compatible /v1/audio/transcriptions APIs.

Provider chain (auto-detected at call time, no config required upfront):

    1. CORTEX_VISION_WHISPER_URL    — LM Studio with a Whisper model loaded,
                                       or any other OpenAI-compatible server
    2. OPENAI_API_KEY               — fallback to OpenAI's hosted Whisper API
                                       (whisper-1, $0.006/min)
    3. None of the above            — raises WhisperUnavailable; the caller
                                       in batch.py logs a warning and proceeds
                                       without per-scene spoken_text

Both providers accept the same multipart-form schema, so this client is
single-implementation. Response shape includes `segments` with `start`/`end`
seconds we can bucket per scene.

Configuration env vars:

    CORTEX_VISION_WHISPER_URL     base URL ending in /v1
    CORTEX_VISION_WHISPER_KEY     auth key (LM Studio doesn't enforce it)
    CORTEX_VISION_WHISPER_MODEL   model id (default: whisper-1)
    OPENAI_API_KEY                fallback path
    OPENAI_BASE_URL               override OpenAI base (default https://api.openai.com/v1)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from cortex_vision import config as _cfg


class WhisperUnavailable(RuntimeError):
    """Raised when no transcription provider is configured / reachable."""


@dataclass
class TranscriptSegment:
    """One timestamped chunk from a Whisper response."""
    start_s: float
    end_s: float
    text: str


@dataclass
class TranscriptionResult:
    """Full transcription output: full text + per-segment timestamps."""
    full_text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None
    provider: str = ""                                  # "lmstudio" | "openai"
    model: str = ""


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

@dataclass
class _Endpoint:
    name: str                                           # "lmstudio" | "openai"
    url: str                                            # full URL ending in /audio/transcriptions
    api_key: str
    default_model: str


def _resolve_endpoint(model_override: str | None = None) -> _Endpoint:
    """Pick the active provider per config-file > env-var > default order."""
    # Path 1: explicit Whisper config (file or CORTEX_VISION_WHISPER_*)
    transcribe_cfg = _cfg.get_transcribe_config()
    whisper_url = (transcribe_cfg.get("url") or "").strip()
    if whisper_url:
        base = whisper_url.rstrip("/")
        return _Endpoint(
            name="lmstudio",
            url=f"{base}/audio/transcriptions",
            api_key=transcribe_cfg.get("api_key") or "lm-studio",
            default_model=(
                model_override
                or transcribe_cfg.get("model")
                or "whisper-1"
            ),
        )

    # Path 2: OpenAI fallback (env var only — no UI for this currently)
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        return _Endpoint(
            name="openai",
            url=f"{base}/audio/transcriptions",
            api_key=openai_key,
            default_model=model_override or "whisper-1",
        )

    raise WhisperUnavailable(
        "No transcription provider configured. Set the Whisper URL in the "
        "Cortex Vision settings UI, or set OPENAI_API_KEY for the cloud fallback."
    )


def is_configured() -> bool:
    """True if any provider is set up. Safe to call without env vars."""
    try:
        _resolve_endpoint()
    except WhisperUnavailable:
        return False
    return True


# ---------------------------------------------------------------------------
# Transcription API
# ---------------------------------------------------------------------------

def transcribe_file(
    wav_path: str | Path,
    model: str | None = None,
    language: str | None = None,
    timeout: float = 300.0,
) -> TranscriptionResult:
    """Transcribe a WAV file via the active provider.

    Args:
        wav_path: 16 kHz mono WAV (from cortex_vision.audio.ffmpeg_extract)
        model: optional override of the configured default model
        language: optional ISO-639-1 code to skip auto-detection
        timeout: seconds before the request is aborted

    Returns:
        TranscriptionResult with full text and segments.

    Raises:
        FileNotFoundError: the WAV doesn't exist
        WhisperUnavailable: no provider configured / reachable / errored
    """
    src = Path(wav_path)
    if not src.exists():
        raise FileNotFoundError(str(src))

    endpoint = _resolve_endpoint(model_override=model)

    files = {
        "file": (src.name, src.open("rb"), "audio/wav"),
    }
    data: dict[str, str] = {
        "model": endpoint.default_model,
        # `verbose_json` returns segments with timestamps. OpenAI supports it;
        # LM Studio's Whisper backend supports it as of 2024-Q4 builds. If the
        # server can't deliver segments we fall back to a single segment
        # spanning [0, total_duration] from the plain `text` field.
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if language:
        data["language"] = language

    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint.url, headers=headers, data=data, files=files
            )
    except httpx.RequestError as e:
        raise WhisperUnavailable(
            f"Could not reach {endpoint.name} at {endpoint.url}: {e}"
        ) from e
    finally:
        files["file"][1].close()                        # type: ignore[union-attr]

    if response.status_code >= 400:
        raise WhisperUnavailable(
            f"{endpoint.name} returned {response.status_code}: "
            f"{response.text[:300]}"
        )

    return _parse_response(response.json(), endpoint)


# ---------------------------------------------------------------------------
# Response parsing & per-scene bucketing
# ---------------------------------------------------------------------------

def _parse_response(payload: dict, endpoint: _Endpoint) -> TranscriptionResult:
    """Normalize provider-specific response shape into TranscriptionResult."""
    full_text = (payload.get("text") or "").strip()
    raw_segments = payload.get("segments") or []
    language = payload.get("language")

    segments: list[TranscriptSegment] = []
    for seg in raw_segments:
        try:
            segments.append(
                TranscriptSegment(
                    start_s=float(seg["start"]),
                    end_s=float(seg["end"]),
                    text=(seg.get("text") or "").strip(),
                )
            )
        except (KeyError, TypeError, ValueError):
            # Malformed segment — skip it. Falling through to fallback is fine
            # since full_text still contains the content.
            continue

    # If the server didn't return segments (older LM Studio Whisper build, or
    # response_format unsupported), we'd lose the per-scene timing. Synthesize
    # a single segment with no specific bounds so the caller can still attach
    # it as a session-level transcript.
    if not segments and full_text:
        segments.append(
            TranscriptSegment(start_s=0.0, end_s=0.0, text=full_text)
        )

    return TranscriptionResult(
        full_text=full_text,
        segments=segments,
        language=language,
        provider=endpoint.name,
        model=endpoint.default_model,
    )


def bucket_segments_by_scene(
    segments: list[TranscriptSegment],
    scene_windows: list[tuple[float, float]],
) -> list[str]:
    """Assign each segment to the scene whose [start_s, end_s) contains its
    start time. Returns a list of joined-text strings, one per scene window.

    Bucketing by START time (not overlap) means each segment goes to exactly
    one scene — which is what people intuitively expect when reading scene
    descriptions side by side with spoken text. A segment that begins in
    scene N and ends in scene N+1 is attributed entirely to scene N.

    Out-of-bounds segments (before the first scene, after the last scene)
    are dropped. They're rare but happen with off-by-one rounding.
    """
    buckets: list[list[str]] = [[] for _ in scene_windows]
    for seg in segments:
        if not seg.text:
            continue
        for i, (start, end) in enumerate(scene_windows):
            if start <= seg.start_s < end:
                buckets[i].append(seg.text)
                break
    return [" ".join(b).strip() for b in buckets]
