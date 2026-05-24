"""Unit tests for TrackMetadata, PlaySession, TracklistEntry, and MetadataSource.

No hardware, network, or external dependencies required.
"""
import pytest
from src.metadata.models import (
    MetadataSource, TracklistEntry, TrackMetadata, PlaySession
)


# ---------------------------------------------------------------------------
# TracklistEntry
# ---------------------------------------------------------------------------

def test_tracklist_entry_basic():
    entry = TracklistEntry(position="A1", title="So What", duration="9:22")
    assert entry.position == "A1"
    assert entry.title == "So What"
    assert entry.duration == "9:22"


def test_tracklist_entry_optional_duration():
    entry = TracklistEntry(position="B2", title="Blue in Green")
    assert entry.duration is None


# ---------------------------------------------------------------------------
# TrackMetadata.is_last_track
# ---------------------------------------------------------------------------

def make_sister_tracklist():
    """Return the tracklist for Sonic Youth - Sister."""
    return [
        TracklistEntry("A1", "Catholic Block"),
        TracklistEntry("A2", "Pipeline/Kill Time"),
        TracklistEntry("A3", "Stereo Sanctity"),
        TracklistEntry("B1", "Tuff Gnarl"),
        TracklistEntry("B2", "Cotton Crown"),
        TracklistEntry("B3", "White Cross"),
        TracklistEntry("B4", "Master-Dik"),
    ]


def make_track(title, tracklist=None, release_id=None, instance_id=None,
               source=MetadataSource.DISCOGS_COLLECTION):
    if tracklist is None:
        tracklist = make_sister_tracklist()
    return TrackMetadata(
        title=title,
        artist="Sonic Youth",
        album="Sister",
        source=source,
        discogs_release_id=release_id,
        discogs_instance_id=instance_id,
        tracklist=tracklist,
    )


def test_is_last_track_true():
    assert make_track("Master-Dik").is_last_track is True


def test_is_last_track_false_for_middle_track():
    assert make_track("Stereo Sanctity").is_last_track is False


def test_is_last_track_false_for_first_track():
    assert make_track("Catholic Block").is_last_track is False


def test_is_last_track_false_with_empty_tracklist():
    track = TrackMetadata(
        title="Master-Dik",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=[],
    )
    assert track.is_last_track is False


def test_is_last_track_case_insensitive():
    assert make_track("master-dik").is_last_track is True  # lowercase


def test_is_last_track_strips_whitespace():
    assert make_track("  Master-Dik  ").is_last_track is True  # extra spaces


def test_is_last_track_false_when_not_in_tracklist():
    assert make_track("Unknown Song").is_last_track is False


# ---------------------------------------------------------------------------
# TrackMetadata.track_display
# ---------------------------------------------------------------------------

def test_track_display_found():
    assert make_track("Stereo Sanctity").track_display == "A3"


def test_track_display_first_track():
    assert make_track("Catholic Block").track_display == "A1"


def test_track_display_last_track():
    assert make_track("Master-Dik").track_display == "B4"


def test_track_display_not_found_returns_empty_string():
    assert make_track("Unknown Song").track_display == ""


def test_track_display_empty_tracklist_returns_empty_string():
    track = TrackMetadata(
        title="Stereo Sanctity",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=[],
    )
    assert track.track_display == ""


# ---------------------------------------------------------------------------
# PlaySession.log_track
# ---------------------------------------------------------------------------

def test_log_track_adds_track_to_session():
    session = PlaySession()
    session.log_track(make_track("Catholic Block"))
    assert len(session.identified_tracks) == 1
    assert session.identified_tracks[0].title == "Catholic Block"


def test_log_track_deduplicates_consecutive_identical_tracks():
    session = PlaySession()
    track = make_track("Catholic Block")
    session.log_track(track)
    session.log_track(track)  # Same object, same title
    assert len(session.identified_tracks) == 1


def test_log_track_allows_different_consecutive_tracks():
    session = PlaySession()
    session.log_track(make_track("Catholic Block"))
    session.log_track(make_track("Pipeline/Kill Time"))
    assert len(session.identified_tracks) == 2


def test_log_track_allows_same_track_after_different_track():
    """A->B->A is valid (unusual but possible), not deduplicated."""
    session = PlaySession()
    session.log_track(make_track("Catholic Block"))
    session.log_track(make_track("Pipeline/Kill Time"))
    session.log_track(make_track("Catholic Block"))  # Back to A (different consecutive)
    assert len(session.identified_tracks) == 3


def test_log_track_sets_potential_last_track_on_last_entry():
    session = PlaySession()
    assert session.potential_last_track is False
    session.log_track(make_track("Master-Dik"))  # Last track
    assert session.potential_last_track is True


def test_log_track_does_not_set_potential_last_track_for_non_last():
    session = PlaySession()
    session.log_track(make_track("Catholic Block"))
    assert session.potential_last_track is False


def test_log_track_latches_release_id_from_first_discogs_track():
    session = PlaySession()
    t1 = make_track("Catholic Block", release_id=100, instance_id=200)
    t2 = make_track("Pipeline/Kill Time", release_id=999, instance_id=888)
    session.log_track(t1)
    session.log_track(t2)
    # Should keep the FIRST release/instance IDs, not replace with subsequent
    assert session.album_release_id == 100
    assert session.album_instance_id == 200


def test_log_track_does_not_latch_fallback_track():
    """Fallback tracks have no discogs_release_id — should not latch."""
    session = PlaySession()
    fallback = TrackMetadata(
        title="Catholic Block",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
    )
    session.log_track(fallback)
    assert session.album_release_id is None
    assert session.album_instance_id is None


def test_log_track_latches_on_second_track_if_first_was_fallback():
    """If first track is fallback (no ID), latch from first Discogs-sourced track."""
    session = PlaySession()
    fallback = TrackMetadata(
        title="Catholic Block",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
    )
    discogs_track = make_track("Pipeline/Kill Time", release_id=42, instance_id=99)
    session.log_track(fallback)
    session.log_track(discogs_track)
    assert session.album_release_id == 42
    assert session.album_instance_id == 99


# ---------------------------------------------------------------------------
# MetadataSource enum
# ---------------------------------------------------------------------------

def test_metadata_source_values_are_distinct():
    sources = [
        MetadataSource.DISCOGS_COLLECTION,
        MetadataSource.DISCOGS_DATABASE,
        MetadataSource.FALLBACK,
    ]
    assert len(set(sources)) == 3


def test_metadata_source_names():
    assert MetadataSource.DISCOGS_COLLECTION.name == "DISCOGS_COLLECTION"
    assert MetadataSource.DISCOGS_DATABASE.name == "DISCOGS_DATABASE"
    assert MetadataSource.FALLBACK.name == "FALLBACK"
