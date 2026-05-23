"""ListenTracker — session tracking and Discogs 'Listened to?' field updater.

Logic:
  - Maintains a PlaySession from first track identification until SESSION_ENDED.
  - When the last track on the album is identified, sets potential_last_track = True.
  - On SESSION_ENDED (sustained silence), if potential_last_track is set,
    calls DiscogsClient.mark_as_listened for the release.
  - Conservative by design: if the last track was never identified (e.g. only
    Side A was played), the field is NOT updated.
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from src.metadata.models import PlaySession, TrackMetadata, MetadataSource
from src.audio.silence import AudioEvent

if TYPE_CHECKING:
    from src.metadata.resolver import MetadataResolver

log = logging.getLogger(__name__)


class ListenTracker:
    """Manages play sessions and triggers Discogs updates on album completion."""

    def __init__(self, config: dict, resolver: "MetadataResolver"):
        self.discogs = resolver.discogs
        self._session: Optional[PlaySession] = None

    def on_silence_event(self, event: AudioEvent):
        """Receive silence events from SilenceDetector (wired up in main.py)."""
        if event == AudioEvent.MUSIC_STARTED:
            self._start_session()
        elif event == AudioEvent.SESSION_ENDED:
            asyncio.create_task(self._end_session())

    def _start_session(self):
        if self._session is None:
            self._session = PlaySession()
            log.info("Play session started.")

    async def _end_session(self):
        """Called when sustained silence signals end of album/side."""
        if self._session is None:
            return

        session = self._session
        self._session = None

        track_count = len(session.identified_tracks)
        log.info(
            f"Play session ended. "
            f"Identified {track_count} track(s). "
            f"Last track reached: {session.potential_last_track}"
        )

        if session.potential_last_track and session.album_release_id:
            log.info(
                f"Last track confirmed for release {session.album_release_id} — "
                f"marking as listened in Discogs."
            )
            success = await asyncio.get_event_loop().run_in_executor(
                None,
                self.discogs.mark_as_listened,
                session.album_release_id,
                session.album_instance_id,
            )
            if success:
                log.info("✅ Discogs 'Listened to?' updated successfully.")
            else:
                log.warning("⚠ Failed to update Discogs 'Listened to?' field.")

        elif session.potential_last_track and not session.album_release_id:
            log.info(
                "Last track reached but release not in Discogs collection — "
                "skipping field update (fallback metadata)."
            )
        else:
            log.info(
                "Last track not reached — not marking as listened "
                "(likely only one side played)."
            )

    async def on_track_identified(self, track: TrackMetadata):
        """Called by RecognitionLoop when a new track is confirmed."""
        if self._session is None:
            self._start_session()
        self._session.log_track(track)
        if track.is_last_track:
            log.info(f"Last track of album identified: '{track.title}' — watching for session end.")
