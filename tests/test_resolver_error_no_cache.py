"""End-to-end check for B-4 / B-13 at the resolver layer.

A "couldn't determine" error during Discogs collection search must leave the
album UNcached, so the next track retries the lookup — instead of pinning the
album to a downgraded fallback result for the rest of the session.  A clean
"searched everywhere, no match" still caches the fallback (the existing,
desired behaviour).
"""
from unittest.mock import MagicMock

import pytest

from src.audio.recognizer import RawRecognitionResult
from src.metadata.models import MetadataSource
from src.metadata.resolver import MetadataResolver


def make_raw():
    return RawRecognitionResult(title="So What", artist="Miles Davis", album="Kind of Blue")


def make_resolver():
    r = MetadataResolver.__new__(MetadataResolver)  # bypass real client construction
    r.reader = MagicMock()
    r.coverart = MagicMock()
    r.coverart.get_cover_art_url.return_value = "https://coverartarchive.org/x/front"
    r._album_cache = {}
    return r


@pytest.mark.asyncio
async def test_transient_collection_error_is_not_cached():
    r = make_resolver()
    r.reader.search_collection.side_effect = ConnectionError("boom")  # couldn't determine
    r.reader.search_database.return_value = None                      # genuine no-match

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK
    # Crucially: NOT cached, so the next track re-attempts the collection search.
    assert r._album_cache == {}


@pytest.mark.asyncio
async def test_collection_error_then_database_hit_is_not_cached():
    """The subtle case: collection lookup ERRORS (couldn't determine), but the
    database search succeeds.  The DATABASE result is returned for this track
    but must NOT be cached — otherwise an album the user may own is pinned to
    no-Play-Count tracking for the whole session (B-4)."""
    r = make_resolver()
    r.reader.search_collection.side_effect = ConnectionError("blip")  # couldn't determine
    r.reader.search_database.return_value = {
        "release_id": 100, "instance_id": None, "album": "X",
    }

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.DISCOGS_DATABASE  # used for this track…
    assert r._album_cache == {}                              # …but NOT cached → retries next track


@pytest.mark.asyncio
async def test_clean_collection_miss_then_database_hit_is_cached():
    """Control: a CLEAN collection miss (not an error) followed by a database
    hit still caches the database result — the existing, desired behaviour."""
    r = make_resolver()
    r.reader.search_collection.return_value = None  # clean "not owned"
    r.reader.search_database.return_value = {
        "release_id": 100, "instance_id": None, "album": "X",
    }

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.DISCOGS_DATABASE
    assert len(r._album_cache) == 1


@pytest.mark.asyncio
async def test_transient_vs_unexpected_collection_errors_log_differently(caplog):
    """A-6: a transient (requests) error is an expected 'couldn't determine'
    (info); a non-transient error is an unexpected bug (warning).  Both still
    leave the album uncached."""
    import logging

    # Transient → info, "transient" in the message.
    r1 = make_resolver()
    r1.reader.search_collection.side_effect = ConnectionError("blip")
    r1.reader.search_database.return_value = None
    with caplog.at_level(logging.INFO):
        await r1.resolve(make_raw())
    transient_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("transient" in m for m in transient_msgs)
    assert r1._album_cache == {}

    caplog.clear()

    # Unexpected (a real bug) → warning, "Unexpected" in the message.
    r2 = make_resolver()
    r2.reader.search_collection.side_effect = ValueError("a real bug")
    r2.reader.search_database.return_value = None
    with caplog.at_level(logging.INFO):
        await r2.resolve(make_raw())
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Unexpected" in m for m in warn_msgs)
    assert r2._album_cache == {}          # unexpected also stays uncached


@pytest.mark.asyncio
async def test_clean_miss_is_cached():
    r = make_resolver()
    r.reader.search_collection.return_value = None  # clean "not owned"
    r.reader.search_database.return_value = None    # clean "no match"

    await r.resolve(make_raw())

    # Clean miss → fallback IS cached (discogs completed without error).
    assert len(r._album_cache) == 1
