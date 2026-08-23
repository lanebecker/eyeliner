"""Unit tests for MetadataResolver — the 3-step fallback chain.

DiscogsClient and CoverArtFallback are injected as mocks so no network
access, Discogs account, or MusicBrainz lookup is needed.

Verifies:
  - Collection hit → DISCOGS_COLLECTION source, database not tried
  - Collection miss, database hit → DISCOGS_DATABASE source
  - Both miss → FALLBACK source with MusicBrainz cover
  - Exceptions in step 1 fall through to step 2
  - Exceptions in step 2 fall through to fallback
  - NotImplementedError (stub) falls through gracefully
  - All TrackMetadata fields are populated correctly from each source
"""
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock
import pytest

import src.metadata.resolver as resolver_mod
from src.audio.recognizer import RawRecognitionResult
from src.metadata.models import MetadataSource, TracklistEntry
from src.metadata.discogs.outcomes import (
    CollectionIdentity,
    CollectionRefreshResult,
    CollectionRefreshState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_raw(title="So What", artist="Miles Davis", album="Kind of Blue"):
    return RawRecognitionResult(title=title, artist=artist, album=album)


def make_discogs_result(release_id=100, instance_id=200):
    return {
        "album": "Kind of Blue",
        "year": "1959",
        "label": "Columbia",
        "catalog_number": "CS 8163",
        "release_id": release_id,
        "instance_id": instance_id,
        "cover_art_url": "https://img.discogs.com/cover.jpg",
        "tracklist": [
            TracklistEntry("A1", "So What"),
            TracklistEntry("A2", "Freddie Freeloader"),
            TracklistEntry("A3", "Blue in Green"),
            TracklistEntry("B1", "All Blues"),
            TracklistEntry("B2", "Flamenco Sketches"),
        ],
        # #211 (R4:test-3): the real _build_result ALWAYS emits a "genres" key;
        # this factory omitted it, so the resolver's genre pass-through was only
        # ever exercised against a shape its producer never produces. Shape parity.
        "genres": ["Jazz"],
    }


@pytest.fixture
def mock_discogs():
    m = MagicMock()
    m.search_collection.return_value = None
    m.search_database.return_value = None
    # #191 (C): a clean collection miss + database hit asks the reader to
    # refresh the index and re-check ownership. Default to "still not owned" so
    # these tests exercise the DATABASE/FALLBACK tiers as before; the C-upgrade
    # path has its own tests in test_cache_expiry.py.
    m.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    # #61: the resolver now dispatches Discogs searches through reader.run(fn, …)
    # (the dedicated-executor delegate) instead of loop.run_in_executor(None, …).
    # The mock's run awaits and simply calls the target, so return values /
    # call-assertions on search_collection / search_database behave as before.
    m.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    return m


@pytest.fixture
def mock_coverart():
    m = MagicMock()
    m.get_cover_art_url.return_value = "https://coverartarchive.org/release/abc/front"
    return m


@pytest.fixture
def resolver(mock_discogs, mock_coverart):
    """Build a MetadataResolver with injected mock clients."""
    # Import here to avoid triggering real client instantiation at module load
    from src.metadata.resolver import MetadataResolver
    from src.util.cache import BoundedCache
    from src.metadata.resolver import _ALBUM_CACHE_MAX
    r = MetadataResolver.__new__(MetadataResolver)
    r.reader = mock_discogs
    r.coverart = mock_coverart
    r._album_cache = BoundedCache(_ALBUM_CACHE_MAX)  # Normally created in __init__ (bypassed via __new__)
    r._reader_gate = asyncio.Lock()
    r._logged_discogs_config = {}
    return r


def _put_collection(resolver, key, result):
    resolver._album_cache.put(
        key, (MetadataSource.DISCOGS_COLLECTION, result, time.monotonic())
    )


@pytest.mark.asyncio
async def test_fallback_outside_reader_gate_cannot_clobber_recovered_collection_identity(
    resolver, mock_discogs, monkeypatch,
):
    """A delayed fallback must not replace a recovery's positive collection cache."""
    resolver._reader_gate = asyncio.Lock()
    cover_started, release_cover = asyncio.Event(), asyncio.Event()

    async def delayed_wait_for(awaitable, timeout):
        cover_started.set()
        await release_cover.wait()
        return await awaitable

    monkeypatch.setattr(resolver_mod.asyncio, "wait_for", delayed_wait_for)
    mock_discogs.rebuild_collection_and_research.return_value = make_discogs_result(100, 200)

    ordinary = asyncio.create_task(resolver.resolve(make_raw()))
    await cover_started.wait()
    recovered = await resolver.recover_collection_instance(
        ("miles davis", "kind of blue"), 100, 200, (300,)
    )
    assert recovered == CollectionIdentity(100, 300)
    assert resolver._album_cache.get(("miles davis", "kind of blue"))[1]["instance_id"] == 300

    release_cover.set()
    await ordinary

    cached = resolver._album_cache.get(("miles davis", "kind of blue"))
    assert cached[0] is MetadataSource.DISCOGS_COLLECTION
    assert cached[1]["instance_id"] == 300


@pytest.mark.asyncio
async def test_recovery_invalidates_exact_stale_positive_and_returns_same_release_new_instance(
    resolver, mock_discogs,
):
    """Using reader's collapsed instance would credit the wrong surviving copy."""
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))
    mock_discogs.rebuild_collection_and_research.return_value = make_discogs_result(100, 200)

    identity = await resolver.recover_collection_instance(key, 100, 200, (300,))

    assert identity == CollectionIdentity(100, 300)
    cached = resolver._album_cache.get(key)
    assert cached[0] is MetadataSource.DISCOGS_COLLECTION
    assert cached[1]["instance_id"] == 300


