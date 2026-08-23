"""Unit tests for TrackCommitService (A-9).

The commit sequence (resolve → state → track → scrobble) was extracted from
RecognitionLoop._commit_track into an application-layer service.  These tests
own the invariants that used to live in the recognizer tests:

  * B-1 — a commit whose session ends mid-resolve is discarded (epoch guard).
  * PCONC-1 — a commit for audio captured in an already-ended session is
    discarded, because the epoch is bound to the audio (passed as audio_epoch),
    not re-sampled at commit entry.
  * B-11 — current_raw is advanced only after set_track succeeds.

…plus the scrobble branch that was previously never exercised because the
recognizer tests never passed a Last.fm client (T-2).

A real PlayerState is used so the epoch logic is live; resolver / tracker /
lastfm are mocks.
"""
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from src.app.track_commit_service import TrackCommitService
from src.audio.recognizer import RawRecognitionResult
from src.state.player_state import PlayerState, PlayerStatus


def make_raw(title="So What", artist="Miles Davis", album="Kind of Blue"):
    return RawRecognitionResult(title=title, artist=artist, album=album)


def make_service(dispatcher=None):
    """TrackCommitService on a real PlayerState; resolver + tracker mocked.

    R10-09 (#422): the confirmed-track scrobble is now handed to a
    ScrobbleDispatcher via a non-blocking ``enqueue`` rather than awaited inline,
    so tests that exercise the scrobble branch pass a dispatcher mock and assert
    on ``enqueue`` (the network call itself, its retry, and error isolation are
    covered by tests/test_scrobble_dispatcher.py).  ``should_scrobble`` defaults
    to True (permit) and ``record_scrobble`` is a spy so the in-flight-reservation
    ordering can be asserted; override per-test as needed.
    """
    state = PlayerState()
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=MagicMock())
    tracker = MagicMock()
    tracker.on_track_identified = AsyncMock()
    tracker.should_scrobble = MagicMock(return_value=True)
    tracker.record_scrobble = MagicMock()
    service = TrackCommitService(state, resolver, tracker, dispatcher)
    return service, state, resolver, tracker


def _dispatcher():
    """A ScrobbleDispatcher stub whose enqueue() is a synchronous spy."""
    d = MagicMock()
    d.enqueue = MagicMock()
    return d


# ---------------------------------------------------------------------------
# Happy path + ordering (B-11)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_sets_track_then_raw_and_notifies_tracker():
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    r = make_raw()
    committed = await service.commit(r, state.session_epoch)

    assert committed is True
    assert state.current_track is meta
    assert state.current_raw is r
    assert state.status == PlayerStatus.PLAYING
    tracker.on_track_identified.assert_awaited_once_with(meta, is_stale=ANY)


@pytest.mark.asyncio
async def test_current_raw_advanced_only_after_set_track():
    """B-11: set_track must precede set_raw, so current_raw never leads
    current_track."""
    service, state, resolver, tracker = make_service()
    order = []

    real_set_track = state.set_track
    real_set_raw = state.set_raw
    state.set_track = lambda m: (order.append("track"), real_set_track(m))[1]
    state.set_raw = lambda r: (order.append("raw"), real_set_raw(r))[1]

    await service.commit(make_raw(), state.session_epoch)

    assert order == ["track", "raw"]


@pytest.mark.asyncio
async def test_current_raw_not_advanced_when_resolve_fails():
    """B-11: a resolver exception propagates and leaves current_raw / track
    unset, so the loop re-attempts the track."""
    service, state, resolver, tracker = make_service()
    resolver.resolve = AsyncMock(side_effect=RuntimeError("resolve boom"))
    state.set_status(PlayerStatus.LISTENING)

    with pytest.raises(RuntimeError):
        await service.commit(make_raw(), state.session_epoch)

    assert state.current_raw is None
    assert state.current_track is None
    tracker.on_track_identified.assert_not_called()


