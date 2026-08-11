"""#187 (R5-03) — a creditable album-split finalize must survive shutdown.

Before the fix, `on_track_identified` awaited `_finalize_detached` INLINE with a
bare await. The recognition pipeline leg that awaits `on_track_identified` is
cancelled by `run_pipeline` at shutdown BEFORE `drain()` runs, so a bare await
propagated that cancellation into the credit and drain() — which waits only on
`_bg_tasks` — never saw it, losing the completed play's credit.

The fix runs the finalize as a tracked `_bg_tasks` task and awaits it via
`asyncio.shield`: NORMAL operation still credits inline (unchanged timing), but
the task is detached from the leg's shutdown cancellation, so it survives into
drain(). drain() keeps its short bound — a stuck credit is abandoned + logged,
not allowed to stall the power-cycle.
"""
import asyncio

import pytest

from src.tracking.listen_tracker import ListenTracker
from src.audio.silence import AudioEvent
from tests.test_listen_tracker import make_writer_mock, make_track


async def _arm_closer(tracker):
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))  # arms


@pytest.mark.asyncio
async def test_normal_split_credits_inline_and_synchronously():
    """Timing is UNCHANGED in normal operation: when on_track_identified returns
    after a replay-boundary split, the detached session's credit has already
    landed (shielded inline await), and no task is left pending."""
    writer = make_writer_mock()
    tracker = ListenTracker(writer=writer)
    await _arm_closer(tracker)
    # Opener of the SAME release re-identified → split; A credited inline.
    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    writer.increment_play_count.assert_called_once_with(111, 222)
    assert not [t for t in tracker._bg_tasks if not t.done()]


@pytest.mark.asyncio
async def test_split_credit_survives_leg_cancellation_and_drain_completes_it():
    """Shutdown: the recognition leg is cancelled while the split write is in
    flight. The shielded task must NOT die with the leg — it stays in _bg_tasks
    so drain() completes the credit."""
    writer = make_writer_mock()
    write_started = asyncio.Event(); release = asyncio.Event()

    async def gated_run(fn, *a):
        if fn is writer.set_play_count:
            write_started.set()
            await release.wait()
        return fn(*a)

    writer.run = gated_run
    tracker = ListenTracker(writer=writer)
    await _arm_closer(tracker)

    leg = asyncio.create_task(
        tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    )
    await asyncio.wait_for(write_started.wait(), timeout=1.0)
    assert len([t for t in tracker._bg_tasks if not t.done()]) == 1

    # run_pipeline cancels the leg BEFORE drain().
    leg.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leg
    assert writer.increment_play_count.call_count == 0   # still gated, not lost

    # The shielded credit survived; drain() waits for it once the write clears.
    release.set()
    await tracker.drain(timeout=1.0)
    assert writer.increment_play_count.call_count == 1
    assert not tracker._bg_tasks


@pytest.mark.asyncio
async def test_drain_stays_bounded_when_the_split_write_is_stuck():
    """drain() keeps its short bound even with an unfinishable credit: it returns
    promptly (abandon + log LOST), never hanging the shutdown."""
    writer = make_writer_mock()
    started = asyncio.Event(); never = asyncio.Event()

    async def stuck_run(fn, *a):
        if fn is writer.set_play_count:
            started.set()
            await never.wait()
        return fn(*a)

    writer.run = stuck_run
    tracker = ListenTracker(writer=writer)
    await _arm_closer(tracker)

    leg = asyncio.create_task(
        tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    leg.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leg

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await tracker.drain(timeout=0.2)
    assert loop.time() - t0 < 1.0
    assert writer.increment_play_count.call_count == 0