@pytest.mark.asyncio
async def test_recovery_allows_absent_cache_entry_after_lru_eviction(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    mock_discogs.rebuild_collection_and_research.return_value = make_discogs_result(100, 999)

    identity = await resolver.recover_collection_instance(key, 100, 200, (300,))

    assert identity == CollectionIdentity(100, 300)


@pytest.mark.asyncio
async def test_recovery_does_not_erase_newer_nonmatching_cache_entry(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    newer = make_discogs_result(101, 301)
    _put_collection(resolver, key, newer)

    assert await resolver.recover_collection_instance(key, 100, 200, (300,)) is None
    assert resolver._album_cache.get(key)[1] == newer
    mock_discogs.rebuild_collection_and_research.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_accepts_newer_cache_only_when_it_matches_proven_singleton(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    newer = make_discogs_result(100, 300)
    _put_collection(resolver, key, newer)

    identity = await resolver.recover_collection_instance(key, 100, 200, (300,))

    assert identity == CollectionIdentity(100, 300)
    mock_discogs.rebuild_collection_and_research.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [(200,), (), (300, 400)])
async def test_recovery_refuses_same_instance_or_non_singleton_evidence(resolver, mock_discogs, observed):
    """Missing/multiple/same evidence must evict only the known stale entry."""
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))

    assert await resolver.recover_collection_instance(key, 100, 200, observed) is None
    assert resolver._album_cache.get(key) is None
    mock_discogs.rebuild_collection_and_research.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_refuses_different_release(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))
    mock_discogs.rebuild_collection_and_research.return_value = make_discogs_result(101, 300)

    assert await resolver.recover_collection_instance(key, 100, 200, (300,)) is None
    assert resolver._album_cache.get(key) is None


@pytest.mark.asyncio
async def test_recovery_failure_leaves_known_stale_album_entry_invalidated(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))
    mock_discogs.rebuild_collection_and_research.side_effect = ConnectionError("offline")

    assert await resolver.recover_collection_instance(key, 100, 200, (300,)) is None
    assert resolver._album_cache.get(key) is None


