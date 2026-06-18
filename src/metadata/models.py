"""Data models for track metadata and play sessions."""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import cached_property
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


@dataclass(frozen=True)
class TracklistEntry:
    """One tracklist row.  Frozen (immutable) so the entry objects can be
    shared across an album's per-track TrackMetadata without one track's code
    accidentally mutating a sibling's view of the tracklist (B-9)."""
    position: str       # e.g. "A1", "B2"
    title: str
    duration: Optional[str] = None  # e.g. "5:32"


@dataclass(frozen=True)
class SideIndex:
    """Every positional fact about one track within its album's tracklist,
    computed **once** from ``(tracklist, title)`` (A-5).

    These facts used to live as eight separate ``TrackMetadata`` properties,
    each re-scanning the tracklist by title on every access — and the renderer
    touches roughly six of them per frame (~10 fps), so the same linear scans
    ran thousands of times per track.  Bundling them into one immutable value
    object that ``TrackMetadata`` caches keeps the model thin and does the work
    exactly once.  The :meth:`from_tracklist` factory is the single home for the
    position-matching logic (and the B-5 / B-10 correctness fixes it encodes).

    All fields are derived; an absent tracklist (or a title that doesn't appear
    in it) yields :meth:`empty`, where every positional fact degrades to
    ``None`` / ``""`` / ``False`` exactly as the old per-property fallbacks did.
    """
    track_display: str                 # Discogs position string, e.g. "A1" ("" if unknown)
    side_letter: Optional[str]         # "A", "B", … (None for numbered tracklists)
    side_position: Optional[int]       # 1-indexed position within the side
    side_total: Optional[int]          # number of tracks on this side
    global_index: Optional[int]        # index in the full tracklist (prev/next anchor)
    is_last_track: bool                # True iff this is the album's final track
    prev_track_title: Optional[str]    # neighbour by global-tracklist adjacency
    next_track_title: Optional[str]

    @classmethod
    def empty(cls) -> "SideIndex":
        """The neutral SideIndex for an empty tracklist / unmatched title."""
        return cls("", None, None, None, None, False, None, None)

    @classmethod
    def from_tracklist(cls, tracklist: list["TracklistEntry"], title: str) -> "SideIndex":
        """Compute the full positional picture for *title* within *tracklist*.

        The current entry is located by title; its position string yields the
        side letter and the within-side ordinal.  Prev/next neighbours are pure
        global-tracklist adjacency (vinyl sides are contiguous), resolved via
        the entry's unique ``position`` string rather than re-scanning by title.
        That position-anchoring is what fixes two historical bugs:

          - **B-5**: a title repeated across sides (e.g. a reprise) no longer
            returns the wrong side's neighbour — the side filter disambiguates
            the occurrence, and its position then pins the exact global index.
          - **B-10**: a numbered tracklist ('1'..'10') has no side letter, so
            the side filter is empty; the logic falls back to the title match
            and still yields correct adjacency.

        ``is_last_track`` is matched by POSITION (not title): it is the sole
        gate on Discogs play-count updates, and pure title matching let an
        earlier track sharing the closer's title (reprises, live sets) latch a
        phantom "last track".  The deliberately conservative failure mode
        remains: when a GENUINE closer duplicates an earlier title, the current
        entry resolves to the first occurrence and ``is_last_track`` is False —
        a missed play count rather than a phantom one.
        """
        if not tracklist:
            return cls.empty()

        title_key = title.lower().strip()

        # The current track's row, matched by title (first occurrence).
        current = next(
            (e for e in tracklist if e.title.lower().strip() == title_key), None
        )

        track_display = current.position if current else ""

        # Side letter from the position prefix (None for numbered tracklists).
        side_letter = None
        if current is not None:
            m = _SIDE_RE.match(current.position)
            side_letter = m.group(1).upper() if m else None

        # All entries sharing this side letter, in tracklist order.
        side_entries: list[TracklistEntry] = []
        if side_letter:
            side_entries = [
                e for e in tracklist
                if (m := _SIDE_RE.match(e.position)) and m.group(1).upper() == side_letter
            ]

        # 1-indexed ordinal within the side (by title), and the side's length.
        side_position = None
        for i, entry in enumerate(side_entries):
            if entry.title.lower().strip() == title_key:
                side_position = i + 1
                break
        side_total = len(side_entries) if side_entries else None

        # Global index — prefer the side-disambiguated occurrence (B-5); fall
        # back to the plain title match for numbered tracklists (B-10).
        target_position = None
        for entry in side_entries:
            if entry.title.lower().strip() == title_key:
                target_position = entry.position
                break
        if target_position is None and current is not None:
            target_position = current.position

        global_index = None
        if target_position is not None:
            # Prefer an entry matching BOTH position and title (robust if two
            # rows ever share a position string); else the first position match.
            fallback = None
            for i, e in enumerate(tracklist):
                if e.position == target_position:
                    if e.title.lower().strip() == title_key:
                        global_index = i
                        break
                    if fallback is None:
                        fallback = i
            if global_index is None:
                global_index = fallback

        is_last_track = bool(current and current.position == tracklist[-1].position)

        prev_title = None
        next_title = None
        if global_index is not None:
            if global_index > 0:
                prev_title = tracklist[global_index - 1].title
            if global_index < len(tracklist) - 1:
                next_title = tracklist[global_index + 1].title

        return cls(
            track_display=track_display,
            side_letter=side_letter,
            side_position=side_position,
            side_total=side_total,
            global_index=global_index,
            is_last_track=is_last_track,
            prev_track_title=prev_title,
            next_track_title=next_title,
        )


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
    # Positional facts (side-awareness + last-track detection)
    #
    # All of these are derived from (tracklist, title) by a single SideIndex
    # value object (A-5), computed once and cached.  TrackMetadata stays thin:
    # the properties below are pure delegations, and the position-matching
    # logic (with its B-5 / B-10 correctness fixes) lives in SideIndex, not
    # here.  cached_property means the linear scans run once per track even
    # though the renderer reads these ~6×/frame at ~10 fps.
    # ------------------------------------------------------------------

    @cached_property
    def side_index(self) -> "SideIndex":
        """The track's full positional picture, computed once from the
        tracklist.  See :class:`SideIndex` for the per-field semantics."""
        return SideIndex.from_tracklist(self.tracklist, self.title)

    @property
    def is_last_track(self) -> bool:
        """True iff this is the album's final track (the sole gate on Discogs
        play-count updates).  See :meth:`SideIndex.from_tracklist`."""
        return self.side_index.is_last_track

    @property
    def track_display(self) -> str:
        """Human-readable track position, e.g. 'A1' ('' if not in tracklist)."""
        return self.side_index.track_display

    @property
    def side_letter(self) -> Optional[str]:
        """The side letter (e.g. 'A'), or None for numbered tracklists."""
        return self.side_index.side_letter

    @property
    def side_position(self) -> Optional[int]:
        """1-indexed position of this track within its side, or None."""
        return self.side_index.side_position

    @property
    def side_total(self) -> Optional[int]:
        """Total number of tracks on this track's side, or None."""
        return self.side_index.side_total

    @property
    def prev_track_title(self) -> Optional[str]:
        """Title of the previous track in the album, or None if this is the
        very first track."""
        return self.side_index.prev_track_title

    @property
    def next_track_title(self) -> Optional[str]:
        """Title of the next track in the album, or None if this is the very
        last track."""
        return self.side_index.next_track_title


