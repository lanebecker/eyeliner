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
from unittest.mock import MagicMock
import pytest

from src.audio.recognizer import RawRecognitionResult
from src.metadata.models import MetadataSource, TracklistEntry


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
    }


@pytest.fixture
def mock_discogs():
    m = MagicMock()
    m.search_collection.return_value = None
    m.search_database.return_value = None
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
    r = MetadataResolver.__new__(MetadataResolver)
    r.discogs = mock_discogs
    r.coverart = mock_coverart
    r._album_cache = {}  # Normally created in __init__ (bypassed via __new__)
    return r


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
    """If Discogs result has no genres key, TrackMetadata.genres is []."""
    mock_discogs.search_collection.return_value = make_discogs_result()  # no genres key

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
