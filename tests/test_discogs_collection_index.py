"""Regression tests for P-1 — session collection index eliminates the N+1.

The old strategy GET'd /collection/releases/{id} once per database candidate
(up to 25 sequential blocking calls per cold album).  The collection is static
within a session, so we now build an in-memory index ONCE and match locally.
"""
from unittest.mock import MagicMock

from tests.factories import make_discogs_reader


def _page(releases, page, pages):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"releases": releases, "pagination": {"page": page, "pages": pages}}
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
        _page([_item(111, 42, "Sister", ["Sonic Youth"], master_id=900)], 1, 2),
        _page([_item(222, 43, "Goo", ["Sonic Youth"])], 2, 2),
    ])

    idx = client._get_collection_index()

    assert set(idx.keys()) == {111, 222}
    # #226: master_id is captured from basic_information (present here) …
    assert idx[111] == {
        "instance_id": 42, "title": "Sister", "artists": ["Sonic Youth"],
        "master_id": 900,
    }
    # … and defaults to None when the release carries no master.
    assert idx[222]["master_id"] is None
    assert client._http.request.call_count == 2          # one GET per page

    # Second call is served from cache — no further HTTP.
    idx2 = client._get_collection_index()
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
    idx = client._get_collection_index()
    assert idx[111]["instance_id"] == 42             # first instance wins


# ---------------------------------------------------------------------------
# STAB-4 — the paging loop has an ABSOLUTE page cap (crash-loop rate-limit guard)
# ---------------------------------------------------------------------------

def test_stab4_paging_stops_at_absolute_cap_with_partial_index():
    """A malformed / hostile ``pagination.pages`` (or a logic bug) must NOT let
    the loop page without bound.  It must stop at an absolute ceiling, keep the
    partial index it built, and cache it (so it is not re-hammered per track).

    On today's uncapped ``while True`` this fetches until the fake's hard stop
    trips (RED); after the fix it stops at the cap (GREEN).
    """
    from src.metadata.discogs import reader as reader_mod

    HARD_STOP = 5000            # today's unbounded loop blows past any sane cap
    calls = {"n": 0}

    def hostile_request(method, url, params=None):
        calls["n"] += 1
        if calls["n"] > HARD_STOP:
            raise AssertionError(f"unbounded paging: fetched >{HARD_STOP} pages")
        page = params["page"]
        # Every page is full and pagination claims a billion pages, so the only
        # thing that can end this loop is an absolute cap.
        return _page([_item(page, page, f"T{page}", ["A"])], page, 10 ** 9)

    client = make_discogs_reader()
    client._collection_index = None
    client._http.request = MagicMock(side_effect=hostile_request)

    idx = client._get_collection_index()               # must RETURN, not run away

    cap = reader_mod._MAX_COLLECTION_PAGES
    assert calls["n"] == cap                            # stopped exactly at the cap
    assert len(idx) == cap                              # partial index preserved
    # Cached: a second call makes no further HTTP (not re-hammered per track).
    client._get_collection_index()
    assert client._http.request.call_count == cap


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
