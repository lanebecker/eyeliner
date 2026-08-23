"""Regression tests for CONC-2 — the lifecycle lock must not be held across the
end-of-session Discogs/Last.fm writes.

Before the fix, `_end_session` held `_lifecycle_lock` for the whole of
`_finalize_session` (up to three executor-dispatched HTTP round trips with
bounded retry). `on_track_identified` — awaited inline by the recognition
pipeline — takes that same lock first, so a slow Play Count write for the record
that just ended blocked recognition of the next record for the full retry
window (measured 3.01s, ~120s worst case), starving the audio queue.

The fix detaches the session synchronously under the lifecycle lock and credits
it OUTSIDE that lock. A dedicated `_finalize_lock` still serializes the crediting
work, so moving it off the lifecycle lock does not let two detached sessions hit
the shared Discogs `requests.Session` (max_workers=2 pool) concurrently.
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.audio.silence import AudioEvent
from src.metadata.discogs.outcomes import (
    CollectionIdentity, PlayCountReadResult, PlayCountReadState,
)
from src.metadata.models import PlaySession
from src.tracking.listen_tracker import ListenTracker
from tests.test_listen_tracker import make_tracker, make_track


class _CancellationBarrierWriter:
    """Real executor seam whose first Discogs write survives cancellation."""

    def __init__(self, *, raise_first: bool = False):
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._state_lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.raise_first = raise_first
        self.calls = []
        self.active = 0
        self.maximum_active = 0

    async def run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, fn, *args
        )

    def set_play_count(self, *args):
        with self._state_lock:
            call_number = len(self.calls) + 1
            self.calls.append(args)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if call_number == 1:
                self.started.set()
                self.release.wait(timeout=5)
                if self.raise_first:
                    raise RuntimeError("late writer failure")
            return True
        finally:
            with self._state_lock:
                self.active -= 1

    def close(self):
        self.release.set()
        self._executor.shutdown(wait=True)


def _detached_creditable_session(release_id: int, instance_id: int) -> PlaySession:
    session = PlaySession()
    session.log_track(
        make_track("Cotton Crown", release_id=release_id, instance_id=instance_id)
    )
    session.log_track(
        make_track("Master-Dik", release_id=release_id, instance_id=instance_id)
    )
    return session


async def _wait_for_thread_event(event: threading.Event):
    await asyncio.get_running_loop().run_in_executor(None, event.wait)


@pytest.mark.asyncio
async def test_conc2_slow_session_credit_does_not_block_recognition():
    """A stuck end-of-session Play Count write must NOT hold the lifecycle lock:
    the next record's track is still recognised while the write is in flight."""
    tracker, writer = make_tracker()

    # Record A plays through its closer → creditable, latched to release 111.
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Cotton Crown", release_id=111, instance_id=222)
    )  # 182 gate: supporting track
    await tracker.on_track_identified(
        make_track("Master-Dik", release_id=111, instance_id=222)
    )
    session_a = tracker._session

    # Gate the Play Count write so the finalize blocks mid-flight, like a slow
    # domestic uplink. Everything else passes through.
    entered_write = asyncio.Event()
    release_write = asyncio.Event()

    async def gated_run(fn, *args):
        # #186: the retried WRITE is now set_play_count (read_play_count runs
        # once, before it). Gate the set — that is the call CONC-2 serializes.
        if fn is writer.set_play_count:
            entered_write.set()
            await release_write.wait()
        return fn(*args)

    writer.run = gated_run

    # A SESSION_ENDED for A begins finalizing and blocks on the stuck write.
    end_task = asyncio.create_task(tracker._end_session(expected=session_a))
    await asyncio.wait_for(entered_write.wait(), timeout=1.0)  # finalize is in flight
    assert tracker._session is None  # A has been detached

    # While A's credit is stuck, record B is confirmed. It must NOT block behind
    # A's write — on the pre-fix code the lifecycle lock is held by the finalize,
    # so this hangs and the wait_for times out.
    try:
        await asyncio.wait_for(
            tracker.on_track_identified(
                make_track("So What", release_id=999, instance_id=888)
            ),
            timeout=1.0,
        )
        recognized_b = True
    except asyncio.TimeoutError:
        recognized_b = False
    finally:
        release_write.set()          # unstick the write so the test can clean up
        await end_task

    assert recognized_b, "on_track_identified blocked behind the in-flight credit (CONC-2)"
    assert tracker._session is not None
    assert tracker._session.last_release_id == 999   # B really was logged
    writer.increment_play_count.assert_called_once_with(111, 222)  # A still credited


