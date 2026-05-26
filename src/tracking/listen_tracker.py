"""ListenTracker — session tracking and Discogs/Last.fm updater.

Logic:
  - Maintains a PlaySession from first track identification until SESSION_ENDED.
  - When the last track on the album is identified, sets potential_last_track = True.
  - On SESSION_ENDED (sustained silence), if potential_last_track is set:
      1. Calls DiscogsClient.increment_play_count for the release.
      2. Calls DiscogsClient.update_last_played if last_played_field_name is configured.
      3. Calls LastFmClient.love on the last track if love_on_completion is enabled.
  - Conservative by design: if the last track was never identified (e.g. only
    Side A was played), none of the above updates are triggered.
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from src.metadata.models import PlaySession, TrackMetadata, MetadataSource
from src.audio.silence import AudioEvent

if TYPE_CHECKING:
    from src.metadata.resolver import MetadataResolver
    from src.tracking.lastfm_client import LastFmClient

log = logging.getLogger(__name__)


class ListenTracker:
    """Manages play sessions and triggers Discogs field updates on album completion."""

    def __init__(
        self,
        config: dict,
        resolver: "MetadataResolver",
        lastfm: Optional["LastFmClient"] = None,
    ):
        self.discogs = resolver.discogs
        self.lastfm = lastfm
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
                f"incrementing Play Count and updating Last Played in Discogs."
            )
            success = await asyncio.get_event_loop().run_in_executor(
                None,
                self.discogs.increment_play_count,
                session.album_release_id,
                session.album_instance_id,
            )
            if success:
                log.info("✅ Discogs Play Count incremented successfully.")
            else:
                log.warning("⚠ Failed to increment Discogs Play Count.")

            if self.discogs.last_played_field_name:
                last_played_success = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.discogs.update_last_played,
                    session.album_release_id,
                    session.album_instance_id,
                )
                if last_played_success:
                    log.info("✅ Discogs Last Played updated successfully.")
                else:
                    log.warning("⚠ Failed to update Discogs Last Played.")

        elif session.potential_last_track and not session.album_release_id:
            log.info(
                "Last track reached but release not in Discogs collection — "
                "skipping Play Count and Last Played updates (fallback metadata)."
            )
        else:
            log.info(
                "Last track not reached — not incrementing Play Count "
                "or updating Last Played (likely only one side played)."
            )

        # Last.fm: love the last track if the full side completed and love is enabled.
        # Runs independently of Discogs — a Discogs failure doesn't prevent this.
        if session.potential_last_track and self.lastfm and self.lastfm.love_on_completion:
            last_track = session.identified_tracks[-1] if session.identified_tracks else None
            if last_track:
                love_success = await asyncio.get_event_loop().run_in_executor(
                    None, self.lastfm.love, last_track
                )
                if love_success:
                    log.info(f"✅ Last.fm loved: {last_track.artist} — {last_track.title}")
                else:
                    log.warning("⚠ Failed to love track on Last.fm.")

    async def on_track_identified(self, track: TrackMetadata):
        """Called by RecognitionLoop when a new track is confirmed."""
        if self._session is None:
            self._start_session()
        self._session.log_track(track)
        if track.is_last_track:
            log.info(f"Last track of album identified: '{track.title}' — watching for session end.")
