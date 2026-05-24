"""Discogs API wrapper.

Handles collection search, database search, tracklist retrieval,
and updating the 'Listened to?' custom field.

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
from typing import Optional

import discogs_client
import requests

from src.metadata.models import TracklistEntry

log = logging.getLogger(__name__)

_API_BASE = "https://api.discogs.com"


class DiscogsClient:
    """Wraps python3-discogs-client and the Discogs REST API for collection lookups."""

    def __init__(self, config: dict):
        cfg = config["discogs"]
        self.username: str = cfg["username"]
        self.listened_field_name: str = cfg["listened_field_name"]
        self.listened_field_value: str = cfg.get("listened_field_value", "Yes")
        self._token: str = cfg["user_token"]

        # High-level client — used for search() and release() lookups
        self._client = discogs_client.Client(
            "vinyl-now-playing/1.0",
            user_token=self._token,
        )

        # Raw requests session — used for collection membership checks and POSTs
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Discogs token={self._token}",
            "User-Agent": "vinyl-now-playing/1.0",
            "Content-Type": "application/json",
        })

        self._collection_fields: Optional[dict] = None  # Lazily fetched, then cached

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

    def mark_as_listened(self, release_id: int, instance_id: int) -> bool:
        """Set the 'Listened to?' custom field to 'Yes' for a collection item.

        Uses the Discogs collection field update endpoint:
          POST /users/{username}/collection/folders/0/releases/{release_id}
               /instances/{instance_id}/fields/{field_id}

        Returns True on success (HTTP 204), False on any failure.
        """
        try:
            fields = self._get_collection_fields()
            field_id = fields.get(self.listened_field_name)
            if field_id is None:
                log.error(
                    f"Custom field '{self.listened_field_name}' not found in Discogs. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False

            url = (
                f"{_API_BASE}/users/{self.username}/collection"
                f"/folders/0/releases/{release_id}"
                f"/instances/{instance_id}/fields/{field_id}"
            )
            resp = self._session.post(url, json={"value": self.listened_field_value})

            if resp.status_code == 204:
                log.info(
                    f"Marked release {release_id} / instance {instance_id} "
                    f"as '{self.listened_field_value}' in Discogs."
                )
                return True

            log.error(
                f"Discogs field update returned {resp.status_code}: {resp.text[:200]}"
            )
            return False

        except Exception as e:
            log.error(f"Failed to mark release {release_id} as listened: {e}")
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

        resp = self._session.get(
            f"{_API_BASE}/users/{self.username}/collection/fields"
        )
        resp.raise_for_status()
        data = resp.json()
        self._collection_fields = {
            f["name"]: f["id"] for f in data.get("fields", [])
        }
        log.debug(f"Collection fields loaded: {self._collection_fields}")
        return self._collection_fields

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
        resp = self._session.get(
            f"{_API_BASE}/users/{self.username}/collection/releases/{release_id}"
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
                resp = self._session.get(
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

        # Year — Discogs returns 0 for unknown years
        year = None
        try:
            if release.year and release.year > 0:
                year = str(release.year)
        except Exception:
            pass

        # Tracklist — fetch separately; log but don't fail on error
        tracklist = self.get_tracklist(release.id)

        return {
            "album": release.title,
            "year": year,
            "label": label_name,
            "catalog_number": catno,
            "release_id": release.id,
            "instance_id": instance_id,
            "cover_art_url": cover_url,
            "tracklist": tracklist,
        }
