"""Regression tests for B-7 and B-11.

B-7 — _miss_count was reset on EVERY non-None result, so neither interspersed
      None-misses nor unconfirmable churn (alternating distinct one-off matches)
      could ever accumulate to surface ERROR — the display spun on the boot
      screen forever.  It now resets only on real progress (same-as-current, or
      a confirmed commit) and counts churn toward ERROR.
B-11 — current_raw was advanced before set_track; if resolve/set_track failed,
      raw led the displayed track and the dedup treated the new track as
      "already playing," so the loop never re-attempted it.  current_raw is now
      advanced only after set_track succeeds.
"""
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from src.audio.recognizer import RawRecognitionResult, RecognitionLoop
from src.state.player_state import PlayerState, PlayerStatus
from tests.factories import make_recognition_config


def make_loop(confirmation_required=2, error_after_misses=3):
    config = make_recognition_config(
        confirmation_required=confirmation_required,
        error_after_misses=error_after_misses,
    )
    state = PlayerState()
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=MagicMock())
    tracker = MagicMock()
    tracker.on_track_identified = AsyncMock()
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, resolver, tracker)
    return loop, state, resolver, tracker


def raw(title, artist="Artist"):
    return RawRecognitionResult(title=title, artist=artist, album="Album")


# ---------------------------------------------------------------------------
# B-7
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_churn_and_misses_accumulate_to_error():
    loop, state, _, _ = make_loop(confirmation_required=2, error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("Track 1"))   # churn (no confirm) → no-progress 1
    await loop._handle_result(None)             # miss              → no-progress 2
    await loop._handle_result(raw("Track 2"))   # churn             → no-progress 3 → ERROR

    assert state.status == PlayerStatus.ERROR


@pytest.mark.asyncio
async def test_same_as_current_resets_progress_counter():
    loop, state, _, _ = make_loop(confirmation_required=2, error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("Track 1"))   # no-progress 1
    await loop._handle_result(None)             # no-progress 2
    state.current_raw = raw("Track 1")          # pretend Track 1 is now playing
    await loop._handle_result(raw("Track 1"))   # same as current → progress

    assert loop._miss_count == 0


@pytest.mark.asyncio
async def test_commit_resets_progress_counter():
    loop, state, _, _ = make_loop(confirmation_required=1, error_after_misses=5)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("Track 1"))   # required=1 → commits immediately

    assert loop._miss_count == 0
    assert state.status == PlayerStatus.PLAYING


# ---------------------------------------------------------------------------
# B-11
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_current_raw_not_advanced_when_resolve_fails():
    loop, state, resolver, _ = make_loop(confirmation_required=1)
    resolver.resolve = AsyncMock(side_effect=RuntimeError("resolve boom"))
    state.set_status(PlayerStatus.LISTENING)

    with pytest.raises(RuntimeError):
        await loop._commit_track(raw("Track 1"))

    # raw must NOT lead the (un-set) track — otherwise the loop would never
    # re-attempt Track 1.
    assert state.current_raw is None
    assert state.current_track is None


@pytest.mark.asyncio
async def test_current_raw_advanced_after_successful_commit():
    loop, state, resolver, _ = make_loop()
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)
    state.set_status(PlayerStatus.LISTENING)

    r = raw("Track 1")
    await loop._commit_track(r)

    assert state.current_track is meta
    assert state.current_raw is r
