"""#454: per-track recognition scheduling — gate the costly backend call between
tracks, driven by Discogs duration + AudD match offset, with safety fallbacks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.audio.recognizer import (
    RawRecognitionResult, RecognitionLoop,
    _BOUNDARY_MARGIN_SECONDS, _MIN_REACTIVATE_SECONDS, _SAME_TRACK_RECHECK_SECONDS,
)
from tests.factories import make_recognition_config


def _loop(**cfg):
    config = make_recognition_config(**cfg)
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        return RecognitionLoop(config, MagicMock(), AsyncMock())


def _track(idx, durations):
    return SimpleNamespace(
        side_index=SimpleNamespace(global_index=idx),
        tracklist=[SimpleNamespace(duration=d) for d in durations],
    )


# --- duration extraction ---

def test_duration_from_current_track():
    loop = _loop()
    loop.state.current_track = _track(1, ["3:00", "4:12"])
    assert loop._current_track_duration_seconds() == 252.0

def test_duration_none_for_missing_track_bad_index_or_no_string():
    loop = _loop()
    loop.state.current_track = None
    assert loop._current_track_duration_seconds() is None
    loop.state.current_track = _track(None, ["3:00"])
    assert loop._current_track_duration_seconds() is None
    loop.state.current_track = _track(5, ["3:00"])       # out of range
    assert loop._current_track_duration_seconds() is None
    loop.state.current_track = _track(0, [None])         # no duration string
    assert loop._current_track_duration_seconds() is None


# --- idle scheduling ---

def test_go_idle_predicts_boundary_from_duration_and_offset():
    loop = _loop()
    loop.state.current_track = _track(0, ["4:00"])       # 240s
    loop._go_idle_until_boundary(SimpleNamespace(match_offset=30.0), now=1000.0)
    assert loop._recognition_active is False
    assert loop._reactivate_at == 1000.0 + (240.0 - 30.0) + _BOUNDARY_MARGIN_SECONDS

def test_go_idle_floors_a_near_end_match():
    loop = _loop()
    loop.state.current_track = _track(0, ["4:00"])
    loop._go_idle_until_boundary(SimpleNamespace(match_offset=239.0), now=0.0)
    assert loop._reactivate_at == _MIN_REACTIVATE_SECONDS + _BOUNDARY_MARGIN_SECONDS

def test_go_idle_uses_safety_interval_without_duration():
    loop = _loop(max_idle_recheck_seconds=240.0)
    loop.state.current_track = None
    loop._go_idle_until_boundary(SimpleNamespace(match_offset=None), now=100.0)
    assert loop._reactivate_at == 340.0 and loop._recognition_active is False

def test_back_off_after_error_uses_short_error_retry():
    # #460: ERROR backs off to the SHORT error retry, not the (long) safety interval —
    # 240s exceeds a track and could never confirm a later one.
    from src.audio.recognizer import _ERROR_RETRY_SECONDS
    loop = _loop(max_idle_recheck_seconds=120.0)
    loop._back_off_after_error(now=5.0)
    assert loop._reactivate_at == 5.0 + _ERROR_RETRY_SECONDS
    assert loop._recognition_active is False

def test_reidle_same_track_is_a_short_beat():
    loop = _loop()
    loop._reidle_same_track(now=0.0)
    assert loop._reactivate_at == _SAME_TRACK_RECHECK_SECONDS and loop._recognition_active is False


# --- gating decision ---

def test_wants_recognition_active_true():
    assert _loop()._wants_recognition(epoch=0, now=0.0) is True

def test_wants_recognition_idle_before_boundary_false():
    loop = _loop(); loop._recognition_active = False
    loop._reactivate_at = 1000.0; loop._last_seen_session_epoch = 0
    assert loop._wants_recognition(epoch=0, now=500.0) is False

def test_wants_recognition_idle_past_boundary_reactivates():
    loop = _loop(); loop._recognition_active = False
    loop._reactivate_at = 100.0; loop._last_seen_session_epoch = 0
    assert loop._wants_recognition(epoch=0, now=150.0) is True
    assert loop._recognition_active is True

def test_wants_recognition_new_session_reactivates_immediately():
    loop = _loop(); loop._recognition_active = False
    loop._reactivate_at = 1e12; loop._last_seen_session_epoch = 0
    assert loop._wants_recognition(epoch=1, now=0.0) is True     # needle drop
    assert loop._recognition_active is True and loop._last_seen_session_epoch == 1


# --- integration: run() skips the backend while idling ---

@pytest.mark.asyncio
async def test_run_idles_after_confirm_and_skips_backend():
    loop = _loop(confirmation_required=1)
    loop.state.session_epoch = 7
    loop.state.current_raw = None
    loop.state.current_track = None                              # -> safety idle
    loop.backend.recognize = AsyncMock(return_value=RawRecognitionResult("T", "A", "Al"))

    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)     # confirms (req=1)
    task = asyncio.create_task(loop.run())
    for _ in range(10):
        await asyncio.sleep(0)
        if loop.on_confirmed.await_count:
            break
    assert loop.backend.recognize.await_count == 1
    assert loop._recognition_active is False                     # idled after confirm

    loop._reactivate_at = 1e12                                   # force "not yet"
    await loop.enqueue(np.ones(4, dtype=np.float32), 44100)      # same epoch, idling
    for _ in range(10):
        await asyncio.sleep(0)
    assert loop.backend.recognize.await_count == 1              # skipped, still 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- cold-review fixes (#454) ---
from src.state.player_state import PlayerStatus  # noqa: E402

def test_listening_status_forces_active_recovery():
    """HIGH: reposition out of ERROR/IDLE re-enters LISTENING WITHOUT a session-epoch
    bump — recognition must re-arm at once, not wait out the idle timer."""
    loop = _loop()
    loop._recognition_active = False
    loop._reactivate_at = 1e12
    loop._last_seen_session_epoch = 3
    loop.state.status = PlayerStatus.LISTENING
    assert loop._wants_recognition(epoch=3, now=0.0) is True
    assert loop._recognition_active is True

def test_error_miss_rebacks_off_instead_of_hammering():
    """MEDIUM: a miss while ERROR is latched re-backs-off (not recognize-every-hop)."""
    loop = _loop(max_idle_recheck_seconds=120.0)
    loop.state.status = PlayerStatus.ERROR
    loop._recognition_active = True
    loop._reactivate_at = 0.0
    loop._register_miss()
    assert loop._recognition_active is False

def test_go_idle_clamps_garbage_duration_to_safety():
    """MEDIUM: a valid-but-garbage Discogs duration is clamped to the safety cap."""
    loop = _loop(max_idle_recheck_seconds=240.0)
    loop.state.current_track = _track(0, ["99:00"])   # 5940s
    loop._go_idle_until_boundary(SimpleNamespace(match_offset=0.0), now=0.0)
    assert loop._reactivate_at == 240.0

def test_parse_mmss_rejects_unicode_digits():
    from src.audio.recognizer import _parse_mmss
    assert _parse_mmss("²:00") is None

async def test_same_track_reidle_only_when_not_building():
    a = RawRecognitionResult("A", "Ar", "Al")
    loop = _loop(confirmation_required=2)
    loop.state.current_raw = a
    loop._pending_result = None; loop._pending_count = 0
    loop._recognition_active = True
    await loop._handle_result(a)                       # steady state -> re-idle
    assert loop._recognition_active is False

    loop2 = _loop(confirmation_required=2)
    loop2.state.current_raw = a
    loop2._pending_result = RawRecognitionResult("B", "Ar", "Al"); loop2._pending_count = 1
    loop2._recognition_active = True
    await loop2._handle_result(a)                      # building B -> stray A hit stays active
    assert loop2._recognition_active is True


# --- #460: a failed opener must not block later-track recognition on a gapless side ---
from src.state.player_state import PlayerStatus  # noqa: E402,F811
import asyncio as _asyncio  # noqa: E402

def test_error_backoff_uses_short_retry_not_safety_interval():
    from src.audio.recognizer import _ERROR_RETRY_SECONDS
    loop = _loop(max_idle_recheck_seconds=240.0)
    loop._back_off_after_error(now=0.0)
    assert loop._reactivate_at == _ERROR_RETRY_SECONDS
    assert _ERROR_RETRY_SECONDS < 240.0

async def test_later_track_confirms_across_error_retries():
    """#460: in ERROR, a later recognizable track confirms across two short-retry
    wakes ~30s apart (no needle lift). Hunk 1 (30s retry) is the fix; each wake is one
    recognition, and two consecutive same-track hits reach confirmation_required."""
    import src.audio.recognizer as rec
    loop = _loop(confirmation_required=2)
    loop.state.status = PlayerStatus.ERROR
    loop.state.current_raw = None
    loop.state.current_track = None
    loop._last_seen_session_epoch = 5
    peddler = RawRecognitionResult("Peddler", "Lunar Vacation", "X")

    # wake 1 @ t=100: reactivates (timer), recognizes -> pending 1, backs off to t=130
    loop._recognition_active = False
    loop._reactivate_at = 100.0
    assert loop._wants_recognition(epoch=5, now=100.0) is True
    with patch.object(rec.time, "monotonic", return_value=100.0):
        await loop._handle_result(peddler)
    assert loop.on_confirmed.await_count == 0
    assert loop._recognition_active is False and loop._reactivate_at == 130.0

    # wake 2 @ t=131 (>= 130): reactivates, recognizes same track -> pending 2 -> confirm
    assert loop._wants_recognition(epoch=5, now=131.0) is True
    with patch.object(rec.time, "monotonic", return_value=131.0):
        await loop._handle_result(peddler)
    assert loop.on_confirmed.await_count == 1
