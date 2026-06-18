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
    r.discogs = MagicMock()
    r.coverart = MagicMock()
    r.coverart.get_cover_art_url.return_value = "https://coverartarchive.org/x/front"
    r._album_cache = {}
    return r


@pytest.mark.asyncio
async def test_transient_collection_error_is_not_cached():
    r = make_resolver()
    r.discogs.search_collection.side_effect = ConnectionError("boom")  # couldn't determine
    r.discogs.search_database.return_value = None                      # genuine no-match

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK
    # Crucially: NOT cached, so the next track re-attempts the collection search.
    assert r._album_cache == {}


@pytest.mark.asyncio
async def test_clean_miss_is_cached():
    r = make_resolver()
    r.discogs.search_collection.return_value = None  # clean "not owned"
    r.discogs.search_database.return_value = None    # clean "no match"

    await r.resolve(make_raw())

    # Clean miss → fallback IS cached (discogs completed without error).
    assert len(r._album_cache) == 1
