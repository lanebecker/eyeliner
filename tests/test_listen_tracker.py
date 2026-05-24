"""Unit tests for ListenTracker — the most business-logic-heavy component.

Covers every edge case from the architecture doc:
  ✓ Full album played → increment_play_count called
  ✓ Only Side A played → NOT incremented
  ✓ Last track recognition missed → NOT incremented
  ✓ Album not in collection (fallback metadata, no release_id) → NOT incremented
  ✓ increment_play_count returns False → no crash
  ✓ SESSION_ENDED with no active session → no crash
  ✓ Already-counted album (idempotent Discogs call) → called once anyway

Covers update_last_played integration:
  ✓ last_played_field_name configured → update_last_played called on album completion
  ✓ last_played_field_name not configured → update_last_played NOT called
  ✓ update_last_played returns False → logs warning, no crash

No audio hardware, display, or Discogs account required. DiscogsClient is mocked.
"""
import asyncio
from unittest.mock import MagicMock
import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import (
    MetadataSource, TracklistEntry, TrackMetadata, PlaySession
)
from src.tracking.listen_tracker import ListenTracker


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_resolver(
    increment_play_count_return=True,
    last_played_field_name=None,
    update_last_played_return=True,
):
    """Mock resolver whose .discogs methods return controlled values.

    last_played_field_name defaults to None (not configured). Set it to a
    non-empty string to simulate a user who has the Last Played field enabled.
    """
    resolver = MagicMock()
    resolver.discogs.increment_play_count.return_value = increment_play_count_return
    resolver.discogs.last_played_field_name = last_played_field_name
    resolver.discogs.update_last_played.return_value = update_last_played_return
    return resolver


def make_tracklist():
    return [
        TracklistEntry("A1", "Catholic Block"),
        TracklistEntry("A2", "Pipeline/Kill Time"),
        TracklistEntry("A3", "Stereo Sanctity"),
        TracklistEntry("B1", "Tuff Gnarl"),
        TracklistEntry("B2", "Cotton Crown"),
        TracklistEntry("B3", "White Cross"),
        TracklistEntry("B4", "Master-Dik"),
    ]


def make_track(
    title,
    release_id=12345,
    instance_id=67890,
    source=MetadataSource.DISCOGS_COLLECTION,
    tracklist=None,
):
    return TrackMetadata(
        title=title,
        artist="Sonic Youth",
        album="Sister",
        source=source,
        discogs_release_id=release_id,
        discogs_instance_id=instance_id,
        tracklist=tracklist if tracklist is not None else make_tracklist(),
    )


def make_tracker(
    increment_play_count_return=True,
    last_played_field_name=None,
    update_last_played_return=True,
):
    resolver = make_resolver(
        increment_play_count_return=increment_play_count_return,
        last_played_field_name=last_played_field_name,
        update_last_played_return=update_last_played_return,
    )
    tracker = ListenTracker({}, resolver)
    return tracker, resolver


# ---------------------------------------------------------------------------
# Session lifecycle via on_silence_event
# ---------------------------------------------------------------------------

def test_session_is_none_at_start():
    tracker, _ = make_tracker()
    assert tracker._session is None


def test_session_starts_on_music_started():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    assert tracker._session is not None
    assert isinstance(tracker._session, PlaySession)


def test_second_music_started_does_not_replace_existing_session():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    first_session = tracker._session
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)  # Should be a no-op
    assert tracker._session is first_session


