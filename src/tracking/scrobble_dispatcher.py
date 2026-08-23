"""ScrobbleDispatcher — isolate optional Last.fm latency from recognition (R10-09/10).

Why this exists
---------------
Before this, ``TrackCommitService.commit`` awaited the synchronous Last.fm
scrobble on the shared executor *from inside the sole recognition consumer*
(``RecognitionLoop.run`` → ``on_confirmed`` → ``commit``).  A slow Last.fm
response therefore paused the next audio dequeue for the full round-trip
(R10-09: an injected 400 ms delay made a zero-cost commit take 404.6 ms), and a
*failed* call was silently latched as delivered with no retry (R10-10).

This dispatcher moves the network call off the recognition path.  ``commit`` does
all its epoch / per-spin-dedup / clock gating on the event-loop thread and then
hands the scrobble to :meth:`enqueue`, which returns immediately.  A single
lifecycle-owned consumer task performs the call on the shared I/O executor and
applies the bounded-retry policy.

Delivery state machine (per job, owner decision on #423)
--------------------------------------------------------
Each job carries the confirmation-time timestamp (#383), reused unchanged on
every attempt so Last.fm's (track, timestamp) de-duplication prevents a double
credit.  The worker calls :meth:`LastFmClient.scrobble_result`, which returns a
:class:`~src.tracking.lastfm_client.ScrobbleResult`:

  * ``DELIVERED``  → done.
  * ``AMBIGUOUS``  → the scrobble may have applied; treated as delivered, NOT
    retried (avoids a double credit).
  * ``RETRYABLE``  → a definite non-apply; retried up to
    ``len(backoff)`` times with the given bounded backoff, then dropped
    (best-effort — no durable cross-restart outbox, per the owner decision).

Concurrency / lifecycle invariants preserved
---------------------------------------------
  * #61 executor serialization: the single consumer never issues two scrobbles
    at once, and ``LastFmClient`` still serializes pylast access under its own
    lock (so a scrobble and a session-end ``love`` cannot touch the Network
    concurrently).
  * #383 confirmation timestamp: captured at enqueue, reused on retry.
  * No untracked bare tasks: the one worker task is tracked in ``self._worker``;
    every ``run_in_executor`` future and ``sleep`` is awaited within it.
  * Bounded memory: a fixed-size queue with a drop-oldest overflow policy; the
    total per-job backoff fits inside the shutdown drain window.
  * Bounded shutdown drain: :meth:`drain` stops accepting, waits (bounded) for
    the queue to empty, then cancels the worker; anything still queued at the
    bound is dropped (a stuck executor call cannot be interrupted, so the owned
    I/O pool's ``cancel_futures`` shutdown is the backstop).
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Optional, Tuple

from src.audio.log_throttle import ThrottledLogger
from src.tracking.lastfm_client import ScrobbleResult

if TYPE_CHECKING:
    from src.metadata.models import TrackMetadata
    from src.tracking.lastfm_client import LastFmClient

log = logging.getLogger(__name__)

# Bounded queue depth.  Scrobbles are ~one per track (tracks run minutes), so in
# normal operation the queue holds 0–1 items; the bound only matters if Last.fm
# hangs across many tracks, capping memory and surfacing a health signal instead
# of growing without limit.
_QUEUE_MAXSIZE = 32

# Bounded in-memory retry backoff for RETRYABLE (definite, non-applied) failures
# only.  One wait BEFORE each retry, so this is (initial try) + len(backoff)
# retries = 3 attempts total.  Kept SHORT for the same reason as the tracker's
# credit backoff: the per-job backoff SLEEPS (1s + 2s = 3s) fit inside the
# shutdown drain window below and never spin.  (The pylast round-trips themselves
# are not counted here; a wedged one is bounded by drain()'s wait_for timeout,
# which cancels the worker regardless.)
_RETRY_BACKOFF_SECONDS: Tuple[float, ...] = (1.0, 2.0)

# Bounded shutdown drain.  Long enough for an in-flight scrobble plus its bounded
# backoff to finish, short enough to stay well within systemd's default 90s stop
# timeout even if the network is wedged.
_SHUTDOWN_DRAIN_SECONDS = 10.0

# Throttle the "queue full — dropped a scrobble" health line: a sustained Last.fm
# outage while music keeps playing would otherwise write one line per dropped
# scrobble.  One line, then a periodic summary.
_OVERFLOW_LOG_INTERVAL_SECONDS = 60.0


class ScrobbleDispatcher:
    """Single-consumer, bounded, lifecycle-owned Last.fm scrobble queue.

    Constructed once at startup wrapping the shared :class:`LastFmClient`, and
    injected into :class:`~src.app.track_commit_service.TrackCommitService`.
    Call :meth:`start` once the event loop is running, :meth:`enqueue` from the
    commit path (non-blocking), and :meth:`drain` from the shutdown path.

    Event-loop-thread-only for :meth:`enqueue` / :meth:`start` / :meth:`drain`
    (like the rest of the pipeline, A-12); the blocking pylast call is the only
    thing that runs off-loop, via ``run_in_executor``.
    """

    def __init__(
        self,
        lastfm: "LastFmClient",
        *,
        maxsize: int = _QUEUE_MAXSIZE,
        backoff: Tuple[float, ...] = _RETRY_BACKOFF_SECONDS,
    ):
        self._lastfm = lastfm
        self._queue: "asyncio.Queue[Tuple[TrackMetadata, int]]" = asyncio.Queue(
            maxsize=maxsize
        )
        self._backoff = tuple(backoff)
        self._worker: Optional[asyncio.Task] = None
        # Once False, no new work is admitted (shutdown has begun).
        self._accepting = True
        self._overflow_log = ThrottledLogger(
            log, _OVERFLOW_LOG_INTERVAL_SECONDS, level=logging.WARNING
        )

    @property
    def started(self) -> bool:
        """True once the consumer task has been created (and not yet drained)."""
        return self._worker is not None

    def start(self) -> None:
        """Create the single consumer task.  Idempotent; requires a running loop."""
        if self._worker is not None:
            return
        self._accepting = True
        self._worker = asyncio.get_running_loop().create_task(
            self._run(), name="scrobble-dispatcher"
        )

    def enqueue(self, metadata: "TrackMetadata", timestamp: int) -> None:
        """Hand a confirmed track's scrobble to the worker (non-blocking).

        Called from :meth:`TrackCommitService.commit` on the event-loop thread
        AFTER every epoch / dedup / clock gate has passed.  Never awaits, so a
        slow or failing Last.fm call cannot delay the next recognition dequeue.

        Overflow policy (bounded memory): if the queue is full — Last.fm has been
        unresponsive across ``maxsize`` tracks — drop the OLDEST pending scrobble
        and admit this one, so the freshest listening history is retained.  The
        drop is a throttled health line, not silent.
        """
        if not self._accepting:
            # Shutdown has begun; do not admit new work that drain() cannot flush.
            log.debug("ScrobbleDispatcher not accepting; dropping scrobble for %r.",
                      getattr(metadata, "title", metadata))
            return
        job = (metadata, timestamp)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # drop the OLDEST pending scrobble
                self._queue.task_done()   # keep join()'s unfinished count exact
            except asyncio.QueueEmpty:  # pragma: no cover — full() just said otherwise
                pass
            self._overflow_log.error(
                "Last.fm scrobble queue full (maxsize=%d) — dropped the oldest "
                "pending scrobble. Last.fm has been unresponsive across many "
                "tracks; scrobbling is best-effort and some plays may be lost."
                % self._queue.maxsize
            )
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:  # pragma: no cover — we just made room
                pass

    async def _run(self) -> None:
        """The single consumer loop."""
        log.info("Scrobble dispatcher started.")
        while True:
            job = await self._queue.get()
            try:
                await self._deliver(job)
            except asyncio.CancelledError:
                # task_done still runs (finally) so a concurrent join() cannot
                # hang on this item; then propagate so the task actually stops.
                raise
            except Exception as e:  # defensive: a bug must not kill the worker
                metadata, _ = job
                log.error(
                    "Scrobble dispatcher worker error (dropping scrobble for "
                    "%s — %s): %r",
                    getattr(metadata, "artist", "?"),
                    getattr(metadata, "title", "?"),
                    e,
                )
            finally:
                self._queue.task_done()

    async def _deliver(self, job: "Tuple[TrackMetadata, int]") -> None:
        """Deliver one scrobble with the bounded RETRYABLE-only retry policy."""
        metadata, timestamp = job
        loop = asyncio.get_running_loop()
        attempts = len(self._backoff) + 1
        for attempt in range(attempts):
            outcome = await loop.run_in_executor(
                None, self._lastfm.scrobble_result, metadata, timestamp
            )
            if outcome is ScrobbleResult.DELIVERED:
                # Work is flowing again; flush any "queue full — dropped N"
                # recovery tally the overflow throttle held during an outage
                # (a no-op when nothing was suppressed).
                self._overflow_log.reset()
                return
            if outcome is ScrobbleResult.AMBIGUOUS:
                log.warning(
                    "Last.fm scrobble outcome ambiguous for %s — %s; NOT retrying "
                    "(it may already have applied).",
                    metadata.artist, metadata.title,
                )
                return
            # RETRYABLE — a definite non-apply; retry with the SAME timestamp.
            if attempt < len(self._backoff):
                await asyncio.sleep(self._backoff[attempt])
            else:
                log.warning(
                    "Last.fm scrobble failed after %d attempt(s) for %s — %s; "
                    "dropping (best-effort, no durable outbox).",
                    attempts, metadata.artist, metadata.title,
                )

    async def drain(self, timeout: float = _SHUTDOWN_DRAIN_SECONDS) -> None:
        """Bounded shutdown drain — flush queued/in-flight scrobbles, then stop.

        Stops accepting new work, waits (bounded) for the queue to fully drain —
        including the in-flight job's bounded retries — then cancels the worker.
        Anything still queued at the timeout is dropped with a warning.  Never
        raises: shutdown must proceed.  Called from ``run_pipeline``'s finally
        BEFORE the shared I/O executor is closed, alongside ``tracker.drain()``.
        """
        self._accepting = False
        worker = self._worker
        if worker is None:
            return
        pending = self._queue.qsize()
        if pending:
            log.info("Draining %d pending Last.fm scrobble(s) before shutdown…", pending)
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except asyncio.TimeoutError:
            log.warning(
                "Last.fm scrobble(s) still pending after a %.0fs drain timeout; "
                "dropping them (best-effort).",
                timeout,
            )
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self._worker = None
