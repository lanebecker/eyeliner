"""Unit tests for TrackMetadata, PlaySession, TracklistEntry, MetadataSource,
DisplayPalette, and the new v1.2.0 side-awareness properties.

No hardware, network, or external dependencies required.
"""
import pytest
from src.metadata.models import (
    MetadataSource, TracklistEntry, TrackMetadata, PlaySession,
    DisplayPalette, FALLBACK_PALETTE, _SIDE_RE,
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


def test_log_track_does_not_latch_database_source_without_instance_id():
    """DISCOGS_DATABASE result has release_id but no instance_id (the user
    doesn't own this pressing). Latching just the release_id would produce a
    doomed POST to /instances/None/fields/... — so log_track refuses to latch
    until both IDs are present.
    """
    session = PlaySession()
    db_track = TrackMetadata(
        title="Catholic Block",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=12345,
        discogs_instance_id=None,
    )
    session.log_track(db_track)
    assert session.album_release_id is None
    assert session.album_instance_id is None


def test_log_track_database_then_collection_latches_collection_only():
    """If a DB-sourced track is logged first (no instance_id), and then a
    collection-sourced track for the same release is logged second, the
    collection IDs should latch — DB-source must NOT have pre-empted the slot.
    """
    session = PlaySession()
    db_track = TrackMetadata(
        title="Catholic Block", artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=12345, discogs_instance_id=None,
    )
    collection_track = make_track(
        "Pipeline/Kill Time", release_id=12345, instance_id=67890,
    )
    session.log_track(db_track)
    session.log_track(collection_track)
    assert session.album_release_id == 12345
    assert session.album_instance_id == 67890


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


# ---------------------------------------------------------------------------
# DisplayPalette and FALLBACK_PALETTE
# ---------------------------------------------------------------------------

def test_display_palette_fields():
    p = DisplayPalette(bg=(10, 10, 10), surface=(22, 22, 22),
                       accent=(200, 150, 80), text=(235, 230, 220), muted=(138, 133, 124))
    assert p.bg == (10, 10, 10)
    assert p.accent == (200, 150, 80)


def test_fallback_palette_is_display_palette():
    assert isinstance(FALLBACK_PALETTE, DisplayPalette)
    # Should be very dark
    assert all(c < 30 for c in FALLBACK_PALETTE.bg)


# ---------------------------------------------------------------------------
# _SIDE_RE regex
# ---------------------------------------------------------------------------

def test_side_re_matches_standard_positions():
    m = _SIDE_RE.match("A1")
    assert m and m.group(1) == "A" and m.group(2) == "1"

def test_side_re_matches_multi_digit():
    m = _SIDE_RE.match("B12")
    assert m and m.group(1) == "B" and m.group(2) == "12"

def test_side_re_no_match_for_numeric_only():
    assert _SIDE_RE.match("1") is None
    assert _SIDE_RE.match("12") is None


# ---------------------------------------------------------------------------
# TrackMetadata.genres (v1.2.0)
# ---------------------------------------------------------------------------

def test_genres_default_empty():
    track = TrackMetadata(
        title="Catholic Block", artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_COLLECTION,
    )
    assert track.genres == []


def test_genres_stored():
    track = TrackMetadata(
        title="Catholic Block", artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_COLLECTION,
        genres=["Noise Rock", "Alt Rock", "Post-Punk"],
    )
    assert track.genres == ["Noise Rock", "Alt Rock", "Post-Punk"]


# ---------------------------------------------------------------------------
# Side-awareness properties (v1.2.0)
# ---------------------------------------------------------------------------

def make_sister_full_tracklist():
    """Full Sonic Youth - Sister tracklist with proper A/B positions."""
    return [
        TracklistEntry("A1", "Catholic Block"),
        TracklistEntry("A2", "Pipeline/Kill Time"),
        TracklistEntry("A3", "Stereo Sanctity"),
        TracklistEntry("B1", "Tuff Gnarl"),
        TracklistEntry("B2", "Cotton Crown"),
        TracklistEntry("B3", "White Cross"),
        TracklistEntry("B4", "Master-Dik"),
    ]


def make_side_track(title, tracklist=None):
    if tracklist is None:
        tracklist = make_sister_full_tracklist()
    return TrackMetadata(
        title=title, artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=tracklist,
    )


# side_letter
def test_side_letter_a_side():
    assert make_side_track("Catholic Block").side_letter == "A"

def test_side_letter_b_side():
    assert make_side_track("Tuff Gnarl").side_letter == "B"

def test_side_letter_none_when_not_in_tracklist():
    assert make_side_track("Unknown Song").side_letter is None

def test_side_letter_none_for_numeric_positions():
    tracklist = [TracklistEntry("1", "Track One"), TracklistEntry("2", "Track Two")]
    assert make_side_track("Track One", tracklist).side_letter is None


# side_position
def test_side_position_first_on_side():
    assert make_side_track("Catholic Block").side_position == 1

def test_side_position_third_on_a_side():
    assert make_side_track("Stereo Sanctity").side_position == 3

def test_side_position_first_on_b_side():
    assert make_side_track("Tuff Gnarl").side_position == 1

def test_side_position_last_on_b_side():
    assert make_side_track("Master-Dik").side_position == 4

def test_side_position_none_when_not_found():
    assert make_side_track("Unknown Song").side_position is None


# side_total
def test_side_total_a_side():
    assert make_side_track("Catholic Block").side_total == 3  # A1, A2, A3

def test_side_total_b_side():
    assert make_side_track("Tuff Gnarl").side_total == 4  # B1, B2, B3, B4

def test_side_total_none_when_not_found():
    assert make_side_track("Unknown Song").side_total is None


# prev_track_title
def test_prev_track_very_first_track_is_none():
    # A1 is the first track globally — no previous track exists
    assert make_side_track("Catholic Block").prev_track_title is None

def test_prev_track_middle_of_a_side():
    assert make_side_track("Stereo Sanctity").prev_track_title == "Pipeline/Kill Time"

def test_prev_track_last_on_b_side():
    assert make_side_track("Master-Dik").prev_track_title == "White Cross"

def test_prev_track_cross_side_b1_returns_last_of_a():
    # B1 (Tuff Gnarl) is first on Side B — should fall back to global tracklist
    # and return A3 (Stereo Sanctity), the last track of Side A.
    assert make_side_track("Tuff Gnarl").prev_track_title == "Stereo Sanctity"

def test_prev_track_none_when_not_found():
    assert make_side_track("Unknown Song").prev_track_title is None


# next_track_title
def test_next_track_first_on_a_side():
    assert make_side_track("Catholic Block").next_track_title == "Pipeline/Kill Time"

def test_next_track_middle_of_b_side():
    assert make_side_track("Cotton Crown").next_track_title == "White Cross"

def test_next_track_very_last_track_is_none():
    # B4 (Master-Dik) is the final track globally — no next track exists
    assert make_side_track("Master-Dik").next_track_title is None

def test_next_track_cross_side_last_a_returns_first_of_b():
    # A3 (Stereo Sanctity) is last on Side A — should fall back to global tracklist
    # and return B1 (Tuff Gnarl), the first track of Side B.
    assert make_side_track("Stereo Sanctity").next_track_title == "Tuff Gnarl"

def test_next_track_none_when_not_found():
    assert make_side_track("Unknown Song").next_track_title is None


# ---------------------------------------------------------------------------
# is_last_track position matching (v1.3.4)
#
# is_last_track now compares the current entry's POSITION to the final
# entry's position instead of comparing titles. Title-only matching let an
# earlier track that shares the closer's title (reprises, title tracks)
# set potential_last_track from side A.
# ---------------------------------------------------------------------------

def _duplicate_title_tracklist():
    """Album whose A2 shares its title with the B3 closer (e.g. a reprise
    pattern, or a title track that bookends the record)."""
    return [
        TracklistEntry("A1", "Opener"),
        TracklistEntry("A2", "Hungry Ghosts"),
        TracklistEntry("A3", "Middle Eight"),
        TracklistEntry("B1", "Deep Cut"),
        TracklistEntry("B2", "Penultimate"),
        TracklistEntry("B3", "Hungry Ghosts"),
    ]


def test_is_last_track_true_for_genuine_closer_with_unique_title():
    track = TrackMetadata(
        title="Penultimate", artist="a", album="b",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=_duplicate_title_tracklist(),
    )
    assert track.is_last_track is False  # B2 is not the closer
    closer = TrackMetadata(
        title="Middle Eight", artist="a", album="b",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=[TracklistEntry("A1", "Opener"), TracklistEntry("A2", "Middle Eight")],
    )
    assert closer.is_last_track is True


def test_duplicate_closer_title_on_side_a_does_not_set_last_track():
    """Regression: playing A2 'Hungry Ghosts' must NOT count as the closer,
    even though B3 shares the title. Pre-v1.3.4 this returned True and could
    phantom-increment the play count after a side-A-only session."""
    track = TrackMetadata(
        title="Hungry Ghosts", artist="a", album="b",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=_duplicate_title_tracklist(),
    )
    # Title matching resolves to the FIRST occurrence (A2), whose position
    # differs from the closer's (B3) -> False. Documented conservative
    # trade-off: the genuine B3 play is also missed (no phantom counts).
    assert track.is_last_track is False


def test_is_last_track_position_match_is_title_normalized():
    """Locating the current entry still tolerates case/whitespace jitter."""
    track = TrackMetadata(
        title="  master-dik  ", artist="a", album="b",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=[
            TracklistEntry("A1", "Catholic Block"),
            TracklistEntry("B4", "Master-Dik"),
        ],
    )
    assert track.is_last_track is True


def test_is_last_track_false_when_title_not_in_tracklist():
    track = TrackMetadata(
        title="Not On This Album", artist="a", album="b",
        source=MetadataSource.DISCOGS_COLLECTION,
        tracklist=_duplicate_title_tracklist(),
    )
    assert track.is_last_track is False
