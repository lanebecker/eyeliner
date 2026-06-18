"""Unit tests for RecognitionLoop confirmation logic.

Tests the 2-of-N consecutive match requirement that prevents flickering
when Shazam returns a noisy or wrong result for a single chunk.

No audio hardware, network, or actual Shazam API calls needed.
The backend is replaced with a MagicMock; we drive _handle_result() directly.
"""
from unittest.mock import MagicMock, AsyncMock, patch

import numpy as np
import pytest

from src.audio.recognizer import RawRecognitionResult, RecognitionLoop
from tests.factories import make_recognition_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_raw(title="So What", artist="Miles Davis", album="Kind of Blue"):
    return RawRecognitionResult(title=title, artist=artist, album=album)


def make_loop(confirmation_required=2):
    config = make_recognition_config(confirmation_required=confirmation_required)
    state = MagicMock()
    state.current_raw = None
    state.current_track = None

    resolver = MagicMock()
    resolved_track = MagicMock()
    resolver.resolve = AsyncMock(return_value=resolved_track)

    tracker = MagicMock()
    tracker.on_track_identified = AsyncMock()

    # Bypass _init_backend so we don't need ShazamIO installed during tests
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, resolver, tracker)

    return loop, state, resolver, tracker


# ---------------------------------------------------------------------------
# Single result never commits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_result_does_not_commit():
    loop, state, resolver, tracker = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw())

    resolver.resolve.assert_not_called()
    tracker.on_track_identified.assert_not_called()


@pytest.mark.asyncio
async def test_single_result_increments_pending_count_to_one():
    loop, state, _, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw())

    assert loop._pending_count == 1
    assert loop._pending_result is not None


# ---------------------------------------------------------------------------
# Two matching results commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_matching_results_commit_track():
    loop, state, resolver, tracker = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    resolver.resolve.assert_called_once_with(raw)
    tracker.on_track_identified.assert_called_once()


@pytest.mark.asyncio
async def test_commit_clears_pending_state():
    """After a commit, pending_count and pending_result should reset."""
    loop, state, _, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    assert loop._pending_count == 0
    assert loop._pending_result is None


@pytest.mark.asyncio
async def test_commit_updates_player_state():
    """_commit_track should call state.set_raw() and state.set_track()."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    state.set_raw.assert_called_once_with(raw)
    state.set_track.assert_called_once()


# ---------------------------------------------------------------------------
# Mismatch resets counter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_title_resets_pending_count():
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What", "Miles Davis"))
    await loop._handle_result(make_raw("Blue in Green", "Miles Davis"))  # Different

    assert loop._pending_count == 1
    assert loop._pending_result.title == "Blue in Green"
    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_different_artist_resets_pending_count():
    """Title match alone is not enough — artist must also match."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What", "Miles Davis"))
    await loop._handle_result(make_raw("So What", "Cover Band"))  # Same title, different artist

    assert loop._pending_count == 1
    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_mismatch_then_two_matching_commits():
    """A mismatch resets, but the next run of matching results still commits."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What"))     # count = 1
    await loop._handle_result(make_raw("Blue in Green"))  # reset, count = 1
    await loop._handle_result(make_raw("Blue in Green"))  # count = 2, commit

    resolver.resolve.assert_called_once()
    assert resolver.resolve.call_args[0][0].title == "Blue in Green"


# ---------------------------------------------------------------------------
# None result (unrecognized audio)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_none_result_does_not_commit():
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(None)

    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_none_result_clears_pending_state():
    loop, state, _, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw())  # count = 1
    await loop._handle_result(None)        # should reset

    assert loop._pending_count == 0
    assert loop._pending_result is None


@pytest.mark.asyncio
async def test_none_after_one_then_two_matching_does_not_commit():
    """None mid-sequence breaks the streak; must start fresh."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What"))  # count = 1
    await loop._handle_result(None)                  # reset
    await loop._handle_result(make_raw("So What"))  # count = 1 again (not 2)

    resolver.resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Same track as currently playing — skip silently
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_as_current_raw_does_not_re_commit():
    """If the recognized track matches what's already playing, do nothing."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = make_raw("So What", "Miles Davis")

    # Both results match the current track
    await loop._handle_result(make_raw("So What", "Miles Davis"))
    await loop._handle_result(make_raw("So What", "Miles Davis"))

    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_new_track_after_current_triggers_confirmation():
    """A different track from the current one should start the confirmation cycle."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = make_raw("So What", "Miles Davis")  # Already playing

    new_raw = make_raw("All Blues", "Miles Davis")
    await loop._handle_result(new_raw)
    await loop._handle_result(new_raw)

    resolver.resolve.assert_called_once_with(new_raw)


# ---------------------------------------------------------------------------
# Higher confirmation_required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_required_does_not_commit_on_two():
    loop, state, resolver, _ = make_loop(confirmation_required=3)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)  # count = 2

    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_three_required_commits_on_three():
    loop, state, resolver, _ = make_loop(confirmation_required=3)
    state.current_raw = None

    raw = make_raw()
    for _ in range(3):
        await loop._handle_result(raw)

    resolver.resolve.assert_called_once()


@pytest.mark.asyncio
async def test_one_required_commits_immediately():
    loop, state, resolver, _ = make_loop(confirmation_required=1)
    state.current_raw = None

    await loop._handle_result(make_raw())

    resolver.resolve.assert_called_once()


# ---------------------------------------------------------------------------
# No double-commit after a successful commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_double_commit_after_success():
    """After committing, subsequent identical results should not re-commit
    (because current_raw now matches)."""
    loop, state, resolver, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)  # Commits; state.set_raw(raw) is called

    # Simulate state.current_raw being updated (as set_raw would do in prod)
    state.current_raw = raw

    # More results for the same track — should be skipped
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    assert resolver.resolve.call_count == 1  # Still only called once


# ---------------------------------------------------------------------------
# enqueue drop-oldest policy (v1.3.5)
#
# When the recognition backend lags and the queue fills, the OLDEST chunk is
# evicted and the incoming one admitted — the freshest audio matters most
# for detecting a track change. (Previously the incoming chunk was discarded
# and stale audio kept being processed first.)
# ---------------------------------------------------------------------------

async def test_enqueue_drops_oldest_when_full():
    loop_obj, _, _, _ = make_loop()
    maxsize = loop_obj._audio_queue.maxsize

    for i in range(maxsize):
        await loop_obj.enqueue(np.full(4, float(i), dtype=np.float32), 44100)
    assert loop_obj._audio_queue.full()

    # One more: the oldest (marker 0.0) must yield to the newest.
    await loop_obj.enqueue(np.full(4, 99.0, dtype=np.float32), 44100)

    assert loop_obj._audio_queue.qsize() == maxsize
    first_audio, _ = loop_obj._audio_queue.get_nowait()
    assert first_audio[0] == 1.0   # marker 0.0 was evicted
    remaining = []
    while not loop_obj._audio_queue.empty():
        audio, _ = loop_obj._audio_queue.get_nowait()
        remaining.append(audio[0])
    assert remaining[-1] == 99.0   # the newest chunk was admitted


async def test_enqueue_below_capacity_keeps_everything():
    loop_obj, _, _, _ = make_loop()
    await loop_obj.enqueue(np.full(4, 1.0, dtype=np.float32), 44100)
    await loop_obj.enqueue(np.full(4, 2.0, dtype=np.float32), 44100)
    assert loop_obj._audio_queue.qsize() == 2
