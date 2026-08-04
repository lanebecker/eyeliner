"""Read-only Discogs access for the resolver (A-4).

`DiscogsReader` owns everything the metadata resolver needs and nothing it
doesn't: database + collection search, tracklist and original-year lookups, and
assembly of the standardised result dict.  It holds the high-level
python3-discogs-client `Client` (the only half that searches/fetches releases)
and the session-cached collection index; it reaches the REST API through the
shared :class:`~src.metadata.discogs.transport.DiscogsHttp`.

It has no knowledge of the write side (play-count / last-played) — that lives in
:class:`~src.metadata.discogs.writer.DiscogsCollectionWriter`.
"""

import logging
from typing import Optional, TYPE_CHECKING
from urllib.parse import quote

import discogs_client

from src.metadata.models import TracklistEntry
from src.metadata.discogs.transport import DiscogsHttp, _API_BASE, _HTTP_TIMEOUT

if TYPE_CHECKING:
    from src.config import DiscogsConfig

log = logging.getLogger(__name__)

# STAB-4: absolute ceiling on the collection-index paging loop.  The loop's
# natural exits are an empty page or ``page >= pagination.pages``; a malformed or
# hostile pagination response (a huge ``pages`` value with never-empty pages) or a
# logic bug would otherwise page WITHOUT BOUND.  Under the documented systemd crash
# loop (Restart=on-failure / RestartSec=10, and StartLimitBurst added by this same
# fix) each restart re-pages from zero, so an unbounded build pins the appliance at
# the authenticated 60-request/minute rate limit.  At 100 releases/page this cap is
# 100,000 records — several times the most extreme personal vinyl collection, so it
# never clips a real one; it only bounds the pathological case.
_MAX_COLLECTION_PAGES = 1000


