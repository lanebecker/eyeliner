"""Data models for track metadata and play sessions."""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import cached_property
from typing import Optional
import time

from src.metadata.normalize import decoration_base, fold_text, strip_title_decoration


class MetadataSource(Enum):
    DISCOGS_COLLECTION = auto()   # Found in user's personal collection
    DISCOGS_DATABASE = auto()     # Found in Discogs DB but not user's collection
    FALLBACK = auto()             # Shazam metadata + MusicBrainz cover art


# Matches Discogs vinyl position strings like "A1", "B12", "AA3", and the
# common separated / spaced variants "A-1", "A.1", "A 1", "A - 1", plus any
# surrounding whitespace ("A1 ").  Group 1 = side letter(s), Group 2 = track
# number within the side (META-9).
#
# A position must START with a letter to count as a vinyl side, and the side
# label is bounded to ONE or TWO letters — real vinyl sides are "A".."Z" and
# the doubled "AA"/"BB" of multi-disc pressings, never a word.  Bounding the run
# is what stops the whitespace tolerance below from turning bonus/video/disc
# rows ("Video 1", "Bonus 2", "Disc 1") into a fabricated ``SIDE VIDEO`` caption;
# such rows now degrade gracefully to a raw-position display, like the bare
# letter "A" (no number) and CD-style "1-01" (leading digit) already do.
_SIDE_RE = re.compile(r"^\s*([A-Za-z]{1,2})\s*[.\-]?\s*(\d+)\s*$")

