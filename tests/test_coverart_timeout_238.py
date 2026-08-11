"""R5-08 (#238) — the MusicBrainz cover-art await must be bounded.

The MB call runs on the shared default executor and musicbrainzngs sets no socket
timeout, so a stalled socket froze resolve() — and, because resolves serialize
through TrackCommitService, the whole commit pipeline — until restart. The fix:
a socket-timeout FLOOR in coverart (so the abandoned thread dies) plus an
asyncio.wait_for at the resolver await (so the pipeline unblocks immediately). A
timeout is treated as transient — returned for this track, NOT cached — so the
next track retries.
"""
import asyncio
import socket
import threading

import pytest
from unittest.mock import AsyncMock, MagicMock

import src.metadata.resolver as resolver_mod
from src.metadata.resolver import MetadataResolver, _ALBUM_CACHE_MAX
from src.metadata.models import MetadataSource
from src.util.cache import BoundedCache
from src.audio.recognizer import RawRecognitionResult


def _resolver(cover_fn):
    r = MetadataResolver.__new__(MetadataResolver)
    r.reader = MagicMock()
    r.reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    r.reader.refresh_index_and_research.return_value = None
    r.reader.search_collection.return_value = None
    r.reader.search_database.return_value = None
    r.coverart = MagicMock()
    r.coverart.get_cover_art_url = cover_fn
    r._album_cache = BoundedCache(_ALBUM_CACHE_MAX)
    r._logged_discogs_config = {}
    return r


def test_coverart_module_sets_a_socket_timeout_floor():
    """Importing coverart bounds any socket created without an explicit timeout
    (the MB urllib path, pylast) — it must not leave the process default at None."""
    import src.metadata.coverart  # noqa: F401
    assert socket.getdefaulttimeout() is not None
    assert socket.getdefaulttimeout() > 0


@pytest.mark.asyncio
async def test_stalled_cover_art_does_not_freeze_resolve(monkeypatch):
    monkeypatch.setattr(resolver_mod, "_COVER_ART_TIMEOUT_SECONDS", 1)
    gate = threading.Event()
    try:
        r = _resolver(lambda a, b: gate.wait(30))
        raw = RawRecognitionResult(title="X", artist="A", album="B")
        # Must return well within the stall (the 1s ceiling), not hang.
        result = await asyncio.wait_for(r.resolve(raw), timeout=5.0)
        assert result.source == MetadataSource.FALLBACK
        # Transient: NOT cached, so the next track retries the cover fetch.
        assert len(r._album_cache) == 0
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_fast_cover_art_still_caches_the_clean_result(monkeypatch):
    """A prompt lookup is unaffected: a clean 'no art' (None) still caches (the
    negative result is load-bearing for MB rate limits)."""
    monkeypatch.setattr(resolver_mod, "_COVER_ART_TIMEOUT_SECONDS", 5)
    r = _resolver(lambda a, b: None)   # returns immediately, no art
    raw = RawRecognitionResult(title="X", artist="A", album="B")
    result = await r.resolve(raw)
    assert result.source == MetadataSource.FALLBACK
    assert len(r._album_cache) == 1   # clean result cached
