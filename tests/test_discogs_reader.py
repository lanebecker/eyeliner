"""Unit tests for DiscogsReader read-surface methods (T-4).

The resolver-facing read methods used to be touched only by the network-only
test_discogs_live.py.  These exercise, offline:

  * get_tracklist's heading / positionless-track filtering, and its
    fail-soft-to-[] error path.

(search_collection's two-strategy fallthrough and the B-4/B-13 error semantics
are covered in tests/test_discogs_search_errors.py.)
"""
from unittest.mock import MagicMock

from src.metadata.models import TracklistEntry
from tests.factories import make_discogs_reader


def _track(position, title, type_=None, duration=None):
    """A python3-discogs-client-style tracklist entry mock."""
    t = MagicMock()
    t.position = position
    t.title = title
    t.type_ = type_
    t.duration = duration
    return t


def test_get_tracklist_filters_headings_and_positionless_tracks():
    reader = make_discogs_reader()
    release = MagicMock()
    release.tracklist = [
        # Heading carries a position here so this asserts the heading-TYPE filter
        # specifically, not just the positionless filter below.
        _track("A", "Side A", type_="heading"),     # heading pseudo-track → drop
        _track("A1", "Catholic Block", duration="3:11"),
        _track("", "Studio Chatter"),               # no position → drop
        _track("A2", "Stereo Sanctity"),            # duration None → kept as None
    ]
    reader._client.release = MagicMock(return_value=release)

    entries = reader.get_tracklist(151481)

    assert all(isinstance(e, TracklistEntry) for e in entries)
    assert [e.position for e in entries] == ["A1", "A2"]
    assert entries[0].title == "Catholic Block"
    assert entries[0].duration == "3:11"
    assert entries[1].duration is None          # blank/None duration normalised


def test_get_tracklist_passes_release_id_to_the_client():
    reader = make_discogs_reader()
    release = MagicMock()
    release.tracklist = []
    reader._client.release = MagicMock(return_value=release)

    reader.get_tracklist(999)

    reader._client.release.assert_called_once_with(999)


def test_get_tracklist_returns_empty_list_on_fetch_error():
    """A failed release fetch is logged and degrades to [] (the tracklist is
    best-effort enrichment, not an identity field)."""
    reader = make_discogs_reader()
    reader._client.release = MagicMock(side_effect=RuntimeError("discogs down"))

    assert reader.get_tracklist(151481) == []
