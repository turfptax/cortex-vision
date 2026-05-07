"""Audio transcription with three-path provider chain.

Provider resolution (highest priority first):

    1. CORTEX_VISION_WHISPER_URL / config file URL
       -- explicit OpenAI-compatible endpoint (LM Studio Whisper, custom server)

    2. cortex-desktop's bundled whisper.cpp
       -- detected at %APPDATA%/Cortex/whisper-cpp/whisper-cli.exe with a
          model file under %APPDATA%/Cortex/whisper-models/. Same install
          the overseer plugin uses for voice journals; we read the same
          binary + model files directly via subprocess. Zero coupling to
          cortex-desktop's API — just shared file conventions.

    3. OPENAI_API_KEY
       -- cloud Whisper API ($0.006/min, ~1s/min latency)

    None of the above raises WhisperUnavailable; the pipeline gracefully
    skips transcription rather than failing the job.

The whisper.cpp path lets users get free local transcription without
configuring anything in cortex-vision — they just install Cortex (which
sets up whisper.cpp for the overseer) and we automatically use the same
files. No re-download, no extra model storage.

Configuration env vars:

    CORTEX_VISION_WHISPER_URL     base URL ending in /v1
    CORTEX_VISION_WHISPER_KEY     auth key (LM Studio doesn't enforce it)
    CORTEX_VISION_WHISPER_MODEL   model id (default: whisper-1)
    OPENAI_API_KEY                cloud fallback
    OPENAI_BASE_URL               override OpenAI base
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import httpx

from cortex_vision import config as _cfg

logger = logging.getLogger(__name__)


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
    provider: str = ""        # "lmstudio_compat" | "openai" | "whisper_cpp"
    model: str = ""


# ---------------------------------------------------------------------------
# Endpoint types — HTTP-based vs local subprocess-based
# ---------------------------------------------------------------------------

@dataclass
class _HttpEndpoint:
    """OpenAI-compatible HTTP server (LM Studio, OpenAI cloud, etc.)."""
    name: str                                           # "lmstudio_compat" | "openai"
    url: str                                            # full URL ending in /audio/transcriptions
    api_key: str
    default_model: str


@dataclass
class _LocalWhisperCpp:
    """cortex-desktop's whisper.cpp binary + model on disk."""
    name: str = "whisper_cpp"
    cli_path: Path = None                               # type: ignore[assignment]
    model_path: Path = None                             # type: ignore[assignment]


_AnyEndpoint = Union[_HttpEndpoint, _LocalWhisperCpp]


# ---------------------------------------------------------------------------
# whisper.cpp detection — read cortex-desktop's install
# ---------------------------------------------------------------------------

# Larger models = better accuracy. Pick the highest-quality one available.
# Order matches what cortex-desktop's overseer transcribe router downloads.
_WHISPER_CPP_MODEL_PREFERENCE = (
    "ggml-large-v3.bin",
    "ggml-large-v3-turbo.bin",
    "ggml-large-v2.bin",
    "ggml-large.bin",
    "ggml-medium.bin",
    "ggml-medium.en.bin",
    "ggml-small.bin",
    "ggml-small.en.bin",
    "ggml-base.bin",
    "ggml-base.en.bin",
)


