"""Unit tests for TrackCommitService (A-9).

The commit sequence (resolve → state → track → scrobble) was extracted from
RecognitionLoop._commit_track into an application-layer service.  These tests
own the invariants that used to live in the recognizer tests:

  * B-1 — a commit whose session ends mid-resolve is discarded (epoch guard).
  * B-11 — current_raw is advanced only after set_track succeeds.

…plus the scrobble branch that was previously never exercised because the
recognizer tests never passed a Last.fm client (T-2).

A real PlayerState is used so the epoch logic is live; resolver / tracker /
lastfm are mocks.
"""
from unittest.mock import AsyncMock, MagicMock

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
    committed = await service.commit(r)

    assert committed is True
    assert state.current_track is meta
    assert state.current_raw is r
    assert state.status == PlayerStatus.PLAYING
    tracker.on_track_identified.assert_awaited_once_with(meta)


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

    await service.commit(make_raw())

    assert order == ["track", "raw"]


@pytest.mark.asyncio
async def test_current_raw_not_advanced_when_resolve_fails():
    """B-11: a resolver exception propagates and leaves current_raw / track
    unset, so the loop re-attempts the track."""
    service, state, resolver, tracker = make_service()
    resolver.resolve = AsyncMock(side_effect=RuntimeError("resolve boom"))
    state.set_status(PlayerStatus.LISTENING)

    with pytest.raises(RuntimeError):
        await service.commit(make_raw())

    assert state.current_raw is None
    assert state.current_track is None
    tracker.on_track_identified.assert_not_called()


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

    committed = await service.commit(make_raw())

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

    committed = await service.commit(make_raw())

    assert committed is True
    assert state.current_track is meta
    assert state.status == PlayerStatus.PLAYING
    tracker.on_track_identified.assert_awaited_once_with(meta)


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

    await service.commit(make_raw())

    lastfm.scrobble.assert_called_once()
    args = lastfm.scrobble.call_args[0]
    assert args[0] is meta
    assert isinstance(args[1], int)  # a unix timestamp


@pytest.mark.asyncio
async def test_no_scrobble_when_lastfm_absent():
    service, state, resolver, tracker = make_service(lastfm=None)
    # Must not raise despite no Last.fm client.
    committed = await service.commit(make_raw())
    assert committed is True


@pytest.mark.asyncio
async def test_scrobble_failure_does_not_break_commit():
    """A throwing scrobble is logged and swallowed — the track still commits."""
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock(side_effect=RuntimeError("last.fm down"))
    service, state, resolver, tracker = make_service(lastfm=lastfm)
    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    committed = await service.commit(make_raw())

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

    async def end_during_tail(metadata):
        state.clear()  # SESSION_ENDED lands during the tracker tail → epoch bumps

    tracker.on_track_identified = AsyncMock(side_effect=end_during_tail)

    await service.commit(make_raw())

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

    await service.commit(make_raw())

    lastfm.scrobble.assert_not_called()
