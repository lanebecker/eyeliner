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
from unittest.mock import MagicMock, patch

import pytest

from src.audio.recognizer import RawRecognitionResult, RecognitionLoop
from src.state.player_state import PlayerState, PlayerStatus
from tests.factories import make_recognition_config


def make_loop(confirmation_required=2, error_after_misses=3):
    """RecognitionLoop on a real PlayerState, with a stub on_confirmed that
    simulates the commit's effect (a confirmed track lands as PLAYING) so the
    B-7 progress-reset assertions — which read state.status — hold.  The commit
    sequence itself is tested in tests/test_track_commit_service.py."""
    config = make_recognition_config(
        confirmation_required=confirmation_required,
        error_after_misses=error_after_misses,
    )
    state = PlayerState()

    async def commit(r):
        state.set_track(MagicMock())
        state.set_raw(r)
        return True

    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, commit)
    return loop, state


def raw(title, artist="Artist"):
    return RawRecognitionResult(title=title, artist=artist, album="Album")


# ---------------------------------------------------------------------------
# B-7
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_churn_and_misses_accumulate_to_error():
    loop, state = make_loop(confirmation_required=2, error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("Track 1"))   # churn (no confirm) → no-progress 1
    await loop._handle_result(None)             # miss              → no-progress 2
    await loop._handle_result(raw("Track 2"))   # churn             → no-progress 3 → ERROR

    assert state.status == PlayerStatus.ERROR


@pytest.mark.asyncio
async def test_same_as_current_resets_progress_counter():
    loop, state = make_loop(confirmation_required=2, error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("Track 1"))   # no-progress 1
    await loop._handle_result(None)             # no-progress 2
    state.current_raw = raw("Track 1")          # pretend Track 1 is now playing
    await loop._handle_result(raw("Track 1"))   # same as current → progress

    assert loop._miss_count == 0


@pytest.mark.asyncio
async def test_commit_resets_progress_counter():
    loop, state = make_loop(confirmation_required=1, error_after_misses=5)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("Track 1"))   # required=1 → commits immediately

    assert loop._miss_count == 0
    assert state.status == PlayerStatus.PLAYING

# B-11 (current_raw advanced only after a successful set_track) moved with the
# commit sequence to tests/test_track_commit_service.py.


# ---------------------------------------------------------------------------
# B-21 — churn telemetry (diagnostic only; no confirmation-behavior change)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_churn_logs_a_warning_after_threshold(caplog):
    """Alternating distinct one-off matches never confirm; after the threshold a
    warning is logged so a 'stuck' display has a breadcrumb (B-21)."""
    import logging
    from src.audio.recognizer import _CHURN_LOG_EVERY

    # High error_after_misses so ERROR doesn't fire before we reach the churn log.
    loop, state = make_loop(confirmation_required=2, error_after_misses=100)
    state.set_status(PlayerStatus.LISTENING)

    with caplog.at_level(logging.WARNING, logger="src.audio.recognizer"):
        # One short of the threshold → still silent (pins the trigger point so a
        # drifted modulo, e.g. == 1, is caught — not just a disabled log).
        for i in range(_CHURN_LOG_EVERY - 1):
            await loop._handle_result(raw(f"Track {i}"))  # all distinct → churn
        assert not any("churning" in r.message.lower() for r in caplog.records)

        await loop._handle_result(raw("Track last"))      # the Nth churn → warn

    assert loop._churn_count == _CHURN_LOG_EVERY
    assert any("churning" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_churn_counter_resets_on_confirm():
    """A real confirmation clears the churn counter — churn measures only the
    consecutive unconfirmable run."""
    loop, state = make_loop(confirmation_required=2, error_after_misses=100)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("A"))   # churn 1
    await loop._handle_result(raw("B"))   # churn 2
    assert loop._churn_count == 2
    await loop._handle_result(raw("B"))   # B confirms (count reaches 2) → reset

    assert loop._churn_count == 0


@pytest.mark.asyncio
async def test_churn_counter_resets_when_current_track_seen_again():
    """Seeing the track that's already playing means recognition works — not
    churn — so the counter resets."""
    loop, state = make_loop(confirmation_required=2, error_after_misses=100)
    state.set_status(PlayerStatus.LISTENING)

    await loop._handle_result(raw("A"))   # churn 1
    await loop._handle_result(raw("B"))   # churn 2
    state.current_raw = raw("A")          # pretend A is now the playing track
    await loop._handle_result(raw("A"))   # same as current → not churn

    assert loop._churn_count == 0