@pytest.mark.asyncio
async def test_conc2_finalizes_are_serialized_no_concurrent_writer_calls():
    """Moving finalize off the lifecycle lock must not let two detached sessions
    credit at once — the shared Discogs requests.Session / max_workers=2 pool
    assumes at most one writer call in flight. The dedicated finalize lock
    preserves that: two overlapping finalizes never hit the writer concurrently."""
    tracker, writer = make_tracker()

    # Two independent creditable, detached sessions (different releases).
    async def detached_creditable(release_id, instance_id):
        tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
        await tracker.on_track_identified(
            make_track("Cotton Crown", release_id=release_id, instance_id=instance_id)
        )  # 182 gate: supporting track
        await tracker.on_track_identified(
            make_track("Master-Dik", release_id=release_id, instance_id=instance_id)
        )
        s = tracker._session
        tracker._session = None
        return s

    s1 = await detached_creditable(111, 222)
    s2 = await detached_creditable(333, 444)

    # Count how many increment_play_count calls are inside the writer at once.
    inside = 0
    max_inside = 0
    gate = asyncio.Event()

    async def counting_run(fn, *args):
        nonlocal inside, max_inside
        # #186: set_play_count is the serialized write (read_play_count precedes it).
        if fn is writer.set_play_count:
            inside += 1
            max_inside = max(max_inside, inside)
            await gate.wait()      # hold the write open so overlap would be visible
            inside -= 1
        return fn(*args)

    writer.run = counting_run

    t1 = asyncio.create_task(tracker._finalize_detached(s1))
    t2 = asyncio.create_task(tracker._finalize_detached(s2))
    # Let both tasks run as far as they can; only ONE should be inside the writer.
    for _ in range(5):
        await asyncio.sleep(0)
    assert max_inside == 1, f"finalizes ran concurrently (max_inside={max_inside})"

    gate.set()
    await asyncio.gather(t1, t2)
    assert max_inside == 1                    # never two writer calls at once
    assert writer.increment_play_count.call_count == 2  # both did credit, serially
    assert writer.set_play_count.call_count == 2            # the serialized write ran twice


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_first", [False, True])
async def test_cancelled_finalize_keeps_lock_until_executor_writer_finishes(
    raise_first,
):
    """Cancellation must not admit another finalizer while its writer lives.

    Removing the cancellation-safe writer drain should make ``first`` finish
    immediately and let the second executor worker overlap the blocked first.
    Both late success and late failure must instead preserve the original
    cancellation after the worker exits.
    """
    tracker, writer = make_tracker()
    runner = _CancellationBarrierWriter(raise_first=raise_first)
    writer.run = runner.run
    writer.set_play_count = runner.set_play_count
    writer.read_play_count.return_value = PlayCountReadResult(
        PlayCountReadState.READY, 3, 0
    )
    first_session = _detached_creditable_session(111, 222)
    second_session = _detached_creditable_session(333, 444)
    first = asyncio.create_task(tracker._finalize_detached(first_session))
    second = None
    try:
        await asyncio.wait_for(_wait_for_thread_event(runner.started), timeout=1)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        first.cancel()  # repeated cancellation must not punch through the drain
        await asyncio.sleep(0)
        second = asyncio.create_task(tracker._finalize_detached(second_session))
        await asyncio.sleep(0.05)

        assert runner.active == 1
        assert len(runner.calls) == 1
        assert not first.done()
        assert not second.done()

        runner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second

        assert runner.maximum_active == 1
        assert len(runner.calls) == 2
        assert [call[:2] for call in runner.calls] == [(111, 222), (333, 444)]
    finally:
        runner.close()
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


@pytest.mark.asyncio
async def test_conc2_recovery_finalizer_does_not_hold_lifecycle_lock():
    """Recovery is finalize work, so a waiting resolver must not stall recognition."""
    tracker, writer = make_tracker()
    writer.read_play_count.return_value = PlayCountReadResult(
        PlayCountReadState.DEFINITIVE_INSTANCE_MISSING,
        observed_instance_ids=(88,),
    )
    entered_recovery = asyncio.Event()
    release_recovery = asyncio.Event()

    async def recovery(*_args):
        entered_recovery.set()
        await release_recovery.wait()
        return None

    tracker = ListenTracker(writer, recover_collection_instance=recovery)
    stale = PlaySession()
    stale.log_track(
        make_track(
            "Master-Dik", release_id=999, instance_id=77,
            resolve_key=("sonic youth", "sister"),
        )
    )
    finalize = asyncio.create_task(tracker._finalize_detached(stale))
    await asyncio.wait_for(entered_recovery.wait(), timeout=1)

    try:
        await asyncio.wait_for(
            tracker.on_track_identified(make_track("So What", release_id=333, instance_id=444)),
            timeout=1,
        )
        recognized = True
    except asyncio.TimeoutError:
        recognized = False
    finally:
        release_recovery.set()
        await finalize

    assert recognized, "recovery finalizer held _lifecycle_lock"
    assert tracker._session is not None
    assert tracker._session.album_release_id == 333


@pytest.mark.asyncio
async def test_conc2_recovery_finalizers_remain_serialized():
    """The recovery callback runs under the finalize queue, not concurrently."""
    _tracker, writer = make_tracker()
    recovery_entered = asyncio.Event()
    release_recovery = asyncio.Event()
    second_read_started = asyncio.Event()

    def read(release_id, instance_id):
        if instance_id == 77:
            return PlayCountReadResult(
                PlayCountReadState.DEFINITIVE_INSTANCE_MISSING,
                observed_instance_ids=(88,),
            )
        second_read_started.set()
        return PlayCountReadResult(PlayCountReadState.READY, 3, 0)

    writer.read_play_count.side_effect = read
    original_run = writer.run

    async def run(fn, *args):
        return await original_run(fn, *args)

    writer.run = run

    async def recovery(*_args):
        recovery_entered.set()
        await release_recovery.wait()
        return CollectionIdentity(999, 88)

    tracker = ListenTracker(writer, recover_collection_instance=recovery)
    stale = PlaySession()
    stale.log_track(
        make_track(
            "Master-Dik", release_id=999, instance_id=77,
            resolve_key=("sonic youth", "sister"),
        )
    )
    independent = PlaySession()
    independent.log_track(make_track("Master-Dik", release_id=333, instance_id=444))
    first = asyncio.create_task(tracker._finalize_detached(stale))
    await asyncio.wait_for(recovery_entered.wait(), timeout=1)
    second = asyncio.create_task(tracker._finalize_detached(independent))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second_read_started.is_set(), "second finalizer bypassed _finalize_lock"

    release_recovery.set()
    await asyncio.gather(first, second)
    assert second_read_started.is_set()
