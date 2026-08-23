"""Regression tests for Wave 2 bundle 3 — stale caches on a long uptime (#191).

The appliance runs 24/7; nothing implements the "restarts daily" premise the
collection index and the resolver album cache leaned on for freshness. So a
record added to Discogs during a long uptime was silently never Play-Count
credited until a manual restart:

  * B1 — the reader's collection index was built once and cached forever, so a
    newly-added record kept missing both search_collection strategies.
  * B2 — even after the index refreshes, the resolver's album cache had already
    pinned the (artist, album) key as a DATABASE-tier downgrade (no instance_id)
    with no TTL, so Step 0 short-circuited every replay before it could re-resolve.
  * C  — on a collection miss where the database DOES know the album (the exact
    signature of a just-added record), the reader now force-refreshes the index
    once (behind a cooldown) and re-checks ownership, upgrading the credit to the
    COLLECTION tier on the very next play instead of after a TTL or a reboot.

Fix: monotonic-clock TTL on the index (B1) and on the resolver's DATABASE/FALLBACK
entries only — COLLECTION hits never expire (B2) — plus a cooldown'd
staleness-triggered refresh (C).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.metadata.models import MetadataSource, TracklistEntry
from src.metadata.discogs.outcomes import CollectionRefreshResult, CollectionRefreshState
from tests.factories import make_discogs_reader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page(releases, page=1, pages=1, per_page=100, items=None):
    resp = MagicMock()
    resp.status_code = 200
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


def _item(release_id, instance_id, title, artists):
    return {
        "instance_id": instance_id,
        "basic_information": {
            "id": release_id,
            "title": title,
            "artists": [{"name": a} for a in artists],
        },
    }


class _Clock:
    """A controllable monotonic clock for TTL/cooldown tests."""
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, seconds):
        self.t += seconds


def _make_resolver(reader=None, coverart=None):
    from src.metadata.resolver import MetadataResolver, _ALBUM_CACHE_MAX
    from src.util.cache import BoundedCache
    r = MetadataResolver.__new__(MetadataResolver)
    r.reader = reader or MagicMock()
    if reader is None:
        r.reader.search_collection.return_value = None
        r.reader.search_database.return_value = None
        r.reader.refresh_index_and_research.return_value = CollectionRefreshResult(
            CollectionRefreshState.CLEAN_NO_MATCH
        )
        r.reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    r.coverart = coverart or MagicMock()
    r.coverart.get_cover_art_url.return_value = None
    r._album_cache = BoundedCache(_ALBUM_CACHE_MAX)
    r._reader_gate = asyncio.Lock()
    r._logged_discogs_config = {}
    return r


def make_raw(title="So What", artist="Sonic Youth", album="Sister"):
    from src.audio.recognizer import RawRecognitionResult
    return RawRecognitionResult(title=title, artist=artist, album=album)


def _db_result(release_id=555, instance_id=None):
    return {
        "album": "Sister",
        "year": "1987",
        "label": "SST",
        "catalog_number": "SST 134",
        "release_id": release_id,
        "instance_id": instance_id,
        "cover_art_url": None,
        "tracklist": [TracklistEntry("A1", "Schizophrenia"), TracklistEntry("B4", "Master-Dik")],
    }


# ---------------------------------------------------------------------------
# B1 — the collection index expires and rebuilds after the TTL.
# ---------------------------------------------------------------------------

def test_index_rebuilds_after_ttl_so_a_newly_added_record_is_found(monkeypatch):
    """A record added to Discogs after the index was built is found once the
    index TTL elapses — today the index caches forever and never sees it."""
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)

    reader = make_discogs_reader()
    reader._collection_index = None
    # Database tier returns no candidates, so strategy 1 is empty and C never
    # fires — this test isolates B1 (the index TTL) via strategy 2.
    reader._database_search = MagicMock(return_value=[])
    # _build_result normally fetches the release via discogs_client; echo the
    # instance_id (threaded from the matched index entry) so the assertion below
    # pins WHICH record was found without needing a real client.
    reader._build_result = MagicMock(side_effect=lambda release, instance_id=None: {"instance_id": instance_id})

    owned_before = [_item(111, 42, "Goo", ["Sonic Youth"])]                 # NOT Sister
    owned_after = owned_before + [_item(555, 77, "Sister", ["Sonic Youth"])]  # added later
    reader._http.request = MagicMock(return_value=_page(owned_before))

    assert reader.search_collection("Sonic Youth", "Sister") is None   # not owned yet

    # The owner adds Sister to their Discogs collection.
    reader._http.request = MagicMock(return_value=_page(owned_after))

    # Still within the TTL: the stale index is served, so it's still "not owned".
    assert reader.search_collection("Sonic Youth", "Sister") is None

    # TTL elapses → next access rebuilds the index → the record is now found.
    clock.advance(reader_mod._COLLECTION_INDEX_TTL_SECONDS + 1)
    result = reader.search_collection("Sonic Youth", "Sister")
    assert result is not None
    assert result["instance_id"] == 77          # the just-added record's instance


def test_index_not_rebuilt_within_ttl(monkeypatch):
    """Within the TTL the index is served from cache — no extra HTTP (P-1)."""
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)

    reader = make_discogs_reader()
    reader._collection_index = None
    reader._http.request = MagicMock(return_value=_page([_item(111, 42, "Sister", ["Sonic Youth"])]))

    reader._get_collection_index()
    calls_after_build = reader._http.request.call_count
    clock.advance(reader_mod._COLLECTION_INDEX_TTL_SECONDS - 1)   # still fresh
    reader._get_collection_index()
    assert reader._http.request.call_count == calls_after_build   # no rebuild


# ---------------------------------------------------------------------------
# C — cooldown'd staleness-triggered refresh (reader).
# ---------------------------------------------------------------------------

def test_refresh_returns_owned_after_complete_rebuild(monkeypatch):
    """refresh_index_and_research force-rebuilds a stale index and re-checks
    ownership, finding a record added after the original build."""
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)

    reader = make_discogs_reader()
    reader._database_search = MagicMock(return_value=[])
    reader._build_result = MagicMock(side_effect=lambda release, instance_id=None: {"instance_id": instance_id})
    reader._collection_index = {111: {"instance_id": 42, "title": "Goo", "artists": ["Sonic Youth"]}}
    reader._collection_index_built_at = clock.t   # freshly built, so B1 alone won't rebuild
    reader._http.request = MagicMock(
        return_value=_page([_item(111, 42, "Goo", ["Sonic Youth"]),
                            _item(555, 77, "Sister", ["Sonic Youth"])])
    )

    outcome = reader.refresh_index_and_research("Sonic Youth", "Sister")
    assert outcome.state is CollectionRefreshState.OWNED
    assert outcome.result["instance_id"] == 77          # the just-added record's instance


def test_refresh_returns_clean_no_match_after_complete_rebuild(monkeypatch):
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)
    reader = make_discogs_reader()
    reader._collection_index = {}
    reader._collection_index_built_at = clock.t
    reader._database_search = MagicMock(return_value=[])
    reader._http.request = MagicMock(return_value=_page([]))

    outcome = reader.refresh_index_and_research("Sonic Youth", "Sister")

    assert outcome.state is CollectionRefreshState.CLEAN_NO_MATCH
    assert outcome.result is None


def test_refresh_returns_cooldown_skipped_after_successful_rebuild(monkeypatch):
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)
    reader = make_discogs_reader()
    reader._collection_index = {}
    reader._collection_index_built_at = clock.t
    reader._database_search = MagicMock(return_value=[])
    reader._http.request = MagicMock(return_value=_page([]))

    reader.refresh_index_and_research("Sonic Youth", "Sister")
    calls = reader._http.request.call_count
    outcome = reader.refresh_index_and_research("Sonic Youth", "Sister")

    assert outcome.state is CollectionRefreshState.COOLDOWN_SKIPPED
    assert outcome.cooldown_follows_successful_rebuild is True
    assert reader._http.request.call_count == calls


def test_failed_refresh_marks_cooldown_provenance_unknown(monkeypatch):
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)
    reader = make_discogs_reader()
    reader._collection_index = {111: {"instance_id": 42, "title": "Goo", "artists": ["Sonic Youth"]}}
    reader._collection_index_built_at = clock.t
    reader._http.request = MagicMock(side_effect=ConnectionError("blip"))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        reader.refresh_index_and_research("Sonic Youth", "Sister")

    outcome = reader.refresh_index_and_research("Sonic Youth", "Sister")
    assert outcome.state is CollectionRefreshState.COOLDOWN_SKIPPED
    assert outcome.cooldown_follows_successful_rebuild is False


def test_cooldown_skip_after_failed_refresh_does_not_repage(monkeypatch):
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)
    reader = make_discogs_reader()
    reader._collection_index = {}
    reader._collection_index_built_at = clock.t
    reader._http.request = MagicMock(side_effect=ConnectionError("blip"))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        reader.refresh_index_and_research("Sonic Youth", "Sister")
    calls = reader._http.request.call_count
    outcome = reader.refresh_index_and_research("Sonic Youth", "Sister")

    assert outcome.state is CollectionRefreshState.COOLDOWN_SKIPPED
    assert outcome.cooldown_follows_successful_rebuild is False
    assert reader._http.request.call_count == calls


def test_refresh_preserves_the_prior_index_on_a_transient_rebuild_failure(monkeypatch):
    """R5-18: a transient failure DURING the forced rebuild must not leave the
    reader index-less. Before the fix, refresh_index_and_research nulled the
    index up front, so a dropped GET during the re-page discarded a
    seconds-old snapshot and forced a full re-page on every later resolve."""
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)

    reader = make_discogs_reader()
    good = {111: {"instance_id": 42, "title": "Goo", "artists": ["Sonic Youth"],
                  "artist_credit": "Sonic Youth", "master_id": None}}
    reader._collection_index = dict(good)
    reader._collection_index_built_at = clock.t
    reader._http.request = MagicMock(side_effect=ConnectionError("blip"))

    with pytest.raises(reader_mod.CollectionIndexIncomplete):
        reader.refresh_index_and_research("Someone", "New Album")

    # The previously-valid index survived; the error still propagated (B-4).
    assert reader._collection_index == good


def test_refresh_and_research_respects_cooldown(monkeypatch):
    """A second refresh within the cooldown does NOT re-page Discogs — the
    cooldown bounds the speculative re-fetch triggered by every unowned record."""
    import src.metadata.discogs.reader as reader_mod
    clock = _Clock()
    monkeypatch.setattr(reader_mod.time, "monotonic", clock)

    reader = make_discogs_reader()
    reader._database_search = MagicMock(return_value=[])
    reader._collection_index = {}
    reader._collection_index_built_at = clock.t
    reader._http.request = MagicMock(return_value=_page([]))   # empty collection

    reader.refresh_index_and_research("Sonic Youth", "Sister")
    first = reader._http.request.call_count
    assert first >= 1                                  # first refresh re-paged

    reader.refresh_index_and_research("Sonic Youth", "Sister")
    assert reader._http.request.call_count == first    # cooldown: no second re-page

    clock.advance(reader_mod._INDEX_REFRESH_COOLDOWN_SECONDS + 1)
    reader.refresh_index_and_research("Sonic Youth", "Sister")
    assert reader._http.request.call_count > first     # cooldown elapsed: re-pages again


# ---------------------------------------------------------------------------
# B2 — the resolver's DATABASE/FALLBACK downgrade expires; COLLECTION persists.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_database_downgrade_cache_expires_and_re_resolves(monkeypatch):
    """A DATABASE-tier downgrade must not pin forever: after the TTL the album
    re-resolves, so a now-owned record can be picked up (today it's cached with
    no TTL and Step 0 short-circuits every replay)."""
    import src.metadata.resolver as resolver_mod
    clock = _Clock()
    monkeypatch.setattr(resolver_mod.time, "monotonic", clock)

    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = None
    reader.search_database.return_value = _db_result()
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    resolver = _make_resolver(reader=reader)

    first = await resolver.resolve(make_raw())
    assert first.source is MetadataSource.DISCOGS_DATABASE
    assert reader.search_database.call_count == 1

    # Replay within the TTL → served from cache, no re-search.
    again = await resolver.resolve(make_raw())
    assert again.source is MetadataSource.DISCOGS_DATABASE
    assert reader.search_database.call_count == 1               # cache hit, not re-searched

    # Owner adds the record; TTL elapses; replay now re-resolves to COLLECTION.
    clock.advance(resolver_mod._DOWNGRADE_TTL_SECONDS + 1)
    reader.search_collection.return_value = {"release_id": 555, "instance_id": 77,
                                             "album": "Sister", "tracklist": []}
    upgraded = await resolver.resolve(make_raw())
    assert upgraded.source is MetadataSource.DISCOGS_COLLECTION
    assert reader.search_collection.call_count >= 2             # cache expired → re-searched


@pytest.mark.asyncio
async def test_collection_cache_never_expires(monkeypatch):
    """A COLLECTION-tier hit is correct and must be cached without a TTL — it is
    NOT re-searched even long after the downgrade TTL would have elapsed."""
    import src.metadata.resolver as resolver_mod
    clock = _Clock()
    monkeypatch.setattr(resolver_mod.time, "monotonic", clock)

    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = {"release_id": 555, "instance_id": 77,
                                             "album": "Sister", "tracklist": []}
    reader.search_database.return_value = None
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    resolver = _make_resolver(reader=reader)

    first = await resolver.resolve(make_raw())
    assert first.source is MetadataSource.DISCOGS_COLLECTION
    calls = reader.search_collection.call_count

    clock.advance(resolver_mod._DOWNGRADE_TTL_SECONDS * 10)     # long past any downgrade TTL
    again = await resolver.resolve(make_raw())
    assert again.source is MetadataSource.DISCOGS_COLLECTION
    assert reader.search_collection.call_count == calls        # COLLECTION never expires


# ---------------------------------------------------------------------------
# C — end-to-end through the resolver: a collection miss + database hit triggers
# the refresh, upgrading the credit to COLLECTION on this very resolve.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolver_upgrades_via_refresh_on_database_hit():
    """Collection misses, database hits, and the cooldown'd refresh finds the
    just-added record → the resolve returns COLLECTION (with an instance_id),
    not the DATABASE downgrade."""
    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = None                # stale index misses
    reader.search_database.return_value = _db_result()          # DB knows the album
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.OWNED,
        {"release_id": 555, "instance_id": 77, "album": "Sister", "tracklist": []},
    )
    resolver = _make_resolver(reader=reader)

    result = await resolver.resolve(make_raw())
    assert result.source is MetadataSource.DISCOGS_COLLECTION
    reader.refresh_index_and_research.assert_called_once_with("Sonic Youth", "Sister")


@pytest.mark.asyncio
async def test_refresh_is_not_triggered_when_the_collection_lookup_errored():
    """C is gated on a CLEAN collection miss. If the collection tier ERRORED
    (a transient blip — "couldn't determine ownership"), the resolver must NOT
    fire a speculative refresh/re-page on top of it: that would add load during
    an outage and break the B-4 leave-uncached-retry-next-track posture."""
    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.side_effect = ConnectionError("blip")   # errored, not a clean miss
    reader.search_database.return_value = _db_result()
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    resolver = _make_resolver(reader=reader)

    result = await resolver.resolve(make_raw())
    assert result.source is MetadataSource.DISCOGS_DATABASE
    reader.refresh_index_and_research.assert_not_called()            # gated off on error


@pytest.mark.asyncio
async def test_resolver_keeps_database_when_refresh_finds_nothing():
    """If the refresh still doesn't find the record (genuinely unowned), the
    resolve degrades to DATABASE as before — C never manufactures ownership."""
    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = None
    reader.search_database.return_value = _db_result()
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    resolver = _make_resolver(reader=reader)

    result = await resolver.resolve(make_raw())
    assert result.source is MetadataSource.DISCOGS_DATABASE
    reader.refresh_index_and_research.assert_called_once()


@pytest.mark.asyncio
async def test_resolver_caches_database_after_clean_no_match():
    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = None
    reader.search_database.return_value = _db_result()
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    resolver = _make_resolver(reader=reader)

    result = await resolver.resolve(make_raw())

    assert result.source is MetadataSource.DISCOGS_DATABASE
    assert len(resolver._album_cache) == 1


@pytest.mark.asyncio
async def test_resolver_caches_database_on_cooldown_after_proven_success():
    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = None
    reader.search_database.return_value = _db_result()
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.COOLDOWN_SKIPPED,
        cooldown_follows_successful_rebuild=True,
    )
    resolver = _make_resolver(reader=reader)

    result = await resolver.resolve(make_raw())

    assert result.source is MetadataSource.DISCOGS_DATABASE
    assert len(resolver._album_cache) == 1


@pytest.mark.asyncio
async def test_resolver_does_not_cache_database_on_cooldown_after_failed_refresh():
    reader = MagicMock()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection.return_value = None
    reader.search_database.return_value = _db_result()
    reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.COOLDOWN_SKIPPED,
        cooldown_follows_successful_rebuild=False,
    )
    resolver = _make_resolver(reader=reader)

    result = await resolver.resolve(make_raw())

    assert result.source is MetadataSource.DISCOGS_DATABASE
    assert len(resolver._album_cache) == 0
