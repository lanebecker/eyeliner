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
from typing import Optional

import discogs_client
import requests

from src.metadata.models import TracklistEntry

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
# header value can't park an executor thread for minutes.
_RATE_LIMIT_MAX_WAIT = 30
_RATE_LIMIT_DEFAULT_WAIT = 2


class DiscogsClient:
    """Wraps python3-discogs-client and the Discogs REST API for collection lookups."""

    def __init__(self, config: dict):
        cfg = config["discogs"]
        self.username: str = cfg["username"]
        self.play_count_field_name: str = cfg["play_count_field_name"]
        self.last_played_field_name: Optional[str] = cfg.get("last_played_field_name")
        self._token: str = cfg["user_token"]

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

    # -------------------------------------------------------------------------
    # HTTP plumbing
    # -------------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue a session request with rate-limit awareness (v1.3.3).

        All direct REST calls in this module go through here so that an HTTP
        429 from Discogs is retried exactly once, after sleeping for the
        server-suggested Retry-After (clamped to _RATE_LIMIT_MAX_WAIT, with
        _RATE_LIMIT_DEFAULT_WAIT as the fallback when the header is missing
        or unparseable).

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
        send = self._session.get if method == "GET" else self._session.post
        resp = send(url, **kwargs)
        if resp.status_code == 429:
            try:
                wait = int(resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT_WAIT))
            except (TypeError, ValueError):
                wait = _RATE_LIMIT_DEFAULT_WAIT
            wait = max(1, min(wait, _RATE_LIMIT_MAX_WAIT))
            log.warning(
                f"Discogs rate limit hit (429) for {method} {url}; "
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

        Strategy 1 — database cross-reference (fast):
          Search the Discogs database for up to 25 candidates. For each, call
          the collection membership endpoint to see if the user owns that pressing.
          Returns the first hit found.

        Strategy 2 — collection walk (slower, catches rare/obscure pressings):
          If strategy 1 finds nothing, page through the user's whole collection
          and fuzzy-match on artist + album title.

        Returns None if the release is not found in the collection.
        """
        # Strategy 1: database candidates → collection membership check
        candidates = self._database_search(artist, album, limit=25)
        for release in candidates:
            try:
                instance_id = self._get_collection_instance_id(release.id)
                if instance_id is not None:
                    log.debug(
                        f"Found in collection (strategy 1): '{release.title}' "
                        f"(release {release.id}, instance {instance_id})"
                    )
                    return self._build_result(release, instance_id=instance_id)
            except Exception as e:
                log.debug(f"Collection check failed for release {release.id}: {e}")
                continue

        # Strategy 2: walk the collection and fuzzy-match
        log.debug(
            f"Strategy 1 found nothing for '{artist} / {album}'; "
            f"falling back to collection walk."
        )
        return self._search_collection_walk(artist, album)

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

            # Read current value; fall back to 0 if blank, missing, or GET fails
            raw_value = self._get_field_value(release_id, instance_id, field_id)
            try:
                current_count = int(raw_value) if raw_value and raw_value.strip() else 0
            except (ValueError, TypeError):
                log.warning(
                    f"Play Count field for release {release_id} / instance {instance_id} "
                    f"contains non-integer value {raw_value!r}; treating as 0."
                )
                current_count = 0

            new_count = current_count + 1

            url = (
                f"{_API_BASE}/users/{self.username}/collection"
                f"/folders/0/releases/{release_id}"
                f"/instances/{instance_id}/fields/{field_id}"
            )
            resp = self._request("POST", url, json={"value": str(new_count)})

            if resp.status_code == 204:
                log.info(
                    f"Play Count updated for release {release_id} / instance {instance_id}: "
                    f"{current_count} → {new_count}."
                )
                return True

            log.error(
                f"Discogs field update returned {resp.status_code}: {resp.text[:200]}"
            )
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

            url = (
                f"{_API_BASE}/users/{self.username}/collection"
                f"/folders/0/releases/{release_id}"
                f"/instances/{instance_id}/fields/{field_id}"
            )
            resp = self._request("POST", url, json={"value": today})

            if resp.status_code == 204:
                log.info(
                    f"Last Played updated for release {release_id} / instance {instance_id}: "
                    f"{today}."
                )
                return True

            log.error(
                f"Discogs Last Played update returned {resp.status_code}: {resp.text[:200]}"
            )
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
                f"{_API_BASE}/users/{self.username}/collection/releases/{release_id}",
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
        """Search the Discogs database and return up to `limit` Release objects."""
        try:
            results = self._client.search(album, artist=artist, type="release")
            return list(results.page(1)[:limit])
        except Exception as e:
            log.warning(f"Discogs database search failed for '{artist} / {album}': {e}")
            return []

    def _get_collection_instance_id(self, release_id: int) -> Optional[int]:
        """Check whether a release is in the user's collection.

        Returns the instance_id if found, or None if not in the collection.
        The instance_id is needed to update per-item custom fields.
        """
        resp = self._request(
            "GET",
            f"{_API_BASE}/users/{self.username}/collection/releases/{release_id}",
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        instances = resp.json().get("releases", [])
        if not instances:
            return None
        # If the user owns multiple copies, use the first instance
        return instances[0].get("instance_id")

    def _search_collection_walk(self, artist: str, album: str) -> Optional[dict]:
        """Walk the user's collection pages and fuzzy-match on artist + album.

        This is the slow path — used only when the database cross-reference
        strategy fails to find the release (e.g. rare pressings, unusual
        artist/album string formatting in the database).
        """
        artist_lower = artist.lower()
        album_lower = album.lower()
        page = 1

        while True:
            try:
                resp = self._request(
                    "GET",
                    f"{_API_BASE}/users/{self.username}/collection/folders/0/releases",
                    params={"page": page, "per_page": 100, "sort": "added", "sort_order": "desc"},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning(f"Collection walk failed on page {page}: {e}")
                return None

            releases = data.get("releases", [])
            if not releases:
                break

            for item in releases:
                basic = item.get("basic_information", {})
                title = basic.get("title", "").lower()
                artists = [a.get("name", "").lower() for a in basic.get("artists", [])]

                if album_lower in title and any(artist_lower in a for a in artists):
                    release_id = basic.get("id")
                    instance_id = item.get("instance_id")
                    log.debug(
                        f"Found in collection (strategy 2): '{basic.get('title')}' "
                        f"(release {release_id}, instance {instance_id})"
                    )
                    try:
                        release_obj = self._client.release(release_id)
                        return self._build_result(release_obj, instance_id=instance_id)
                    except Exception as e:
                        log.debug(f"Failed to build result for collection item {release_id}: {e}")
                        continue

            # Check if there are more pages
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1

        log.debug(f"Collection walk found nothing for '{artist} / {album}'.")
        return None

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

        All fields are extracted defensively — a missing field logs a debug
        message and falls back to None rather than raising.
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
