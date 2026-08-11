"""Cover art fallback via MusicBrainz Cover Art Archive.

Used when a release cannot be found in Discogs at all.
Free, open, and covers most commercially released albums.
"""

import logging
import socket
from typing import Optional

import musicbrainzngs

from src.metadata.errors import is_transient

log = logging.getLogger(__name__)

# R5-08: musicbrainzngs is urllib-based and sets NO socket timeout, and the app
# leaves socket.getdefaulttimeout() at None — so a MusicBrainz/CAA socket that
# accepts the connection but never responds parks the executor thread FOREVER
# (Discogs traffic is timeout-disciplined twice over; this fallback tier was the
# gap).  Set the process-wide default socket timeout (only if it is still unset)
# so any socket created without an explicit timeout is bounded.  In this app that
# is effectively just the musicbrainzngs urllib path: the Discogs transport
# (requests / discogs-client) and the cover fetch (urllib3) pass timeout=
# explicitly, and Shazam (aiohttp) and Last.fm (pylast, which uses httpx) manage
# their own — none of them are loosened by this default.
# The resolver ALSO wraps the call in asyncio.wait_for (belt and braces): the
# wait_for guarantees the pipeline never freezes even if a socket somehow evades
# this floor, while the floor ensures the abandoned thread eventually dies rather
# than leaking a default-pool worker over 24/7 uptime.
_MB_SOCKET_TIMEOUT_SECONDS = 15
if socket.getdefaulttimeout() is None:
    socket.setdefaulttimeout(_MB_SOCKET_TIMEOUT_SECONDS)

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
                except Exception as e:
                    # #175: classify with the shared metadata taxonomy. A
                    # TRANSIENT failure (MusicBrainz unreachable/timeout —
                    # NetworkError) means the whole service is down, so the
                    # remaining releases would fail the same way: re-raise to the
                    # outer handler (which logs once and returns None) instead of
                    # hammering a dead service with every candidate.
                    if is_transient(e):
                        raise
                    # Anything else is definitive for THIS release, not the
                    # service: a ResponseError/404 or AuthenticationError for this
                    # MBID, or a malformed/unexpected shape in an UNTRUSTED payload
                    # (the TQ-3 parse errors and their kin). Cover art is a
                    # best-effort fallback, so skip this candidate and try the next
                    # rather than aborting the lookup — the outer handler still
                    # returns None gracefully if every candidate fails. We
                    # deliberately do NOT single out "unexpected" errors to abort
                    # loudly here: on this non-critical, untrusted-payload path a
                    # skip-to-None is the right degradation (#175 cold review).
                    log.debug("skipping release with no usable cover art: %s", e)
                    continue  # try the next release

            return None

        except Exception as e:
            # #190: a TRANSIENT failure (MusicBrainz unreachable/timeout —
            # NetworkError, re-raised here by the inner loop) means the service
            # is down.  Propagate it so the resolver leaves the album
            # uncached/retryable (mirroring the Discogs tiers' B-4 behaviour)
            # instead of flattening it to None and caching that None as the
            # album's FALLBACK payload for the whole session.  Everything else
            # (ResponseError/parse/auth — definitive for this lookup) still
            # degrades to None: cover art is best-effort.
            if is_transient(e):
                raise
            log.warning(
                f"MusicBrainz cover art lookup failed for '{artist} / {album}': {e}"
            )
            return None
