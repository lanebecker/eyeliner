"""Adversarial tests for ScrobbleDispatcher (R10-09 / R10-10, #422 / #423).

These attack the delivery/retry/lifecycle contract, not the happy path:
non-blocking enqueue, single-consumer serialization, RETRYABLE-only bounded
retry with the ORIGINAL timestamp, AMBIGUOUS no-retry, retry exhaustion,
overflow drop-oldest, bounded shutdown drain (including a wedged executor call
and cancellation during backoff), executor-exception survival, and task-leak
absence.  A real default executor is used so the run_in_executor boundary is
exercised for real (an async mock that dies with its caller cannot reproduce the
concurrency this class must survive).
"""
import asyncio
import threading
import time
import types

import pytest

from src.tracking.lastfm_client import ScrobbleResult
from src.tracking.scrobble_dispatcher import ScrobbleDispatcher


def _meta(artist="Miles Davis", title="So What"):
    return types.SimpleNamespace(artist=artist, title=title)


class FakeLastfm:
    """Synchronous stand-in for LastFmClient.scrobble_result.

    ``outcomes`` is a list consumed one per call; when exhausted it defaults to
    DELIVERED.  Each entry may be a ScrobbleResult or a zero-arg callable (to
    raise, or to block).  Every call and its timestamp is recorded (thread-safe).
    """

    def __init__(self, outcomes=None):
        self.enabled = True
        self._outcomes = list(outcomes or [])
        self._lock = threading.Lock()
        self.calls = []

    def scrobble_result(self, track, timestamp):
        with self._lock:
            self.calls.append((track, timestamp))
            outcome = self._outcomes.pop(0) if self._outcomes else ScrobbleResult.DELIVERED
        if callable(outcome):
            return outcome()
        return outcome

    @property
    def call_count(self):
        with self._lock:
            return len(self.calls)


