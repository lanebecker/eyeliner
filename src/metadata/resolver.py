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
  - The cache is bounded (insertion-order eviction, LRU-refresh on hit).
    COLLECTION hits are correct and cached without a TTL. DATABASE/FALLBACK
    entries are DOWNGRADES ("not owned right now"), so they carry a monotonic
    TTL (_DOWNGRADE_TTL_SECONDS, #191): on a 24/7 appliance a record played
    before it was added to Discogs would otherwise pin its downgrade forever,
    with Step 0 short-circuiting every replay before it could re-resolve.

Note: an empty Shazam album string ("") keys all of an artist's unknown-album
tracks together.  Those tracks would resolve identically anyway, so the
collision is harmless and saves further duplicate lookups.

Concurrency: ordinary resolution and finalizer recovery can both enter the
resolver.  The resolver-owned reader gate serializes each complete mutable
reader sequence, including a submitted executor worker after its awaiting
coroutine is cancelled.  Album-cache hits remain a lock-free fast path, and
cover-art I/O remains outside the gate.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from src.metadata.models import TrackMetadata, MetadataSource
from src.metadata.coverart import CoverArtFallback
from src.metadata.discogs.outcomes import CollectionIdentity, CollectionRefreshState
from src.metadata.errors import is_transient, http_status
from src.util.cache import BoundedCache

if TYPE_CHECKING:
    from src.audio.recognizer import RawRecognitionResult
    from src.metadata.discogs.reader import DiscogsReader

log = logging.getLogger(__name__)

# Cap on the per-(artist, album) result cache.  64 albums is far more than a
# single listening session will ever touch; eviction exists purely to bound
# memory on very long uptimes.
_ALBUM_CACHE_MAX = 64

# #191 (stab-2): DATABASE/FALLBACK entries are DOWNGRADES — they mean "not found
# in the collection right now" and carry no instance_id, so nothing is credited.
# On a 24/7 appliance a record played before it was added to Discogs would pin
# that downgrade forever (Step 0 short-circuits every replay), so these entries
# get a monotonic-clock TTL: once elapsed the album re-resolves, giving the
# refreshed index (B1) / staleness refresh (C) a chance to upgrade it to
# COLLECTION. A COLLECTION hit is correct and is cached WITHOUT a TTL — it never
# expires. monotonic for the same reason as the index TTL (reboot-safe).
_DOWNGRADE_TTL_SECONDS = 3600

# R5-08: hard ceiling on the MusicBrainz cover-art await.  The call runs on the
# shared default executor and MusicBrainz sets no socket timeout, so without this
# a stalled socket froze resolve() — and, because resolves serialize through
# TrackCommitService, the whole commit pipeline — until restart.  asyncio.wait_for
# ABANDONS (does not kill) the executor thread on timeout, so the pipeline
# continues immediately; the coverart module's socket-timeout floor then lets that
# abandoned thread die instead of leaking a pool worker.  Set above the socket
# floor (15s) so the socket-level error, when it fires, is the one that surfaces
# (as a transient), keeping the retry/caching semantics below; wait_for is the
# backstop for a socket that evades the floor entirely.
_COVER_ART_TIMEOUT_SECONDS = 20


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
        # (artist_lower, album_lower) → (MetadataSource, payload, stored_at)
        #   payload is the Discogs result dict for Discogs tiers,
        #   or the cover art URL (Optional[str]) for FALLBACK.
        # arch-4/#220: the ONE bounded-LRU implementation (src/util/cache.py),
        # shared with the renderer's six caches rather than re-hand-rolled here.
        # The #191 downgrade TTL lives on top of it in _cache_get/_cache_store.
        self._album_cache = BoundedCache(_ALBUM_CACHE_MAX)
        # Reader methods mutate the collection index and the one-entry database
        # memo.  Both ordinary resolves and stale-instance recovery share this
        # gate; the executor is a transport boundary, not a state lock.
        self._reader_gate = asyncio.Lock()
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
        """Return the cached ``(source, payload)`` for key (refreshing its LRU
        position), or None. #191: a DATABASE/FALLBACK downgrade past
        _DOWNGRADE_TTL_SECONDS is treated as a miss and evicted, so the album
        re-resolves; a COLLECTION entry never expires."""
        # BoundedCache.get already refreshes the entry's LRU position on a hit.
        entry = self._album_cache.get(key)
        if entry is None:
            return None
        source, payload, stored_at = entry
        if source is not MetadataSource.DISCOGS_COLLECTION and (
            time.monotonic() - stored_at >= _DOWNGRADE_TTL_SECONDS
        ):
            # Stale downgrade: drop it so the caller re-runs the lookup chain.
            # (get() refreshed it a line ago; pop removes it outright — net gone.)
            self._album_cache.pop(key)
            return None
        return (source, payload)

    def _cache_store(self, key: tuple, source: MetadataSource, payload):
        """Insert an entry, evicting oldest entries beyond _ALBUM_CACHE_MAX.
        #191: stamps a monotonic timestamp for the downgrade TTL in _cache_get.
        BoundedCache.put owns the insertion-order eviction (arch-4/#220)."""
        self._album_cache.put(key, (source, payload, time.monotonic()))

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

        # A concurrent miss may have completed while this task waited.  Keep
        # only mutable Discogs work inside the gate; cover-art fallback remains
        # independent and cannot delay a collection cache hit.
        async with self._reader_gate:
            cached = self._cache_get(key)
            if cached is not None:
                log.debug(f"Album cache hit after reader gate: {raw.artist} / {raw.album}")
                return self._from_cache(raw, cached)
            discogs_result, discogs_completed = await self._resolve_discogs_locked(raw, key)
        if discogs_result is not None:
            return discogs_result

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
            cover_url = await asyncio.wait_for(
                loop.run_in_executor(
                    None, self.coverart.get_cover_art_url, raw.artist, raw.album
                ),
                timeout=_COVER_ART_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            cover_url = None
            cover_completed = False
            log.warning(
                "Cover art lookup exceeded %ss for '%s / %s'; abandoning it and "
                "continuing (the commit pipeline is not blocked).",
                _COVER_ART_TIMEOUT_SECONDS, raw.artist, raw.album,
            )
        except Exception as e:
            cover_url = None
            if is_transient(e):
                cover_completed = False
                log.info(f"Cover art couldn't determine (transient): {e}")
            else:
                log.warning(f"Unexpected error in cover art lookup: {e}")
        if discogs_completed and cover_completed:
            await self._cache_fallback_if_absent(key, cover_url)
        return TrackMetadata(
            title=raw.title,
            artist=raw.artist,
            album=raw.album,
            cover_art_url=cover_url,
            source=MetadataSource.FALLBACK,
            resolve_key=key,
        )

    async def _cache_fallback_if_absent(self, key: tuple[str, str], cover_url) -> None:
        """Store a clean fallback only if no gated resolve/recovery superseded it.

        Cover-art I/O intentionally happens outside the reader gate.  A recovery
        can therefore publish a fresh immortal collection identity while that
        I/O is pending; re-entering the gate and compare-storing prevents the
        old fallback completion from clobbering it.
        """
        async with self._reader_gate:
            if self._cache_get(key) is None:
                self._cache_store(key, MetadataSource.FALLBACK, cover_url)

    async def _run_reader_locked(self, fn, *args):
        """Await one reader sequence without releasing the gate on cancellation.

        ``run_in_executor`` cancellation only stops the await, not an already
        running worker.  Shield the reader awaitable, then, when this caller is
        cancelled, keep ownership of ``_reader_gate`` until that worker is done
        before re-propagating cancellation.  The cancelled caller never consumes
        the late result, so it cannot publish a cache entry after cancellation.
        """
        reader_task = asyncio.ensure_future(self.reader.run(fn, *args))
        try:
            return await asyncio.shield(reader_task)
        except asyncio.CancelledError:
            while not reader_task.done():
                try:
                    await asyncio.shield(reader_task)
                except asyncio.CancelledError:
                    # A repeated cancellation must still not release the gate
                    # while the executor worker owns mutable reader state.
                    continue
            try:
                reader_task.result()
            except BaseException:
                # The caller is already cancelled; retrieve a late reader error
                # to avoid an unhandled-task warning, then preserve cancellation.
                pass
            raise

    async def _resolve_discogs_locked(
        self, raw: "RawRecognitionResult", key: tuple[str, str]
    ) -> tuple["Optional[TrackMetadata]", bool]:
        """Run the mutable Discogs sequence while ``_reader_gate`` is held."""

        # Tracks whether both Discogs tiers ran to completion.  Only a clean
        # "looked everywhere, found nothing" outcome may cache the fallback —
        # a raised exception (network blip, 429) must stay retryable.
        discogs_completed = True

        # Step 1: User's Discogs collection.  This run_in_executor call is a
        # true error boundary (A-6): a transient failure is expected and leaves
        # the album uncached/retryable (B-4); anything else is an unexpected bug
        # and is logged loudly so it isn't mistaken for a routine miss.
        try:
            result = await self._run_reader_locked(
                self.reader.search_collection, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs collection: {raw.artist} / {raw.album}")
                self._logged_discogs_config.pop("collection", None)   # #189: re-arm THIS tier
                self._cache_store(key, MetadataSource.DISCOGS_COLLECTION, result)
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_COLLECTION), True
        except Exception as e:
            discogs_completed = False
            self._log_discogs_error("collection", e)

        # Step 2: Discogs database
        try:
            result = await self._run_reader_locked(
                self.reader.search_database, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs database: {raw.artist} / {raw.album}")
                self._logged_discogs_config.pop("database", None)   # #189: re-arm THIS tier
                # #191 (C): the collection tier missed but the DATABASE knows this
                # album — the signature of a record the owner may have just added
                # (the index is a snapshot from up to the TTL ago). Only when the
                # collection lookup was a CLEAN miss (not an error), ask the reader
                # to force-refresh the index (cooldown'd) and re-check ownership.
                # If it is now owned, credit it via the COLLECTION tier instead of
                # pinning the no-instance_id DATABASE downgrade.
                if discogs_completed:
                    try:
                        refresh_outcome = await self._run_reader_locked(
                            self.reader.refresh_index_and_research, raw.artist, raw.album
                        )
                    except Exception as e:
                        # A speculative refresh failed transiently: keep the
                        # DATABASE result for this track but do NOT cache the
                        # downgrade (ownership unconfirmed), so a later play
                        # retries — mirrors the B-4 posture.
                        discogs_completed = False
                        # Attribute to the "collection" tier: the refresh re-hits
                        # the same collection endpoint, so its errors share that
                        # tier's #189 throttle key and actionable hint (and a later
                        # collection success re-arms it) rather than an orphan key
                        # that never re-arms.
                        self._log_discogs_error("collection", e)
                        refresh_outcome = None
                    if refresh_outcome is None:
                        pass
                    elif refresh_outcome.state is CollectionRefreshState.OWNED:
                        upgraded = dict(refresh_outcome.result)
                        log.info(
                            f"Collection index was stale — {raw.artist} / {raw.album} "
                            f"is owned after a refresh; crediting via the collection."
                        )
                        self._logged_discogs_config.pop("collection", None)
                        self._cache_store(key, MetadataSource.DISCOGS_COLLECTION, upgraded)
                        return self._from_discogs(
                            raw, upgraded, MetadataSource.DISCOGS_COLLECTION
                        ), True
                    elif refresh_outcome.state is CollectionRefreshState.CLEAN_NO_MATCH:
                        pass
                    elif refresh_outcome.state is CollectionRefreshState.COOLDOWN_SKIPPED:
                        if not refresh_outcome.cooldown_follows_successful_rebuild:
                            discogs_completed = False
                    else:
                        raise ValueError(f"unknown collection refresh state: {refresh_outcome.state}")
                # Only cache the database downgrade if BOTH the collection lookup
                # and the refresh above completed cleanly (a transient error must
                # stay retryable, B-4).
                if discogs_completed:
                    self._cache_store(key, MetadataSource.DISCOGS_DATABASE, result)
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_DATABASE), discogs_completed
        except Exception as e:
            discogs_completed = False
            self._log_discogs_error("database", e)

        return None, discogs_completed

    @staticmethod
    def _valid_recovery_key(key) -> bool:
        return (
            isinstance(key, tuple)
            and len(key) == 2
            and all(isinstance(part, str) and bool(part.strip()) for part in key)
        )

    @staticmethod
    def _collection_pair(entry) -> "Optional[tuple[int, int]]":
        if entry is None:
            return None
        source, payload, _stored_at = entry
        if source is not MetadataSource.DISCOGS_COLLECTION or not isinstance(payload, dict):
            return None
        release_id = payload.get("release_id")
        instance_id = payload.get("instance_id")
        if type(release_id) is not int or release_id <= 0:
            return None
        if type(instance_id) is not int or instance_id <= 0:
            return None
        return release_id, instance_id

    async def recover_collection_instance(
        self,
        resolve_key: tuple[str, str],
        expected_release_id: int,
        expected_instance_id: int,
        observed_instance_ids: tuple[int, ...],
    ) -> "Optional[CollectionIdentity]":
        """Replace exactly one definitively stale collection identity safely."""
        if (
            not self._valid_recovery_key(resolve_key)
            or type(expected_release_id) is not int or expected_release_id <= 0
            or type(expected_instance_id) is not int or expected_instance_id <= 0
            or not isinstance(observed_instance_ids, tuple)
            or len(set(observed_instance_ids)) != len(observed_instance_ids)
            or any(type(value) is not int or value <= 0 for value in observed_instance_ids)
        ):
            safe_release_id = (
                expected_release_id
                if type(expected_release_id) is int and expected_release_id > 0
                else None
            )
            safe_instance_id = (
                expected_instance_id
                if type(expected_instance_id) is int and expected_instance_id > 0
                else None
            )
            if safe_release_id is not None and safe_instance_id is not None:
                log.warning(
                    "Discogs collection recovery stage=invalid-evidence "
                    "expected_release_id=%d expected_instance_id=%d.",
                    safe_release_id, safe_instance_id,
                )
            elif safe_release_id is not None:
                log.warning(
                    "Discogs collection recovery stage=invalid-evidence "
                    "expected_release_id=%d.", safe_release_id,
                )
            elif safe_instance_id is not None:
                log.warning(
                    "Discogs collection recovery stage=invalid-evidence "
                    "expected_instance_id=%d.", safe_instance_id,
                )
            else:
                log.warning("Discogs collection recovery stage=invalid-evidence.")
            return None

        async with self._reader_gate:
            entry = self._album_cache.get(resolve_key)
            pair = self._collection_pair(entry)
            stale_pair = (expected_release_id, expected_instance_id)
            if pair == stale_pair:
                self._album_cache.pop(resolve_key)
            elif entry is not None:
                # Another resolve has replaced this key.  It is already safe
                # only when it exactly agrees with the writer's singleton proof.
                if (
                    len(observed_instance_ids) == 1
                    and pair == (expected_release_id, observed_instance_ids[0])
                ):
                    return CollectionIdentity(*pair)
                cached_release_id, cached_instance_id = pair if pair is not None else (None, None)
                log.info(
                    "Discogs collection recovery stage=newer-cache-refusal "
                    "expected_release_id=%d expected_instance_id=%d "
                    "observed_instance_ids=%s cached_release_id=%s "
                    "cached_instance_id=%s; leaving cache intact.",
                    expected_release_id, expected_instance_id, observed_instance_ids,
                    cached_release_id, cached_instance_id,
                )
                return None

            if len(observed_instance_ids) != 1 or observed_instance_ids[0] == expected_instance_id:
                log.info(
                    "Discogs collection recovery stage=observed-evidence-refusal "
                    "expected_release_id=%d expected_instance_id=%d "
                    "observed_instance_ids=%s.",
                    expected_release_id, expected_instance_id, observed_instance_ids,
                )
                return None
            replacement_instance_id = observed_instance_ids[0]
            try:
                fresh = await self._run_reader_locked(
                    self.reader.rebuild_collection_and_research,
                    resolve_key[0], resolve_key[1],
                )
            except Exception:
                log.warning(
                    "Discogs collection recovery stage=rebuild-failed "
                    "expected_release_id=%d expected_instance_id=%d "
                    "observed_instance_id=%d.",
                    expected_release_id, expected_instance_id, replacement_instance_id,
                )
                return None
            if not isinstance(fresh, dict):
                log.info(
                    "Discogs collection recovery stage=rebuild-no-match "
                    "expected_release_id=%d expected_instance_id=%d "
                    "observed_instance_id=%d.",
                    expected_release_id, expected_instance_id, replacement_instance_id,
                )
                return None
            release_id = fresh.get("release_id")
            if type(release_id) is not int or release_id != expected_release_id:
                rebuilt_release_id = release_id if type(release_id) is int else None
                log.info(
                    "Discogs collection recovery stage=rebuild-release-refusal "
                    "expected_release_id=%d expected_instance_id=%d "
                    "observed_instance_id=%d rebuilt_release_id=%s.",
                    expected_release_id, expected_instance_id, replacement_instance_id,
                    rebuilt_release_id,
                )
                return None
            recovered = dict(fresh)
            recovered["instance_id"] = replacement_instance_id
            self._cache_store(resolve_key, MetadataSource.DISCOGS_COLLECTION, recovered)
            return CollectionIdentity(expected_release_id, replacement_instance_id)

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
