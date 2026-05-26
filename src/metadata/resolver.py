"""MetadataResolver — orchestrates the 3-step lookup chain.

Lookup order:
  1. User's Discogs collection (best: your specific pressing)
  2. Discogs database         (good: generic release metadata)
  3. Fallback                 (Shazam raw + MusicBrainz cover art)

All consumers (display, tracker) receive a TrackMetadata regardless of source.
The `source` field indicates which tier succeeded.
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from src.metadata.models import TrackMetadata, MetadataSource
from src.metadata.discogs_client import DiscogsClient
from src.metadata.coverart import CoverArtFallback

if TYPE_CHECKING:
    from src.audio.recognizer import RawRecognitionResult

log = logging.getLogger(__name__)


class MetadataResolver:
    """Resolves a RawRecognitionResult into a full TrackMetadata."""

    def __init__(self, config: dict):
        self.discogs = DiscogsClient(config)
        self.coverart = CoverArtFallback()

    async def resolve(self, raw: "RawRecognitionResult") -> TrackMetadata:
        """Run the full lookup chain. Always returns a TrackMetadata."""
        loop = asyncio.get_running_loop()

        # Step 1: User's Discogs collection
        try:
            result = await loop.run_in_executor(
                None, self.discogs.search_collection, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs collection: {raw.artist} / {raw.album}")
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_COLLECTION)
        except NotImplementedError:
            pass  # Not yet implemented — fall through
        except Exception as e:
            log.warning(f"Discogs collection search failed: {e}")

        # Step 2: Discogs database
        try:
            result = await loop.run_in_executor(
                None, self.discogs.search_database, raw.artist, raw.album
            )
            if result:
                log.debug(f"Resolved from Discogs database: {raw.artist} / {raw.album}")
                return self._from_discogs(raw, result, MetadataSource.DISCOGS_DATABASE)
        except NotImplementedError:
            pass  # Not yet implemented — fall through
        except Exception as e:
            log.warning(f"Discogs database search failed: {e}")

        # Step 3: Fallback — Shazam data + MusicBrainz cover art
        log.info(f"Using fallback metadata for: {raw.artist} / {raw.album}")
        cover_url = await loop.run_in_executor(
            None, self.coverart.get_cover_art_url, raw.artist, raw.album
        )
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
            tracklist=discogs_result.get("tracklist", []),
            genres=discogs_result.get("genres", []),
            source=source,
        )
