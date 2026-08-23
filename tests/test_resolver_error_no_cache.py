"""End-to-end check for B-4 / B-13 at the resolver layer.

A "couldn't determine" error during Discogs collection search must leave the
album UNcached, so the next track retries the lookup — instead of pinning the
album to a downgraded fallback result for the rest of the session.  A clean
"searched everywhere, no match" still caches the fallback (the existing,
desired behaviour).
"""
from unittest.mock import MagicMock, AsyncMock

import pytest

from src.audio.recognizer import RawRecognitionResult
from src.metadata.models import MetadataSource
from src.metadata.discogs.outcomes import CollectionRefreshResult, CollectionRefreshState
from src.metadata.resolver import MetadataResolver, _ALBUM_CACHE_MAX
from src.util.cache import BoundedCache
from tests.factories import make_discogs_reader


def make_raw():
    return RawRecognitionResult(title="So What", artist="Miles Davis", album="Kind of Blue")


def make_resolver():
    r = MetadataResolver.__new__(MetadataResolver)  # bypass real client construction
    r.reader = MagicMock()
    # #61: resolver dispatches Discogs searches through reader.run(fn, …); the
    # mock awaits and calls the target, so a search_collection side_effect
    # (ConnectionError, etc.) still propagates exactly as run_in_executor did.
    r.reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    # #191 (C): default the staleness-refresh to "still not owned" so a clean
    # collection miss + database hit degrades to DATABASE as these tests expect.
    r.reader.refresh_index_and_research.return_value = CollectionRefreshResult(
        CollectionRefreshState.CLEAN_NO_MATCH
    )
    r.coverart = MagicMock()
    r.coverart.get_cover_art_url.return_value = "https://coverartarchive.org/x/front"
    r._album_cache = BoundedCache(_ALBUM_CACHE_MAX)
    r._logged_discogs_config = {}
    return r


@pytest.mark.asyncio
async def test_transient_collection_error_is_not_cached():
    r = make_resolver()
    r.reader.search_collection.side_effect = ConnectionError("boom")  # couldn't determine
    r.reader.search_database.return_value = None                      # genuine no-match

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK
    # Crucially: NOT cached, so the next track re-attempts the collection search.
    assert len(r._album_cache) == 0


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
    assert len(r._album_cache) == 0                              # …but NOT cached → retries next track


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
async def test_failed_refresh_then_cooldown_skip_keeps_database_uncached():
    """#420: a failed speculative rebuild leaves the next cooldown skip
    non-authoritative, so both displayed database results remain retryable."""
    reader = make_discogs_reader()
    reader.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    reader.search_collection = MagicMock(return_value=None)
    reader.search_database = MagicMock(return_value={
        "release_id": 100, "instance_id": None, "album": "Kind of Blue",
    })
    reader._build_collection_index = MagicMock(side_effect=ConnectionError("blip"))
    reader._http.request = MagicMock()

    r = make_resolver()
    r.reader = reader

    first = await r.resolve(make_raw())
    second = await r.resolve(make_raw())

    assert first.source is MetadataSource.DISCOGS_DATABASE
    assert second.source is MetadataSource.DISCOGS_DATABASE
    assert len(r._album_cache) == 0
    assert reader._http.request.call_count == 0


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
    assert len(r1._album_cache) == 0

    caplog.clear()

    # Unexpected (a real bug) → warning, "Unexpected" in the message.
    r2 = make_resolver()
    r2.reader.search_collection.side_effect = ValueError("a real bug")
    r2.reader.search_database.return_value = None
    with caplog.at_level(logging.INFO):
        await r2.resolve(make_raw())
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Unexpected" in m for m in warn_msgs)
    assert len(r2._album_cache) == 0          # unexpected also stays uncached


@pytest.mark.asyncio
async def test_clean_miss_is_cached():
    r = make_resolver()
    r.reader.search_collection.return_value = None  # clean "not owned"
    r.reader.search_database.return_value = None    # clean "no match"

    await r.resolve(make_raw())

    # Clean miss → fallback IS cached (discogs completed without error).
    assert len(r._album_cache) == 1


# ---------------------------------------------------------------------------
# #190 (err-4) — a transient MusicBrainz outage must not be cached as the
# album's FALLBACK payload; a clean "no art" still caches.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transient_coverart_outage_is_not_cached():
    """Both Discogs tiers cleanly miss, but MusicBrainz is DOWN (transient):
    the fallback must be returned for this track yet left UNcached, so the next
    track retries once the service recovers."""
    import musicbrainzngs

    r = make_resolver()
    r.reader.search_collection.return_value = None      # clean not-owned
    r.reader.search_database.return_value = None         # clean no-match
    r.coverart.get_cover_art_url.side_effect = musicbrainzngs.NetworkError("MB down")

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK
    assert result.cover_art_url is None
    assert len(r._album_cache) == 0          # NOT cached — transient outage


@pytest.mark.asyncio
async def test_clean_coverart_miss_is_still_cached():
    """Control: MusicBrainz answered, there simply is no art (None) — that
    negative result IS cached (load-bearing for MB rate limits)."""
    r = make_resolver()
    r.reader.search_collection.return_value = None
    r.reader.search_database.return_value = None
    r.coverart.get_cover_art_url.return_value = None     # clean "no art exists"

    result = await r.resolve(make_raw())

    assert result.source == MetadataSource.FALLBACK
    assert result.cover_art_url is None
    assert len(r._album_cache) == 1      # cached


