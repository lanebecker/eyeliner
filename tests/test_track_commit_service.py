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


def make_service(lastfm=None):
    """TrackCommitService on a real PlayerState; resolver + tracker mocked."""
    state = PlayerState()
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=MagicMock())
    tracker = MagicMock()
    tracker.on_track_identified = AsyncMock()
    service = TrackCommitService(state, resolver, tracker, lastfm)
    return service, state, resolver, tracker


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
    scrobble sits after set_raw, so it is deferred to the successful retry rather
    than double-firing (the retry re-runs the whole commit)."""
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock()
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    state.set_status(PlayerStatus.LISTENING)
    resolver.resolve = AsyncMock(return_value=MagicMock())
    tracker.on_track_identified = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await service.commit(make_raw(), state.session_epoch)

    lastfm.scrobble.assert_not_called()
    assert state.current_raw is None


@pytest.mark.asyncio
async def test_needle_lift_during_tracker_does_not_advance_current_raw():
    """B-19/LB-1: moving set_raw after the tracker await (per LB-1) means a needle
    lift DURING on_track_identified (SESSION_ENDED → clear() bumps the epoch and
    nulls current_raw) must not let set_raw resurrect the dead session's dedup
    key.  The epoch guard on set_raw skips the advance, so current_raw stays null
    and a re-drop of the same record can commit again.  (The end-state null holds
    under the old order too — clear() nulled it after set_raw ran — so this test
    pins the GUARD specifically: with the guard removed, set_raw runs
    unconditionally and resurrects the key, which the mutation check confirms.)"""
    service, state, resolver, tracker = make_service()
    state.set_status(PlayerStatus.LISTENING)
    resolver.resolve = AsyncMock(return_value=MagicMock())

    async def end_during_tail(metadata, is_stale=None):
        state.clear()   # needle lifts mid-tracker → epoch bumps, current_raw nulled

    tracker.on_track_identified = AsyncMock(side_effect=end_during_tail)

    await service.commit(make_raw(), state.session_epoch)

    assert state.current_raw is None        # set_raw skipped; not resurrected
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
# Last.fm scrobble branch (T-2 — previously never exercised)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrobble_called_with_metadata_and_timestamp():
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock()
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    await service.commit(make_raw(), state.session_epoch)

    lastfm.scrobble.assert_called_once()
    args = lastfm.scrobble.call_args[0]
    assert args[0] is meta
    assert isinstance(args[1], int)  # a unix timestamp


@pytest.mark.asyncio
async def test_no_scrobble_when_lastfm_absent():
    service, state, resolver, tracker = make_service(lastfm=None)
    # Must not raise despite no Last.fm client.
    committed = await service.commit(make_raw(), state.session_epoch)
    assert committed is True


@pytest.mark.asyncio
async def test_scrobble_failure_does_not_break_commit():
    """A throwing scrobble is logged and swallowed — the track still commits."""
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock(side_effect=RuntimeError("last.fm down"))
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is True
    assert state.current_track is meta  # commit completed despite scrobble error


@pytest.mark.asyncio
async def test_scrobble_skipped_when_session_ends_during_tracker_tail():
    """B-19: on_track_identified can yield (its album-split path awaits a Discogs
    write).  If the needle lifts during that window, the scrobble for the now-
    ended track must be skipped — even though the display commit already ran."""
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock()
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    async def end_during_tail(metadata, is_stale=None):
        state.clear()  # SESSION_ENDED lands during the tracker tail → epoch bumps

    tracker.on_track_identified = AsyncMock(side_effect=end_during_tail)

    await service.commit(make_raw(), state.session_epoch)

    tracker.on_track_identified.assert_awaited_once()  # the tail did run...
    lastfm.scrobble.assert_not_called()                # ...but the scrobble was skipped


@pytest.mark.asyncio
async def test_stale_commit_does_not_scrobble():
    """When the session ends mid-resolve, nothing is scrobbled."""
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock()
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    state.set_status(PlayerStatus.LISTENING)

    async def resolve_then_needle_lifts(raw):
        state.clear()
        return MagicMock()

    resolver.resolve = AsyncMock(side_effect=resolve_then_needle_lifts)

    await service.commit(make_raw(), state.session_epoch)

    lastfm.scrobble.assert_not_called()


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
async def test_scrobble_skipped_when_clock_untrustworthy():
    """STAB-2: a pre-NTP clock captured an epoch/stale timestamp at the top of
    commit(); the scrobble is skipped rather than submitting a wrong time that
    Last.fm would silently drop or mis-place. The rest of the commit still runs."""
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock()
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    state.set_status(PlayerStatus.LISTENING)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    with patch("src.app.track_commit_service.clock_is_trustworthy", return_value=False):
        committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is True              # commit still succeeds (display + tracker)
    assert state.current_track is meta
    lastfm.scrobble.assert_not_called()   # only the scrobble is skipped