async def _wait_until(predicate, timeout=2.0):
    """Yield to the loop until predicate() is true or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------
# enqueue is non-blocking; a slow delivery never stalls the event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enqueue_is_nonblocking_and_slow_delivery_does_not_stall_loop():
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(2.0)
        return ScrobbleResult.DELIVERED

    fake = FakeLastfm([block])
    d = ScrobbleDispatcher(fake, backoff=())
    d.start()
    try:
        t0 = time.monotonic()
        d.enqueue(_meta(), 111)
        assert time.monotonic() - t0 < 0.05          # enqueue did not await the network
        assert await _wait_until(started.is_set)      # worker took it, now blocked in executor
        # The loop is still responsive while the scrobble blocks off-loop.
        await asyncio.sleep(0)
        release.set()
        await d.drain()
        assert fake.call_count == 1
    finally:
        release.set()
        await d.drain()


# ---------------------------------------------------------------------------
# Delivery outcomes: DELIVERED, AMBIGUOUS (no retry), RETRYABLE (retry)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivered_calls_once():
    fake = FakeLastfm([ScrobbleResult.DELIVERED])
    d = ScrobbleDispatcher(fake, backoff=(0, 0))
    d.start()
    d.enqueue(_meta(), 111)
    await d.drain()
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_ambiguous_is_not_retried():
    fake = FakeLastfm([ScrobbleResult.AMBIGUOUS])
    d = ScrobbleDispatcher(fake, backoff=(0, 0))
    d.start()
    d.enqueue(_meta(), 111)
    await d.drain()
    assert fake.call_count == 1     # AMBIGUOUS: exactly one attempt, never retried


@pytest.mark.asyncio
async def test_retryable_then_delivered_reuses_original_timestamp():
    fake = FakeLastfm([ScrobbleResult.RETRYABLE, ScrobbleResult.DELIVERED])
    d = ScrobbleDispatcher(fake, backoff=(0, 0))
    d.start()
    d.enqueue(_meta(), 555)
    await d.drain()
    assert fake.call_count == 2
    # #383: the confirmation-time timestamp is reused UNCHANGED on the retry, so
    # Last.fm's (track, timestamp) de-dup prevents a double credit.
    assert [ts for _, ts in fake.calls] == [555, 555]


@pytest.mark.asyncio
async def test_retryable_exhausts_bounded_budget_then_drops_without_raising():
    # Always RETRYABLE → attempts = len(backoff)+1 = 3, then dropped (best-effort).
    fake = FakeLastfm([ScrobbleResult.RETRYABLE] * 10)
    d = ScrobbleDispatcher(fake, backoff=(0, 0))
    d.start()
    d.enqueue(_meta(), 111)
    await d.drain()
    assert fake.call_count == 3     # bounded: no infinite retry


# ---------------------------------------------------------------------------
# Single-consumer serialization + FIFO
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_consumer_processes_jobs_one_at_a_time_in_order():
    fake = FakeLastfm()
    d = ScrobbleDispatcher(fake, backoff=())
    d.start()
    d.enqueue(_meta(title="A"), 1)
    d.enqueue(_meta(title="B"), 2)
    d.enqueue(_meta(title="C"), 3)
    await d.drain()
    assert [ts for _, ts in fake.calls] == [1, 2, 3]     # FIFO, one consumer


# ---------------------------------------------------------------------------
# Overflow: drop-oldest, bounded memory (no running worker needed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overflow_drops_oldest_and_stays_bounded():
    fake = FakeLastfm()
    d = ScrobbleDispatcher(fake, maxsize=2, backoff=())
    # Do NOT start the worker, so nothing is consumed and the queue really fills.
    d.enqueue(_meta(title="oldest"), 1)
    d.enqueue(_meta(title="mid"), 2)
    d.enqueue(_meta(title="newest"), 3)     # overflow → drop the oldest (1)
    assert d._queue.qsize() == 2            # hard bound held
    remaining = [d._queue.get_nowait(), d._queue.get_nowait()]
    assert [ts for _, ts in remaining] == [2, 3]   # oldest (1) was dropped


# ---------------------------------------------------------------------------
# Shutdown drain: flush, then no leaked worker task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_flushes_then_leaves_no_worker_task():
    fake = FakeLastfm()
    d = ScrobbleDispatcher(fake, backoff=())
    d.start()
    worker = d._worker
    d.enqueue(_meta(), 1)
    d.enqueue(_meta(), 2)
    await d.drain()
    assert fake.call_count == 2         # everything queued was delivered
    assert d._worker is None            # no leaked task
    assert worker.done()                # the consumer actually stopped


@pytest.mark.asyncio
async def test_enqueue_after_drain_is_dropped():
    fake = FakeLastfm()
    d = ScrobbleDispatcher(fake, backoff=())
    d.start()
    await d.drain()
    d.enqueue(_meta(), 1)               # not accepting anymore
    assert d._queue.qsize() == 0


@pytest.mark.asyncio
async def test_drain_is_safe_when_never_started():
    d = ScrobbleDispatcher(FakeLastfm(), backoff=())
    await d.drain()                     # must not raise
    assert d._worker is None


# ---------------------------------------------------------------------------
# Bounded drain under a WEDGED executor call — must not hang shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_is_bounded_when_the_executor_call_is_wedged():
    release = threading.Event()
    started = threading.Event()

    def wedged():
        started.set()
        release.wait(5.0)
        return ScrobbleResult.DELIVERED

    fake = FakeLastfm([wedged])
    d = ScrobbleDispatcher(fake, backoff=())
    d.start()
    try:
        d.enqueue(_meta(), 1)
        assert await _wait_until(started.is_set)
        t0 = time.monotonic()
        await d.drain(timeout=0.2)      # the join() cannot complete → bounded, then cancel
        assert time.monotonic() - t0 < 2.0     # did NOT wait out the 5s wedge
        assert d._worker is None
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Cancellation during retry backoff — clean, no hang
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_during_retry_backoff_cancels_cleanly():
    # First attempt RETRYABLE, then a long backoff the drain must interrupt.
    fake = FakeLastfm([ScrobbleResult.RETRYABLE, ScrobbleResult.DELIVERED])
    d = ScrobbleDispatcher(fake, backoff=(5.0,))
    d.start()
    d.enqueue(_meta(), 1)
    assert await _wait_until(lambda: fake.call_count >= 1)   # first attempt done, now sleeping
    t0 = time.monotonic()
    await d.drain(timeout=0.2)          # interrupt the 5s backoff
    assert time.monotonic() - t0 < 2.0
    assert d._worker is None
    assert fake.call_count == 1         # the retry never ran (cancelled during backoff)


# ---------------------------------------------------------------------------
# An executor exception must not kill the single consumer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_survives_an_unexpected_executor_exception():
    def boom():
        raise RuntimeError("scrobble_result blew up unexpectedly")

    fake = FakeLastfm([boom])           # first job explodes; second is fine
    d = ScrobbleDispatcher(fake, backoff=())
    d.start()
    d.enqueue(_meta(title="explodes"), 1)
    assert await _wait_until(lambda: fake.call_count >= 1)
    # The worker must still be alive to process the next job.
    d.enqueue(_meta(title="fine"), 2)
    assert await _wait_until(lambda: fake.call_count >= 2)
    assert not d._worker.done()         # worker survived the exception
    await d.drain()


# ---------------------------------------------------------------------------
# start() is idempotent (one consumer, not N)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_is_idempotent():
    d = ScrobbleDispatcher(FakeLastfm(), backoff=())
    d.start()
    w1 = d._worker
    d.start()
    assert d._worker is w1              # no second consumer task
    await d.drain()


# ---------------------------------------------------------------------------
# Disabled client: DELIVERED no-op still drains cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_client_no_ops_cleanly():
    class Disabled:
        enabled = False

        def scrobble_result(self, track, ts):
            return ScrobbleResult.DELIVERED

    d = ScrobbleDispatcher(Disabled(), backoff=())
    d.start()
    d.enqueue(_meta(), 1)
    await d.drain()
    assert d._worker is None
