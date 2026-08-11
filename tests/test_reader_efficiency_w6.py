"""R5 Wave 6 (#256/#257/#258) — reader.py efficiency: spend the API budget once.

R5-19: `_build_result` re-fetched /releases/{id} to read the tracklist (a second
       GET per resolved album). It now reads the tracklist off the already-fetched
       release via `_parse_tracklist`.
R5-20: the same (artist, album) database search was issued 2-3× per resolve
       (strategy 1, the database tier, the staleness refresh). A one-entry memo
       fetches the page once and slices to each caller's limit.
R5-27: `search_collection` re-folded every index title on every call. The folded
       keys are precomputed at index-build time.

All three are behaviour-preserving — verified against the matching/resolver suite;
these tests pin the COST.
"""
from unittest.mock import MagicMock

from tests.factories import make_discogs_reader
import src.metadata.discogs.reader as reader_mod


def _release(rid):
    r = MagicMock()
    r.id = rid
    r.images = []
    r.labels = []
    r.year = 1990
    r.styles = ["Rock"]
    r.genres = ["Rock"]
    r.master = None
    r.tracklist = [MagicMock(type_=None, position="A1", title="One", duration="3:00")]
    return r


def _page(items):
    def make(pn, pg):
        resp = MagicMock(); resp.status_code = 200
        resp.json.return_value = {"releases": items, "pagination": {"page": pn, "pages": pg}}
        return resp
    return make


def _item(rid, iid, title, artists):
    return {"instance_id": iid,
            "basic_information": {"id": rid, "title": title,
                                  "artists": [{"name": a} for a in artists]}}


def test_r5_19_build_result_does_not_refetch_the_release_for_the_tracklist():
    reader = make_discogs_reader()
    reader.get_original_year = MagicMock(return_value=None)   # skip the master GET
    reader._client.release = MagicMock(side_effect=_release)  # would count a 2nd fetch

    result = reader._build_result(_release(555), instance_id=None)

    reader._client.release.assert_not_called()               # RED before R5-19: called once
    assert [e.position for e in result["tracklist"]] == ["A1"]


def test_r5_20_same_query_is_searched_once_across_collection_and_database():
    reader = make_discogs_reader()
    calls = {"n": 0}

    def fake_search(album, artist=None, type=None):
        calls["n"] += 1
        m = MagicMock(); m.page = MagicMock(return_value=[])
        return m

    reader._client.search = MagicMock(side_effect=fake_search)
    reader._get_collection_index = MagicMock(return_value={})

    reader.search_collection("Radiohead", "Kid A")   # strategy 1, limit 25
    reader.search_database("Radiohead", "Kid A")       # database tier, limit 3

    assert calls["n"] == 1                             # RED before R5-20: 2


def test_r5_20_a_different_query_bypasses_the_memo():
    reader = make_discogs_reader()
    calls = {"n": 0}

    def fake_search(album, artist=None, type=None):
        calls["n"] += 1
        m = MagicMock(); m.page = MagicMock(return_value=[])
        return m

    reader._client.search = MagicMock(side_effect=fake_search)
    reader._database_search("A", "one")
    reader._database_search("B", "two")   # different query → fresh fetch
    assert calls["n"] == 2


def test_r5_20_memo_respects_the_requested_limit():
    reader = make_discogs_reader()
    releases = [MagicMock(id=i) for i in range(25)]
    m = MagicMock(); m.page = MagicMock(return_value=releases)
    reader._client.search = MagicMock(return_value=m)

    assert len(reader._database_search("X", "y", limit=25)) == 25
    assert len(reader._database_search("X", "y", limit=3)) == 3   # cache hit, re-sliced
    reader._client.search.assert_called_once()


def test_r5_27_index_titles_are_not_refolded_on_every_search():
    reader = make_discogs_reader()
    reader._http.request = MagicMock(
        return_value=_page([_item(i, i, f"Album {i}", [f"Artist {i}"]) for i in range(200)])(1, 1)
    )
    reader._database_search = MagicMock(return_value=[])
    reader._get_collection_index()          # builds + precomputes the folded keys

    folds = {"n": 0}
    orig = reader_mod._normalize_term
    reader_mod._normalize_term = lambda s: (folds.__setitem__("n", folds["n"] + 1) or orig(s))
    try:
        reader.search_collection("Nobody", "Nothing")   # a MISS → tier 1 + tier 2
    finally:
        reader_mod._normalize_term = orig

    assert folds["n"] <= 5                    # RED before R5-27: ~601 (fold per entry per tier)


def test_r6_15_memo_expires_after_its_ttl(monkeypatch):
    """R6-15: the R5-20 one-entry memo must not replay a stale (possibly empty)
    page for a 24/7 SAME-record repeat. Within the TTL the intra-resolve dedup is
    preserved (one fetch); past the TTL the same (artist, album) re-fetches."""
    reader = make_discogs_reader()
    calls = {"n": 0}

    def fake_search(album, artist=None, type=None):
        calls["n"] += 1
        m = MagicMock(); m.page = MagicMock(return_value=[])
        return m

    reader._client.search = MagicMock(side_effect=fake_search)
    clock = {"t": 1000.0}
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: clock["t"])

    reader._database_search("A", "one")                       # fetch 1
    reader._database_search("A", "one")                       # within TTL → memo hit
    assert calls["n"] == 1
    clock["t"] += reader_mod._DB_SEARCH_MEMO_TTL_SECONDS + 1
    reader._database_search("A", "one")                       # past TTL → re-fetch
    assert calls["n"] == 2
