"""Regression tests for B-4 and B-13 — Discogs collection-search error semantics.

A hard/transient error during collection search must propagate as "couldn't
determine" so the resolver leaves the album uncached and retries on the next
track — NOT be swallowed and read as "you don't own this," which silently
downgrades an owned record to a database/fallback result for the whole session.
A definitive 404 ("not this pressing") is the only thing that means "not owned."
"""
from unittest.mock import MagicMock

import pytest
import requests

from tests.factories import make_discogs_reader


# ---------------------------------------------------------------------------
# B-13 — _database_search raises on hard error instead of returning []
# ---------------------------------------------------------------------------

def test_database_search_raises_on_hard_error():
    client = make_discogs_reader()
    client._client = MagicMock()
    client._client.search.side_effect = requests.exceptions.ConnectionError("boom")
    with pytest.raises(requests.exceptions.ConnectionError):
        client._database_search("artist", "album")


def test_database_search_returns_empty_on_genuine_no_match():
    client = make_discogs_reader()
    client._client = MagicMock()
    page = MagicMock()
    page.page.return_value = []          # no matches (not an error)
    client._client.search.return_value = page
    assert client._database_search("artist", "album") == []


# ---------------------------------------------------------------------------
# B-13 / B-4 — a hard error building the collection index propagates as
# "couldn't determine" (vs. a false "not owned").  With the P-1 index, the only
# HTTP during matching is the one-time index build; once built, matching is
# local, so there is no per-candidate membership error to swallow.
# ---------------------------------------------------------------------------

def test_collection_index_build_error_propagates():
    from src.metadata.discogs.reader import CollectionIndexIncomplete

    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=requests.exceptions.Timeout("slow"))
    with pytest.raises(CollectionIndexIncomplete) as raised:
        client.search_collection("artist", "album")
    assert isinstance(raised.value.__cause__, requests.exceptions.Timeout)


# ---------------------------------------------------------------------------
# B-4 / P-1 — local index matching: owned → result; not-owned → None
# ---------------------------------------------------------------------------

def _candidate(release_id=111, title="Sister"):
    rel = MagicMock()
    rel.id = release_id
    rel.title = title
    return rel


def _index(release_id=111, instance_id=42, title="Sister", artists=("Sonic Youth",)):
    return {release_id: {"instance_id": instance_id, "title": title, "artists": list(artists)}}


def test_owned_candidate_returns_built_result():
    client = make_discogs_reader()
    client._collection_index = _index(111, 42)        # pre-built index (no HTTP)
    client._database_search = MagicMock(return_value=[_candidate(111)])
    client._build_result = MagicMock(return_value={"release_id": 111, "instance_id": 42})

    result = client.search_collection("Sonic Youth", "Sister")
    assert result == {"release_id": 111, "instance_id": 42}
    client._build_result.assert_called_once()


def test_candidate_not_in_index_is_not_owned():
    client = make_discogs_reader()
    client._collection_index = _index(999, 7)         # owns a DIFFERENT release
    client._database_search = MagicMock(return_value=[_candidate(111)])  # candidate not owned

    # No id match and no fuzzy match → not in collection.
    assert client.search_collection("Some Artist", "Other Album") is None


def test_strategy_2_fuzzy_matches_index_without_extra_http():
    """A candidate whose release_id isn't owned still resolves if the index has
    a fuzzy artist+album match — matched locally, no per-release GET."""
    client = make_discogs_reader()
    client._collection_index = _index(111, 42, title="Sister", artists=("Sonic Youth",))
    client._database_search = MagicMock(return_value=[])  # strategy 1 finds nothing
    client._client = MagicMock()
    client._client.release.return_value = MagicMock()
    client._build_result = MagicMock(return_value={"release_id": 111, "instance_id": 42})

    result = client.search_collection("sonic youth", "sister")
    assert result == {"release_id": 111, "instance_id": 42}
    client._client.release.assert_called_once_with(111)