@dataclass
class PlaySession:
    """Tracks the state of a single play session (needle drop to lift)."""
    started_at: float = field(default_factory=time.monotonic)
    identified_tracks: list[TrackMetadata] = field(default_factory=list)
    potential_last_track: bool = False
    album_release_id: Optional[int] = None
    album_instance_id: Optional[int] = None
    # Most recent release ID seen from ANY source that carries one — including
    # DISCOGS_DATABASE results, which never latch the album_* pair above.
    # Used by ListenTracker's album-change auto-split (v1.3.5): comparing
    # against the latch alone missed swaps where the first record was
    # DB-resolved (nothing latched → no difference detected → record 2 could
    # be phantom-credited with record 1's completed play).
    last_release_id: Optional[int] = None
    # Set True once this session's Play Count has been credited, so a re-entrant
    # end (the B-2 race, or a split misfire that finalizes the same session
    # twice) cannot double-increment the same release (B-8).
    credited: bool = False

    def log_track(self, track: TrackMetadata):
        """Record a newly identified track in this session."""
        # Avoid duplicate *consecutive* entries (the same physical track
        # re-identified across overlapping chunks).  Dedup on the full identity
        # (release_id, title, artist) — NOT title alone (B-3): otherwise a
        # swapped-in record whose first track shares a title with the previous
        # record's last logged track ("Intro", a self-titled track, a
        # compilation repeat) is silently dropped — so that record never
        # latches its release and can never earn a Play Count — and a genuinely
        # different track that merely shares the previous title corrupts
        # is_last_track accounting.
        if self.identified_tracks:
            last = self.identified_tracks[-1]
            if (
                last.title == track.title
                and last.artist == track.artist
                and last.discogs_release_id == track.discogs_release_id
            ):
                return
        self.identified_tracks.append(track)
        if track.is_last_track:
            self.potential_last_track = True
        if track.discogs_release_id:
            self.last_release_id = track.discogs_release_id
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