# ---------------------------------------------------------------------------
# Tracker failure must not strand the track (LB-1, #84)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tracker_failure_does_not_advance_current_raw():
    """LB-1: if on_track_identified raises (its album-split path awaits a Discogs
    write), current_raw must NOT advance.  The old order advanced it BEFORE the
    tracker await, so on failure the recognition loop's dedup treated the
    never-recorded track as 'already playing' and never re-attempted it —
    displayed, but never tracked, never scrobbled, never retried.  Now the
    exception propagates before set_raw, so the loop re-commits on the next
    chunk; set_track still ran (the display already showed it), only the dedup
    key is left clean."""
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)
    tracker.on_track_identified = AsyncMock(side_effect=RuntimeError("discogs write blew up"))

    with pytest.raises(RuntimeError):
        await service.commit(make_raw(), state.session_epoch)

    assert state.current_raw is None       # un-advanced → the loop re-attempts
    assert state.current_track is meta      # display already updated (set_track ran)


@pytest.mark.asyncio
async def test_tracker_failure_defers_the_scrobble_to_the_retry():
    """LB-1: a tracker failure must not scrobble on the doomed commit — the
    enqueue sits after set_raw, so it is deferred to the successful retry rather
    than double-firing (the retry re-runs the whole commit)."""
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    state.set_status(PlayerStatus.LISTENING)
    resolver.resolve = AsyncMock(return_value=MagicMock())
    tracker.on_track_identified = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await service.commit(make_raw(), state.session_epoch)

    disp.enqueue.assert_not_called()
    assert state.current_raw is None


@pytest.mark.asyncio
async def test_needle_lift_during_tracker_does_not_advance_current_raw():
    """LB-1 + #196: a needle lift DURING on_track_identified (SESSION_ENDED →
    clear() bumps the epoch and nulls current_raw) must not let the commit
    resurrect the dead session's dedup key. The post-tracker epoch gate (#196)
    sees the bumped epoch, discards the commit (returns False), and never reaches
    set_raw — so current_raw stays null and a re-drop of the same record can
    commit again. (set_raw also carries a defensive still-current guard, but the
    #196 gate returns before it is reached, so this scenario is pinned by the gate
    + the end-state, not by that now-redundant guard.)"""
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    resolver.resolve = AsyncMock(return_value=MagicMock())

    async def end_during_tail(metadata, is_stale=None):
        state.clear()   # needle lifts mid-tracker → epoch bumps, current_raw nulled

    tracker.on_track_identified = AsyncMock(side_effect=end_during_tail)

    committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is False               # discarded at the #196 gate
    assert state.current_raw is None        # not resurrected
    assert state.status == PlayerStatus.IDLE


# ---------------------------------------------------------------------------
# Epoch guard (B-1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_discarded_when_session_ends_during_resolve():
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)  # a live session, awaiting first ID

    async def resolve_then_needle_lifts(raw):
        # The needle lifts mid-resolution: SESSION_ENDED → state.clear().
        state.clear()
        return MagicMock()  # resolved metadata, now stale

    resolver.resolve = AsyncMock(side_effect=resolve_then_needle_lifts)

    committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is False
    # The dead track must NOT be resurrected onto the screen…
    assert state.current_track is None
    assert state.status == PlayerStatus.IDLE
    # …nor logged into the fresh session.
    tracker.on_track_identified.assert_not_called()


@pytest.mark.asyncio
async def test_commit_proceeds_when_session_stable():
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is True
    assert state.current_track is meta
    assert state.status == PlayerStatus.PLAYING
    tracker.on_track_identified.assert_awaited_once_with(meta, is_stale=ANY)


