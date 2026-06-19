"""Regression tests for B-2 — _end_session re-entrancy across an album split.

The race: a SESSION_ENDED for record A is scheduled fire-and-forget, then —
before it runs — the user drops record B.  on_track_identified splits the
session (ends A, starts B).  The stale SESSION_ENDED then runs and, pre-fix,
saw a non-None session (B) and ended it prematurely, crediting/clearing a
session meant to keep running.

The fix binds each SESSION_ENDED to the session that was active when it fired
(`expected=`), and serializes the whole lifecycle under a lock, so a stale end
becomes a no-op.  These tests drive the interleaving deterministically.
"""
import pytest

from src.audio.silence import AudioEvent
from tests.test_listen_tracker import make_tracker, make_track


@pytest.mark.asyncio
async def test_stale_session_ended_does_not_end_new_session_after_split():
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    # Record A plays through its closer → creditable, latched to release 111.
    await tracker.on_track_identified(
        make_track("Master-Dik", release_id=111, instance_id=222)
    )
    session_a = tracker._session

    # Record B is dropped quickly → the split ends A (crediting it) and starts B.
    await tracker.on_track_identified(
        make_track("So What", release_id=999, instance_id=888)
    )
    session_b = tracker._session
    assert session_b is not session_a
    writer.increment_play_count.assert_called_once_with(111, 222)

    # The SESSION_ENDED that fired for A's silence now finally runs, bound to A.
    await tracker._end_session(expected=session_a)

    # It must NOT end B…
    assert tracker._session is session_b
    # …and must NOT have credited anything further.
    writer.increment_play_count.assert_called_once_with(111, 222)


@pytest.mark.asyncio
async def test_session_ended_for_current_session_still_ends_it():
    """The guard only suppresses *stale* ends; an end bound to the live session
    still works."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    live = tracker._session

    await tracker._end_session(expected=live)

    assert tracker._session is None
    writer.increment_play_count.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_session_ended_after_split_is_a_noop():
    """End-to-end via the real fire-and-forget path: schedule SESSION_ENDED,
    split before it runs, then let the loop drain — the new session survives."""
    import asyncio

    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Master-Dik", release_id=111, instance_id=222)
    )

    # SESSION_ENDED fires for A and is scheduled fire-and-forget (not yet run).
    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    assert len(tracker._bg_tasks) == 1

    # Before the task runs, record B is dropped → split.
    await tracker.on_track_identified(
        make_track("So What", release_id=999, instance_id=888)
    )
    session_b = tracker._session

    # Drain the scheduled SESSION_ENDED task.
    for _ in range(5):
        await asyncio.sleep(0)

    assert not tracker._bg_tasks
    assert tracker._session is session_b           # B was NOT ended
    writer.increment_play_count.assert_called_once_with(111, 222)


@pytest.mark.asyncio
async def test_lock_free_music_started_during_end_does_not_corrupt(tmp_path=None):
    """B-20: the lock-free MUSIC_STARTED → _start_session path is race-free.

    Reproduces the one interleaving that matters — a MUSIC_STARTED landing after
    an end has nulled `_session` but before its finalize completes — and shows
    no corruption: `_start_session` creates a FRESH session (never resurrects the
    ended one), and the detached old session still credits the correct release.
    """
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Master-Dik", release_id=111, instance_id=222)
    )  # record A, played through its closer → creditable
    session_a = tracker._session

    # An end nulls `_session` (as _end_session_locked does) BEFORE finalizing...
    tracker._session = None
    # ...and a MUSIC_STARTED fires in that window (the lock-free create path).
    tracker._start_session()
    session_b = tracker._session

    assert session_b is not session_a       # a fresh session, A not resurrected
    assert session_b.album_release_id is None  # B starts clean (no inherited latch)

    # The detached A is finalized (operating on the local ref, as the end does).
    await tracker._finalize_session(session_a)

    writer.increment_play_count.assert_called_once_with(111, 222)  # A credited
    assert tracker._session is session_b     # B untouched and still current


@pytest.mark.asyncio
async def test_real_session_ended_for_current_session_credits_once():
    """Sanity: with no split, the scheduled SESSION_ENDED still credits the
    live session exactly once."""
    import asyncio

    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Master-Dik", release_id=111, instance_id=222)
    )

    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    for _ in range(5):
        await asyncio.sleep(0)

    assert tracker._session is None
    writer.increment_play_count.assert_called_once_with(111, 222)
