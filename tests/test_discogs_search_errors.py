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

from tests.test_discogs_client import make_client


# ---------------------------------------------------------------------------
# B-13 — _database_search raises on hard error instead of returning []
# ---------------------------------------------------------------------------

def test_database_search_raises_on_hard_error():
    client = make_client()
    client._client = MagicMock()
    client._client.search.side_effect = requests.exceptions.ConnectionError("boom")
    with pytest.raises(requests.exceptions.ConnectionError):
        client._database_search("artist", "album")


def test_database_search_returns_empty_on_genuine_no_match():
    client = make_client()
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
    client = make_client()
    client._collection_index = None
    client._request = MagicMock(side_effect=requests.exceptions.Timeout("slow"))
    with pytest.raises(requests.exceptions.Timeout):
        client.search_collection("artist", "album")


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
    client = make_client()
    client._collection_index = _index(111, 42)        # pre-built index (no HTTP)
    client._database_search = MagicMock(return_value=[_candidate(111)])
    client._build_result = MagicMock(return_value={"release_id": 111, "instance_id": 42})

    result = client.search_collection("Sonic Youth", "Sister")
    assert result == {"release_id": 111, "instance_id": 42}
    client._build_result.assert_called_once()


def test_candidate_not_in_index_is_not_owned():
    client = make_client()
    client._collection_index = _index(999, 7)         # owns a DIFFERENT release
    client._database_search = MagicMock(return_value=[_candidate(111)])  # candidate not owned

    # No id match and no fuzzy match → not in collection.
    assert client.search_collection("Some Artist", "Other Album") is None


def test_strategy_2_fuzzy_matches_index_without_extra_http():
    """A candidate whose release_id isn't owned still resolves if the index has
    a fuzzy artist+album match — matched locally, no per-release GET."""
    client = make_client()
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
    client = make_client()
    client._collection_index = _index(111, 42, title="Sister", artists=("Sonic Youth",))
    client._database_search = MagicMock(return_value=[])
    client._client = MagicMock()
    client._client.release.side_effect = requests.exceptions.Timeout("slow")

    with pytest.raises(requests.exceptions.Timeout):
        client.search_collection("sonic youth", "sister")
