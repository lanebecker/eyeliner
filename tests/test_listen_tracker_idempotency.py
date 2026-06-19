"""Regression tests for B-8 — Play Count crediting must be idempotent.

`increment_play_count` is a GET→+1→POST read-modify-write.  If the same session
is finalized twice (the B-2 race, or a split misfire), the release would be
incremented twice.  A per-session `credited` flag guards against that.
"""
import pytest

from src.audio.silence import AudioEvent
from tests.test_listen_tracker import make_tracker, make_track


@pytest.mark.asyncio
async def test_double_finalize_credits_once():
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))
    session = tracker._session

    # First finalize credits the session…
    await tracker._finalize_session(session)
    # …a second finalize of the SAME session must be a no-op (idempotent).
    await tracker._finalize_session(session)

    writer.increment_play_count.assert_called_once_with(111, 222)


@pytest.mark.asyncio
async def test_credited_flag_set_after_crediting():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))
    session = tracker._session
    assert session.credited is False
    await tracker._finalize_session(session)
    assert session.credited is True


@pytest.mark.asyncio
async def test_non_creditable_session_stays_uncredited():
    """Only Side A played → not creditable → credited stays False, no double-guard
    surprises if finalized again."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))  # not last track
    session = tracker._session

    await tracker._finalize_session(session)
    assert session.credited is False
    writer.increment_play_count.assert_not_called()
