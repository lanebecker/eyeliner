"""Regression tests for P-1 — session collection index eliminates the N+1.

The old strategy GET'd /collection/releases/{id} once per database candidate
(up to 25 sequential blocking calls per cold album).  The collection is static
within a session, so we now build an in-memory index ONCE and match locally.
"""
from unittest.mock import MagicMock

from tests.test_discogs_client import make_client


def _page(releases, page, pages):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"releases": releases, "pagination": {"page": page, "pages": pages}}
    return resp


def _item(release_id, instance_id, title, artists):
    return {
        "instance_id": instance_id,
        "basic_information": {
            "id": release_id,
            "title": title,
            "artists": [{"name": a} for a in artists],
        },
    }


def _candidate(release_id):
    rel = MagicMock()
    rel.id = release_id
    rel.title = "candidate"
    return rel


def test_index_built_once_paginated_and_cached():
    client = make_client()
    client._collection_index = None
    client._request = MagicMock(side_effect=[
        _page([_item(111, 42, "Sister", ["Sonic Youth"])], 1, 2),
        _page([_item(222, 43, "Goo", ["Sonic Youth"])], 2, 2),
    ])

    idx = client._get_collection_index()

    assert set(idx.keys()) == {111, 222}
    assert idx[111] == {"instance_id": 42, "title": "Sister", "artists": ["Sonic Youth"]}
    assert client._request.call_count == 2          # one GET per page

    # Second call is served from cache — no further HTTP.
    idx2 = client._get_collection_index()
    assert idx2 is idx
    assert client._request.call_count == 2


def test_search_collection_issues_no_per_candidate_http():
    """The N+1 is gone: with the index pre-built, checking 26 candidates makes
    ZERO membership GETs (previously up to 25 sequential blocking calls)."""
    client = make_client()
    client._collection_index = {
        111: {"instance_id": 42, "title": "Sister", "artists": ["Sonic Youth"]},
    }
    # 25 non-owned candidates, then the owned one last — worst case for the old code.
    candidates = [_candidate(1000 + i) for i in range(25)] + [_candidate(111)]
    client._database_search = MagicMock(return_value=candidates)
    client._build_result = MagicMock(return_value={"ok": True})
    client._request = MagicMock()  # spy: must NOT be called

    result = client.search_collection("Sonic Youth", "Sister")

    assert result == {"ok": True}
    client._request.assert_not_called()             # no membership round-trips


def test_first_instance_kept_for_duplicate_release():
    client = make_client()
    client._collection_index = None
    client._request = MagicMock(side_effect=[
        _page([
            _item(111, 42, "Sister", ["Sonic Youth"]),
            _item(111, 99, "Sister", ["Sonic Youth"]),   # duplicate copy
        ], 1, 1),
    ])
    idx = client._get_collection_index()
    assert idx[111]["instance_id"] == 42             # first instance wins
