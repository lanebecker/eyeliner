"""Discogs API wrapper.

Handles collection search, database search, tracklist retrieval,
and updating the 'Listened to?' custom field.
"""

import logging
from typing import Optional

import discogs_client

from src.metadata.models import TracklistEntry

log = logging.getLogger(__name__)


class DiscogsClient:
    """Wraps python3-discogs-client for collection and database lookups."""

    def __init__(self, config: dict):
        cfg = config["discogs"]
        self.username: str = cfg["username"]
        self.listened_field_name: str = cfg["listened_field_name"]
        self.listened_field_value: str = cfg.get("listened_field_value", "Yes")
        self._client = discogs_client.Client(
            "vinyl-now-playing/1.0",
            user_token=cfg["user_token"],
        )
        self._user = None
        self._collection_fields: Optional[dict] = None  # field_name → field_id

    def _get_user(self):
        if self._user is None:
            self._user = self._client.identity()
        return self._user

    def _get_collection_fields(self) -> dict:
        """Lazily fetch and cache the user's collection custom field definitions."""
        if self._collection_fields is not None:
            return self._collection_fields
        fields = self._client.get(f"/users/{self.username}/collection/fields")
        self._collection_fields = {
            f["name"]: f["id"] for f in fields.get("fields", [])
        }
        log.debug(f"Collection fields: {self._collection_fields}")
        return self._collection_fields

    def search_collection(self, artist: str, album: str) -> Optional[dict]:
        """Search the user's Discogs collection for a release matching artist + album.

        Returns a dict with release_id, instance_id, cover_art_url, tracklist, etc.
        or None if not found.

        TODO: Implement this method.
        Hint: GET /users/{username}/collection/releases?q={artist} {album}
              or page through folders and filter client-side.
        """
        raise NotImplementedError

    def search_database(self, artist: str, album: str) -> Optional[dict]:
        """Search the full Discogs database (not just the user's collection).

        Returns a minimal release dict or None.

        TODO: Implement this method.
        Hint: self._client.search(album, artist=artist, type='release')
              and return the best match.
        """
        raise NotImplementedError

    def get_tracklist(self, release_id: int) -> list[TracklistEntry]:
        """Fetch the full tracklist for a release by ID.

        TODO: Implement this method.
        Hint: release = self._client.release(release_id)
              return [TracklistEntry(t.position, t.title, t.duration)
                      for t in release.tracklist]
        """
        raise NotImplementedError

    def mark_as_listened(self, release_id: int, instance_id: int) -> bool:
        """Set the 'Listened to?' custom field to 'Yes' for a collection item.

        Returns True on success, False on failure.

        TODO: Implement this method.
        Hint: POST /users/{username}/collection/folders/0/releases/{release_id}/
                    instances/{instance_id}/fields/{field_id}
              Body: {"value": self.listened_field_value}
        """
        try:
            fields = self._get_collection_fields()
            field_id = fields.get(self.listened_field_name)
            if field_id is None:
                log.error(
                    f"Custom field '{self.listened_field_name}' not found. "
                    f"Available fields: {list(fields.keys())}"
                )
                return False
            raise NotImplementedError
        except NotImplementedError:
            raise
        except Exception as e:
            log.error(f"Failed to mark release {release_id} as listened: {e}")
            return False
