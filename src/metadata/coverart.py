"""Cover art fallback via MusicBrainz Cover Art Archive.

Used when a release cannot be found in Discogs at all.
Free, open, and covers most commercially released albums.
"""

import logging
from typing import Optional

import musicbrainzngs

log = logging.getLogger(__name__)

# Identify our app to MusicBrainz (required by their API policy)
musicbrainzngs.set_useragent(
    "vinyl-now-playing",
    "1.0",
    "https://github.com/lanebecker/vinyl-now-playing",
)


class CoverArtFallback:
    """Fetches cover art URLs from the MusicBrainz Cover Art Archive."""

    def get_cover_art_url(self, artist: str, album: str) -> Optional[str]:
        """Search MusicBrainz for the release and return a front cover image URL."""
        try:
            result = musicbrainzngs.search_releases(
                release=album,
                artist=artist,
                limit=5,
            )
            releases = result.get("release-list", [])
            if not releases:
                return None

            # Try each result until we find one with cover art
            for release in releases:
                mbid = release["id"]
                try:
                    art = musicbrainzngs.get_image_list(mbid)
                    images = art.get("images", [])
                    front = next(
                        (img for img in images if img.get("front")), None
                    )
                    if front:
                        return (
                            front.get("thumbnails", {}).get("large")
                            or front.get("image")
                        )
                except musicbrainzngs.ResponseError:
                    continue  # This release has no cover art, try next

            return None

        except Exception as e:
            log.warning(
                f"MusicBrainz cover art lookup failed for '{artist} / {album}': {e}"
            )
            return None