@pytest.mark.asyncio
async def test_recovery_refusal_log_includes_safe_identity_stage(resolver, mock_discogs, caplog):
    caplog.set_level("INFO", logger="src.metadata.resolver")
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))

    assert await resolver.recover_collection_instance(key, 100, 200, (200,)) is None

    assert "stage=observed-evidence-refusal" in caplog.text
    assert "expected_release_id=100" in caplog.text
    assert "expected_instance_id=200" in caplog.text
    assert "observed_instance_ids=(200,)" in caplog.text


@pytest.mark.asyncio
async def test_recovery_rebuild_failure_log_redacts_exception_value(resolver, mock_discogs, caplog):
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))
    mock_discogs.rebuild_collection_and_research.side_effect = RuntimeError("secret response body")

    assert await resolver.recover_collection_instance(key, 100, 200, (300,)) is None

    assert "stage=rebuild-failed" in caplog.text
    assert "expected_release_id=100" in caplog.text
    assert "expected_instance_id=200" in caplog.text
    assert "observed_instance_id=300" in caplog.text
    assert "secret response body" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_release_id, expected_instance_id, observed_instance_ids, safe_fields, sentinel",
    [
        (100, "invalid-instance-sentinel", (300,), ("expected_release_id=100",), "invalid-instance-sentinel"),
        ("invalid-release-sentinel", 200, (300,), ("expected_instance_id=200",), "invalid-release-sentinel"),
        (100, 200, ("invalid-observed-sentinel",),
         ("expected_release_id=100", "expected_instance_id=200"), "invalid-observed-sentinel"),
    ],
)
async def test_invalid_recovery_evidence_log_keeps_only_independently_valid_ids(
    resolver, mock_discogs, caplog, expected_release_id, expected_instance_id,
    observed_instance_ids, safe_fields, sentinel,
):
    """Malformed evidence is diagnosable without emitting its raw values."""
    caplog.set_level("WARNING", logger="src.metadata.resolver")

    result = await resolver.recover_collection_instance(
        ("miles davis", "kind of blue"), expected_release_id,
        expected_instance_id, observed_instance_ids,
    )

    assert result is None
    assert "stage=invalid-evidence" in caplog.text
    for safe_field in safe_fields:
        assert safe_field in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_recovery_refuses_same_instance(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))

    assert await resolver.recover_collection_instance(key, 100, 200, (200,)) is None
    assert resolver._album_cache.get(key) is None


@pytest.mark.asyncio
async def test_recovery_refuses_zero_or_multiple_replacement_instances(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    for observed in ((), (300, 400)):
        _put_collection(resolver, key, make_discogs_result(100, 200))
        assert await resolver.recover_collection_instance(key, 100, 200, observed) is None
        assert resolver._album_cache.get(key) is None


@pytest.mark.asyncio
async def test_recovery_empty_enumeration_invalidates_stale_entry_and_refuses(resolver, mock_discogs):
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))

    assert await resolver.recover_collection_instance(key, 100, 200, ()) is None
    assert resolver._album_cache.get(key) is None


@pytest.mark.asyncio
async def test_duplicate_instances_leave_album_key_uncached(resolver, mock_discogs):
    """Invalid writer proof must not touch an immortal cache entry."""
    key = ("miles davis", "kind of blue")
    stale = make_discogs_result(100, 200)
    _put_collection(resolver, key, stale)

    assert await resolver.recover_collection_instance(key, 100, 200, (300, 300)) is None
    assert resolver._album_cache.get(key)[1] == stale


@pytest.mark.asyncio
async def test_recovery_during_failed_speculative_cooldown_does_not_change_cooldown_state(
    resolver, mock_discogs,
):
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result(100, 200))
    mock_discogs.rebuild_collection_and_research.return_value = make_discogs_result(100, 200)
    mock_discogs._last_index_refresh_at = 123.0
    mock_discogs._last_speculative_refresh_succeeded = False

    await resolver.recover_collection_instance(key, 100, 200, (300,))

    assert mock_discogs._last_index_refresh_at == 123.0
    assert mock_discogs._last_speculative_refresh_succeeded is False