def test_strategy_2_release_fetch_error_propagates():
    """A transient error fetching the matched release in strategy 2 must
    propagate (couldn't-determine), not be swallowed as 'not owned' (B-4)."""
    client = make_discogs_reader()
    client._collection_index = _index(111, 42, title="Sister", artists=("Sonic Youth",))
    client._database_search = MagicMock(return_value=[])
    client._client = MagicMock()
    client._client.release.side_effect = requests.exceptions.Timeout("slow")

    with pytest.raises(requests.exceptions.Timeout):
        client.search_collection("sonic youth", "sister")


# ---------------------------------------------------------------------------
# SEC-1 — an incomplete recognition (empty / whitespace artist OR album) must
# NOT select an arbitrary owned record as the Play Count / Last Played write
# target. The strategy-2 substring test degenerates on an empty term
# (`"" in title` is always True) — and even a single space is a substring of
# most titles ("kind of blue" contains spaces) — so it would hand back the
# most-recently-added owned release with its instance_id. That instance_id is
# what the collection writer POSTs to, so a junk Shazam match becomes a wrong
# write to real collection data. search_collection must return None instead, so
# the track falls through to the database/fallback tiers (no instance_id, no
# write) rather than crediting a play to the wrong record.
# ---------------------------------------------------------------------------

def _wrong_target_reader():
    """A reader whose collection would be wrongly selected by a degenerate match:
    strategy 1 finds nothing, and any strategy-2 hit would build a write target."""
    client = make_discogs_reader()
    client._collection_index = _index(111, 42, title="Kind of Blue", artists=("Miles Davis",))
    client._database_search = MagicMock(return_value=[])   # strategy 1 finds nothing
    client._client = MagicMock()
    client._client.release.return_value = MagicMock()
    client._build_result = MagicMock(return_value={"release_id": 111, "instance_id": 42})
    return client


def test_empty_album_does_not_select_a_write_target():
    client = _wrong_target_reader()

    result = client.search_collection("Miles Davis", "")

    assert result is None
    client._build_result.assert_not_called()   # never chose a write target
    client._client.release.assert_not_called()


def test_empty_artist_does_not_select_a_write_target():
    client = _wrong_target_reader()

    result = client.search_collection("", "Kind of Blue")

    assert result is None
    client._build_result.assert_not_called()


def test_single_space_album_does_not_select_a_write_target():
    """A single space IS a substring of most titles ('kind of blue' contains
    spaces), so an un-stripped guard would still degenerate — this pins the
    .strip() on the album term and reproduces the bug on the pre-fix code."""
    client = _wrong_target_reader()

    result = client.search_collection("Miles Davis", " ")

    assert result is None
    client._build_result.assert_not_called()


def test_single_space_artist_does_not_select_a_write_target():
    """Pins the .strip() on the artist term (a single space is a substring of
    'miles davis')."""
    client = _wrong_target_reader()

    result = client.search_collection(" ", "Kind of Blue")

    assert result is None
    client._build_result.assert_not_called()


def test_both_terms_empty_does_not_select_a_write_target():
    client = _wrong_target_reader()

    result = client.search_collection("", "")

    assert result is None
    client._build_result.assert_not_called()


def test_legitimately_short_album_still_matches():
    """The guard rejects EMPTY/whitespace only — a real, short album title must
    still match (no arbitrary minimum-length rejection that would false-negative
    titles like '4' or 'Q')."""
    client = make_discogs_reader()
    client._collection_index = _index(111, 42, title="4", artists=("Beyoncé",))
    client._database_search = MagicMock(return_value=[])
    client._client = MagicMock()
    client._client.release.return_value = MagicMock()
    client._build_result = MagicMock(return_value={"release_id": 111, "instance_id": 42})

    result = client.search_collection("Beyoncé", "4")

    assert result == {"release_id": 111, "instance_id": 42}
