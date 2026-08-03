"""Regression tests for T-1 — main.py wiring + shutdown had zero coverage.

Covers the two pieces extracted from main() for testability:
  - handle_silence_event: the IDLE/ERROR → LISTENING transition and
    SESSION_ENDED → clear() (the exact paths the B-1 epoch guard relies on).
  - run_pipeline: FIRST_COMPLETED shutdown — pending legs cancelled, a faulted
    leg's exception re-raised, capture/display stopped in the finally.
"""
import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock

import pytest

# main.py imports AudioCapture, which imports sounddevice (needs PortAudio at
# import time).  Stub it before importing main so this test runs on machines
# without the audio stack — mirrors tests/test_capture.py.  setdefault leaves a
# real sounddevice untouched where it exists.
sys.modules.setdefault("sounddevice", MagicMock())

from main import handle_silence_event, run_pipeline, install_io_executor, _IO_EXECUTOR_MAX_WORKERS
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

def _drainable_tracker():
    """A tracker stub whose drain() is an awaitable no-op — run_pipeline awaits
    it on shutdown so an in-flight end-of-session credit isn't torn (CONC-1)."""
    t = MagicMock()
    t.drain = AsyncMock()
    return t


@pytest.mark.asyncio
async def test_run_pipeline_cancels_pending_and_stops():
    capture, display = MagicMock(), MagicMock()

    async def quick():
        return

    async def forever():
        await asyncio.sleep(3600)

    done_leg = asyncio.create_task(quick())
    pending_leg = asyncio.create_task(forever())

    await run_pipeline([done_leg, pending_leg], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

    assert pending_leg.cancelled()
    capture.stop.assert_called_once()
    display.stop.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_drains_then_stops_subsystems_then_closes_pools():
    """CONC-1 + #61 + CRIT-3: shutdown must (1) await the tracker's in-flight
    credit tasks (drain) BEFORE tearing down capture/display, and (2) close BOTH
    thread pools LAST — after drain, because those credit writes run on them (the
    Discogs pool via writer.run, the shared I/O pool via lastfm.love), so closing
    earlier could reject an in-flight write. The owned I/O pool is shut with
    cancel_futures so queued work is dropped. Deleting either close (or reordering
    before drain) fails this test."""
    capture, display = MagicMock(), MagicMock()
    tracker = _drainable_tracker()
    discogs_http = MagicMock()
    io_executor = MagicMock()
    order = []
    tracker.drain.side_effect = lambda *a, **k: order.append("drain")
    capture.stop.side_effect = lambda: order.append("capture.stop")
    display.stop.side_effect = lambda: order.append("display.stop")
    discogs_http.close.side_effect = lambda: order.append("discogs.close")
    io_executor.shutdown.side_effect = lambda *a, **k: order.append("io.shutdown")

    async def quick():
        return

    await run_pipeline([asyncio.create_task(quick())], capture, display, tracker, discogs_http, io_executor)

    tracker.drain.assert_awaited_once()
    discogs_http.close.assert_called_once()
    io_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert order[0] == "drain"                 # credit finishes before anything else
    assert order == ["drain", "capture.stop", "display.stop", "discogs.close", "io.shutdown"]


@pytest.mark.asyncio
async def test_run_pipeline_still_drains_when_a_leg_faults():
    """A faulted leg must not abandon an in-flight credit — drain still runs in
    the finally before the exception propagates."""
    capture, display = MagicMock(), MagicMock()
    tracker = _drainable_tracker()

    async def boom():
        raise RuntimeError("leg died")

    discogs_http = MagicMock()
    with pytest.raises(RuntimeError, match="leg died"):
        await run_pipeline([asyncio.create_task(boom())], capture, display, tracker, discogs_http, MagicMock())

    tracker.drain.assert_awaited_once()
    capture.stop.assert_called_once()
    display.stop.assert_called_once()
    # #61: a faulted leg must not leak the dedicated executor — close() is in the
    # same finally, so it runs before the exception propagates.
    discogs_http.close.assert_called_once()


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
        await run_pipeline([boom_leg, pending_leg], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

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
            await run_pipeline([leg_a, leg_b], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "leg A died" in logged
    assert "leg B died" in logged
    capture.stop.assert_called_once()
    display.stop.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_shuts_down_the_io_executor_dropping_queued_work():
    """CRIT-3: run_pipeline owns the bounded I/O executor and shuts it down with
    cancel_futures at the end of teardown — so QUEUED blocking work is DROPPED at
    exit rather than waited on. On the interpreter's default pool that queued work
    gated process exit (asyncio.run awaits shutdown_default_executor, a wait=True
    join with no timeout)."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    io_executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    ran_queued = []

    def occupy():
        started.set()
        release.wait(timeout=2)

    def queued():
        ran_queued.append("ran")

    io_executor.submit(occupy)
    assert started.wait(timeout=1)       # the single worker is busy
    io_executor.submit(queued)           # this one sits in the queue

    capture, display = MagicMock(), MagicMock()

    async def quick():
        return

    await run_pipeline(
        [asyncio.create_task(quick())], capture, display,
        _drainable_tracker(), MagicMock(), io_executor,
    )

    # The executor was shut down — new submissions are rejected…
    with pytest.raises(RuntimeError):
        io_executor.submit(lambda: None)
    # …and the queued work was cancelled (cancel_futures), never run.
    release.set()
    time.sleep(0.1)
    assert ran_queued == [], "queued blocking work was NOT cancelled at shutdown"


@pytest.mark.asyncio
async def test_install_io_executor_routes_default_run_in_executor():
    """CRIT-3: after install_io_executor, every run_in_executor(None, …) runs on
    the OWNED bounded pool (thread-name prefix 'vnp-io'), not the interpreter's
    default — so the cancel_futures shutdown actually governs those calls. Without
    the set_default_executor inside, this routing is lost and the fix is inert."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    ex = install_io_executor(loop)
    try:
        assert isinstance(ex, ThreadPoolExecutor)
        assert ex._max_workers == _IO_EXECUTOR_MAX_WORKERS   # bounded, owned pool

        seen = {}

        def work():
            seen["thread"] = threading.current_thread().name

        await loop.run_in_executor(None, work)   # None → loop default → owned pool
        assert seen["thread"].startswith("vnp-io"), seen
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
