"""Data models for track metadata and play sessions."""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import time


class MetadataSource(Enum):
    DISCOGS_COLLECTION = auto()   # Found in user's personal collection
    DISCOGS_DATABASE = auto()     # Found in Discogs DB but not user's collection
    FALLBACK = auto()             # Shazam metadata + MusicBrainz cover art


@dataclass
class TracklistEntry:
    position: str       # e.g. "A1", "B2"
    title: str
    duration: Optional[str] = None  # e.g. "5:32"


@dataclass
class TrackMetadata:
    """Fully resolved metadata for a track, ready for display and tracking."""
    title: str
    artist: str
    album: str
    source: MetadataSource

    # Enriched from Discogs
    year: Optional[str] = None
    label: Optional[str] = None
    catalog_number: Optional[str] = None
    discogs_release_id: Optional[int] = None
    discogs_instance_id: Optional[int] = None  # Needed for collection field updates
    cover_art_url: Optional[str] = None
    tracklist: list["TracklistEntry"] = field(default_factory=list)

    @property
    def is_last_track(self) -> bool:
        """True if this track is the final track on the album."""
        if not self.tracklist:
            return False
        last = self.tracklist[-1]
        return self.title.lower().strip() == last.title.lower().strip()

    @property
    def track_display(self) -> str:
        """Human-readable track position, e.g. 'A1'"""
        for entry in self.tracklist:
            if entry.title.lower().strip() == self.title.lower().strip():
                return entry.position
        return ""


@dataclass
class PlaySession:
    """Tracks the state of a single play session (needle drop to lift)."""
    started_at: float = field(default_factory=time.monotonic)
    identified_tracks: list[TrackMetadata] = field(default_factory=list)
    potential_last_track: bool = False
    album_release_id: Optional[int] = None
    album_instance_id: Optional[int] = None

    def log_track(self, track: TrackMetadata):
        """Record a newly identified track in this session."""
        # Avoid duplicate consecutive entries
        if self.identified_tracks and self.identified_tracks[-1].title == track.title:
            return
        self.identified_tracks.append(track)
        if track.is_last_track:
            self.potential_last_track = True
        # Latch the release/instance IDs from the first Discogs-sourced track
        if self.album_release_id is None and track.discogs_release_id:
            self.album_release_id = track.discogs_release_id
            self.album_instance_id = track.discogs_instance_id
