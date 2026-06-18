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
from src.metadata.discogs_client import DiscogsClient
from src.metadata.coverart import CoverArtFallback
from src.metadata.errors import is_transient

if TYPE_CHECKING:
    from src.audio.recognizer import RawRecognitionResult
    from src.config import DiscogsConfig

log = logging.getLogger(__name__)

# Cap on the per-(artist, album) result cache.  64 albums is far more than a
# single listening session will ever touch; eviction exists purely to bound
# memory on very long uptimes.
_ALBUM_CACHE_MAX = 64


class MetadataResolver:
    """Resolves a RawRecognitionResult into a full TrackMetadata."""

    def __init__(self, config: "DiscogsConfig"):
        self.discogs = DiscogsClient(config)
        self.coverart = CoverArtFallback()
        # (artist_lower, album_lower) → (MetadataSource, payload)
        #   payload is the Discogs result dict for Discogs tiers,
        #   or the cover art URL (Optional[str]) for FALLBACK.
        self._album_cache: dict = {}

    @staticmethod
    def _cache_key(raw: "RawRecognitionResult") -> tuple:
        """Normalize (artist, album) the same way RecognitionLoop compares tracks."""
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
            )
        return self._from_discogs(raw, payload, source)

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
            result = await loop.run_in_executor(
                None, self.discogs.search_collection, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs collection: {raw.artist} / {raw.album}")
                self._cache_store(key, MetadataSource.DISCOGS_COLLECTION, result)
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_COLLECTION)
        except Exception as e:
            discogs_completed = False
            if is_transient(e):
                log.info(f"Discogs collection search couldn't determine (transient): {e}")
            else:
                log.warning(f"Unexpected error in Discogs collection search: {e}")

        # Step 2: Discogs database
        try:
            result = await loop.run_in_executor(
                None, self.discogs.search_database, raw.artist, raw.album
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
                if discogs_completed:
                    self._cache_store(key, MetadataSource.DISCOGS_DATABASE, result)
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_DATABASE)
        except Exception as e:
            discogs_completed = False
            if is_transient(e):
                log.info(f"Discogs database search couldn't determine (transient): {e}")
            else:
                log.warning(f"Unexpected error in Discogs database search: {e}")

        # Step 3: Fallback — Shazam data + MusicBrainz cover art
        log.info(f"Using fallback metadata for: {raw.artist} / {raw.album}")
        cover_url = await loop.run_in_executor(
            None, self.coverart.get_cover_art_url, raw.artist, raw.album
        )
        if discogs_completed:
            self._cache_store(key, MetadataSource.FALLBACK, cover_url)
        return TrackMetadata(
            title=raw.title,
            artist=raw.artist,
            album=raw.album,
            cover_art_url=cover_url,
            source=MetadataSource.FALLBACK,
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