@pytest.mark.asyncio
async def test_commit_discarded_when_audio_predates_the_current_session():
    """PCONC-1 (#80): audio captured in an EARLIER session must be discarded when
    the live session has already moved on — even though the epoch stays 'stable'
    across the resolve.  This is the queue-lag stale-commit the commit-time epoch
    sample (pre-fix) could NOT see: the needle lifted and a new session began
    BEFORE this confirmed commit ran, so an entry-time sample would read the new
    epoch, find it stable across the resolve, and commit the dead track into the
    fresh session (polluting the display and, downstream, the Discogs write)."""
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    audio_epoch = state.session_epoch          # audio captured now (epoch 0)

    state.clear()                              # needle lifts: session ends (epoch 1)
    state.set_status(PlayerStatus.LISTENING)   # a NEW record starts a new session

    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)   # epoch stays 1 across resolve

    committed = await service.commit(make_raw(), audio_epoch)

    assert committed is False                  # stale audio discarded, not committed
    assert state.current_track is None         # the fresh session is NOT polluted
    tracker.on_track_identified.assert_not_called()


# ---------------------------------------------------------------------------
# Last.fm scrobble branch — now a NON-BLOCKING enqueue to the ScrobbleDispatcher
# (R10-09/#422).  The commit path does all epoch/dedup/clock gating on the loop
# thread and hands the scrobble off; delivery, retry, and error isolation are the
# dispatcher's concern (tests/test_scrobble_dispatcher.py).  These tests own the
# COMMIT-SIDE contract: what is enqueued, and — crucially — what is NOT.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrobble_enqueued_with_metadata_and_timestamp():
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    await service.commit(make_raw(), state.session_epoch)

    disp.enqueue.assert_called_once()
    args = disp.enqueue.call_args[0]
    assert args[0] is meta
    assert isinstance(args[1], int)  # a unix timestamp


@pytest.mark.asyncio
async def test_record_scrobble_reserved_before_enqueue():
    """#163 in-flight reservation: the per-spin latch is recorded (so a concurrent
    swing-back re-commit is suppressed) BEFORE the scrobble is handed off."""
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    order = []
    tracker.record_scrobble = MagicMock(side_effect=lambda m: order.append("record"))
    disp.enqueue = MagicMock(side_effect=lambda *a: order.append("enqueue"))
    resolver.resolve = AsyncMock(return_value=MagicMock())

    await service.commit(make_raw(), state.session_epoch)

    assert order == ["record", "enqueue"]


@pytest.mark.asyncio
async def test_swing_back_duplicate_is_not_enqueued():
    """R8-09: a track already scrobbled this spin (should_scrobble False) must not
    be enqueued again, and the reservation latch is not re-recorded."""
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    tracker.should_scrobble = MagicMock(return_value=False)
    resolver.resolve = AsyncMock(return_value=MagicMock())

    committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is True
    disp.enqueue.assert_not_called()
    tracker.record_scrobble.assert_not_called()


@pytest.mark.asyncio
async def test_no_scrobble_when_dispatcher_absent():
    service, state, resolver, tracker = make_service(dispatcher=None)
    # Must not raise despite no dispatcher (scrobbling effectively disabled).
    committed = await service.commit(make_raw(), state.session_epoch)
    assert committed is True


@pytest.mark.asyncio
async def test_scrobble_not_enqueued_when_session_ends_during_tracker_tail():
    """B-19: on_track_identified can yield (its album-split path awaits a Discogs
    write).  If the needle lifts during that window, the scrobble for the now-
    ended track must NOT be enqueued — even though the display commit already ran."""
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    async def end_during_tail(metadata, is_stale=None):
        state.clear()  # SESSION_ENDED lands during the tracker tail → epoch bumps

    tracker.on_track_identified = AsyncMock(side_effect=end_during_tail)

    await service.commit(make_raw(), state.session_epoch)

    tracker.on_track_identified.assert_awaited_once()  # the tail did run...
    disp.enqueue.assert_not_called()                   # ...but nothing was enqueued


