"""Tests for ListenTracker — the most edge-case-heavy component."""

import asyncio
import pytest
from unittest.mock import MagicMock

from src.metadata.models import TrackMetadata, MetadataSource, TracklistEntry
from src.audio.silence import AudioEvent


KOB_TRACKLIST = [
    TracklistEntry("A1", "So What"),
    TracklistEntry("A2", "Freddie Freeloader"),
    TracklistEntry("B1", "Blue in Green"),
    TracklistEntry("B2", "All Blues"),
    TracklistEntry("B3", "Flamenco Sketches"),
]


def make_track(
    title: str,
    source: MetadataSource = MetadataSource.DISCOGS_COLLECTION,
    release_id: int = 42,
    instance_id: int = 99,
) -> TrackMetadata:
    return TrackMetadata(
        title=title,
        artist="Miles Davis",
        album="Kind of Blue",
        source=source,
        discogs_release_id=release_id if source != MetadataSource.FALLBACK else None,
        discogs_instance_id=instance_id if source != MetadataSource.FALLBACK else None,
        tracklist=KOB_TRACKLIST,
    )


class TestListenTracker:
    def setup_method(self):
        from src.tracking.listen_tracker import ListenTracker
        self.mock_discogs = MagicMock()
        self.mock_discogs.mark_as_listened = MagicMock(return_value=True)
        mock_resolver = MagicMock()
        mock_resolver.discogs = self.mock_discogs
        config = {
            "discogs": {
                "user_token": "x", "username": "u",
                "listened_field_name": "Listened to?",
                "listened_field_value": "Yes",
            }
        }
        self.tracker = ListenTracker(config, mock_resolver)

    @pytest.mark.asyncio
    async def test_marks_listened_after_last_track_and_session_end(self):
        """Full album play: last track identified → SESSION_ENDED → should update Discogs."""
        await self.tracker.on_track_identified(make_track("Flamenco Sketches"))
        self.tracker.on_silence_event(AudioEvent.SESSION_ENDED)
        await asyncio.sleep(0.01)
        self.mock_discogs.mark_as_listened.assert_called_once_with(42, 99)

    @pytest.mark.asyncio
    async def test_does_not_mark_if_last_track_not_reached(self):
        """Only Side A played — last track never identified → should NOT update Discogs."""
        await self.tracker.on_track_identified(make_track("So What"))
        await self.tracker.on_track_identified(make_track("Freddie Freeloader"))
        self.tracker.on_silence_event(AudioEvent.SESSION_ENDED)
        await asyncio.sleep(0.01)
        self.mock_discogs.mark_as_listened.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_mark_if_not_in_discogs_collection(self):
        """Last track reached but no Discogs release ID (fallback metadata) → skip update."""
        await self.tracker.on_track_identified(
            make_track("Flamenco Sketches", source=MetadataSource.FALLBACK)
        )
        self.tracker.on_silence_event(AudioEvent.SESSION_ENDED)
        await asyncio.sleep(0.01)
        self.mock_discogs.mark_as_listened.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_resets_after_end(self):
        """After a session ends, a new MUSIC_STARTED should begin a fresh session."""
        self.tracker.on_silence_event(AudioEvent.SESSION_ENDED)
        await asyncio.sleep(0.01)
        assert self.tracker._session is None
        self.tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
        assert self.tracker._session is not None
