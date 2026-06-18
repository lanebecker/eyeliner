"""Regression tests for B-3 — consecutive-track dedup must use full identity.

Old behaviour deduped on title alone, which (a) dropped the first track of a
swapped-in record when its title matched the previous record's last track —
so that record never latched a release and could never earn a Play Count —
and (b) dropped a genuinely different track that merely shared the previous
title, corrupting is_last_track accounting.
"""
from src.metadata.models import MetadataSource, TrackMetadata, PlaySession


def _track(title, artist="Sonic Youth", release_id=111, instance_id=222,
           source=MetadataSource.DISCOGS_COLLECTION, is_last=False):
    tl = []
    return TrackMetadata(
        title=title,
        artist=artist,
        album="Sister",
        source=source,
        discogs_release_id=release_id,
        discogs_instance_id=instance_id,
        tracklist=tl,
    )


def test_identical_consecutive_track_is_deduped():
    s = PlaySession()
    s.log_track(_track("Catholic Block"))
    s.log_track(_track("Catholic Block"))  # same title+artist+release → dedup
    assert len(s.identified_tracks) == 1


def test_swapped_record_sharing_a_title_is_not_dropped():
    """B-3 core case: record B's first track shares a title with record A's
    last track, but a different release — it MUST be logged and latch B."""
    s = PlaySession()
    s.log_track(_track("Untitled", release_id=111, instance_id=222))
    s.log_track(_track("Untitled", release_id=999, instance_id=888))  # different record

    assert len(s.identified_tracks) == 2
    # B latched its own release/instance, so it can earn a Play Count.
    assert s.last_release_id == 999


def test_same_title_different_artist_is_not_dropped():
    s = PlaySession()
    s.log_track(_track("Intro", artist="Band A", release_id=111))
    s.log_track(_track("Intro", artist="Band B", release_id=222))
    assert len(s.identified_tracks) == 2


def test_distinct_titles_still_append():
    s = PlaySession()
    s.log_track(_track("Catholic Block"))
    s.log_track(_track("Stereo Sanctity"))
    assert len(s.identified_tracks) == 2