@pytest.mark.asyncio
async def test_stale_commit_does_not_enqueue():
    """When the session ends mid-resolve, nothing is enqueued."""
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    state.set_status(PlayerStatus.LISTENING)

    async def resolve_then_needle_lifts(raw):
        state.clear()
        return MagicMock()

    resolver.resolve = AsyncMock(side_effect=resolve_then_needle_lifts)

    await service.commit(make_raw(), state.session_epoch)

    disp.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_commit_hands_tracker_an_epoch_staleness_predicate():
    """CONC-6: commit passes on_track_identified an is_stale() predicate that
    reports False while the audio's session is live and True once it ends (the
    epoch bumps) — so the tracker can drop a stale track after acquiring the
    lifecycle lock instead of starting a phantom session."""
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    resolver.resolve = AsyncMock(return_value=MagicMock())
    seen = {}

    async def capture(metadata, is_stale=None):
        seen["while_live"] = is_stale()   # epoch unchanged → not stale
        state.clear()                      # needle lifts → epoch bumps
        seen["after_end"] = is_stale()     # epoch changed → stale

    tracker.on_track_identified = AsyncMock(side_effect=capture)

    await service.commit(make_raw(), state.session_epoch)

    assert seen["while_live"] is False
    assert seen["after_end"] is True


@pytest.mark.asyncio
async def test_scrobble_not_enqueued_when_clock_untrustworthy():
    """STAB-2: a pre-NTP clock captured an epoch/stale timestamp at the top of
    commit(); the scrobble is skipped (never enqueued) rather than queuing a wrong
    time that Last.fm would silently drop or mis-place. The clock gate stays on the
    commit path — BEFORE enqueue — so doomed work is never queued. The rest of the
    commit still runs. The in-flight reservation latch is still recorded, matching
    the prior record-then-skip ordering (R9-08)."""
    disp = _dispatcher()
    service, state, resolver, tracker = make_service(dispatcher=disp)
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    with patch("src.app.track_commit_service.clock_is_trustworthy", return_value=False):
        committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is True              # commit still succeeds (display + tracker)
    assert state.current_track is meta
    disp.enqueue.assert_not_called()      # only the scrobble is skipped
    tracker.record_scrobble.assert_called_once()  # reservation still recorded (R9-08)


# ---------------------------------------------------------------------------
# R10-09 (#422) integration: commit latency is INDEPENDENT of Last.fm latency —
# a real dispatcher + a blocking Last.fm client, driven through commit().
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_does_not_block_on_a_slow_scrobble():
    """The finding measured a 400 ms Last.fm delay making a zero-cost commit take
    404.6 ms — the sole recognition consumer paused for the network. With the
    dispatcher, commit() returns as soon as it ENQUEUES; the slow scrobble runs
    off the commit path. Uses a REAL ScrobbleDispatcher and a blocking client."""
    import asyncio
    import threading
    import time as _time
    from src.tracking.lastfm_client import ScrobbleResult
    from src.tracking.scrobble_dispatcher import ScrobbleDispatcher

    started = threading.Event()
    release = threading.Event()

    class BlockingLastfm:
        enabled = True

        def scrobble_result(self, track, timestamp):
            started.set()
            release.wait(2.0)
            return ScrobbleResult.DELIVERED

    dispatcher = ScrobbleDispatcher(BlockingLastfm(), backoff=())
    dispatcher.start()
    service, state, resolver, tracker = make_service(dispatcher=dispatcher)
    resolver.resolve = AsyncMock(return_value=MagicMock())
    try:
        t0 = _time.monotonic()
        committed = await service.commit(make_raw(), state.session_epoch)
        elapsed = _time.monotonic() - t0

        assert committed is True
        assert elapsed < 0.2                    # commit did NOT wait on the scrobble

        # Yield to the loop (do NOT block it) so the worker can pick up the job
        # and dispatch it to the executor — proving the scrobble runs off-path.
        for _ in range(400):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
    finally:
        release.set()
        await dispatcher.drain()