# ---------------------------------------------------------------------------
# Happy path: full album → increment Play Count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_album_calls_increment_play_count():
    """Playing through the last track + SESSION_ENDED → Discogs Play Count incremented."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))
    await tracker.on_track_identified(make_track("Master-Dik"))  # Last track

    assert tracker._session.potential_last_track is True

    await tracker._end_session()  # Direct await for reliable test execution

    resolver.discogs.increment_play_count.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_session_cleared_after_end():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()
    assert tracker._session is None


@pytest.mark.asyncio
async def test_increment_play_count_uses_correct_release_and_instance_ids():
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik", release_id=99, instance_id=77))
    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_called_once_with(99, 77)


# ---------------------------------------------------------------------------
# Edge case: only Side A played
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_side_a_played_does_not_increment():
    """Session ends before last track identified → no Discogs update."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))
    await tracker.on_track_identified(make_track("Stereo Sanctity"))
    # Side B tracks never identified

    assert tracker._session.potential_last_track is False
    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: last track never recognized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_but_last_track_identified_does_not_increment():
    """Recognizer missed the last track (e.g. needle skip) → no update."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    for title in ["Catholic Block", "Pipeline/Kill Time", "Stereo Sanctity",
                  "Tuff Gnarl", "Cotton Crown", "White Cross"]:
        await tracker.on_track_identified(make_track(title))
    # Master-Dik (B4, last) never identified

    assert tracker._session.potential_last_track is False
    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: album not in Discogs collection (fallback metadata)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_track_reached_but_fallback_source_does_not_increment():
    """Last track identified but metadata is FALLBACK (no release_id) → skip."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    fallback_last = TrackMetadata(
        title="Master-Dik",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(fallback_last)

    # potential_last_track IS True (we did identify the last track)
    assert tracker._session.potential_last_track is True
    # But there's no release_id to update
    assert tracker._session.album_release_id is None

    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_database_source_without_instance_id_does_not_increment():
    """DISCOGS_DATABASE result has no instance_id → can't update collection field."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    db_last = TrackMetadata(
        title="Master-Dik",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=12345,
        discogs_instance_id=None,  # DB results don't have instance IDs
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(db_last)
    assert tracker._session.potential_last_track is True
    assert tracker._session.album_instance_id is None

    await tracker._end_session()
    # increment_play_count will be called with instance_id=None — the tracker passes
    # whatever is on the session. The client handles None gracefully.
    # (This documents the current behavior — if future code guards against None,
    #  update this test accordingly.)
    resolver.discogs.increment_play_count.assert_called_once_with(12345, None)


# ---------------------------------------------------------------------------
# Edge case: no tracks identified at all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_with_no_identified_tracks_does_not_increment():
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    # Music was heard but recognition never succeeded
    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: SESSION_ENDED with no active session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_session_with_no_session_does_not_crash():
    """Spurious SESSION_ENDED (no active session) should be a safe no-op."""
    tracker, resolver = make_tracker()
    assert tracker._session is None
    # Should not raise
    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# increment_play_count failure handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_increment_play_count_returning_false_does_not_raise():
    """Discogs API returning failure should log a warning but not crash."""
    tracker, resolver = make_tracker(increment_play_count_return=False)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    # Should complete without raising
    await tracker._end_session()
    resolver.discogs.increment_play_count.assert_called_once()


# ---------------------------------------------------------------------------
# on_track_identified wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_track_identified_starts_session_if_not_running():
    """on_track_identified can create a session if called before MUSIC_STARTED."""
    tracker, _ = make_tracker()
    assert tracker._session is None
    await tracker.on_track_identified(make_track("Catholic Block"))
    assert tracker._session is not None


@pytest.mark.asyncio
async def test_on_track_identified_appends_to_session():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))
    assert len(tracker._session.identified_tracks) == 2


@pytest.mark.asyncio
async def test_on_track_identified_sets_potential_last_track():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    assert tracker._session.potential_last_track is False
    await tracker.on_track_identified(make_track("Master-Dik"))
    assert tracker._session.potential_last_track is True


# ---------------------------------------------------------------------------
# Already-counted (idempotent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_counted_album_still_calls_increment_once():
    """increment_play_count handles existing counts — we just call it once per session."""
    tracker, resolver = make_tracker(increment_play_count_return=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()
    # We called it; Discogs handles the read-before-write
    resolver.discogs.increment_play_count.assert_called_once()


# ---------------------------------------------------------------------------
# update_last_played integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_album_calls_update_last_played_when_configured():
    """When last_played_field_name is configured, update_last_played is called on completion."""
    tracker, resolver = make_tracker(last_played_field_name="Last Played")
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    resolver.discogs.increment_play_count.assert_called_once_with(12345, 67890)
    resolver.discogs.update_last_played.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_full_album_does_not_call_update_last_played_when_not_configured():
    """When last_played_field_name is None, update_last_played is never called."""
    tracker, resolver = make_tracker(last_played_field_name=None)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    resolver.discogs.increment_play_count.assert_called_once()
    resolver.discogs.update_last_played.assert_not_called()


@pytest.mark.asyncio
async def test_update_last_played_returning_false_does_not_raise():
    """update_last_played failure should log a warning but not crash the session."""
    tracker, resolver = make_tracker(
        last_played_field_name="Last Played",
        update_last_played_return=False,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    # Should complete without raising
    await tracker._end_session()
    resolver.discogs.update_last_played.assert_called_once()