class DiscogsReader:
    """Database/collection search, tracklist + original-year, result assembly."""

    def __init__(self, http: DiscogsHttp, config: "DiscogsConfig"):
        self._http = http
        self.username: str = config.username
        # SEC-7: percent-encode the username once for use as a URL PATH SEGMENT
        # (operator-authored, so a '/', '?' or '#' would otherwise reshape the
        # request path). Mirrors DiscogsCollectionWriter; the collection-index
        # GET below uses this encoded form, not the raw ``self.username``.
        self._username_path: str = quote(config.username, safe="")

        # High-level client — used for search() and release() lookups.
        # set_timeout() applies the same timeout discipline to the library's
        # internal fetcher that we apply to our direct session calls; without
        # it, a hung TCP connection in the library can sit on an executor thread
        # indefinitely.
        self._client = discogs_client.Client(
            "vinyl-now-playing/1.0",
            user_token=config.user_token,
        )
        self._client.set_timeout(connect=5, read=_HTTP_TIMEOUT)

        # Lazily-built, session-cached index of the user's collection:
        #   {release_id: {"instance_id", "title", "artists"}}.
        # The collection is static within a session, so building this ONCE and
        # matching locally replaces the per-candidate N+1 membership GETs (P-1).
        self._collection_index: Optional[dict] = None

    async def run(self, fn, *args):
        """Dispatch one of this reader's blocking methods on the shared,
        dedicated Discogs executor (#61) rather than the default pool.

        Thin delegate to :meth:`DiscogsHttp.run`; the transport owns the one
        pool both halves (reader + writer) share. The resolver calls
        ``await reader.run(reader.search_collection, artist, album)`` in place of
        ``loop.run_in_executor(None, …)``.
        """
        return await self._http.run(fn, *args)

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def search_collection(self, artist: str, album: str) -> Optional[dict]:
        """Search the user's Discogs collection for a release matching artist + album.

        Both strategies match against a session-cached in-memory index of the
        collection (built once, the collection being static within a session),
        so neither pays a per-candidate HTTP cost (P-1):

        Strategy 1 — database cross-reference (fast):
          Search the Discogs database for up to 25 candidates and check each
          against the local index by release_id.  Returns the first hit.

        Strategy 2 — index fuzzy-match (catches rare/obscure pressings):
          If strategy 1 finds nothing, fuzzy-match the index entries on
          artist + album title.

        Returns None if the release is not found in the collection.  The index
        build raises on a hard error so the resolver treats it as
        "couldn't determine" (leaves the album uncached, retries next track)
        rather than a false "not owned" (B-4/B-13).
        """
        # SEC-1: an incomplete recognition (empty / whitespace artist OR album)
        # cannot identify a SPECIFIC owned pressing.  The strategy-2 substring
        # test degenerates on an empty term — ``"" in title`` and ``"" in
        # artist`` are always True, and even a single space is a substring of
        # most titles ("Kind of Blue" contains spaces) — so it would return an
        # arbitrary owned release, whose ``instance_id`` becomes the Play Count /
        # Last Played write target.  Refuse to guess: return None so the track
        # resolves via the database / fallback tiers (no instance_id, no write)
        # instead of crediting a play to the wrong record.  This does NOT reject
        # legitimately short titles ("4", "Q") — only empty/whitespace terms.
        if not artist.strip() or not album.strip():
            log.info(
                "Skipping collection match for an incomplete recognition "
                "(artist=%r, album=%r): cannot identify a specific owned pressing "
                "without both fields, so not selecting a write target.",
                artist, album,
            )
            return None

        index = self._get_collection_index()

        # Strategy 1: database candidates, matched locally against the index.
        candidates = self._database_search(artist, album, limit=25)
        for release in candidates:
            entry = index.get(release.id)
            if entry is not None:
                log.debug(
                    f"Found in collection (strategy 1): '{release.title}' "
                    f"(release {release.id}, instance {entry['instance_id']})"
                )
                return self._build_result(release, instance_id=entry["instance_id"])

        # Strategy 2: fuzzy-match the index locally (no extra HTTP).
        log.debug(
            f"Strategy 1 found nothing for '{artist} / {album}'; "
            f"fuzzy-matching the collection index."
        )
        artist_lower = artist.lower()
        album_lower = album.lower()
        # dict iteration is insertion order = collection "added desc", so on a
        # substring collision the most-recently-added owned album wins (matches
        # the old collection walk's first-hit-wins behaviour).
        for release_id, entry in index.items():
            title = entry["title"].lower()
            artists = [a.lower() for a in entry["artists"]]
            if album_lower in title and any(artist_lower in a for a in artists):
                log.debug(
                    f"Found in collection (strategy 2): '{entry['title']}' "
                    f"(release {release_id}, instance {entry['instance_id']})"
                )
                # Fetch + build like strategy 1.  A fetch/build error is allowed
                # to PROPAGATE rather than be swallowed as "not owned": the index
                # says the user owns this, so a transient blip should leave the
                # album uncached for retry, not downgrade it (B-4/B-13 parity).
                release_obj = self._client.release(release_id)
                return self._build_result(release_obj, instance_id=entry["instance_id"])

        return None

    def search_database(self, artist: str, album: str) -> Optional[dict]:
        """Search the full Discogs database (not just the user's collection).

        Returns the best matching release without an instance_id (since we don't
        know if — or which pressing of — it's in the collection).

        Returns None if nothing useful is found.
        """
        candidates = self._database_search(artist, album, limit=3)
        if not candidates:
            return None

        for release in candidates:
            try:
                return self._build_result(release, instance_id=None)
            except Exception as e:
                log.debug(f"Failed to build result for release {release.id}: {e}")
                continue

        return None

    def get_tracklist(self, release_id: int) -> list:
        """Fetch and return the full tracklist for a release.

        Filters out Discogs "heading" pseudo-tracks (e.g. "Side A", "Side B")
        which have no position value and aren't playable tracks.
        """
        try:
            release = self._client.release(release_id)
            entries = []
            for track in release.tracklist:
                # Headings have type_ == 'heading' and typically no position
                if getattr(track, "type_", None) == "heading":
                    continue
                if not track.position:
                    continue
                entries.append(TracklistEntry(
                    position=track.position,
                    title=track.title,
                    duration=track.duration or None,
                ))
            return entries
        except Exception as e:
            log.warning(f"Failed to fetch tracklist for release {release_id}: {e}")
            return []

    def get_original_year(self, release) -> Optional[str]:
        """Fetch the ORIGINAL release year from the pressing's master.

        A Discogs release carries the pressing year — a 2026 reissue of a
        2005 album says 2026.  The master carries the original year, which
        is what the display should show (DESIGN.md §7 album schema; product
        decision 2026-06-11).

        One extra GET per album, routed through the rate-limited transport;
        the resolver's album-level cache means it runs once per album per
        session.  Returns None when the release has no master or the lookup
        fails — callers fall back to the pressing year.
        """
        try:
            master = release.master
            master_id = master.id if master else None
        except Exception:
            master_id = None
        if not master_id:
            return None

        try:
            resp = self._http.request("GET", f"{_API_BASE}/masters/{master_id}")
            resp.raise_for_status()
            year = resp.json().get("year")
            if year and int(year) > 0:
                return str(year)
        except Exception as e:
            log.debug(f"Master year lookup failed for master {master_id}: {e}")
        return None

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _database_search(self, artist: str, album: str, limit: int = 25) -> list:
        """Search the Discogs database and return up to `limit` Release objects.

        A genuine "no matches" returns an empty list; a hard API error (network,
        429, 5xx) is allowed to RAISE so the caller can treat it as "couldn't
        determine" rather than "not found" (B-13).  Swallowing the error here
        used to make every track fall through to the slow collection walk and
        let an owned album be cached as a database/fallback downgrade.
        """
        results = self._client.search(album, artist=artist, type="release")
        return list(results.page(1)[:limit])

    def _get_collection_index(self) -> dict:
        """Build (once per session) and return an in-memory index of the user's
        collection: ``{release_id: {"instance_id", "title", "artists"}}``.

        Replaces the old per-candidate membership GET (one per database
        candidate, up to 25) and the full re-walk with a single paginated fetch
        + local lookups (P-1).  The collection is static within a session and
        the process restarts daily, so there is no TTL.

        Raises on a hard fetch error so the caller (search_collection) lets it
        propagate to the resolver, which treats it as "couldn't determine" and
        leaves the album uncached for retry — rather than a false "not owned"
        that pins a downgrade for the session (B-4/B-13).  A successfully built
        (possibly empty) index is cached.
        """
        if self._collection_index is not None:
            return self._collection_index

        index: dict = {}
        page = 1
        while True:
            resp = self._http.request(
                "GET",
                f"{_API_BASE}/users/{self._username_path}/collection/folders/0/releases",
                params={"page": page, "per_page": 100, "sort": "added", "sort_order": "desc"},
            )
            resp.raise_for_status()
            data = resp.json()

            releases = data.get("releases", [])
            if not releases:
                break

            for item in releases:
                basic = item.get("basic_information", {})
                release_id = basic.get("id")
                if release_id is None:
                    continue
                # Keep the first instance seen per release (mirrors the old
                # "use instances[0]" behaviour for users who own duplicates).
                if release_id not in index:
                    index[release_id] = {
                        "instance_id": item.get("instance_id"),
                        "title": basic.get("title", ""),
                        "artists": [a.get("name", "") for a in basic.get("artists", [])],
                    }

            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            if page >= _MAX_COLLECTION_PAGES:
                # STAB-4: absolute safety ceiling.  Reaching it means the
                # pagination response is malformed (no real personal collection
                # has 100,000+ records), so stop with the partial index rather
                # than paging without bound and hammering the rate limit.  The
                # partial index is still cached below, so this build is not
                # re-attempted per track.  A partial index can only cause
                # false-negatives (an album beyond the cap resolves via the
                # database tier with no instance_id) — never a wrong write target.
                log.warning(
                    "Collection paging hit the absolute cap of %d pages "
                    "(%d releases indexed); stopping with a partial index. "
                    "Suspect a malformed Discogs pagination response.",
                    _MAX_COLLECTION_PAGES, len(index),
                )
                break
            page += 1

        self._collection_index = index
        log.debug(f"Built collection index: {len(index)} release(s).")
        return index

    def _build_result(self, release, instance_id: Optional[int]) -> dict:
        """Build a standardised result dict from a Discogs Release object.

        Per-field defensive extraction is an INTENTIONAL design choice, not a
        swallowed error (A-6): the identity fields the rest of the pipeline gates
        on — `release_id` and `instance_id` — are passed in by the caller and are
        always trustworthy; the enrichment fields below (cover, year, label,
        catalog, genres, tracklist) are best-effort decoration, so a missing or
        malformed one degrades that field to None/[] rather than failing the
        whole resolve.  This is graceful degradation of optional data, distinct
        from the transient-vs-unexpected error taxonomy in errors.py that governs
        the resolve *boundary*.
        """
        # Cover art — prefer primary image, fall back to first available
        cover_url = None
        try:
            images = release.images
            if images:
                primary = next(
                    (img for img in images if img.get("type") == "primary"),
                    images[0],
                )
                cover_url = primary.get("uri")
        except Exception:
            pass

        # Label and catalog number
        label_name = None
        catno = None
        try:
            if release.labels:
                label_name = release.labels[0].name
                raw_catno = release.labels[0].catno
                # Discogs uses the string "none" when there's no catalog number
                catno = raw_catno if raw_catno and raw_catno.lower() != "none" else None
        except Exception:
            pass

        # Year — prefer the album's ORIGINAL year from the master (v1.4.2);
        # release.year is the pressing year, so a reissue would otherwise
        # display its repress date.  Falls back to the pressing year when
        # there's no master or the lookup fails.  (Discogs returns 0 for
        # unknown years.)
        year = self.get_original_year(release)
        if year is None:
            try:
                if release.year and release.year > 0:
                    year = str(release.year)
            except Exception:
                pass

        # Tracklist — fetch separately; log but don't fail on error
        tracklist = self.get_tracklist(release.id)

        # Genres and styles — styles are more specific so they come first.
        # Both are already present in the release object; no extra API call needed.
        genres: list = []
        try:
            if release.styles:
                genres.extend(release.styles)
        except Exception:
            pass
        try:
            if release.genres:
                genres.extend(release.genres)
        except Exception:
            pass

        return {
            "album": release.title,
            "year": year,
            "label": label_name,
            "catalog_number": catno,
            "release_id": release.id,
            "instance_id": instance_id,
            "cover_art_url": cover_url,
            "tracklist": tracklist,
            "genres": genres,
        }
