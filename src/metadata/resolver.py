"""MetadataResolver — orchestrates the 3-step lookup chain.

Lookup order:
  1. User's Discogs collection (best: your specific pressing)
  2. Discogs database         (good: generic release metadata)
  3. Fallback                 (Shazam raw + MusicBrainz cover art)

All consumers (display, tracker) receive a TrackMetadata regardless of source.
The `source` field indicates which tier succeeded.

Album-level caching (v1.3.3)
----------------------------
A single resolve() against Discogs can cost 30+ HTTP requests (database search,
up to 25 collection-membership checks, release + tracklist fetches), and every
track on an album shares the same (artist, album) pair.  Without a cache, a
10-track LP repeats the identical lookup 10 times and flirts with Discogs'
60 requests/minute rate limit.

resolve() therefore caches per normalized (artist, album) key:
  - Discogs hits cache the result dict + source tier.
  - Fallback results cache the cover art URL — but ONLY when both Discogs
    tiers completed without raising.  A network blip should not pin an album
    to fallback metadata for the rest of the session.
  - The cache is bounded (insertion-order eviction, LRU-refresh on hit) and
    deliberately has no TTL: collection metadata is effectively static within
    a single listening session, and the process restarts daily in practice.

Note: an empty Shazam album string ("") keys all of an artist's unknown-album
tracks together.  Those tracks would resolve identically anyway, so the
collision is harmless and saves further duplicate lookups.

Concurrency: resolve() is only ever awaited sequentially from
TrackCommitService.commit on the single event loop, so the cache needs
no locking.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from src.metadata.models import TrackMetadata, MetadataSource
from src.metadata.coverart import CoverArtFallback
from src.metadata.errors import is_transient, http_status

if TYPE_CHECKING:
    from src.audio.recognizer import RawRecognitionResult
    from src.metadata.discogs.reader import DiscogsReader

log = logging.getLogger(__name__)

# Cap on the per-(artist, album) result cache.  64 albums is far more than a
# single listening session will ever touch; eviction exists purely to bound
# memory on very long uptimes.
_ALBUM_CACHE_MAX = 64


class MetadataResolver:
    """Resolves a RawRecognitionResult into a full TrackMetadata."""

    def __init__(self, reader: "DiscogsReader", coverart=None):
        # A-4: the resolver depends only on the read half of Discogs, injected
        # at the composition root (main.py) — it no longer owns a God client the
        # tracker has to reach into (the old A-3 `resolver.discogs` back-channel).
        self.reader = reader
        # ARCH-8: optional injection seam — defaults to the real CoverArtFallback,
        # but a test can pass a substitute instead of overwriting the attribute.
        self.coverart = coverart if coverart is not None else CoverArtFallback()
        # (artist_lower, album_lower) → (MetadataSource, payload)
        #   payload is the Discogs result dict for Discogs tiers,
        #   or the cover art URL (Optional[str]) for FALLBACK.
        self._album_cache: dict = {}
        # #189: a dead Discogs credential (401/403) or wrong username (404) is a
        # PERMANENT error that recurs on EVERY track's resolve — so its
        # actionable warning is logged once and then suppressed until a Discogs
        # lookup next succeeds, rather than spamming the journal per track (a
        # clockless throttle, cf. #178's per-interval capture-log throttle).
        # Keyed PER TIER by the last-logged permanent status ({tier: status}),
        # for two reasons (#188 cold review, defects 2 + D1): a DIFFERENT fault
        # on a tier (token fixed → wrong-username 404) still surfaces; and a
        # success on ONE tier does not re-arm the OTHER's warning — a wrong
        # username fails only the collection tier while the database tier keeps
        # succeeding every track, so a single shared flag cleared by either
        # success would re-log the collection 404 on every track (the exact
        # per-track spam this throttle exists to kill).
        self._logged_discogs_config: dict = {}

    @staticmethod
    def _cache_key(raw: "RawRecognitionResult") -> tuple:
        """Normalize (artist, album) for the album cache (strip + lower).

        NOTE: intentionally simpler than both RecognitionLoop's dedup
        normalizer (casefold + whitespace collapse) and the #179/#180
        matching folds — the key is only ever compared against keys from
        this same function (cache lookups, and #184's tier-upgrade check via
        ``TrackMetadata.resolve_key``), so self-consistency is all that is
        required.  String variance across chunks yields different keys and
        fails safe (cache miss / conservative split).
        """
        return (raw.artist.strip().lower(), raw.album.strip().lower())

    def _cache_get(self, key: tuple):
        """Return the cached entry for key (refreshing its LRU position), or None."""
        entry = self._album_cache.get(key)
        if entry is not None:
            # LRU-ish refresh: pop and re-insert so this entry isn't first to evict.
            self._album_cache.pop(key)
            self._album_cache[key] = entry
        return entry

    def _cache_store(self, key: tuple, source: MetadataSource, payload):
        """Insert an entry, evicting oldest entries beyond _ALBUM_CACHE_MAX."""
        self._album_cache[key] = (source, payload)
        while len(self._album_cache) > _ALBUM_CACHE_MAX:
            # dict preserves insertion order — iter(...) yields oldest first
            self._album_cache.pop(next(iter(self._album_cache)))

    def _from_cache(self, raw: "RawRecognitionResult", entry: tuple) -> TrackMetadata:
        """Rebuild a per-track TrackMetadata from a cached album-level entry."""
        source, payload = entry
        if source is MetadataSource.FALLBACK:
            return TrackMetadata(
                title=raw.title,
                artist=raw.artist,
                album=raw.album,
                cover_art_url=payload,
                source=source,
                resolve_key=self._cache_key(raw),
            )
        return self._from_discogs(raw, payload, source)

    def _log_discogs_error(self, tier: str, exc: Exception) -> None:
        """Log a Discogs-tier failure at the right level (#189).

        Transient (429/5xx/network blip) → INFO "(transient)", a routine
        couldn't-determine.  A definitive credential/config error (401/403 dead
        token, 404 wrong username) → an ACTIONABLE ERROR naming the fix, logged
        ONCE and then throttled (it recurs every track until the operator
        intervenes; a success re-arms it).  Anything else → WARNING "Unexpected".
        """
        if is_transient(exc):
            log.info(f"Discogs {tier} search couldn't determine (transient): {exc}")
            return
        status = http_status(exc)
        if status in (401, 403, 404):
            if self._logged_discogs_config.get(tier) != status:
                self._logged_discogs_config[tier] = status
                # A 404 on the collection tier points at the username; a
                # database-tier 404 (rare — build 404s are swallowed) does not.
                if status in (401, 403):
                    hint = "check your Discogs user_token"
                elif tier == "collection":
                    hint = "check your discogs.username"
                else:
                    hint = "the requested Discogs resource was not found"
                log.error(
                    "Discogs %s search rejected (HTTP %s) — %s. Play Count / "
                    "Last Played updates are disabled until this is fixed; "
                    "further identical occurrences suppressed until the %s "
                    "lookup succeeds.",
                    tier, status, hint, tier,
                )
            return
        log.warning(f"Unexpected error in Discogs {tier} search: {exc}")

    async def resolve(self, raw: "RawRecognitionResult") -> TrackMetadata:
        """Run the full lookup chain. Always returns a TrackMetadata."""
        loop = asyncio.get_running_loop()

        # Step 0: album-level cache — same album as a previous track?
        key = self._cache_key(raw)
        cached = self._cache_get(key)
        if cached is not None:
            log.debug(f"Album cache hit for: {raw.artist} / {raw.album}")
            return self._from_cache(raw, cached)

        # Tracks whether both Discogs tiers ran to completion.  Only a clean
        # "looked everywhere, found nothing" outcome may cache the fallback —
        # a raised exception (network blip, 429) must stay retryable.
        discogs_completed = True

        # Step 1: User's Discogs collection.  This run_in_executor call is a
        # true error boundary (A-6): a transient failure is expected and leaves
        # the album uncached/retryable (B-4); anything else is an unexpected bug
        # and is logged loudly so it isn't mistaken for a routine miss.
        try:
            result = await self.reader.run(
                self.reader.search_collection, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs collection: {raw.artist} / {raw.album}")
                self._logged_discogs_config.pop("collection", None)   # #189: re-arm THIS tier
                self._cache_store(key, MetadataSource.DISCOGS_COLLECTION, result)
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_COLLECTION)
        except Exception as e:
            discogs_completed = False
            self._log_discogs_error("collection", e)

        # Step 2: Discogs database
        try:
            result = await self.reader.run(
                self.reader.search_database, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs database: {raw.artist} / {raw.album}")
                # Only cache the database result if the collection tier above
                # completed cleanly.  If the collection lookup ERRORED (a
                # transient blip — "couldn't determine ownership"), caching this
                # DATABASE downgrade would pin an album the user may actually own
                # to no-Play-Count tracking for the rest of the session (B-4).
                # Return it for this track, but leave it uncached so the next
                # track retries the collection lookup.
                self._logged_discogs_config.pop("database", None)   # #189: re-arm THIS tier
                if discogs_completed:
                    self._cache_store(key, MetadataSource.DISCOGS_DATABASE, result)
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_DATABASE)
        except Exception as e:
            discogs_completed = False
            self._log_discogs_error("database", e)

        # Step 3: Fallback — Shazam data + MusicBrainz cover art
        log.info(f"Using fallback metadata for: {raw.artist} / {raw.album}")
        # #190: a TRANSIENT MusicBrainz outage must not be cached as this
        # album's FALLBACK payload — otherwise the album is pinned coverless
        # for the whole session even after the service recovers.  Mirror the
        # discogs_completed pattern: on a transient cover-art failure, return
        # the fallback for THIS track but skip the cache so the next track
        # retries.  A clean "no art exists" (None) still caches — that
        # negative result is load-bearing for MusicBrainz rate limits.
        cover_completed = True
        try:
            cover_url = await loop.run_in_executor(
                None, self.coverart.get_cover_art_url, raw.artist, raw.album
            )
        except Exception as e:
            cover_url = None
            if is_transient(e):
                cover_completed = False
                log.info(f"Cover art couldn't determine (transient): {e}")
            else:
                log.warning(f"Unexpected error in cover art lookup: {e}")
        if discogs_completed and cover_completed:
            self._cache_store(key, MetadataSource.FALLBACK, cover_url)
        return TrackMetadata(
            title=raw.title,
            artist=raw.artist,
            album=raw.album,
            cover_art_url=cover_url,
            source=MetadataSource.FALLBACK,
            resolve_key=key,
        )

    def _from_discogs(
        self,
        raw: "RawRecognitionResult",
        discogs_result: dict,
        source: MetadataSource,
    ) -> TrackMetadata:
        """Build a TrackMetadata from a Discogs search result dict."""
        return TrackMetadata(
            title=raw.title,
            artist=raw.artist,
            resolve_key=self._cache_key(raw),
            album=discogs_result.get("album", raw.album),
            year=discogs_result.get("year"),
            label=discogs_result.get("label"),
            catalog_number=discogs_result.get("catalog_number"),
            discogs_release_id=discogs_result.get("release_id"),
            discogs_instance_id=discogs_result.get("instance_id"),
            cover_art_url=discogs_result.get("cover_art_url"),
            # Shallow-copy the cached tracklist so each track of an album gets
            # its own list object — a defensive .sort()/append on one track's
            # tracklist can't corrupt its siblings'.  `or []` normalizes an
            # explicit None to an empty list so the tracklist properties never
            # see None (B-9).
            tracklist=list(discogs_result.get("tracklist") or []),
            genres=list(discogs_result.get("genres") or []),
            source=source,
        )
