"""Regression tests for B-1 — a track resurrecting itself after the needle lifts.

`_commit_track` does set_raw → await resolve → set_track.  The resolve await
yields the loop; a SESSION_ENDED during it runs state.clear() (status → IDLE,
track → None) and bumps state.session_epoch.  The commit must notice the epoch
moved and discard itself instead of flipping the display back to PLAYING and
logging/scrobbling audio that already stopped.
"""
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from src.audio.recognizer import RawRecognitionResult, RecognitionLoop
from src.state.player_state import PlayerState, PlayerStatus


def make_raw(title="So What", artist="Miles Davis", album="Kind of Blue"):
    return RawRecognitionResult(title=title, artist=artist, album=album)


def make_loop_real_state():
    """RecognitionLoop wired to a real PlayerState so the epoch logic is live."""
    config = {
        "recognition": {
            "backend": "shazamio",
            "poll_interval_seconds": 30,
            "confirmation_required": 2,
        }
    }
    state = PlayerState()
    resolver = MagicMock()
    tracker = MagicMock()
    tracker.on_track_identified = AsyncMock()
    lastfm = MagicMock()
    lastfm.scrobble = MagicMock()
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, resolver, tracker, lastfm)
    return loop, state, resolver, tracker, lastfm


# ---------------------------------------------------------------------------
# PlayerState epoch mechanics
# ---------------------------------------------------------------------------

def test_clear_bumps_session_epoch():
    s = PlayerState()
    before = s.session_epoch
    s.clear()
    assert s.session_epoch == before + 1


def test_set_track_does_not_bump_epoch():
    s = PlayerState()
    before = s.session_epoch
    s.set_track(MagicMock())
    assert s.session_epoch == before


# ---------------------------------------------------------------------------
# B-1: stale commit is discarded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_discarded_when_session_ends_during_resolve():
    loop, state, resolver, tracker, lastfm = make_loop_real_state()
    state.set_status(PlayerStatus.LISTENING)  # a live session, awaiting first ID

    async def resolve_then_needle_lifts(raw):
        # The needle lifts mid-resolution: SESSION_ENDED → state.clear().
        state.clear()
        return MagicMock()  # resolved metadata, now stale

    resolver.resolve = AsyncMock(side_effect=resolve_then_needle_lifts)

    await loop._commit_track(make_raw())

    # The dead track must NOT be resurrected onto the screen…
    assert state.current_track is None
    assert state.status == PlayerStatus.IDLE
    # …nor logged into the fresh session, nor scrobbled.
    tracker.on_track_identified.assert_not_called()
    lastfm.scrobble.assert_not_called()


@pytest.mark.asyncio
async def test_commit_proceeds_when_session_stable():
    loop, state, resolver, tracker, lastfm = make_loop_real_state()
    state.set_status(PlayerStatus.LISTENING)

    meta = MagicMock()
    resolver.resolve = AsyncMock(return_value=meta)

    await loop._commit_track(make_raw())

    # No session end during resolve → the commit lands normally.
    assert state.current_track is meta
    assert state.status == PlayerStatus.PLAYING
    tracker.on_track_identified.assert_awaited_once_with(meta)
