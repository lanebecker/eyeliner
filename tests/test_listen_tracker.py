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
    # A-3: ListenTracker now takes a DiscogsClient directly (was resolver).
    tracker = ListenTracker(resolver.discogs)
    return tracker, resolver


# ---------------------------------------------------------------------------
# Session lifecycle via on_silence_event
# ---------------------------------------------------------------------------

def test_tracker_uses_the_injected_discogs_client():
    """A-3: the tracker depends on a DiscogsClient injected directly, not one
    dug out of a resolver's internals."""
    discogs = MagicMock()
    tracker = ListenTracker(discogs)
    assert tracker.discogs is discogs


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
async def test_database_source_without_instance_id_does_not_call_increment():
    """DISCOGS_DATABASE result has no instance_id → log_track refuses to latch
    the release_id (since we can't build a valid field-update URL without an
    instance_id), so _end_session sees album_release_id is None and skips the
    Discogs update entirely.
    """
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
    # potential_last_track IS True (we DID identify the last track)
    assert tracker._session.potential_last_track is True
    # But the release_id was NOT latched, because there's no instance_id to go with it
    assert tracker._session.album_release_id is None
    assert tracker._session.album_instance_id is None

    await tracker._end_session()
    # No POST attempted with instance_id=None
    resolver.discogs.increment_play_count.assert_not_called()
    resolver.discogs.update_last_played.assert_not_called()


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


# ---------------------------------------------------------------------------
# Background task management (v1.3.3)
#
# SESSION_ENDED schedules _end_session() as an asyncio task. asyncio holds
# only weak references to tasks, so ListenTracker must keep a strong
# reference until the task — which performs the Discogs play-count write —
# completes.
# ---------------------------------------------------------------------------

async def test_session_ended_task_is_referenced_until_done():
    tracker = ListenTracker(make_resolver().discogs)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    assert len(tracker._bg_tasks) == 1  # Strong reference held while running

    # Let the scheduled _end_session task run to completion
    for _ in range(5):
        await asyncio.sleep(0)

    assert tracker._session is None      # Session was ended
    assert len(tracker._bg_tasks) == 0   # Reference released on completion


# ---------------------------------------------------------------------------
# Album-change auto-split (v1.3.4)
#
# Swapping records faster than session_end_silence_seconds used to merge two
# albums into one session, letting record 2's closer credit record 1 with a
# play. on_track_identified now splits the session when a confirmed track's
# release_id differs from the latched one.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_album_change_splits_session():
    """A track from a different release ends the old session and starts fresh."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    first_session = tracker._session

    await tracker.on_track_identified(make_track("So What", release_id=999, instance_id=888))

    assert tracker._session is not first_session
    assert tracker._session.album_release_id == 999
    assert len(tracker._session.identified_tracks) == 1


@pytest.mark.asyncio
async def test_album_change_credits_first_record_if_its_closer_played():
    """Record 1 finished (closer identified), record 2 dropped within 45s:
    the split must still increment record 1's play count."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))
    assert tracker._session.potential_last_track is True

    await tracker.on_track_identified(make_track("So What", release_id=999, instance_id=888))

    resolver.discogs.increment_play_count.assert_called_once_with(111, 222)


@pytest.mark.asyncio
async def test_album_change_does_not_credit_unfinished_first_record():
    """Record 1 abandoned mid-side: the split ends its session WITHOUT updates."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    await tracker.on_track_identified(make_track("So What", release_id=999, instance_id=888))

    resolver.discogs.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_same_release_does_not_split_session():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block"))
    first_session = tracker._session
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))

    assert tracker._session is first_session
    assert len(tracker._session.identified_tracks) == 2


@pytest.mark.asyncio
async def test_fallback_track_without_release_id_does_not_split():
    """FALLBACK metadata (no release_id) can't be distinguished — no split."""
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    first_session = tracker._session

    fallback = TrackMetadata(
        title="Mystery Tune",
        artist="Unknown",
        album="Bootleg",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
        tracklist=[],
    )
    await tracker.on_track_identified(fallback)

    assert tracker._session is first_session


@pytest.mark.asyncio
async def test_no_split_when_nothing_latched_yet():
    """First identified track of a session never triggers a split, whatever
    its release_id — there's nothing latched to differ from."""
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    first_session = tracker._session

    await tracker.on_track_identified(make_track("Catholic Block", release_id=777, instance_id=555))

    assert tracker._session is first_session
    assert tracker._session.album_release_id == 777


# ---------------------------------------------------------------------------
# Auto-split via last_release_id (v1.3.5)
#
# The v1.3.4 split compared against the LATCHED album_release_id, which only
# collection-owned tracks set. A DB-resolved record 1 (never latches) +
# closer played + quick swap to a collection-owned record 2 evaded detection,
# and record 2 inherited — and was phantom-credited for — record 1's
# completed play. Detection now compares against last_release_id, which
# updates from any source carrying a release ID.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_record_then_collection_record_splits():
    """Regression: a DB-resolved record 1 must not let record 2 inherit its
    session. Pre-v1.3.5 this merged sessions and phantom-credited record 2."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    # Record 1: DB-resolved (release_id but NO instance_id → never latches),
    # and its closer plays.
    db_closer = TrackMetadata(
        title="Master-Dik", artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=111, discogs_instance_id=None,
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(db_closer)
    assert tracker._session.potential_last_track is True
    assert tracker._session.album_release_id is None     # No latch (DB-only)
    assert tracker._session.last_release_id == 111       # But it WAS seen

    # Record 2 (collection-owned) dropped within 45s → must split.
    await tracker.on_track_identified(
        make_track("Catholic Block", release_id=999, instance_id=888)
    )

    # The split ended record 1's session; with no latch there was nothing to
    # credit (correct — we can't update a pressing the user doesn't own)...
    resolver.discogs.increment_play_count.assert_not_called()
    # ...and record 2 starts CLEAN: no inherited potential_last_track that
    # could phantom-credit it at session end.
    assert tracker._session.potential_last_track is False
    assert tracker._session.album_release_id == 999
    assert tracker._session.last_release_id == 999


@pytest.mark.asyncio
async def test_collection_then_db_record_still_splits():
    """The original v1.3.4 direction (collection → DB) keeps working under
    last_release_id comparison."""
    tracker, resolver = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))
    db_track = TrackMetadata(
        title="So What", artist="Miles Davis", album="Kind of Blue",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=555, discogs_instance_id=None,
        tracklist=[],
    )
    await tracker.on_track_identified(db_track)

    # Record 1 was collection-owned and finished → credited by the split.
    resolver.discogs.increment_play_count.assert_called_once_with(111, 222)
    assert tracker._session.last_release_id == 555
