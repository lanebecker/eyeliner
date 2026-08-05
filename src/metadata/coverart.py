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

            # Try each result until we find one with cover art.  The payload is
            # untrusted (TQ-3): a single malformed release must NOT abort the
            # whole loop — skip it and try the next, exactly as we already do for
            # a ResponseError.  A non-dict image entry, a non-dict `art`, a
            # non-iterable `images`, or a release without an ``id`` are all
            # tolerated by skipping that release.
            for release in releases:
                try:
                    mbid = release["id"]
                    art = musicbrainzngs.get_image_list(mbid)
                    images = art.get("images", [])
                    front = next(
                        (
                            img
                            for img in images
                            if isinstance(img, dict) and img.get("front")
                        ),
                        None,
                    )
                    if front:
                        url = (
                            front.get("thumbnails", {}).get("large")
                            or front.get("image")
                        )
                        # The fetcher (cover_cache) is the URL/SSRF gate; here we
                        # only guarantee a str is handed downstream (else fall
                        # through and try the next release).
                        if isinstance(url, str):
                            return url
                except (musicbrainzngs.ResponseError, AttributeError, TypeError, KeyError) as e:
                    log.debug("skipping release with no usable cover art: %s", e)
                    continue  # try the next release

            return None

        except Exception as e:
            log.warning(
                f"MusicBrainz cover art lookup failed for '{artist} / {album}': {e}"
            )
            return None
