"""LM Studio client with vision (image) support.

Calls any OpenAI-compatible chat-completions endpoint — works against LM
Studio (default), but the URL/model is configurable so the same code drives
OpenRouter, OpenAI, Ollama, or any compatible server.

Configuration resolution (highest priority first):

    1. Per-call kwargs (model="...", timeout=N, etc.)
    2. Config file at %APPDATA%/Cortex/video/config.json (set via UI)
    3. Environment variables (CORTEX_VISION_LLM_URL, _MODEL, _KEY, _TIMEOUT)
    4. Built-in defaults

The config file is read on every call so UI changes take effect without
restarting the sidecar. Reads are cheap (single JSON parse, no caching) and
this only runs per-describe-call which is already seconds.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

from cortex_vision import config as _cfg


class LMStudioUnavailable(RuntimeError):
    """Raised when the LLM server is unreachable, returns an error status, or
    is missing required configuration. Callers should fall back to a
    deterministic offline behavior or surface the error to the user."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return _cfg.get_describer_config()["url"].rstrip("/")


def _api_key() -> str:
    return _cfg.get_describer_config()["api_key"]


def _default_model() -> str:
    return _cfg.get_describer_config()["model"]


def _default_timeout() -> float:
    try:
        return float(os.environ.get("CORTEX_VISION_LLM_TIMEOUT", "120"))
    except ValueError:
        return 120.0


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: float | None = None,
) -> str:
    """Plain text chat-completions. Returns the assistant message content."""
    use_model = model or _default_model()
    if not use_model:
        raise LMStudioUnavailable(
            "No model specified. Set CORTEX_VISION_LLM_MODEL or pass model=..."
        )

    payload = {
        "model": use_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    return _post_chat(payload, timeout or _default_timeout())


def chat_with_images(
    text: str,
    image_paths: list[str | Path] | None = None,
    image_bytes_list: list[bytes] | None = None,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    timeout: float | None = None,
) -> str:
    """Send a vision chat request — text plus one or more images.

    Either provide ``image_paths`` (read from disk) or ``image_bytes_list``
    (raw JPEG/PNG bytes). Both are encoded as base64 data URLs and passed in
    OpenAI-compatible multipart content blocks.

    The caller is responsible for picking a vision-capable model. If the
    configured model is text-only, the server will silently ignore the images
    (or return an error).
    """
    use_model = model or _default_model()
    if not use_model:
        raise LMStudioUnavailable(
            "No model specified. Set CORTEX_VISION_LLM_MODEL or pass model=..."
        )

    content_blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if image_paths:
        for path in image_paths:
            content_blocks.append(_image_block_from_path(path))
    if image_bytes_list:
        for raw in image_bytes_list:
            content_blocks.append(_image_block_from_bytes(raw, "image/jpeg"))

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content_blocks})

    payload = {
        "model": use_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    return _post_chat(payload, timeout or _default_timeout())


def list_models(timeout: float = 5.0) -> list[str]:
    """GET /v1/models on the configured server. Useful for verifying which
    model id to pass to chat_with_images()."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{_base_url()}/models", headers=_headers())
        r.raise_for_status()
    except httpx.RequestError as e:
        raise LMStudioUnavailable(f"Could not reach {_base_url()}: {e}") from e
    except httpx.HTTPStatusError as e:
        raise LMStudioUnavailable(
            f"{_base_url()}/models returned {e.response.status_code}: "
            f"{e.response.text[:200]}"
        ) from e

    data = r.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    out: list[str] = []
    for it in items or []:
        if isinstance(it, dict):
            out.append(it.get("id") or it.get("name") or str(it))
        else:
            out.append(str(it))
    return out


def health_check(timeout: float = 3.0) -> bool:
    """Return True if the server responds to /models, False otherwise.
    Never raises — for use in describer fallback chains."""
    try:
        list_models(timeout=timeout)
        return True
    except LMStudioUnavailable:
        return False


# ---------------------------------------------------------------------------
# Vision-model auto-detect (heuristic by model name)
# ---------------------------------------------------------------------------

_VISION_MARKERS = (
    "vl", "vision", "smolvlm", "llava", "minicpm", "moondream",
    "qwen-vl", "qwen2-vl", "phi-3-vision", "internvl", "cogvlm",
    "llama-3.2-vision", "pixtral", "florence",
)


def is_vision_model(model_id: str) -> bool:
    """Heuristic check for whether a model id refers to a vision-capable model.

    Lifted from VisualFast/server.py — works well for the common LM Studio
    catalog. False negatives are fine (the user can override); false positives
    just mean the request will fail at runtime with a clear error.
    """
    m = model_id.lower()
    return any(marker in m for marker in _VISION_MARKERS)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _image_block_from_path(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    mime, _ = mimetypes.guess_type(p.name)
    mime = mime or "image/jpeg"
    with open(p, "rb") as f:
        raw = f.read()
    return _image_block_from_bytes(raw, mime)


def _image_block_from_bytes(raw: bytes, mime: str) -> dict[str, Any]:
    b64 = base64.b64encode(raw).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    }


def _post_chat(payload: dict, timeout: float) -> str:
    url = f"{_base_url()}/chat/completions"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, headers=_headers(), content=json.dumps(payload))
    except httpx.RequestError as e:
        raise LMStudioUnavailable(f"Could not reach {url}: {e}") from e

    if r.status_code >= 400:
        raise LMStudioUnavailable(
            f"{url} returned {r.status_code}: {r.text[:300]}"
        )

    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LMStudioUnavailable(
            f"Malformed response from {url}: {data!r}"
        ) from e