@pytest.mark.asyncio
async def test_coverart_reraises_transient_from_its_outer_handler():
    """CoverArtFallback itself must propagate a transient (service-down) error
    rather than flattening it to None — the resolver relies on that to skip the
    cache."""
    import musicbrainzngs
    from unittest.mock import patch
    from src.metadata.coverart import CoverArtFallback

    cover = CoverArtFallback()
    with patch("src.metadata.coverart.musicbrainzngs.search_releases",
               side_effect=musicbrainzngs.NetworkError("MB down")):
        with pytest.raises(musicbrainzngs.NetworkError):
            cover.get_cover_art_url("Miles Davis", "Kind of Blue")


@pytest.mark.asyncio
async def test_coverart_still_returns_none_on_permanent_error():
    """A permanent MusicBrainz error (bad response / auth) still degrades to
    None — cover art is best-effort."""
    import musicbrainzngs
    from unittest.mock import patch
    from src.metadata.coverart import CoverArtFallback

    cover = CoverArtFallback()
    with patch("src.metadata.coverart.musicbrainzngs.search_releases",
               side_effect=musicbrainzngs.ResponseError("bad response")):
        assert cover.get_cover_art_url("Miles Davis", "Kind of Blue") is None


# ---------------------------------------------------------------------------
# #189 — a dead Discogs credential logs an ACTIONABLE, throttled error.
# ---------------------------------------------------------------------------

def _discogs_http_error(status):
    import discogs_client.exceptions
    return discogs_client.exceptions.HTTPError("denied", status)


@pytest.mark.asyncio
async def test_revoked_token_logs_actionable_error_not_transient(caplog):
    import logging
    r = make_resolver()
    r.reader.search_collection.side_effect = _discogs_http_error(401)
    r.reader.search_database.return_value = None
    r.coverart.get_cover_art_url.return_value = None
    with caplog.at_level(logging.INFO):
        await r.resolve(make_raw())
    errors = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR]
    assert any("user_token" in m for m in errors), errors
    # Not misfiled as a transient blip.
    assert not any("transient" in rec.getMessage()
                   for rec in caplog.records if rec.levelno == logging.INFO
                   and "collection" in rec.getMessage())
    assert len(r._album_cache) == 0          # still uncached/retryable (no downgrade pinned)


@pytest.mark.asyncio
async def test_wrong_username_404_names_the_username(caplog):
    import logging
    r = make_resolver()
    r.reader.search_collection.side_effect = _discogs_http_error(404)
    r.reader.search_database.return_value = None
    r.coverart.get_cover_art_url.return_value = None
    with caplog.at_level(logging.ERROR):
        await r.resolve(make_raw())
    assert any("username" in rec.getMessage() for rec in caplog.records
               if rec.levelno == logging.ERROR)


@pytest.mark.asyncio
async def test_credential_error_is_throttled_then_rearmed_on_success(caplog):
    import logging
    r = make_resolver()
    r.reader.search_database.return_value = None
    r.coverart.get_cover_art_url.return_value = None

    # Two consecutive revoked-token resolves → the actionable ERROR logs ONCE.
    r.reader.search_collection.side_effect = _discogs_http_error(401)
    with caplog.at_level(logging.ERROR):
        await r.resolve(make_raw())
        await r.resolve(make_raw())
    assert sum(1 for rec in caplog.records
               if rec.levelno == logging.ERROR and "user_token" in rec.getMessage()) == 1

    # A success re-arms the warning…
    r.reader.search_collection.side_effect = None
    r.reader.search_collection.return_value = {
        "release_id": 1, "instance_id": 2, "album": "X",
    }
    await r.resolve(RawRecognitionResult(title="t", artist="other", album="other"))

    # …so a later dead-credential resolve logs again.
    caplog.clear()
    r.reader.search_collection.side_effect = _discogs_http_error(401)
    r.reader.search_collection.return_value = None
    with caplog.at_level(logging.ERROR):
        await r.resolve(RawRecognitionResult(title="t2", artist="third", album="third"))
    assert any("user_token" in rec.getMessage() for rec in caplog.records
               if rec.levelno == logging.ERROR)


@pytest.mark.asyncio
async def test_distinct_config_faults_each_surface(caplog):
    """#188 cold-review defect 2: after one config fault is logged, a DIFFERENT
    one (e.g. token fixed → wrong-username 404) must still surface, not be
    swallowed by a stale single flag."""
    import logging
    r = make_resolver()
    r.reader.search_database.return_value = None
    r.coverart.get_cover_art_url.return_value = None

    r.reader.search_collection.side_effect = _discogs_http_error(401)
    with caplog.at_level(logging.ERROR):
        await r.resolve(make_raw())
    r.reader.search_collection.side_effect = _discogs_http_error(404)
    with caplog.at_level(logging.ERROR):
        await r.resolve(make_raw())

    msgs = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR]
    assert any("user_token" in m for m in msgs)     # the 401
    assert any("username" in m for m in msgs)        # the DISTINCT 404


@pytest.mark.asyncio
async def test_wrong_username_404_is_throttled_even_while_database_succeeds(caplog):
    """#188 cold-review D1: a wrong discogs.username fails the COLLECTION tier
    (404) every track while the DATABASE tier keeps succeeding. A throttle
    cleared by ANY tier's success would re-log the 404 every track — the exact
    spam the throttle exists to kill. Per-tier throttling logs it ONCE."""
    import logging
    r = make_resolver()
    r.reader.search_collection.side_effect = _discogs_http_error(404)
    r.reader.search_database.return_value = {
        "release_id": 1, "instance_id": None, "album": "X",
    }
    with caplog.at_level(logging.ERROR):
        for _ in range(4):
            await r.resolve(make_raw())
    n = sum(1 for rec in caplog.records
            if rec.levelno == logging.ERROR and "username" in rec.getMessage())
    assert n == 1, f"expected the 404 logged once across 4 tracks, got {n}"
