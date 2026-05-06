"""Narrative rollup — turn a list of scene descriptions into a coherent paragraph.

This is the piece that makes a video understandable as a story rather than a
list of independent observations. Runs after every scene has been described.

The rollup uses a *text* model — no images needed at this stage, since each
scene description already encodes what the vision model saw. This means the
narrative pass works against any LM Studio / OpenRouter model, including
small text-only models like Qwen 0.5-3B that ship with limited context.

Usage:
    from cortex_vision.description.narrative import roll_up
    paragraph = roll_up(
        scene_descriptions=[
            "A car driving down a country road at dusk.",
            "Close-up of a man's face, looking concerned.",
            ...
        ],
        title="My TikTok video",
    )
"""
from __future__ import annotations

from cortex_vision.description.lmstudio_client import LMStudioUnavailable, chat


_NARRATIVE_SYSTEM_PROMPT = (
    "You are a video summarization assistant. The user will give you a list "
    "of per-scene descriptions of a video, in chronological order. Your job "
    "is to write a single coherent narrative paragraph that captures what "
    "happens in the video as a whole.\n\n"
    "Guidelines:\n"
    "- Write in past tense, like recounting something that was watched.\n"
    "- Be specific about what happens, not abstract about themes.\n"
    "- Combine adjacent scenes that are part of the same action.\n"
    "- Don't speculate beyond what the descriptions say.\n"
    "- 2-5 sentences for short videos, up to 3 paragraphs for long ones.\n"
    "- No preamble, no 'In this video,' opener — start with the action.\n"
)


def roll_up(
    scene_descriptions: list[str],
    title: str | None = None,
    duration_s: float | None = None,
    model: str | None = None,
    max_tokens: int = 600,
) -> str:
    """Generate a narrative paragraph from chronological scene descriptions.

    Args:
        scene_descriptions: ordered list of per-scene descriptions (the
            output of the vision describer for each scene).
        title: optional video title to include as context.
        duration_s: optional video duration to scale the expected length.
        model: optional override for the configured CORTEX_VISION_LLM_MODEL.
            Falls back to the default if not specified.
        max_tokens: max output length.

    Returns:
        The narrative paragraph as a single string. Trailing whitespace
        stripped. Empty string if scene_descriptions is empty.

    Raises:
        LMStudioUnavailable: if the LLM server can't be reached or returns
            an error. Caller decides whether to fall back to concatenation
            or surface the error.
    """
    # Filter out empty descriptions (failed describer calls)
    non_empty = [d.strip() for d in scene_descriptions if d and d.strip()]
    if not non_empty:
        return ""

    # For a single scene, the description IS the narrative — no LLM call needed
    if len(non_empty) == 1:
        return non_empty[0]

    user_lines: list[str] = []
    if title:
        user_lines.append(f"Title: {title}")
    if duration_s is not None and duration_s > 0:
        user_lines.append(f"Duration: {duration_s:.0f} seconds")
    user_lines.append(f"Scenes ({len(non_empty)}):")
    for i, desc in enumerate(non_empty, 1):
        user_lines.append(f"{i}. {desc}")
    user_lines.append("")
    user_lines.append("Write the narrative paragraph(s).")

    messages = [
        {"role": "system", "content": _NARRATIVE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]

    response = chat(
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        temperature=0.3,           # low; we want descriptive accuracy, not creativity
    )
    return _clean(response)


def fallback_rollup(scene_descriptions: list[str]) -> str:
    """Deterministic fallback when the LLM is unavailable.

    Just concatenates scene descriptions with sentence-boundary normalization.
    Less coherent than the LLM rollup, but never fails.
    """
    parts: list[str] = []
    for desc in scene_descriptions:
        if not desc:
            continue
        d = desc.strip()
        if not d.endswith((".", "!", "?")):
            d += "."
        parts.append(d)
    return " ".join(parts)


def _clean(text: str) -> str:
    """Strip common LLM artifacts: leading/trailing whitespace, surrounding
    quotes, '<think>' blocks from reasoning models."""
    t = text.strip()
    # Strip <think>...</think> blocks emitted by Qwen reasoning models
    if "<think>" in t and "</think>" in t:
        end = t.find("</think>") + len("</think>")
        t = t[end:].strip()
    # Strip surrounding quotes if the model wrapped its output
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1].strip()
    return t


# Convenience: combined describer prompt builder used by batch.py per-scene.
# Lives here because it's part of the description module's prompt shape.
SCENE_DESCRIBER_SYSTEM = (
    "You are a video scene description assistant. The user will show you "
    "1-3 keyframes from a single scene of a video. Describe what is visible "
    "in the scene in 1-3 sentences.\n\n"
    "Guidelines:\n"
    "- Describe what's in frame, not what's implied.\n"
    "- Note people, objects, settings, actions, on-screen text.\n"
    "- If multiple keyframes show motion, describe the motion.\n"
    "- Be concrete. 'A red car parked on a street' not 'a vehicle'.\n"
    "- No preamble. Start with the description.\n"
)


def build_scene_describer_prompt(
    scene_index: int,
    duration_s: float,
    keyframe_count: int,
) -> str:
    """User-message text shown to the vision model alongside keyframes."""
    return (
        f"Scene {scene_index + 1}, {duration_s:.1f} seconds long, "
        f"{keyframe_count} keyframe{'s' if keyframe_count != 1 else ''}.\n\n"
        f"Describe what happens in this scene."
    )
