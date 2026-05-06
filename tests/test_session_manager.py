"""SessionManager CRUD tests — Phase 1."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cortex_vision.models.schemas import SceneEntry, TranscriptEntry
from cortex_vision.pipeline.session_manager import (
    SessionManager,
    SessionTransitionError,
)


def _sm(tmp_path: Path) -> SessionManager:
    return SessionManager(db_path=tmp_path / "sessions.db")


def test_create_and_get(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(
        mode="file",
        source={"kind": "url", "url": "https://example.com/v.mp4"},
    )
    assert s.id
    assert s.status == "queued"

    fetched = sm.get(s.id)
    assert fetched is not None
    assert fetched.id == s.id
    assert fetched.source["url"] == "https://example.com/v.mp4"
    assert fetched.scenes == []
    assert fetched.transcript == []


def test_get_missing_returns_none(tmp_path: Path):
    sm = _sm(tmp_path)
    assert sm.get("does-not-exist") is None


def test_list_most_recent_first(tmp_path: Path):
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})
    b = sm.create(mode="file", source={"url": "b"})
    c = sm.create(mode="journal", source={"kind": "screen_recording"})

    listed = sm.list(limit=10)
    assert [s.id for s in listed] == [c.id, b.id, a.id]

    only_files = sm.list(mode="file")
    assert {s.id for s in only_files} == {a.id, b.id}


def test_list_filter_by_status(tmp_path: Path):
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})
    b = sm.create(mode="file", source={"url": "b"})
    sm.update_status(a.id, "capturing")

    queued = sm.list(status="queued")
    capturing = sm.list(status="capturing")
    assert {s.id for s in queued} == {b.id}
    assert {s.id for s in capturing} == {a.id}


def test_list_filter_by_pushed(tmp_path: Path):
    """The common bridge query: status=complete AND pushed=false."""
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})
    b = sm.create(mode="file", source={"url": "b"})
    c = sm.create(mode="file", source={"url": "c"})

    # Drive a + b to complete via the state machine
    for s in (a, b, c):
        sm.update_status(s.id, "capturing")
        sm.update_status(s.id, "describing")
        sm.update_status(s.id, "narrating")
        sm.update_status(s.id, "complete")

    # Mark only `a` as pushed
    sm.mark_pushed_to_overseer(a.id)

    pushed = sm.list(pushed=True)
    not_pushed = sm.list(pushed=False)
    assert {s.id for s in pushed} == {a.id}
    assert {s.id for s in not_pushed} == {b.id, c.id}

    # The bridge query: complete AND not pushed
    bridge_query = sm.list(status="complete", pushed=False)
    assert {s.id for s in bridge_query} == {b.id, c.id}


def test_list_filters_combine(tmp_path: Path):
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})
    b = sm.create(mode="journal", source={"url": "b"})
    sm.update_status(a.id, "capturing")
    sm.update_status(b.id, "capturing")

    # mode=file AND status=capturing -> only `a`
    out = sm.list(mode="file", status="capturing")
    assert {s.id for s in out} == {a.id}


def test_status_transitions_valid(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})

    sm.update_status(s.id, "capturing")
    sm.update_status(s.id, "processing")
    sm.update_status(s.id, "describing")
    sm.update_status(s.id, "narrating")
    sm.update_status(s.id, "complete")

    final = sm.get(s.id)
    assert final.status == "complete"
    assert final.ended_at is not None
    assert final.duration_s is not None
    assert final.duration_s >= 0


def test_status_transitions_invalid_rejected(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})

    # Can't skip directly from queued -> complete
    with pytest.raises(SessionTransitionError):
        sm.update_status(s.id, "complete")

    # Can't go backwards
    sm.update_status(s.id, "capturing")
    with pytest.raises(SessionTransitionError):
        sm.update_status(s.id, "queued")


def test_terminal_states_are_terminal(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})
    sm.update_status(s.id, "error", error="boom")

    with pytest.raises(SessionTransitionError):
        sm.update_status(s.id, "capturing")


def test_any_state_can_go_to_error(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})
    sm.update_status(s.id, "capturing")
    sm.update_status(s.id, "processing")
    sm.update_status(s.id, "error", error="describer crashed")

    final = sm.get(s.id)
    assert final.status == "error"
    assert final.error == "describer crashed"


def test_append_scene_persists_and_hydrates(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})

    scene = SceneEntry(
        index=0, start_s=0.0, end_s=4.2,
        keyframe_paths=["/tmp/a.jpg", "/tmp/b.jpg"],
        description="A red car",
        describer_model="lmstudio:smolvlm",
        objects=["car", "tree"],
        trigger_method="scenedetect",
    )
    sm.append_scene(s.id, scene)

    fetched = sm.get(s.id)
    assert len(fetched.scenes) == 1
    assert fetched.scenes[0].description == "A red car"
    assert fetched.scenes[0].keyframe_paths == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert fetched.scenes[0].objects == ["car", "tree"]


def test_append_scene_idempotent(tmp_path: Path):
    """Re-appending the same (session, scene_index) should replace, not duplicate."""
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})

    sm.append_scene(s.id, SceneEntry(index=0, start_s=0, end_s=1, description="first"))
    sm.append_scene(s.id, SceneEntry(index=0, start_s=0, end_s=1, description="second"))

    fetched = sm.get(s.id)
    assert len(fetched.scenes) == 1
    assert fetched.scenes[0].description == "second"


def test_append_transcript_and_set_narrative(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})

    sm.append_transcript(
        s.id,
        TranscriptEntry(
            timestamp=datetime.now(timezone.utc),
            text="hello world",
            duration_s=3.0,
            chunk_index=0,
        ),
    )
    sm.set_narrative(s.id, "A coherent story.")

    fetched = sm.get(s.id)
    assert len(fetched.transcript) == 1
    assert fetched.transcript[0].text == "hello world"
    assert fetched.narrative == "A coherent story."


def test_mark_pushed_to_overseer(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})
    assert sm.get(s.id).pushed_to_overseer is False

    sm.mark_pushed_to_overseer(s.id)
    assert sm.get(s.id).pushed_to_overseer is True


def test_update_progress(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})

    sm.update_progress(s.id, {"current_scene": 4, "total_scenes": 12})

    fetched = sm.get(s.id)
    assert fetched.progress == {"current_scene": 4, "total_scenes": 12}


def test_update_status_unknown_session(tmp_path: Path):
    sm = _sm(tmp_path)
    with pytest.raises(KeyError):
        sm.update_status("nope", "capturing")


def test_update_scene_description_partial(tmp_path: Path):
    sm = _sm(tmp_path)
    s = sm.create(mode="file", source={"url": "x"})
    sm.append_scene(s.id, SceneEntry(index=0, start_s=0, end_s=1, description=""))

    sm.update_scene_description(s.id, 0, "Now described", "lmstudio:smolvlm")

    fetched = sm.get(s.id)
    assert fetched.scenes[0].description == "Now described"
    assert fetched.scenes[0].describer_model == "lmstudio:smolvlm"


def test_cleanup_orphaned_sessions_transitions_non_terminals(tmp_path: Path):
    """Sessions in non-terminal states should be marked 'error' on cleanup."""
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})           # queued
    b = sm.create(mode="file", source={"url": "b"})
    c = sm.create(mode="file", source={"url": "c"})
    d = sm.create(mode="file", source={"url": "d"})

    sm.update_status(b.id, "capturing")
    sm.update_status(c.id, "capturing")
    sm.update_status(c.id, "describing")
    # d goes to complete cleanly — should NOT be touched
    sm.update_status(d.id, "capturing")
    sm.update_status(d.id, "describing")
    sm.update_status(d.id, "narrating")
    sm.update_status(d.id, "complete")

    orphans = sm.cleanup_orphaned_sessions(error_message="test interrupt")
    assert set(orphans) == {a.id, b.id, c.id}                 # all 3 non-terminal

    # All transitioned to error
    for sid in (a.id, b.id, c.id):
        s = sm.get(sid)
        assert s.status == "error"
        assert s.error == "test interrupt"

    # d still complete
    assert sm.get(d.id).status == "complete"


def test_cleanup_orphaned_sessions_no_op_when_clean(tmp_path: Path):
    """When everything's already in a terminal state, return empty list."""
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})
    sm.update_status(a.id, "capturing")
    sm.update_status(a.id, "describing")
    sm.update_status(a.id, "narrating")
    sm.update_status(a.id, "complete")

    orphans = sm.cleanup_orphaned_sessions()
    assert orphans == []


def test_cleanup_runs_idempotently(tmp_path: Path):
    """Running cleanup twice should be safe — second call is a no-op."""
    sm = _sm(tmp_path)
    a = sm.create(mode="file", source={"url": "a"})
    sm.update_status(a.id, "capturing")

    first = sm.cleanup_orphaned_sessions()
    second = sm.cleanup_orphaned_sessions()
    assert first == [a.id]
    assert second == []
