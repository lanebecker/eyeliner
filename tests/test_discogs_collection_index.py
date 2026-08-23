"""Regression tests for P-1 — session collection index eliminates the N+1.

The old strategy GET'd /collection/releases/{id} once per database candidate
(up to 25 sequential blocking calls per cold album).  The collection is static
within a session, so we now build an in-memory index ONCE and match locally.
"""
from unittest.mock import MagicMock

import pytest

from tests.factories import make_discogs_reader


def _page(releases, page, pages, per_page=None, items=None):
    resp = MagicMock()
    resp.status_code = 200
    if per_page is None:
        per_page = max(len(releases), 1)
    resp.json.return_value = {
        "releases": releases,
        "pagination": {
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "items": len(releases) if items is None else items,
        },
    }
    return resp


def _item(release_id, instance_id, title, artists, master_id=None):
    basic = {
        "id": release_id,
        "title": title,
        "artists": [{"name": a} for a in artists],
    }
    if master_id is not None:
        basic["master_id"] = master_id
    return {
        "instance_id": instance_id,
        "basic_information": basic,
    }


def _candidate(release_id):
    rel = MagicMock()
    rel.id = release_id
    rel.title = "candidate"
    return rel


def test_index_built_once_paginated_and_cached():
    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=[
        _page([_item(111, 42, "Sister", ["Sonic Youth"], master_id=900)], 1, 2, items=2),
        _page([_item(222, 43, "Goo", ["Sonic Youth"])], 2, 2, items=2),
    ])

    idx = client._get_collection_index().index

    assert set(idx.keys()) == {111, 222}
    # #226: master_id is captured from basic_information (present here) …
    assert idx[111] == {
        "instance_id": 42, "title": "Sister", "artists": ["Sonic Youth"],
        # R5-07: the reconstructed multi-artist credit string (a single artist
        # reconstructs to its own name).
        "artist_credit": "Sonic Youth",
        # R5-27: folded match keys precomputed at build time.
        "_title_key": "sister",
        "_artist_keys": ["sonic youth"],
        "_credit_key": "sonic youth",
        "master_id": 900,
    }
    # … and defaults to None when the release carries no master.
    assert idx[222]["master_id"] is None
    assert client._http.request.call_count == 2          # one GET per page

    # Second call is served from cache — no further HTTP.
    idx2 = client._get_collection_index().index
    assert idx2 is idx
    assert client._http.request.call_count == 2


def test_search_collection_issues_no_per_candidate_http():
    """The N+1 is gone: with the index pre-built, checking 26 candidates makes
    ZERO membership GETs (previously up to 25 sequential blocking calls)."""
    client = make_discogs_reader()
    client._collection_index = {
        111: {"instance_id": 42, "title": "Sister", "artists": ["Sonic Youth"]},
    }
    # 25 non-owned candidates, then the owned one last — worst case for the old code.
    candidates = [_candidate(1000 + i) for i in range(25)] + [_candidate(111)]
    client._database_search = MagicMock(return_value=candidates)
    client._build_result = MagicMock(return_value={"ok": True})
    client._http.request = MagicMock()  # spy: must NOT be called

    result = client.search_collection("Sonic Youth", "Sister")

    assert result == {"ok": True}
    client._http.request.assert_not_called()             # no membership round-trips


def test_first_instance_kept_for_duplicate_release():
    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=[
        _page([
            _item(111, 42, "Sister", ["Sonic Youth"]),
            _item(111, 99, "Sister", ["Sonic Youth"]),   # duplicate copy
        ], 1, 1),
    ])
    idx = client._get_collection_index().index
    assert idx[111]["instance_id"] == 42             # first instance wins


# ---------------------------------------------------------------------------
# STAB-4 — the paging loop has an ABSOLUTE page cap (crash-loop rate-limit guard)
# ---------------------------------------------------------------------------

