"""Tests for narrative rollup helpers — Phase 1.

The LLM-backed roll_up() requires a server, so we don't test the round trip
here. We do test the deterministic helpers (fallback_rollup, _clean) and the
short-circuit behavior (single scene returns its description directly)."""
from cortex_vision.description.narrative import (
    _clean,
    build_scene_describer_prompt,
    fallback_rollup,
    roll_up,
)


def test_roll_up_empty_returns_empty():
    assert roll_up([]) == ""
    assert roll_up(["", "  ", None]) == ""  # type: ignore[list-item]


def test_roll_up_single_scene_short_circuit():
    """One scene = no LLM call needed; description IS the narrative."""
    out = roll_up(["A red car parked on a street."])
    assert out == "A red car parked on a street."


def test_fallback_rollup_basic():
    out = fallback_rollup([
        "Scene one happens",
        "Scene two follows",
        "Scene three concludes.",
    ])
    assert out == "Scene one happens. Scene two follows. Scene three concludes."


def test_fallback_rollup_skips_empty():
    out = fallback_rollup(["First", "", "Second"])
    assert out == "First. Second."


def test_fallback_rollup_handles_existing_punctuation():
    out = fallback_rollup(["Already!", "Question?", "Plain"])
    assert out == "Already! Question? Plain."


def test_clean_strips_think_blocks():
    """Qwen reasoning models emit <think>...</think> before the real answer."""
    raw = "<think>I should describe this</think>The actual narrative."
    assert _clean(raw) == "The actual narrative."


def test_clean_strips_surrounding_quotes():
    assert _clean('"A wrapped narrative."') == "A wrapped narrative."
    assert _clean("'single quoted'") == "single quoted"


def test_clean_strips_whitespace():
    assert _clean("   spacious   ") == "spacious"


def test_build_scene_describer_prompt_contents():
    prompt = build_scene_describer_prompt(
        scene_index=2, duration_s=4.5, keyframe_count=3,
    )
    assert "Scene 3" in prompt           # 1-indexed for human-friendliness
    assert "4.5" in prompt
    assert "3 keyframes" in prompt


def test_build_scene_describer_prompt_singular_keyframe():
    prompt = build_scene_describer_prompt(
        scene_index=0, duration_s=2.0, keyframe_count=1,
    )
    assert "1 keyframe" in prompt and "1 keyframes" not in prompt
