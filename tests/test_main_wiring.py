"""Regression tests for T-1 — main.py wiring + shutdown had zero coverage.

Covers the two pieces extracted from main() for testability:
  - handle_silence_event: the IDLE/ERROR → LISTENING transition and
    SESSION_ENDED → clear() (the exact paths the B-1 epoch guard relies on).
  - run_pipeline: FIRST_COMPLETED shutdown — pending legs cancelled, a faulted
    leg's exception re-raised, capture/display stopped in the finally.
"""
import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# main.py imports AudioCapture, which imports sounddevice (needs PortAudio at
# import time).  Stub it before importing main so this test runs on machines
# without the audio stack — mirrors tests/test_capture.py.  setdefault leaves a
# real sounddevice untouched where it exists.
sys.modules.setdefault("sounddevice", MagicMock())

from main import handle_silence_event, run_pipeline
from src.audio.silence import AudioEvent
from src.state.player_state import PlayerState, PlayerStatus


# ---------------------------------------------------------------------------
# handle_silence_event
# ---------------------------------------------------------------------------

def test_music_started_from_idle_enters_listening():
    state = PlayerState()
    tracker = MagicMock()
    handle_silence_event(AudioEvent.MUSIC_STARTED, state, tracker)
    assert state.status == PlayerStatus.LISTENING
    tracker.on_silence_event.assert_called_once_with(AudioEvent.MUSIC_STARTED)


def test_music_started_from_error_enters_listening():
    state = PlayerState()
    state.set_status(PlayerStatus.ERROR)
    handle_silence_event(AudioEvent.MUSIC_STARTED, state, MagicMock())
    assert state.status == PlayerStatus.LISTENING


def test_music_started_during_playing_keeps_now_playing_card():
    state = PlayerState()
    state.set_status(PlayerStatus.PLAYING)
    handle_silence_event(AudioEvent.MUSIC_STARTED, state, MagicMock())
    assert state.status == PlayerStatus.PLAYING  # not dropped to LISTENING


def test_session_ended_clears_and_bumps_epoch():
    state = PlayerState()
    state.set_status(PlayerStatus.PLAYING)
    epoch0 = state.session_epoch
    tracker = MagicMock()
    handle_silence_event(AudioEvent.SESSION_ENDED, state, tracker)
    assert state.status == PlayerStatus.IDLE
    assert state.session_epoch == epoch0 + 1  # B-1 epoch advances on clear()
    tracker.on_silence_event.assert_called_once_with(AudioEvent.SESSION_ENDED)


# ---------------------------------------------------------------------------
# run_pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_pipeline_cancels_pending_and_stops():
    capture, display = MagicMock(), MagicMock()

    async def quick():
        return

    async def forever():
        await asyncio.sleep(3600)

    done_leg = asyncio.create_task(quick())
    pending_leg = asyncio.create_task(forever())

    await run_pipeline([done_leg, pending_leg], capture, display)

    assert pending_leg.cancelled()
    capture.stop.assert_called_once()
    display.stop.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_reraises_faulted_leg_and_still_cleans_up():
    capture, display = MagicMock(), MagicMock()

    async def boom():
        raise RuntimeError("leg died")

    async def forever():
        await asyncio.sleep(3600)

    boom_leg = asyncio.create_task(boom())
    pending_leg = asyncio.create_task(forever())

    with pytest.raises(RuntimeError, match="leg died"):
        await run_pipeline([boom_leg, pending_leg], capture, display)

    # finally ran despite the re-raise…
    capture.stop.assert_called_once()
    display.stop.assert_called_once()
    # …and the other leg was cancelled.
    assert pending_leg.cancelled()


@pytest.mark.asyncio
async def test_run_pipeline_logs_every_faulted_leg(caplog):
    """B-14: when several legs die at once, ALL their exceptions are logged
    (not just the first), and one is still re-raised."""
    import logging

    capture, display = MagicMock(), MagicMock()

    async def boom_a():
        raise RuntimeError("leg A died")

    async def boom_b():
        raise ValueError("leg B died")

    leg_a = asyncio.create_task(boom_a(), name="legA")
    leg_b = asyncio.create_task(boom_b(), name="legB")
    # Let both finish so both land in `done` deterministically.
    await asyncio.gather(leg_a, leg_b, return_exceptions=True)

    with caplog.at_level(logging.ERROR):
        with pytest.raises((RuntimeError, ValueError)):
            await run_pipeline([leg_a, leg_b], capture, display)

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "leg A died" in logged
    assert "leg B died" in logged
    capture.stop.assert_called_once()
    display.stop.assert_called_once()