def test_page_cap_rejects_partial_index_and_preserves_prior_complete_snapshot(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod, "_MAX_COLLECTION_PAGES", 2)
    prior = {99: {"instance_id": 9, "title": "Known", "artists": ["A"]}}
    client = make_discogs_reader()
    client._collection_index = prior
    client._collection_index_built_at = -reader_mod._COLLECTION_INDEX_TTL_SECONDS
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: reader_mod._COLLECTION_INDEX_TTL_SECONDS + 1)
    client._http.request = MagicMock(side_effect=[
        _page([_item(1, 1, "One", ["A"])], 1, 3, per_page=1, items=3),
        _page([_item(2, 2, "Two", ["A"])], 2, 3, per_page=1, items=3),
    ])

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()

    assert client._collection_index is prior
    assert client._collection_index_built_at == -reader_mod._COLLECTION_INDEX_TTL_SECONDS


def test_page_cap_without_prior_snapshot_leaves_index_unavailable(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod, "_MAX_COLLECTION_PAGES", 1)
    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(return_value=_page(
        [_item(1, 1, "One", ["A"])], 1, 2, per_page=1, items=2
    ))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()

    assert client._collection_index is None


def test_failed_build_backoff_prevents_second_page_walk_with_prior_snapshot(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: clock["t"])
    prior = {99: {"instance_id": 9, "title": "Known", "artists": ["A"]}}
    client = make_discogs_reader()
    client._collection_index = prior
    client._collection_index_built_at = -reader_mod._COLLECTION_INDEX_TTL_SECONDS
    client._http.request = MagicMock(side_effect=ConnectionError("down"))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()
    calls = client._http.request.call_count
    view = client._get_collection_index()

    assert view.index is prior
    assert view.misses_are_authoritative is False
    assert client._http.request.call_count == calls


def test_failed_build_backoff_prevents_second_page_walk_without_snapshot(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: 1000.0)
    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=ConnectionError("down"))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()
    calls = client._http.request.call_count
    with pytest.raises(reader_mod.CollectionOwnershipUnknown):
        client._get_collection_index()

    assert client._http.request.call_count == calls


def test_build_backoff_serves_positive_from_prior_snapshot(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: 1000.0)
    client = make_discogs_reader()
    client._collection_index = {1: {"instance_id": 7, "title": "Sister", "artists": ["Sonic Youth"]}}
    client._collection_index_built_at = -reader_mod._COLLECTION_INDEX_TTL_SECONDS
    client._http.request = MagicMock(side_effect=ConnectionError("down"))
    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()
    client._database_search = MagicMock(return_value=[])
    client._client.release = MagicMock(return_value=MagicMock(id=1))
    client._build_result = MagicMock(return_value={"instance_id": 7})

    assert client.search_collection("Sonic Youth", "Sister") == {"instance_id": 7}


def test_build_backoff_miss_is_unknown_not_clean(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: 1000.0)
    client = make_discogs_reader()
    client._collection_index = {1: {"instance_id": 7, "title": "Goo", "artists": ["Sonic Youth"]}}
    client._collection_index_built_at = -reader_mod._COLLECTION_INDEX_TTL_SECONDS
    client._http.request = MagicMock(side_effect=ConnectionError("down"))
    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()
    client._database_search = MagicMock(return_value=[])

    with pytest.raises(reader_mod.CollectionOwnershipUnknown):
        client.search_collection("Sonic Youth", "Sister")


def test_build_backoff_refusal_does_not_change_speculative_cooldown_state(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: clock["t"])
    client = make_discogs_reader()
    client._last_collection_build_failure_at = clock["t"]
    client._last_index_refresh_at = -1000.0

    with pytest.raises(reader_mod.CollectionOwnershipUnknown):
        client.refresh_index_and_research("Sonic Youth", "Sister")

    assert client._last_index_refresh_at == -1000.0


def test_successful_complete_rebuild_clears_failure_backoff(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: 2000.0)
    client = make_discogs_reader()
    client._last_collection_build_failure_at = 2000.0 - reader_mod._COLLECTION_BUILD_FAILURE_BACKOFF_SECONDS - 1
    client._collection_index = None
    client._http.request = MagicMock(return_value=_page([], 1, 1, per_page=100, items=0))

    view = client._get_collection_index()

    assert view.index == {}
    assert client._last_collection_build_failure_at is None


def test_complete_multi_page_duplicate_release_counts_raw_rows(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: 1000.0)
    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=[
        _page([_item(1, 7, "One", ["A"]), _item(2, 8, "Two", ["A"])], 1, 2,
              per_page=2, items=3),
        _page([_item(1, 9, "One", ["A"])], 2, 2, per_page=2, items=3),
    ])

    view = client._get_collection_index()

    assert set(view.index) == {1, 2}
    assert view.index[1]["instance_id"] == 7