def _whisper_cpp_appdata_root() -> Path | None:
    """Return %APPDATA%/Cortex if APPDATA is set, else None."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Cortex"


def find_whisper_cli() -> Path | None:
    r"""Return the path to a usable whisper-cli binary, or None.

    Search order (first match wins):

      1. CORTEX_VISION_WHISPER_CLI env var — explicit override
      2. cortex-desktop's PyInstaller install — where the overseer's
         transcribe router actually bundles whisper-cli:
           %ProgramFiles(x86)%\CortexHub\_internal\backend\bin\whisper-cli.exe
           %ProgramFiles%\CortexHub\_internal\backend\bin\whisper-cli.exe
           %LOCALAPPDATA%\Programs\CortexHub\_internal\backend\bin\whisper-cli.exe
      3. %APPDATA%\Cortex\whisper-cpp\whisper-cli.exe — manual install fallback
      4. PATH lookup via shutil.which

    The cortex-desktop install paths are the most common source on
    Windows; v0.3.5 only checked path #3 and missed every regular install.
    """
    name = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"

    # 1. Explicit env override
    env = os.environ.get("CORTEX_VISION_WHISPER_CLI", "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p

    # 2. cortex-desktop's PyInstaller install — the canonical Windows location
    cortex_desktop_roots: list[Path] = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        v = os.environ.get(var)
        if not v:
            continue
        cortex_desktop_roots.append(Path(v) / "CortexHub")
    # LOCALAPPDATA per-user installer convention
    if localappdata := os.environ.get("LOCALAPPDATA"):
        cortex_desktop_roots.append(Path(localappdata) / "Programs" / "CortexHub")

    for root in cortex_desktop_roots:
        candidate = root / "_internal" / "backend" / "bin" / name
        if candidate.is_file():
            return candidate

    # 3. APPDATA/Cortex/whisper-cpp/ — manual install fallback (v0.3.5 path)
    appdata_root = _whisper_cpp_appdata_root()
    if appdata_root is not None:
        candidate = appdata_root / "whisper-cpp" / name
        if candidate.is_file():
            return candidate

    # 4. PATH lookup — last resort
    import shutil
    found = shutil.which(name)
    if found:
        return Path(found)

    return None


def find_whisper_model() -> Path | None:
    """Return the path to a usable whisper.cpp model file, or None.

    Prefers the highest-accuracy model present (large > medium > small > base).
    cortex-desktop's overseer downloads a single model on demand; whichever
    one the user picked there is the one we'll use too.
    """
    root = _whisper_cpp_appdata_root()
    if root is None:
        return None
    models_dir = root / "whisper-models"
    if not models_dir.is_dir():
        return None

    for name in _WHISPER_CPP_MODEL_PREFERENCE:
        p = models_dir / name
        if p.is_file():
            return p

    # Fall back to any ggml-*.bin if the user has a non-canonical model
    found = sorted(models_dir.glob("ggml-*.bin"))
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

def _resolve_endpoint(model_override: str | None = None) -> _AnyEndpoint:
    """Pick the active provider per the three-path order documented at the
    top of this module."""
    # Path 1: explicit Whisper config (file or CORTEX_VISION_WHISPER_URL)
    transcribe_cfg = _cfg.get_transcribe_config()
    whisper_url = (transcribe_cfg.get("url") or "").strip()
    if whisper_url:
        base = whisper_url.rstrip("/")
        return _HttpEndpoint(
            name="lmstudio_compat",
            url=f"{base}/audio/transcriptions",
            api_key=transcribe_cfg.get("api_key") or "lm-studio",
            default_model=(
                model_override
                or transcribe_cfg.get("model")
                or "whisper-1"
            ),
        )

    # Path 2: shared whisper.cpp install from cortex-desktop's overseer
    cli = find_whisper_cli()
    model = find_whisper_model()
    if cli is not None and model is not None:
        return _LocalWhisperCpp(cli_path=cli, model_path=model)

    # Path 3: OpenAI cloud fallback
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        return _HttpEndpoint(
            name="openai",
            url=f"{base}/audio/transcriptions",
            api_key=openai_key,
            default_model=model_override or "whisper-1",
        )

    raise WhisperUnavailable(
        "No transcription provider configured. Either:\n"
        "  1. Configure a Whisper-compatible URL in cortex-vision settings, OR\n"
        "  2. Have cortex-desktop's overseer install whisper.cpp (it lives at\n"
        "     %APPDATA%/Cortex/whisper-cpp/ when set up), OR\n"
        "  3. Set OPENAI_API_KEY for the cloud Whisper API."
    )


def is_configured() -> bool:
    """True if any provider is set up. Never raises."""
    try:
        _resolve_endpoint()
        return True
    except WhisperUnavailable:
        return False


def active_provider_info() -> dict:
    """Return what the diagnostics endpoint should display about transcription.

    Tells the user which path is currently active so they understand which
    provider their job will hit. No secrets in the output.
    """
    try:
        endpoint = _resolve_endpoint()
    except WhisperUnavailable:
        return {
            "configured": False,
            "provider": None,
            "url": None,
        }

    if isinstance(endpoint, _LocalWhisperCpp):
        return {
            "configured": True,
            "provider": "whisper_cpp",
            "cli_path": str(endpoint.cli_path),
            "model_path": str(endpoint.model_path),
            "model": endpoint.model_path.name,
        }
    return {
        "configured": True,
        "provider": endpoint.name,
        "url": endpoint.url.rsplit("/audio/transcriptions", 1)[0],
        "model": endpoint.default_model,
    }


# ---------------------------------------------------------------------------
# Transcription API — dispatches by endpoint type
# ---------------------------------------------------------------------------

def transcribe_file(
    wav_path: str | Path,
    model: str | None = None,
    language: str | None = None,
    timeout: float = 300.0,
) -> TranscriptionResult:
    """Transcribe a WAV file via the active provider."""
    src = Path(wav_path)
    if not src.exists():
        raise FileNotFoundError(str(src))

    endpoint = _resolve_endpoint(model_override=model)

    if isinstance(endpoint, _LocalWhisperCpp):
        return _transcribe_via_whisper_cpp(endpoint, src, language, timeout)
    return _transcribe_via_http(endpoint, src, language, timeout)


# ---------------------------------------------------------------------------
# HTTP path (LM Studio / OpenAI)
# ---------------------------------------------------------------------------

def _transcribe_via_http(
    endpoint: _HttpEndpoint,
    src: Path,
    language: str | None,
    timeout: float,
) -> TranscriptionResult:
    files = {
        "file": (src.name, src.open("rb"), "audio/wav"),
    }
    data: dict[str, str] = {
        "model": endpoint.default_model,
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

    return _parse_http_response(response.json(), endpoint)


def _parse_http_response(
    payload: dict, endpoint: _HttpEndpoint
) -> TranscriptionResult:
    """OpenAI-compat verbose_json shape:
        { "text": "...", "language": "...", "segments": [{start, end, text}, ...] }
    """
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
            continue

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


# ---------------------------------------------------------------------------
# whisper.cpp subprocess path
# ---------------------------------------------------------------------------

def _transcribe_via_whisper_cpp(
    endpoint: _LocalWhisperCpp,
    src: Path,
    language: str | None,
    timeout: float,
) -> TranscriptionResult:
    """Invoke cortex-desktop's whisper-cli on a normalized 16kHz mono WAV.

    Flags follow cortex-desktop's `routers/transcribe.py` invocation pattern:
        -m <model>     model file
        -f <wav>       input
        -oj            output JSON
        -of <base>     output base path (whisper-cli adds .json)
        -l <lang>      optional language hint (en, es, fr, ...)
        -t <threads>   thread count (default = cpu_count)

    Output JSON format (whisper.cpp v1.5+):
        {
          "transcription": [
            {"timestamps": {"from": "00:00:00,000", "to": "00:00:03,500"},
             "offsets": {"from": 0, "to": 3500},
             "text": " hello world"},
            ...
          ]
        }
    """
    output_base = src.parent / src.stem
    output_json = output_base.with_suffix(".json")

    cmd: list[str] = [
        str(endpoint.cli_path),
        "-m", str(endpoint.model_path),
        "-f", str(src),
        "-oj",
        "-of", str(output_base),
        # CPU-only mode. Default whisper.cpp builds attempt CUDA init via
        # ggml's GPU backend selector, and on machines with mismatched CUDA
        # toolkits / no compatible GPU / Optimus-style hybrid setups this
        # crashes natively (exit 0xC000041D / STATUS_FATAL_USER_CALLBACK_
        # EXCEPTION). The crash can take down the surrounding sidecar
        # because the long blocking subprocess + health-check timeouts
        # trigger the plugin manager's restart_on_crash watchdog.
        # CPU is fast enough for our use case (~real-time on modern cores).
        "-ng",
    ]
    if language:
        cmd.extend(["-l", language])

    logger.info(
        "running whisper.cpp: model=%s wav=%s",
        endpoint.model_path.name, src.name,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise WhisperUnavailable(
            f"whisper-cli timed out after {timeout}s on {src.name}"
        ) from e
    except OSError as e:
        raise WhisperUnavailable(
            f"whisper-cli failed to launch: {e}"
        ) from e

    if result.returncode != 0:
        raise WhisperUnavailable(
            f"whisper-cli exit {result.returncode}: {result.stderr[:300].strip()}"
        )

    if not output_json.exists():
        raise WhisperUnavailable(
            "whisper-cli ran but produced no output JSON"
        )

    try:
        with open(output_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise WhisperUnavailable(
            f"could not read whisper-cli output {output_json}: {e}"
        ) from e
    finally:
        try:
            output_json.unlink()
        except OSError:
            pass

    return _parse_whisper_cpp_response(payload, endpoint)


def _parse_whisper_cpp_response(
    payload: dict, endpoint: _LocalWhisperCpp
) -> TranscriptionResult:
    """whisper.cpp's JSON shape is different from OpenAI's — uses millisecond
    offsets nested under `transcription[].offsets.from`/`.to`."""
    chunks = payload.get("transcription") or []
    full_text_parts: list[str] = []
    segments: list[TranscriptSegment] = []

    for chunk in chunks:
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        offsets = chunk.get("offsets") or {}
        try:
            start_ms = int(offsets.get("from", 0))
            end_ms = int(offsets.get("to", 0))
        except (TypeError, ValueError):
            continue
        full_text_parts.append(text)
        segments.append(
            TranscriptSegment(
                start_s=start_ms / 1000.0,
                end_s=end_ms / 1000.0,
                text=text,
            )
        )

    full_text = " ".join(full_text_parts)
    # whisper.cpp doesn't return language at the top level by default;
    # it lives under "result" if the user passed --print-progress
    language = (payload.get("result") or {}).get("language")

    return TranscriptionResult(
        full_text=full_text,
        segments=segments,
        language=language,
        provider="whisper_cpp",
        model=endpoint.model_path.name,
    )


# ---------------------------------------------------------------------------
# Per-scene bucketing — same for both providers
# ---------------------------------------------------------------------------

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