# NOTE: DisplayPalette + FALLBACK_PALETTE moved to src/display/palette.py (ARCH-7)
# — they are pure display types with no consumer in src/metadata, so they belong
# in the display layer beside extract_palette(), not up here in the model layer.


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

        Title comparison is tiered normalised matching (#180; see the inline
        comment): losslessly folded exact equality first, then a keyword-gated
        one-side decoration strip requiring a unique folded title.  One
        ``matcher`` predicate serves every comparison site in this method.

        The current entry is located by title (first occurrence); its position
        string yields the side letter and — after a numeric sort of the side by
        track number (META-8) — the within-side ordinal.  Prev/next neighbours
        are pure global-tracklist adjacency (vinyl sides are contiguous),
        resolved via the entry's ``position`` string paired with its title:

          - **B-5 / reprise**: a title repeated across sides resolves to its
            FIRST occurrence and its neighbours come from THAT row, never a
            later same-titled row's side.  (The current entry is the first
            title match, so this is inherent in how ``current`` is chosen — an
            earlier side-filtered re-scan that appeared to do this work was
            proven inert and removed, MUT-16.)
          - **B-10**: a numbered tracklist ('1'..'10') has no side letter, so
            the anchor is simply ``current``'s own position and adjacency still
            resolves correctly by the plain title match.

        ``is_last_track`` is derived from the disambiguated ``global_index``
        (matched by position AND title), NOT from a bare position-string
        comparison: it is the sole gate on Discogs play-count updates, and both
        a duplicated TITLE (reprises, live sets) and a duplicated POSITION string
        (Discogs positions are community-edited free text and not guaranteed
        unique, META-4) would otherwise let an earlier track latch a phantom
        "last track".  The deliberately conservative failure mode remains: when a
        GENUINE closer duplicates an earlier title, the current entry resolves to
        the first occurrence and ``is_last_track`` is False — a missed play count
        rather than a phantom one.
        """
        if not tracklist:
            return cls.empty()

        # #180: locate the row via tiered normalised matching, replacing bare
        # ``lower().strip()`` equality (which missed every Shazam-decorated
        # title — "Eclipse - 2011 Remastered Version" vs a tracklist row
        # "Eclipse" — silently forfeiting the album-completion Play Count).
        #
        # Tier 1: exact equality of losslessly FOLDED text (punctuation fold +
        # NFKC + casefold + whitespace collapse; src/metadata/normalize.py).
        # Tier 2: retry with a keyword-gated trailing decoration stripped from
        # ONE side at a time (never both — distinct decorated siblings like
        # "Song (Live)" vs "Song (Mono)" are different rows), and only when
        # the matching rows share ONE folded title across the whole tracklist
        # — on ambiguity the conservative empty() failure is kept, so the
        # META-4/#78 phantom-last-track class cannot resurface.
        #
        # ``matcher`` is the SAME predicate for all three comparison sites
        # below (current row, side ordinal, global index), so the positional
        # facts can never desync (#180 fix note, point 1).
        fold_key = fold_text(title)

        def _tier1(entry_title: str) -> bool:
            return fold_text(entry_title) == fold_key

        matcher = _tier1
        current = next((e for e in tracklist if _tier1(e.title)), None)

        if current is None:
            stripped_key = strip_title_decoration(fold_key)

            def _tier2(entry_title: str) -> bool:
                entry_key = fold_text(entry_title)
                # Decorated query vs plain row ("Eclipse (Remastered)" → row
                # "Eclipse") — only when the query actually carried decoration,
                # and never on a degenerate empty base (query "(Live)" must not
                # match a whitespace-only row; the truthiness guards mirror
                # reader.py's #179 equivalents).
                if stripped_key and stripped_key != fold_key and stripped_key == entry_key:
                    return True
                # Decorated row vs plain query (row "Song (2019 Mix)" →
                # "Song") — only when the row actually carried decoration.
                entry_stripped = strip_title_decoration(entry_key)
                return (
                    bool(entry_stripped)
                    and entry_stripped != entry_key
                    and entry_stripped == fold_key
                )

            tier2_rows = [e for e in tracklist if _tier2(e.title)]
            # Uniqueness by folded TITLE, not row count: a reprise (the same
            # folded title on two rows) is one match group resolved to its
            # first occurrence, preserving B-5 semantics; two DIFFERENT folded
            # titles both qualifying is a genuine ambiguity — refuse.
            if len({fold_text(e.title) for e in tier2_rows}) == 1:
                accepted_key = fold_text(tier2_rows[0].title)
                # Contested-base refusal (#180 cold review): a row whose OWN
                # stripped base equals the query's stripped base, but which
                # matched neither one-side branch — its decoration diverges
                # from the query's only in syntax (row "Song (Demo)" vs query
                # "Song - Demo") — is invisible to the branches yet is a
                # plausible true target.  Accepting the plain twin instead
                # would arm a phantom last track (the META-4/#78 class) when
                # the twin is the closer.  The base is contested: refuse.
                # The scan uses decoration_base — a fixpoint strip that also
                # sees bracket and stacked forms ("Song [Demo]",
                # "Song (Demo) (Live)") the single-strip matcher cannot;
                # refusal-only aggression is safe (#180 second pass).
                contested = any(
                    fold_text(e.title) != accepted_key
                    and decoration_base(fold_text(e.title)) == stripped_key
                    for e in tracklist
                )
                if not contested:
                    matcher = _tier2
                    current = tier2_rows[0]

        # Strip surrounding whitespace so a padded Discogs row ("A1 ") renders
        # a clean caption rather than a trailing-space artifact (META-9).
        track_display = current.position.strip() if current else ""

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

        # 1-indexed ordinal within the side, and the side's length.  The ordinal
        # follows the parsed track NUMBER, not the tracklist ROW order (META-8):
        # a release that lists its rows out of sequence ([A2, A1]) must still
        # rank A1 as "01 OF 02", keeping the "NN OF MM" caption coherent (N<=M).
        # Every side entry matched _SIDE_RE by construction, so group(2) is safe.
        sorted_side = sorted(
            side_entries, key=lambda e: int(_SIDE_RE.match(e.position).group(2))
        )
        side_position = None
        for i, entry in enumerate(sorted_side):
            if matcher(entry.title):
                side_position = i + 1
                break
        side_total = len(side_entries) if side_entries else None

        # Global index anchor.  ``current`` is already the first tracklist row
        # whose title matches, and it is itself a member of ``side_entries`` (its
        # own side letter is what defined that set), so the first side entry that
        # matches the title is ALWAYS ``current`` — a fuzz over 96,800 tracklists
        # found 0 divergence.  The redundant re-scan that recomputed this from
        # ``side_entries`` was therefore inert and is removed (MUT-16).  For a
        # numbered tracklist (no side letter) ``side_entries`` is empty and this
        # is simply ``current``'s position — the B-10 fallback, unchanged.
        target_position = current.position if current else None

        global_index = None
        if target_position is not None:
            # Resolve the exact occurrence by matching BOTH position AND title.
            # This is what correctly handles a DUPLICATE position string (two
            # rows both at "B2" — Discogs positions are community-edited free
            # text and not guaranteed unique): the position+title pair pins the
            # right row rather than the first row at that position.
            #
            # target_position is ALWAYS derived from a title-bearing entry (the
            # side-disambiguated match above, or `current`'s own position), so a
            # row matching both this position and the title always exists — the
            # loop never falls through. A prior "else use the first position-only
            # match" fallback was removed as provably-unreachable dead code
            # (MUT-15): because target_position can never point at a position
            # with no title match, that branch never executed — an 816k-case
            # fuzz over duplicate-position tracklists confirmed 0 hits, and its
            # three surviving mutants were equivalent (they only mutated a
            # variable nothing consumed).
            for i, e in enumerate(tracklist):
                if e.position == target_position and matcher(e.title):
                    global_index = i
                    break

        # is_last_track gates the collection Play Count write, so a wrong True
        # is a data-integrity bug. Derive it from the disambiguated global_index
        # (matched by position AND title, above) rather than a naive comparison
        # of position strings: Discogs positions are community-edited free text
        # and NOT guaranteed unique, so a mid-album track that merely SHARES the
        # closer's position string must not be flagged the last track (META-4).
        is_last_track = global_index is not None and global_index == len(tracklist) - 1

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
    # Set True once this session's Play Count has actually been credited
    # (the write LANDED), so a re-entrant end (the B-2 race, or a split misfire
    # that finalizes the same session twice) cannot double-increment the same
    # release (B-8).  #163: this is the "committed" flag — set only AFTER the
    # write succeeds, NOT before the await, so a transient write failure leaves it
    # False and the completed play stays eligible for the bounded finalize retry
    # instead of being silently marked done.
    credited: bool = False
    # #163: the "in-flight" latch, SEPARATE from `credited`.  Set True the moment
    # the Play Count write BEGINS (before any await), so a re-entrant finalize
    # that slips in mid-write bails instead of issuing a second increment — the
    # B-8 guarantee, now preserved WITHOUT prematurely recording success.
    crediting: bool = False
    # Set True once this session's last track has actually been Loved on Last.fm
    # (the love LANDED), so the same re-entrant/double-finalize paths can't
    # double-love it (B-23).  Tracked separately from `credited` because loving
    # runs independently of the Discogs credit (a Discogs failure doesn't gate
    # it).  #163: like `credited`, set only AFTER the love succeeds.
    loved: bool = False
    # #163: the love-side "in-flight" latch — the B-23 analogue of `crediting`.
    loving: bool = False

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
