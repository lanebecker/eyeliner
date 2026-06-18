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
# B-13 — the collection walk re-raises a hard page error (vs. None for no-match)
# ---------------------------------------------------------------------------

def test_collection_walk_reraises_on_page_error():
    client = make_client()
    client._request = MagicMock(side_effect=requests.exceptions.Timeout("slow"))
    with pytest.raises(requests.exceptions.Timeout):
        client._search_collection_walk("artist", "album")


# ---------------------------------------------------------------------------
# B-4 — search_collection: 404 → "not owned" (continue); other errors propagate
# ---------------------------------------------------------------------------

def _candidate(release_id=111, title="Sister"):
    rel = MagicMock()
    rel.id = release_id
    rel.title = title
    return rel


def test_transient_membership_error_propagates():
    client = make_client()
    client._database_search = MagicMock(return_value=[_candidate()])
    # A 500/timeout on the membership check → couldn't determine → must raise.
    client._get_collection_instance_id = MagicMock(
        side_effect=requests.exceptions.HTTPError("500")
    )
    with pytest.raises(requests.exceptions.HTTPError):
        client.search_collection("artist", "album")


def test_404_candidate_is_treated_as_not_owned_and_falls_through_to_walk():
    client = make_client()
    client._database_search = MagicMock(return_value=[_candidate()])
    client._get_collection_instance_id = MagicMock(return_value=None)  # 404 → not owned
    client._search_collection_walk = MagicMock(return_value=None)      # genuine no-match

    assert client.search_collection("artist", "album") is None
    client._search_collection_walk.assert_called_once()


def test_owned_candidate_returns_built_result():
    client = make_client()
    client._database_search = MagicMock(return_value=[_candidate()])
    client._get_collection_instance_id = MagicMock(return_value=42)
    client._build_result = MagicMock(return_value={"release_id": 111, "instance_id": 42})

    result = client.search_collection("artist", "album")
    assert result == {"release_id": 111, "instance_id": 42}