@pytest.mark.asyncio
async def test_waiting_resolve_rechecks_cache_after_acquiring_reader_gate(resolver, mock_discogs):
    """Removing the post-lock check causes a second reader sequence."""
    # pytest-asyncio's synchronous fixture runs before this test's loop on
    # Python 3.9, so bind the contention lock in the active loop.
    resolver._reader_gate = asyncio.Lock()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def run(fn, *args):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return fn(*args)

    mock_discogs.run = AsyncMock(side_effect=run)
    mock_discogs.search_collection.return_value = make_discogs_result()
    first = asyncio.create_task(resolver.resolve(make_raw()))
    await started.wait()
    second = asyncio.create_task(resolver.resolve(make_raw(title="All Blues")))
    release.set()
    await asyncio.gather(first, second)

    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_cache_misses_serialize_reader_sequences(resolver, mock_discogs):
    """A removed gate lets the mutable reader sequence overlap."""
    resolver._reader_gate = asyncio.Lock()
    started, release = asyncio.Event(), asyncio.Event()
    in_reader = maximum = 0

    async def run(fn, *args):
        nonlocal in_reader, maximum
        in_reader += 1
        maximum = max(maximum, in_reader)
        started.set()
        await release.wait()
        try:
            return fn(*args)
        finally:
            in_reader -= 1

    mock_discogs.run = AsyncMock(side_effect=run)
    mock_discogs.search_collection.return_value = make_discogs_result()
    first = asyncio.create_task(resolver.resolve(make_raw(album="First")))
    await started.wait()
    second = asyncio.create_task(resolver.resolve(make_raw(album="Second")))
    await asyncio.sleep(0)
    assert maximum == 1
    release.set()
    await asyncio.gather(first, second)
    assert maximum == 1


@pytest.mark.asyncio
async def test_collection_cache_fast_path_does_not_enter_reader_gate(resolver, mock_discogs):
    """A positive cache hit must not wait behind unrelated recovery I/O."""
    resolver._reader_gate = asyncio.Lock()
    key = ("miles davis", "kind of blue")
    _put_collection(resolver, key, make_discogs_result())
    await resolver._reader_gate.acquire()
    try:
        result = await asyncio.wait_for(resolver.resolve(make_raw()), timeout=0.05)
    finally:
        resolver._reader_gate.release()
    assert result.source is MetadataSource.DISCOGS_COLLECTION
    mock_discogs.run.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_resolve_and_recovery_are_serialized_by_one_gate(resolver, mock_discogs):
    """Recovery and ordinary resolution share, rather than race through, reader.run."""
    resolver._reader_gate = asyncio.Lock()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def run(fn, *args):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return fn(*args)

    mock_discogs.run = AsyncMock(side_effect=run)
    mock_discogs.rebuild_collection_and_research.return_value = make_discogs_result(100, 200)
    mock_discogs.search_collection.return_value = make_discogs_result(101, 301)
    recovery = asyncio.create_task(
        resolver.recover_collection_instance(("miles davis", "kind of blue"), 100, 200, (300,))
    )
    await started.wait()
    ordinary = asyncio.create_task(resolver.resolve(make_raw(album="Other")))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    await asyncio.gather(recovery, ordinary)
    assert calls == 2


# ---------------------------------------------------------------------------
# #61 — both Discogs tiers are dispatched through reader.run (the dedicated
# executor delegate), never the shared default run_in_executor(None, …) pool.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discogs_searches_dispatch_through_the_dedicated_executor(resolver, mock_discogs):
    """Both the collection and the database search go through reader.run, in
    order. Reverting either call site to loop.run_in_executor(None, …) would stop
    calling reader.run and fail this. The fallback cover-art fetch deliberately
    stays on the default pool, so it must NOT show up in reader.run's calls."""
    mock_discogs.search_collection.return_value = None  # miss → database tier runs
    mock_discogs.search_database.return_value = None    # miss → fallback runs

    await resolver.resolve(make_raw())

    dispatched = [c.args[0] for c in mock_discogs.run.call_args_list]
    assert dispatched == [mock_discogs.search_collection, mock_discogs.search_database]
    # the two searches, and ONLY the two searches, were dispatched via reader.run
    assert mock_discogs.run.call_count == 2


