"""Data models for track metadata and play sessions."""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import time


class MetadataSource(Enum):
    DISCOGS_COLLECTION = auto()   # Found in user's personal collection
    DISCOGS_DATABASE = auto()     # Found in Discogs DB but not user's collection
    FALLBACK = auto()             # Shazam metadata + MusicBrainz cover art


# Matches Discogs position strings like "A1", "B12", "AA3".
# Group 1 = side letter(s), Group 2 = track number within the side.
_SIDE_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


@dataclass
class DisplayPalette:
    """5-color palette for dynamic theming, extracted from album art.

    All values are (R, G, B) tuples in 0-255 range.

    Matches the palette schema from the Claude Design mockups:
      bg      — main background tint (very dark, ~15-22% lightness)
      surface — slightly lighter card/panel tone for radial gradient
      accent  — vibrant extracted color (divider line, album name, badge borders)
      text    — primary text color (near-white, slightly tinted)
      muted   — secondary/meta text color (medium gray, slightly tinted)
    """
    bg: tuple
    surface: tuple
    accent: tuple
    text: tuple
    muted: tuple


# Used when no cover art is available or palette extraction fails.
FALLBACK_PALETTE = DisplayPalette(
    bg=(10, 10, 10),
    surface=(22, 22, 22),
    accent=(200, 200, 200),
    text=(235, 230, 220),
    muted=(138, 133, 124),
)


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
    genres: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Last-track detection
    # ------------------------------------------------------------------

    @property
    def is_last_track(self) -> bool:
        """True if this track is the final track on the album.

        Matched by tracklist POSITION rather than by title (v1.3.4): the
        current entry is located by title via _current_entry, then its
        position string is compared to the final entry's position.

        Why: this property is the sole gate on Discogs play-count updates.
        Pure title matching let any earlier track that shares the closer's
        title (title-track reprises, live sets) latch potential_last_track
        from side A, producing a phantom play count if the session ended
        there.  Note the deliberately conservative failure mode that remains:
        when an album's GENUINE closer duplicates an earlier title,
        _current_entry resolves to the first occurrence and this returns
        False — a missed play count rather than a phantom one, matching the
        tracker's listening-completion philosophy.
        """
        if not self.tracklist:
            return False
        entry = self._current_entry
        if entry is None:
            return False
        return entry.position == self.tracklist[-1].position

    @property
    def track_display(self) -> str:
        """Human-readable track position, e.g. 'A1'."""
        for entry in self.tracklist:
            if entry.title.lower().strip() == self.title.lower().strip():
                return entry.position
        return ""

    # ------------------------------------------------------------------
    # Side-awareness (v1.2.0 display; foundation for v1.5.0 logic)
    # ------------------------------------------------------------------

    @property
    def _current_entry(self) -> Optional["TracklistEntry"]:
        """The TracklistEntry for this track, matched by title."""
        title_key = self.title.lower().strip()
        for entry in self.tracklist:
            if entry.title.lower().strip() == title_key:
                return entry
        return None

    @property
    def side_letter(self) -> Optional[str]:
        """The side letter for this track (e.g. 'A', 'B'), or None.

        Returns None when the position string has no alphabetic prefix
        (e.g. numbered-only tracklists like '1', '2', '3').
        """
        entry = self._current_entry
        if not entry:
            return None
        m = _SIDE_RE.match(entry.position)
        return m.group(1).upper() if m else None

    @property
    def _side_entries(self) -> list["TracklistEntry"]:
        """All tracklist entries that share this track's side letter, in order."""
        letter = self.side_letter
        if not letter:
            return []
        return [
            e for e in self.tracklist
            if (m := _SIDE_RE.match(e.position)) and m.group(1).upper() == letter
        ]

    @property
    def side_position(self) -> Optional[int]:
        """1-indexed position of this track within its side.

        E.g. the 3rd track on Side A returns 3, regardless of whether
        its Discogs position string is 'A3' or something else.
        """
        title_key = self.title.lower().strip()
        for i, entry in enumerate(self._side_entries):
            if entry.title.lower().strip() == title_key:
                return i + 1
        return None

    @property
    def side_total(self) -> Optional[int]:
        """Total number of tracks on this track's side."""
        entries = self._side_entries
        return len(entries) if entries else None

    @property
    def prev_track_title(self) -> Optional[str]:
        """Title of the previous track, or None if this is the very first track.

        Searches within the current side first.  When this track is the first
        on its side (i == 0), falls back to the global tracklist to find the
        preceding track — e.g. B1 correctly returns A7 (the last track of
        Side A) rather than None.
        """
        title_key = self.title.lower().strip()
        entries = self._side_entries
        for i, entry in enumerate(entries):
            if entry.title.lower().strip() == title_key:
                if i > 0:
                    return entries[i - 1].title
                # First track on this side — fall back to global tracklist
                for j, global_entry in enumerate(self.tracklist):
                    if global_entry.title.lower().strip() == title_key:
                        return self.tracklist[j - 1].title if j > 0 else None
        return None

    @property
    def next_track_title(self) -> Optional[str]:
        """Title of the next track, or None if this is the very last track.

        Searches within the current side first.  When this track is the last
        on its side, falls back to the global tracklist to find the following
        track — e.g. A7 correctly returns B1 (the first track of Side B)
        rather than None.
        """
        title_key = self.title.lower().strip()
        entries = self._side_entries
        for i, entry in enumerate(entries):
            if entry.title.lower().strip() == title_key:
                if i < len(entries) - 1:
                    return entries[i + 1].title
                # Last track on this side — fall back to global tracklist
                for j, global_entry in enumerate(self.tracklist):
                    if global_entry.title.lower().strip() == title_key:
                        return self.tracklist[j + 1].title if j < len(self.tracklist) - 1 else None
        return None


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
        # Latch the release/instance IDs from the first collection-sourced track.
        # We require BOTH release_id and instance_id to be set — a release_id alone
        # (which is what DISCOGS_DATABASE returns) is not enough to call the
        # collection field update endpoint, since the endpoint URL needs the
        # instance_id of the user's specific copy.
        if (
            self.album_release_id is None
            and track.discogs_release_id
            and track.discogs_instance_id
        ):
            self.album_release_id = track.discogs_release_id
            self.album_instance_id = track.discogs_instance_id
