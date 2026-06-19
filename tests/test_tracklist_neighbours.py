"""Regression tests for B-5, B-9, B-10 — tracklist neighbour logic & sharing.

B-5  — prev/next resolve by the entry's unique POSITION, not a title re-scan,
       so they're internally consistent (no cross-side leak for repeated titles).
B-10 — numbered tracklists ('1'..'N', no side letters) now yield prev/next
       instead of always None.
B-9  — TracklistEntry is frozen; each track of an album gets its own tracklist
       list; an explicit None tracklist is normalized to [].
"""
from unittest.mock import MagicMock

import pytest

from src.audio.recognizer import RawRecognitionResult
from src.metadata.models import (
    MetadataSource, TracklistEntry, TrackMetadata,
)
from src.metadata.resolver import MetadataResolver


def md(title, tracklist):
    return TrackMetadata(
        title=title, artist="A", album="X",
        source=MetadataSource.DISCOGS_COLLECTION, tracklist=tracklist,
    )


# ---------------------------------------------------------------------------
# B-10 — numbered tracklists
# ---------------------------------------------------------------------------

def test_numbered_tracklist_has_neighbours():
    tl = [TracklistEntry("1", "One"), TracklistEntry("2", "Two"), TracklistEntry("3", "Three")]
    assert md("Two", tl).prev_track_title == "One"
    assert md("Two", tl).next_track_title == "Three"


def test_numbered_tracklist_boundaries_return_none():
    tl = [TracklistEntry("1", "One"), TracklistEntry("2", "Two"), TracklistEntry("3", "Three")]
    assert md("One", tl).prev_track_title is None
    assert md("Three", tl).next_track_title is None


# ---------------------------------------------------------------------------
# B-5 — sided adjacency across the side boundary, by position
# ---------------------------------------------------------------------------

def _sided():
    return [
        TracklistEntry("A1", "Open"), TracklistEntry("A2", "Two"),
        TracklistEntry("A3", "Three"),
        TracklistEntry("B1", "Four"), TracklistEntry("B2", "Close"),
    ]


def test_side_boundary_prev_crosses_to_previous_side():
    # B1 "Four" → prev should be A3 "Three" (last of side A).
    assert md("Four", _sided()).prev_track_title == "Three"


def test_side_boundary_next_crosses_to_next_side():
    # A3 "Three" → next should be B1 "Four" (first of side B).
    assert md("Three", _sided()).next_track_title == "Four"


def test_album_first_and_last_return_none():
    assert md("Open", _sided()).prev_track_title is None
    assert md("Close", _sided()).next_track_title is None


def test_repeated_title_resolves_consistently_by_position():
    """A title that appears on both sides resolves (by position) to a single
    occurrence and reports THAT occurrence's neighbours — no mixing the A-side
    copy's prev with the B-side copy's next (B-5)."""
    tl = [
        TracklistEntry("A1", "Intro"), TracklistEntry("A2", "Reprise"),
        TracklistEntry("B1", "Reprise"), TracklistEntry("B2", "Outro"),
    ]
    t = md("Reprise", tl)
    # Resolves to the first occurrence (A2): prev=Intro(A1), next=Reprise(B1).
    # The point: both neighbours come from the SAME resolved index, not a
    # re-scan that could pick a different occurrence for the fallback.
    assert t.prev_track_title == "Intro"
    assert t.next_track_title == "Reprise"


def test_cd_style_positions_have_neighbours():
    """CD / multi-disc positions ('1-1', '1.01') don't match the side regex, so
    the old code returned None.  They now get global adjacency too."""
    tl = [
        TracklistEntry("1-1", "Alpha"), TracklistEntry("1-2", "Bravo"),
        TracklistEntry("2-1", "Charlie"),
    ]
    assert md("Bravo", tl).prev_track_title == "Alpha"
    assert md("Bravo", tl).next_track_title == "Charlie"


def test_duplicate_position_resolves_to_title_matching_entry():
    """If two rows ever share a position string, resolve to the one whose title
    also matches — not blindly the first position match."""
    tl = [
        TracklistEntry("A1", "First"),
        TracklistEntry("A1", "Second"),   # same position, different title
        TracklistEntry("A2", "Third"),
    ]
    t = md("Second", tl)
    # "Second" is the index-1 entry → prev=First, next=Third.
    assert t.prev_track_title == "First"
    assert t.next_track_title == "Third"


# ---------------------------------------------------------------------------
# B-9 — frozen entries, per-track list copies, None normalization
# ---------------------------------------------------------------------------

def test_tracklist_entry_is_frozen():
    e = TracklistEntry("A1", "Open")
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        e.title = "Changed"


def _resolver():
    r = MetadataResolver.__new__(MetadataResolver)
    r.reader = MagicMock()
    r.coverart = MagicMock()
    r._album_cache = {}
    return r


def _raw(title):
    return RawRecognitionResult(title=title, artist="A", album="X")


@pytest.mark.asyncio
async def test_each_track_gets_its_own_tracklist_list():
    r = _resolver()
    shared = [TracklistEntry("A1", "One"), TracklistEntry("A2", "Two")]
    r.reader.search_collection.return_value = {
        "album": "X", "release_id": 1, "instance_id": 2, "tracklist": shared,
    }

    t1 = await r.resolve(_raw("One"))
    t2 = await r.resolve(_raw("Two"))  # cache hit, same album

    assert t1.tracklist == t2.tracklist          # equal contents
    assert t1.tracklist is not t2.tracklist      # but distinct list objects
    assert t1.tracklist is not shared            # and not the cached source list


@pytest.mark.asyncio
async def test_none_tracklist_normalized_to_empty_list():
    r = _resolver()
    r.reader.search_collection.return_value = {
        "album": "X", "release_id": 1, "instance_id": 2, "tracklist": None,
    }
    t = await r.resolve(_raw("One"))
    assert t.tracklist == []
    # And the title properties don't blow up on it.
    assert t.prev_track_title is None
    assert t.next_track_title is None
    assert t.is_last_track is False