def test_advertised_pages_beyond_cap_never_promotes_candidate_index(monkeypatch):
    from src.metadata.discogs import reader as reader_mod
    monkeypatch.setattr(reader_mod, "_MAX_COLLECTION_PAGES", 1)
    client = make_discogs_reader()
    prior = {99: {"instance_id": 9, "title": "Known", "artists": ["A"]}}
    client._collection_index = prior
    client._collection_index_built_at = 0
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: reader_mod._COLLECTION_INDEX_TTL_SECONDS + 1)
    client._http.request = MagicMock(return_value=_page(
        [_item(1, 1, "One", ["A"])], 1, 2, per_page=1, items=2
    ))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()

    assert client._collection_index is prior


def test_advertised_extra_empty_final_page_never_promotes_candidate_index():
    """The metadata cannot advertise a second empty page after all claimed
    items were already supplied by page one."""
    from src.metadata.discogs import reader as reader_mod

    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=[
        _page([_item(1, 1, "One", ["A"])], 1, 2, per_page=1, items=1),
        _page([], 2, 2, per_page=1, items=1),
    ])

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()

    assert client._collection_index is None


@pytest.mark.parametrize("responses", [
    [{"releases": [], "pagination": None}],
    [{"releases": [], "pagination": {"page": 2, "pages": 1, "per_page": 100, "items": 0}}],
    [
        {"releases": [_item(1, 1, "One", ["A"])], "pagination": {"page": 1, "pages": 2, "per_page": 1, "items": 2}},
        {"releases": [_item(2, 2, "Two", ["A"])], "pagination": {"page": 2, "pages": 3, "per_page": 1, "items": 2}},
    ],
    [
        {"releases": [_item(1, 1, "One", ["A"])], "pagination": {"page": 1, "pages": 2, "per_page": 1, "items": 2}},
        {"releases": [_item(2, 2, "Two", ["A"])], "pagination": {"page": 2, "pages": 2, "per_page": 1, "items": 3}},
    ],
    [{"releases": [], "pagination": {"page": 1, "pages": 1, "per_page": True, "items": 0}}],
    [{"releases": [_item(True, 1, "One", ["A"])], "pagination": {"page": 1, "pages": 1, "per_page": 1, "items": 1}}],
    [{"releases": [_item(1, False, "One", ["A"])], "pagination": {"page": 1, "pages": 1, "per_page": 1, "items": 1}}],
    [
        {"releases": [_item(1, 1, "One", ["A"])], "pagination": {"page": 1, "pages": 2, "per_page": 1, "items": 2}},
        {"releases": [], "pagination": {"page": 2, "pages": 2, "per_page": 1, "items": 2}},
    ],
])
def test_incomplete_pagination_never_promotes_candidate_index(monkeypatch, responses):
    from src.metadata.discogs import reader as reader_mod
    prior = {99: {"instance_id": 9, "title": "Known", "artists": ["A"]}}
    client = make_discogs_reader()
    client._collection_index = prior
    client._collection_index_built_at = 0
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: reader_mod._COLLECTION_INDEX_TTL_SECONDS + 1)
    mocked = []
    for data in responses:
        response = MagicMock(status_code=200)
        response.json.return_value = data
        mocked.append(response)
    client._http.request = MagicMock(side_effect=mocked)

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        client._get_collection_index()

    assert client._collection_index is prior


def test_stab4_cap_is_generous_enough_for_any_real_collection():
    """The cap is a safety ceiling, not a functional limit: it must sit far above
    any realistic personal vinyl collection so it never clips a genuine one.
    100 items/page × cap should comfortably exceed tens of thousands of records."""
    from src.metadata.discogs import reader as reader_mod
    assert reader_mod._MAX_COLLECTION_PAGES >= 500      # >= 50,000 releases
    # ...but it must remain a real ceiling: an absurdly high value silently
    # defeats the crash-loop guard, so pin the sane upper end explicitly rather
    # than relying on another test's fixture to trip on it incidentally.
    assert reader_mod._MAX_COLLECTION_PAGES <= 5000     # <= 500,000 releases
