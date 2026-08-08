"""Regression tests for #181 (R4:data-3) — the Last.fm love target.

The module contract ("Calls LastFmClient.love on the last track") means the
album's CLOSER — the track whose ``is_last_track`` armed
``potential_last_track``.  The old ``_finalize_session`` recomputed the target
as ``session.identified_tracks[-1]``: the last track *identified*, which
equals the closer only if nothing was identified after it.  Two realistic
sequences broke that (both executed in the audit):

1. REPLAY: the user finishes the album and re-drops side A within the 45s
   silence window — same release id, no album split, so the replayed opener is
   appended after the closer and gets loved instead.
2. FALLBACK SWAP: a record that resolves as FALLBACK (no discogs_release_id)
   never triggers the album split, so its tracks append to the old session —
   and the love lands on a track of a DIFFERENT record/artist entirely, on the
   operator's real Last.fm profile.

The fix records the arming track on the session (``PlaySession.closing_track``,
set in ``log_track`` where ``is_last_track`` arms) and loves that, falling
back to ``identified_tracks[-1]`` only when unset.
"""
from unittest.mock import MagicMock

import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, PlaySession, TrackMetadata
from src.tracking.listen_tracker import ListenTracker
from tests.test_listen_tracker import make_track, make_writer_mock


def _love_tracker():
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.love_on_completion = True
    lastfm.love = MagicMock(return_value=True)
    tracker = ListenTracker(writer, lastfm)
    return tracker, lastfm


def _loved_title(lastfm):
    assert lastfm.love.call_count == 1
    return lastfm.love.call_args[0][0].title


@pytest.mark.asyncio
async def test_replay_after_closer_still_loves_the_closer():
    """Sequence 1: closer arms the session, side A is re-dropped inside the
    silence window (same release, no split) — the love must target the closer,
    not the replayed opener."""
    tracker, lastfm = _love_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))        # closer (B4)
    await tracker.on_track_identified(make_track("Catholic Block"))    # replayed opener (A1)
    session = tracker._session

    # Production path: detach + finalize via _end_session, not the private
    # finalize on a still-attached session (#181 cold-review note).
    await tracker._end_session()

    assert _loved_title(lastfm) == "Master-Dik"
    assert session.loved is True


@pytest.mark.asyncio
async def test_fallback_swap_does_not_love_the_other_record():
    """Sequence 2: after the closer, a FALLBACK-resolved track from a
    different record/artist appends to the same session (no release id, no
    split) — the love must not land on the other artist's track."""
    tracker, lastfm = _love_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Cotton Crown"))     # supporting (#182 gate)
    await tracker.on_track_identified(make_track("Master-Dik"))        # closer
    swapped = TrackMetadata(
        title="Unrelated Song",
        artist="Different Band",
        album="Different Album",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
        tracklist=[],
    )
    await tracker.on_track_identified(swapped)

    await tracker._end_session()

    assert _loved_title(lastfm) == "Master-Dik"


@pytest.mark.asyncio
async def test_normal_completion_still_loves_the_closer():
    """Control: nothing identified after the closer — behaviour unchanged."""
    tracker, lastfm = _love_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Cotton Crown"))      # B2
    await tracker.on_track_identified(make_track("Master-Dik"))        # closer

    await tracker._end_session()

    assert _loved_title(lastfm) == "Master-Dik"


@pytest.mark.asyncio
async def test_no_closer_no_love():
    """Control: potential_last_track never armed — no love (unchanged)."""
    tracker, lastfm = _love_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    session = tracker._session

    await tracker._end_session()

    lastfm.love.assert_not_called()
    assert session.loved is False


@pytest.mark.asyncio
async def test_unset_closing_track_falls_back_to_last_identified():
    """Edge parity: a session armed without a recorded closing track (not
    producible via log_track, but the fallback keeps old-session behaviour
    identical) loves the last identified track."""
    tracker, lastfm = _love_tracker()
    session = PlaySession()
    session.potential_last_track = True
    session.identified_tracks.append(make_track("Cotton Crown"))

    await tracker._finalize_session(session)

    assert _loved_title(lastfm) == "Cotton Crown"


def test_log_track_records_the_arming_track():
    """PlaySession.closing_track is set by exactly the track whose
    is_last_track armed potential_last_track, and a later non-closer does not
    overwrite it."""
    session = PlaySession()
    closer = make_track("Master-Dik")
    assert closer.is_last_track is True
    session.log_track(make_track("Cotton Crown"))
    assert session.closing_track is None
    session.log_track(closer)
    assert session.closing_track is closer
    session.log_track(make_track("Catholic Block"))
    assert session.closing_track is closer