# ---------------------------------------------------------------------------
# Step 1: Discogs collection hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collection_hit_returns_discogs_collection_source(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result()

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.DISCOGS_COLLECTION


@pytest.mark.asyncio
async def test_collection_hit_skips_database(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result()

    await resolver.resolve(make_raw())

    mock_discogs.search_database.assert_not_called()


@pytest.mark.asyncio
async def test_collection_hit_populates_all_metadata_fields(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result(
        release_id=100, instance_id=200
    )

    result = await resolver.resolve(make_raw(title="So What", artist="Miles Davis"))

    assert result.title == "So What"
    assert result.artist == "Miles Davis"
    assert result.album == "Kind of Blue"
    assert result.year == "1959"
    assert result.label == "Columbia"
    assert result.catalog_number == "CS 8163"
    assert result.discogs_release_id == 100
    assert result.discogs_instance_id == 200
    assert result.cover_art_url == "https://img.discogs.com/cover.jpg"
    assert len(result.tracklist) == 5


@pytest.mark.asyncio
async def test_collection_hit_tracklist_enables_last_track_detection(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result()

    result = await resolver.resolve(make_raw(title="Flamenco Sketches"))

    assert result.is_last_track is True


@pytest.mark.asyncio
async def test_collection_search_called_with_artist_and_album(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result()

    await resolver.resolve(make_raw(artist="Miles Davis", album="Kind of Blue"))

    mock_discogs.search_collection.assert_called_once_with("Miles Davis", "Kind of Blue")


# ---------------------------------------------------------------------------
# Step 2: Discogs database hit (collection miss)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collection_miss_falls_through_to_database(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = make_discogs_result(instance_id=None)

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.DISCOGS_DATABASE
    mock_discogs.search_database.assert_called_once()


@pytest.mark.asyncio
async def test_database_result_has_no_instance_id(resolver, mock_discogs):
    """Database results don't have an instance_id (not owned by the user)."""
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = make_discogs_result(
        release_id=100, instance_id=None
    )

    result = await resolver.resolve(make_raw())

    assert result.discogs_instance_id is None
    assert result.discogs_release_id == 100


@pytest.mark.asyncio
async def test_database_hit_populates_enriched_fields(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = make_discogs_result(instance_id=None)

    result = await resolver.resolve(make_raw())

    assert result.year == "1959"
    assert result.label == "Columbia"
    assert result.catalog_number == "CS 8163"


# ---------------------------------------------------------------------------
# Step 3: Fallback (both Discogs steps return None)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_discogs_miss_returns_fallback_source(resolver, mock_discogs, mock_coverart):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK


@pytest.mark.asyncio
async def test_fallback_cover_art_fetched_from_musicbrainz(resolver, mock_discogs, mock_coverart):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None
    mock_coverart.get_cover_art_url.return_value = "https://musicbrainz.org/img/cover.jpg"

    result = await resolver.resolve(make_raw())

    mock_coverart.get_cover_art_url.assert_called_once_with("Miles Davis", "Kind of Blue")
    assert result.cover_art_url == "https://musicbrainz.org/img/cover.jpg"


@pytest.mark.asyncio
async def test_fallback_uses_shazam_title_artist_album(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None

    raw = make_raw(title="My Track", artist="My Artist", album="My Album")
    result = await resolver.resolve(raw)

    assert result.title == "My Track"
    assert result.artist == "My Artist"
    assert result.album == "My Album"


@pytest.mark.asyncio
async def test_fallback_has_no_discogs_ids(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None

    result = await resolver.resolve(make_raw())

    assert result.discogs_release_id is None
    assert result.discogs_instance_id is None


@pytest.mark.asyncio
async def test_fallback_has_empty_tracklist(resolver, mock_discogs):
    """Fallback metadata has no tracklist — last-track detection won't fire."""
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None

    result = await resolver.resolve(make_raw())

    assert result.tracklist == []
    assert result.is_last_track is False


# ---------------------------------------------------------------------------
# Exception handling — graceful fallthrough
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collection_exception_falls_through_to_database(resolver, mock_discogs):
    mock_discogs.search_collection.side_effect = Exception("Discogs network timeout")
    mock_discogs.search_database.return_value = make_discogs_result(instance_id=None)

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.DISCOGS_DATABASE


@pytest.mark.asyncio
async def test_database_exception_falls_through_to_fallback(resolver, mock_discogs, mock_coverart):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.side_effect = Exception("Rate limited")

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK


@pytest.mark.asyncio
async def test_both_exceptions_fall_through_to_fallback(resolver, mock_discogs, mock_coverart):
    mock_discogs.search_collection.side_effect = Exception("Error 1")
    mock_discogs.search_database.side_effect = Exception("Error 2")

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK


@pytest.mark.asyncio
async def test_not_implemented_error_falls_through_gracefully(resolver, mock_discogs):
    """NotImplementedError is treated as 'stub not yet built' — fall through silently."""
    mock_discogs.search_collection.side_effect = NotImplementedError
    mock_discogs.search_database.return_value = make_discogs_result(instance_id=None)

    result = await resolver.resolve(make_raw())

    assert result.source == MetadataSource.DISCOGS_DATABASE


# ---------------------------------------------------------------------------
# resolve() always returns a TrackMetadata (never raises, never returns None)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_always_returns_track_metadata(resolver, mock_discogs, mock_coverart):
    """Even with both Discogs steps failing, resolve() returns a valid FALLBACK TrackMetadata."""
    from src.metadata.models import TrackMetadata
    mock_discogs.search_collection.side_effect = Exception("boom")
    mock_discogs.search_database.side_effect = Exception("boom")
    # Cover art fallback succeeds (returns None when nothing found — normal behaviour)
    mock_coverart.get_cover_art_url.return_value = None

    result = await resolver.resolve(make_raw())

    assert isinstance(result, TrackMetadata)
    assert result.source == MetadataSource.FALLBACK


@pytest.mark.asyncio
async def test_title_from_raw_is_preserved_through_all_paths(resolver, mock_discogs):
    """The raw Shazam title is always present in the final result, whatever path was taken."""
    # Collection path
    mock_discogs.search_collection.return_value = make_discogs_result()
    result = await resolver.resolve(make_raw(title="So What"))
    assert result.title == "So What"

    # Fallback path
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None
    result = await resolver.resolve(make_raw(title="So What"))
    assert result.title == "So What"


# ---------------------------------------------------------------------------
# v1.2.0: genres passthrough
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_genres_passed_through_from_discogs_result(resolver, mock_discogs):
    """Genres from the Discogs result dict are stored on TrackMetadata."""
    result_with_genres = make_discogs_result()
    result_with_genres["genres"] = ["Post-Hardcore", "Punk"]
    mock_discogs.search_collection.return_value = result_with_genres

    result = await resolver.resolve(make_raw())
    assert result.genres == ["Post-Hardcore", "Punk"]


@pytest.mark.asyncio
async def test_genres_default_empty_when_missing_from_result(resolver, mock_discogs):
    """If a Discogs result has no genres key, TrackMetadata.genres is []."""
    # The factory now carries genres for shape parity with the real
    # _build_result (#211), so construct the missing-key case explicitly here.
    result_without_genres = make_discogs_result()
    del result_without_genres["genres"]
    mock_discogs.search_collection.return_value = result_without_genres

    result = await resolver.resolve(make_raw())
    assert result.genres == []


@pytest.mark.asyncio
async def test_genres_empty_on_fallback_path(resolver, mock_discogs):
    """Fallback metadata (no Discogs) always has empty genres."""
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = None

    result = await resolver.resolve(make_raw())
    assert result.genres == []


# ---------------------------------------------------------------------------
# Album-level result cache (v1.3.3)
#
# A full Discogs lookup can cost 30+ HTTP requests, and every track on an
# album shares the same (artist, album) pair — so resolve() caches per
# normalized key. These tests pin the caching contract.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_track_same_album_hits_cache_not_discogs(resolver, mock_discogs):
    """Track 2 of an album must not repeat the Discogs lookup."""
    mock_discogs.search_collection.return_value = make_discogs_result()

    first = await resolver.resolve(make_raw(title="So What"))
    second = await resolver.resolve(make_raw(title="Freddie Freeloader"))

    mock_discogs.search_collection.assert_called_once()
    assert second.source == MetadataSource.DISCOGS_COLLECTION
    assert second.title == "Freddie Freeloader"      # Per-track field preserved
    assert second.album == first.album                # Album-level fields shared
    assert second.discogs_release_id == first.discogs_release_id


@pytest.mark.asyncio
async def test_cache_key_normalizes_case_and_whitespace(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result()

    await resolver.resolve(make_raw(artist="Miles Davis", album="Kind of Blue"))
    await resolver.resolve(make_raw(artist="  MILES DAVIS ", album="kind of blue  "))

    mock_discogs.search_collection.assert_called_once()


@pytest.mark.asyncio
async def test_database_results_are_cached_too(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = None
    mock_discogs.search_database.return_value = make_discogs_result(instance_id=None)

    await resolver.resolve(make_raw(title="So What"))
    second = await resolver.resolve(make_raw(title="All Blues"))

    mock_discogs.search_database.assert_called_once()
    assert second.source == MetadataSource.DISCOGS_DATABASE


@pytest.mark.asyncio
async def test_fallback_cached_when_discogs_lookups_complete_cleanly(
    resolver, mock_discogs, mock_coverart
):
    """Both tiers returning None (genuinely not found) caches the fallback."""
    await resolver.resolve(make_raw(title="So What"))
    await resolver.resolve(make_raw(title="All Blues"))

    mock_discogs.search_collection.assert_called_once()
    mock_coverart.get_cover_art_url.assert_called_once()


@pytest.mark.asyncio
async def test_fallback_not_cached_after_discogs_exception(
    resolver, mock_discogs, mock_coverart
):
    """A network blip must NOT pin the album to fallback metadata forever."""
    mock_discogs.search_collection.side_effect = ConnectionError("flaky wifi")

    await resolver.resolve(make_raw(title="So What"))
    # Discogs recovers; the next track must retry the real lookup
    mock_discogs.search_collection.side_effect = None
    mock_discogs.search_collection.return_value = make_discogs_result()

    second = await resolver.resolve(make_raw(title="All Blues"))

    assert second.source == MetadataSource.DISCOGS_COLLECTION
    assert mock_discogs.search_collection.call_count == 2


@pytest.mark.asyncio
async def test_different_albums_resolve_independently(resolver, mock_discogs):
    mock_discogs.search_collection.return_value = make_discogs_result()

    await resolver.resolve(make_raw(album="Kind of Blue"))
    await resolver.resolve(make_raw(album="Sketches of Spain"))

    assert mock_discogs.search_collection.call_count == 2


@pytest.mark.asyncio
async def test_cache_is_bounded(resolver, mock_discogs):
    from src.metadata.resolver import _ALBUM_CACHE_MAX
    mock_discogs.search_collection.return_value = make_discogs_result()

    for i in range(_ALBUM_CACHE_MAX + 10):
        await resolver.resolve(make_raw(album=f"Album {i}"))

    assert len(resolver._album_cache) == _ALBUM_CACHE_MAX


def test_coverart_can_be_injected():
    """ARCH-8: an injected CoverArtFallback is used verbatim."""
    from src.metadata.resolver import MetadataResolver
    sentinel = MagicMock(name="fake-coverart")
    resolver = MetadataResolver(MagicMock(), coverart=sentinel)
    assert resolver.coverart is sentinel
