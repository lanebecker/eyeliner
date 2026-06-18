"""Discogs API wrapper.

Handles collection search, database search, tracklist retrieval,
incrementing the 'Play Count' custom field, and recording the 'Last Played'
date on each completed listen.

Design notes:
  - python3-discogs-client is used for high-level search operations.
  - requests is used directly for endpoints the library doesn't expose cleanly:
    the per-release collection membership check and the field update POST.
  - search_collection() uses two strategies in order:
      1. Search the Discogs database for up to 25 candidates, then cross-reference
         each against the user's collection via the membership endpoint.
      2. If that misses (e.g. the user owns a rare pressing not near the top of
         search results), fall back to a fuzzy walk of the collection itself.
"""

import logging
import time
from datetime import date
from typing import Optional, TYPE_CHECKING

import discogs_client
import requests

from src.metadata.models import TracklistEntry

if TYPE_CHECKING:
    from src.config import DiscogsConfig

log = logging.getLogger(__name__)

_API_BASE = "https://api.discogs.com"

# Network timeout for every Discogs HTTP call.  Discogs is normally well under a
# second, but a flaky network or a CDN hiccup can hang a TCP connection for
# minutes without one.  All session.get/post calls in this module pass this
# explicitly so an executor thread can't sit indefinitely on a stalled socket.
_HTTP_TIMEOUT = 15

# Discogs allows 60 requests/minute for authenticated callers and answers
# excess traffic with HTTP 429 + a Retry-After header (seconds).  When that
# happens we honour the header once per request, capped so a pathological
# header value can't park an executor thread for long.
#
# The cap is 10s (was 30s): _request runs on the SHARED run_in_executor(None,…)
# pool, which also serves cover downloads and Last.fm scrobbles, so a long
# sleep here parks a worker those tasks could use (P-2).  10s still honours any
# realistic Retry-After (Discogs rarely asks for more than a few seconds) while
# bounding the worst-case stall — and the P-1 collection index slashed Discogs
# request volume, making 429 bursts far less likely in the first place.
_RATE_LIMIT_MAX_WAIT = 10
_RATE_LIMIT_DEFAULT_WAIT = 2


def _as_id(value, name: str) -> int:
    """Coerce an identifier to a positive int before it is interpolated into a
    write URL.

    `release_id`, `instance_id`, and `field_id` come from Discogs' own API
    responses and are interpolated directly into the collection-field POST path
    (finding S-5).  They are normally well-formed, but a corrupt or unexpected
    API response could otherwise build a surprising request path silently.
    Coercing here makes a malformed value fail loudly at the boundary instead.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if coerced <= 0:
        raise ValueError(f"{name} must be a positive integer, got {coerced}")
    return coerced


def _redact_url(url: str) -> str:
    """Return a log-safe version of a Discogs URL: path only, with the username
    segment masked and the query string dropped (finding S-4).

    The auth token rides in a header (never the URL), so this isn't a live leak
    today, but the full request path embeds the account username and any future
    query-string credential would otherwise land in the logs verbatim.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        segments = parts.path.split("/")
        # Mask the segment immediately after ".../users/" if present.
        for i, seg in enumerate(segments):
            if seg == "users" and i + 1 < len(segments) and segments[i + 1]:
                segments[i + 1] = "{user}"
                break
        return "/".join(segments) or url
    except Exception:
        return "<unparseable-url>"


class DiscogsClient:
    """Wraps python3-discogs-client and the Discogs REST API for collection lookups."""

    def __init__(self, config: "DiscogsConfig"):
        self.username: str = config.username
        self.play_count_field_name: str = config.play_count_field_name
        self.last_played_field_name: Optional[str] = config.last_played_field_name
        self._token: str = config.user_token

        # High-level client — used for search() and release() lookups.
        # set_timeout() applies the same timeout discipline to the library's
        # internal fetcher that we apply to our direct session.get/post calls
        # below; without this, a hung TCP connection in the library can sit
        # on an executor thread indefinitely.
        self._client = discogs_client.Client(
            "vinyl-now-playing/1.0",
            user_token=self._token,
        )
        self._client.set_timeout(connect=5, read=_HTTP_TIMEOUT)

        # Raw requests session — used for collection membership checks and POSTs
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Discogs token={self._token}",
            "User-Agent": "vinyl-now-playing/1.0",
            "Content-Type": "application/json",
        })

        self._collection_fields: Optional[dict] = None  # Lazily fetched, then cached
        # Lazily-built, session-cached index of the user's collection:
        #   {release_id: {"instance_id", "title", "artists"}}.
        # The collection is static within a session, so building this ONCE and
        # matching locally replaces the per-candidate N+1 membership GETs (P-1).
        self._collection_index: Optional[dict] = None

    # -------------------------------------------------------------------------
    # HTTP plumbing
    # -------------------------------------------------------------------------

    def _request(
        self, method: str, url: str, retry_on_429: Optional[bool] = None, **kwargs
    ) -> requests.Response:
        """Issue a session request with rate-limit awareness (v1.3.3).

        All direct REST calls in this module go through here so that an HTTP
        429 from Discogs is retried exactly once, after sleeping for the
        server-suggested Retry-After (clamped to _RATE_LIMIT_MAX_WAIT, with
        _RATE_LIMIT_DEFAULT_WAIT as the fallback when the header is missing
        or unparseable).

        `retry_on_429` controls whether that one retry happens.  It defaults to
        True for GET (always safe to repeat) and False for POST: a blind POST
        retry is only safe when the body is an idempotent absolute-set, not a
        server-side increment (B-15).  The two POST callers here (Play Count and
        Last Played) write absolute values, so they opt in explicitly; any
        future non-idempotent POST gets no surprise double-submit.

        This runs on an executor thread (every caller is dispatched via
        run_in_executor), so the time.sleep() here never blocks the event
        loop.  Calls made by the python3-discogs-client library itself
        (search/release) are NOT routed through this helper — the library
        does its own fetching — so 429s there surface as exceptions and are
        handled by callers' existing fallback paths.

        Dispatches via self._session.get / self._session.post (rather than
        session.request) so tests can keep mocking those two methods as the
        single HTTP seam.
        """
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        if retry_on_429 is None:
            retry_on_429 = (method == "GET")
        send = self._session.get if method == "GET" else self._session.post
        resp = send(url, **kwargs)
        if resp.status_code == 429 and retry_on_429:
            try:
                wait = int(resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT_WAIT))
            except (TypeError, ValueError):
                wait = _RATE_LIMIT_DEFAULT_WAIT
            wait = max(1, min(wait, _RATE_LIMIT_MAX_WAIT))
            log.warning(
                f"Discogs rate limit hit (429) for {method} {_redact_url(url)}; "
                f"retrying once in {wait}s."
            )
            time.sleep(wait)
            resp = send(url, **kwargs)
        return resp

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

    def increment_play_count(self, release_id: int, instance_id: int) -> bool:
        """Increment the 'Play Count' custom field by 1 for a collection item.

        Reads the current value first (defaulting to 0 if blank, unreadable,
        or unreachable), then POSTs the incremented value back.

        Uses the Discogs collection field update endpoint:
          POST /users/{username}/collection/folders/0/releases/{release_id}
               /instances/{instance_id}/fields/{field_id}

        Returns True on success (HTTP 204), False on any failure.
        """
        try:
            fields = self._get_collection_fields()
            field_id = fields.get(self.play_count_field_name)
            if field_id is None:
                log.error(
                    f"Custom field '{self.play_count_field_name}' not found in Discogs. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False

            # Read current value; fall back to 0 if blank, missing, or GET fails.
            # Coerce via str() before .strip(): the value is normally a string,
            # but if Discogs ever returns it as a JSON number, calling .strip()
            # on an int would raise AttributeError and silently skip the
            # increment (B-16).
            raw_value = self._get_field_value(release_id, instance_id, field_id)
            text = str(raw_value).strip() if raw_value is not None else ""
            try:
                current_count = int(text) if text else 0
            except (ValueError, TypeError):
                log.warning(
                    f"Play Count field for release {release_id} / instance {instance_id} "
                    f"contains non-integer value {raw_value!r}; treating as 0."
                )
                current_count = 0

            new_count = current_count + 1

            # Validate every ID before it lands in the write URL (S-5).
            url = (
                f"{_API_BASE}/users/{self.username}/collection"
                f"/folders/0/releases/{_as_id(release_id, 'release_id')}"
                f"/instances/{_as_id(instance_id, 'instance_id')}"
                f"/fields/{_as_id(field_id, 'field_id')}"
            )
            # Idempotent absolute-set (writes new_count, not an increment), so a
            # single 429 retry is safe (B-15).
            resp = self._request(
                "POST", url, retry_on_429=True, json={"value": str(new_count)}
            )

            if resp.status_code == 204:
                log.info(
                    f"Play Count updated for release {release_id} / instance {instance_id}: "
                    f"{current_count} → {new_count}."
                )
                return True

            # Log the status code only; the raw 4xx body is not logged (S-4).
            log.error(f"Discogs field update returned {resp.status_code}.")
            return False

        except Exception as e:
            log.error(f"Failed to increment Play Count for release {release_id}: {e}")
            return False

    def update_last_played(self, release_id: int, instance_id: int) -> bool:
        """Write today's date (ISO 8601, YYYY-MM-DD) to the 'Last Played' custom field.

        If last_played_field_name is not configured in config.yaml, this is a
        graceful no-op that returns True without making any API calls.

        Uses the Discogs collection field update endpoint:
          POST /users/{username}/collection/folders/0/releases/{release_id}
               /instances/{instance_id}/fields/{field_id}

        Returns True on success (HTTP 204) or if not configured, False on any failure.
        """
        if not self.last_played_field_name:
            return True  # Not configured — graceful no-op

        try:
            fields = self._get_collection_fields()
            field_id = fields.get(self.last_played_field_name)
            if field_id is None:
                log.error(
                    f"Custom field '{self.last_played_field_name}' not found in Discogs. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False

            today = date.today().isoformat()  # e.g. "2026-05-24"

            # Validate every ID before it lands in the write URL (S-5).
            url = (
                f"{_API_BASE}/users/{self.username}/collection"
                f"/folders/0/releases/{_as_id(release_id, 'release_id')}"
                f"/instances/{_as_id(instance_id, 'instance_id')}"
                f"/fields/{_as_id(field_id, 'field_id')}"
            )
            # Idempotent absolute-set (writes today's date), so a single 429
            # retry is safe (B-15).
            resp = self._request("POST", url, retry_on_429=True, json={"value": today})

            if resp.status_code == 204:
                log.info(
                    f"Last Played updated for release {release_id} / instance {instance_id}: "
                    f"{today}."
                )
                return True

            # Log the status code only; the raw 4xx body is not logged (S-4).
            log.error(f"Discogs Last Played update returned {resp.status_code}.")
            return False

        except Exception as e:
            log.error(f"Failed to update Last Played for release {release_id}: {e}")
            return False

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _get_collection_fields(self) -> dict:
        """Lazily fetch and cache the user's collection custom field definitions.

        Returns a dict of {field_name: field_id}.
        """
        if self._collection_fields is not None:
            return self._collection_fields

        resp = self._request(
            "GET", f"{_API_BASE}/users/{self.username}/collection/fields"
        )
        resp.raise_for_status()
        data = resp.json()
        self._collection_fields = {
            f["name"]: f["id"] for f in data.get("fields", [])
        }
        log.debug(f"Collection fields loaded: {self._collection_fields}")
        return self._collection_fields

    def _get_field_value(
        self, release_id: int, instance_id: int, field_id: int
    ) -> Optional[str]:
        """Read the current value of a custom field for a specific collection instance.

        GETs /users/{username}/collection/releases/{release_id} and finds the
        matching instance_id, then returns the note value for field_id.

        Returns None if the GET fails, the instance isn't found, or the field
        has no value set.
        """
        try:
            resp = self._request(
                "GET",
                f"{_API_BASE}/users/{self.username}/collection"
                f"/releases/{_as_id(release_id, 'release_id')}",
            )
            if resp.status_code != 200:
                log.debug(
                    f"_get_field_value: GET returned {resp.status_code} "
                    f"for release {release_id}; defaulting to 0."
                )
                return None
            instances = resp.json().get("releases", [])
            for inst in instances:
                if inst.get("instance_id") == instance_id:
                    for note in inst.get("notes", []):
                        if note.get("field_id") == field_id:
                            return note.get("value")
                    # Instance found but field not set — return None (treat as 0)
                    return None
            log.debug(
                f"_get_field_value: instance {instance_id} not found in "
                f"release {release_id} response; defaulting to 0."
            )
            return None
        except Exception as e:
            log.debug(f"_get_field_value failed for release {release_id}: {e}")
            return None

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
            resp = self._request(
                "GET",
                f"{_API_BASE}/users/{self.username}/collection/folders/0/releases",
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
            page += 1

        self._collection_index = index
        log.debug(f"Built collection index: {len(index)} release(s).")
        return index

    def get_original_year(self, release) -> Optional[str]:
        """Fetch the ORIGINAL release year from the pressing's master.

        A Discogs release carries the pressing year — a 2026 reissue of a
        2005 album says 2026.  The master carries the original year, which
        is what the display should show (DESIGN.md §7 album schema; product
        decision 2026-06-11).

        One extra GET per album, routed through the rate-limited _request
        helper; the resolver's album-level cache means it runs once per
        album per session.  Returns None when the release has no master or
        the lookup fails — callers fall back to the pressing year.
        """
        try:
            master = release.master
            master_id = master.id if master else None
        except Exception:
            master_id = None
        if not master_id:
            return None

        try:
            resp = self._request("GET", f"{_API_BASE}/masters/{master_id}")
            resp.raise_for_status()
            year = resp.json().get("year")
            if year and int(year) > 0:
                return str(year)
        except Exception as e:
            log.debug(f"Master year lookup failed for master {master_id}: {e}")
        return None

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
