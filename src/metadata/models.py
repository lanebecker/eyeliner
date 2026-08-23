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


def _match_side(position: str):
    """Return the :data:`_SIDE_RE` match for *position* ONLY when its letter
    label is a real vinyl side, else ``None``.

    R5-16(b): ``_SIDE_RE`` allows one OR two letters, the two-letter form for
    the doubled ``AA``/``BB`` sides of multi-disc pressings (per its own
    comment).  But ``"CD1"``/``"LP1"``/``"DV1"``/``"Cd2"`` also matched, so a
    bonus-CD / DVD / LP-label row rendered a fabricated ``SIDE CD · 01 OF 02``
    caption AND (via the completion anchor, R5-16(a)) counted as a vinyl side.
    A genuine doubled side always repeats ONE letter, so require the two-letter
    form to be the SAME letter twice; a single letter is always fine.  Bounding
    to real sides is exactly what the regex comment promised.
    """
    m = _SIDE_RE.match(position)
    if m is None:
        return None
    letters = m.group(1)
    if len(letters) == 2 and letters[0].lower() != letters[1].lower():
        return None
    return m


def _playable_row_count(tracklist) -> int:
    """Number of PLAYABLE rows on *tracklist* for completion arithmetic (R6-07).

    Mirrors the R5-16(a) completion anchor: on a hybrid LP+CD (or LP+file)
    release the never-playable bonus rows must not inflate the count, so count
    the VINYL side rows (per :func:`_match_side`).  For a side-letter-less list
    (a numbered or CD-only tracklist, where NO row matches a vinyl side) fall
    back to every row — preserving the B-10 numbered-tracklist behaviour, exactly
    as the anchor falls back to the last row there.
    """
    vinyl = sum(1 for e in tracklist if _match_side(e.position) is not None)
    return vinyl if vinyl > 0 else len(tracklist)

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
    first_vinyl_index: Optional[int]   # global index of the release's FIRST vinyl row (R6-08)

    @classmethod
    def empty(cls) -> "SideIndex":
        """The neutral SideIndex for an empty tracklist / unmatched title."""
        return cls("", None, None, None, None, False, None, None, None)

    @property
    def first_playable_index(self) -> int:
        """Global index of the release's FIRST playable (vinyl) row — the opener a
        genuine re-drop identifies first, which the #185 replay boundary anchors
        on (R6-08).  Falls back to 0 for a side-letter-less (numbered / CD-only)
        list, mirroring the completion anchor's own fallback to the last row, so
        a plain numbered tracklist behaves exactly as the pre-R6-08 ``== 0``
        anchor did."""
        return self.first_vinyl_index if self.first_vinyl_index is not None else 0

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
            m = _match_side(current.position)
            side_letter = m.group(1).upper() if m else None

        # All entries sharing this side letter, in tracklist order.
        side_entries: list[TracklistEntry] = []
        if side_letter:
            side_entries = [
                e for e in tracklist
                if (m := _match_side(e.position)) and m.group(1).upper() == side_letter
            ]

        # 1-indexed ordinal within the side, and the side's length.  The ordinal
        # follows the parsed track NUMBER, not the tracklist ROW order (META-8):
        # a release that lists its rows out of sequence ([A2, A1]) must still
        # rank A1 as "01 OF 02", keeping the "NN OF MM" caption coherent (N<=M).
        # Every side entry matched _SIDE_RE by construction, so group(2) is safe.
        sorted_side = sorted(
            side_entries, key=lambda e: int(_match_side(e.position).group(2))
        )
        # #224: locate side_position by `current`'s IDENTITY within sorted_side,
        # not by re-matching the title. A duplicated title on one side with
        # out-of-order rows ([("A2","Theme"),("A1","Theme")], query "Theme")
        # would otherwise desync — track_display is the row-order first match
        # ("A2") while a title re-match here picks the number-order first match
        # (the OTHER "Theme" row), yielding an incoherent "A2 · 01 OF 02".
        # `current` is a member of sorted_side (its side letter defined the set),
        # so exactly one entry satisfies `is current`; None when current is None.
        side_position = next(
            (i + 1 for i, entry in enumerate(sorted_side) if entry is current),
            None,
        )
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
        #
        # R5-16(a): the anchor is the last VINYL-SIDE row, not the last tracklist
        # ROW.  On a hybrid LP+CD (or LP+digital) release the vinyl closer is
        # followed by bonus CD/DVD/file rows, so a last-ROW anchor never armed
        # completion for a full vinyl play — a permanent lost Play Count for every
        # hybrid edition owned.  This is a TURNTABLE tracker: the non-vinyl rows
        # never play on the platter, so "album complete" is the end of the vinyl
        # (Lane approved 2026-08-11).  When the tracklist has NO vinyl sides at all
        # (a numbered or CD-only tracklist, e.g. "1".."12" or "1-01"), fall back to
        # the last row — preserving the B-10 numbered-tracklist behaviour unchanged.
        # This relies on the R5-16(b) _match_side tightening so trailing "CD1"/"DV1"
        # rows are NOT counted as vinyl sides.
        last_vinyl_index = None
        first_vinyl_index = None
        for i, e in enumerate(tracklist):
            if _match_side(e.position) is not None:
                if first_vinyl_index is None:
                    first_vinyl_index = i   # R6-08: the replay-boundary opener anchor
                last_vinyl_index = i
        completion_anchor = (
            last_vinyl_index if last_vinyl_index is not None else len(tracklist) - 1
        )
        is_last_track = global_index is not None and global_index == completion_anchor

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
            # Part of the track's positional picture: None when the title does
            # not place on this tracklist, so an unmatched title still collapses
            # to SideIndex.empty() (the replay boundary only reads this on a
            # track that HAS a global_index, so gating it here loses nothing).
            first_vinyl_index=(first_vinyl_index if global_index is not None else None),
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
    # #184: the resolver's normalised (artist, album) cache key that produced
    # this track — RAW Shazam strings, not the Discogs album title (which can
    # differ between pressings).  Lets the split detector recognise a B-4
    # tier upgrade (DATABASE→COLLECTION re-resolve of the SAME album under a
    # different release id) without trusting cross-pressing album strings.
    # None for tracks built outside the resolver (older tests, direct
    # construction) — a None key never suppresses a split.
    resolve_key: Optional[tuple] = None

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
    # Monotonic session-start time.  R7-05: formerly dead state; R8-01 (#345):
    # the flip-resume window is now measured as `new_session.started_at -
    # prev.ended_at` (the GAP between the sessions), so this is the near edge of
    # that gap.  NOTE (test trap, R8-04): `default_factory=time.monotonic` binds
    # the REAL function at class-definition time — a test that patches
    # time.monotonic must stamp `started_at` explicitly.
    started_at: float = field(default_factory=time.monotonic)
    # R8-01 (#345): monotonic time this session was DETACHED (stamped in
    # ListenTracker._detach_session_locked — the SESSION_ENDED / split moment).
    # The far edge of the flip-resume gap: the R7-03 window used to be anchored
    # at the prior session's STARTED_AT, which (fragment play + gap + tail play
    # + trailing silence) always exceeded 300s for real music — the feature
    # could never fire.  None until detached.
    ended_at: Optional[float] = None
    identified_tracks: list[TrackMetadata] = field(default_factory=list)
    potential_last_track: bool = False
    album_release_id: Optional[int] = None
    album_instance_id: Optional[int] = None
    # The exact normalized resolver key of the track that latched the write
    # identity.  It must not follow later tracks' split-detection evidence.
    album_resolve_key: Optional[tuple[str, str]] = None
    # Most recent release ID seen from ANY source that carries one — including
    # DISCOGS_DATABASE results, which never latch the album_* pair above.
    # Used by ListenTracker's album-change auto-split (v1.3.5): comparing
    # against the latch alone missed swaps where the first record was
    # DB-resolved (nothing latched → no difference detected → record 2 could
    # be phantom-credited with record 1's completed play).
    last_release_id: Optional[int] = None
    # #184: recorded alongside last_release_id so the split detector can
    # recognise a B-4 tier upgrade (the last id came from a DATABASE-sourced
    # degraded resolve; the incoming COLLECTION-sourced track re-resolved the
    # SAME album — same resolver cache key — under the owned pressing's id)
    # and suppress the spurious split.
    last_release_source: Optional[MetadataSource] = None
    last_release_resolve_key: Optional[tuple] = None
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
    # #181: the track whose is_last_track ARMED potential_last_track — the
    # album's closer, recorded at arming time.  The Last.fm love targets this,
    # NOT identified_tracks[-1]: tracks can legitimately be identified AFTER
    # the closer within the same session (side A re-dropped inside the 45s
    # silence window; a FALLBACK-resolved record swap that can't trigger the
    # album split), and the love must not land on those.
    closing_track: Optional["TrackMetadata"] = None
    # R7-03 (flip-resume): closing-side global indices inherited from an
    # immediately-prior unarmed session of the SAME release and closing side —
    # populated by ListenTracker when a full play of a MULTI-row closing side is
    # split across sessions by a mid-side silence gap (or a long flip), so the
    # armed session that holds only the tail of the side still credits.  Empty
    # in the normal case; the tracker restricts what it adds to genuine
    # closing-side rows, so a union can never manufacture coverage.  Affects the
    # completion gate only (not the love target, which stays :attr:`closing_track`).
    inherited_side_rows: set[int] = field(default_factory=set)
    # R7-02: True when this session was opened by a genuine #185 replay boundary
    # (the record was physically re-dropped).  Such a session is a real second
    # spin, so its Play Count credit is EXEMPT from the credited-memory guard
    # even if the release was credited moments ago; a session opened by an
    # attribution-swing split (or MUSIC_STARTED) leaves this False and is subject
    # to the guard on BOTH the split and the terminal SESSION_ENDED credit paths.
    opened_by_replay_boundary: bool = False

    @property
    def completion_supported(self) -> bool:
        """#182 / R7-01: is this session's armed completion SUPPORTED by its
        history?

        A Shazam attribution swing can mint a split-off session whose sole
        track latches an owned compilation AND arms ``potential_last_track``
        (compilations routinely close with the hit) — phantom-crediting a
        record that never left its sleeve.  The original #182 gate (Lane,
        2026-08-08) demanded ≥2 distinct tracklist rows of the latched
        release.  R7-01 showed that gate defeated from the mirror direction:
        a compilation sequencing TWO tracks of one owned album with that
        album's closer among them (canonically "Brain Damage" → "Eclipse" on
        *Echoes: The Best of Pink Floyd*, both resolving to *Dark Side of the
        Moon* via the collection tier) satisfies "two rows" while the album
        never left its sleeve.

        The gate is therefore strengthened to SIDE-COVERAGE (Lane,
        2026-08-11, R7-01 — LOCKED): a latched-release completion counts only
        when EVERY vinyl row of the CLOSING SIDE (all rows sharing the
        closer's side letter) was identified this session.  The comp shape
        fails because it covers only 2 of the closing side's rows (side 2 of
        *Dark Side* has five); a genuine straight-through play of the closing
        side covers them all.  This is strictly stronger than the old
        two-rows rule and deliberately accepts the missed-credit cost when
        recognition is weak on the closing side (the codebase's standing
        missed-over-phantom / META-4 posture).

        Two carve-outs preserve prior correct behaviour:

        * **One-row closing side** (a side-long closer such as "Echoes", or a
          genuine single) is fully covered by the closer alone — the R6-07
          single-(vinyl-)row carve-out, now expressed as full coverage of a
          1-row side.  This is also what fixes R7-03 for the *Meddle* shape
          (side B is one row) without needing flip-resume.

        * **Sideless tracklist** (numbered / CD-only — no row parses to a
          vinyl side, so ``side_letter`` is None): "closing side" is
          undefined, so fall back to the pre-R7 evidence rule (≥2 distinct
          rows, or a single playable row) — BUT the closer-identity check
          (R5-05, below) runs FIRST, which is a deliberate behaviour change
          from pre-v1.5.20 (R8-13/#367, RATIFIED Lane 2026-08-12): a sideless
          release whose armed closer is FOREIGN is now suppressed even with
          ≥2 supporting rows, where the old gate credited.  A foreign closer
          arming is itself the attribution-swing signature, so suppression is
          the missed-over-phantom-consistent choice.  (An earlier version of
          this docstring promised sideless behaviour was "unchanged" while
          the gate order change shipped three lines below it — the R8 audit's
          R8-13 finding; see CHANGELOG [1.5.20] correction note.)

        R5-05 is preserved throughout: the closer must be a row OF the latched
        release.  A Shazam swing to a FOREIGN single arms
        ``potential_last_track`` on its own one-row tracklist; without this
        guard that lone row would "cover" a side and phantom-credit the
        multi-track album it was latched to.

        Row identity is the resolved ``side_index.global_index`` (#182
        second-pass): a decorated re-identification of a row ("The Hit - 2011
        Remaster" after "The Hit") resolves to the SAME index and is counted
        once, while genuine sibling variants resolve to different indices.  A
        track whose title resolves to NO row contributes nothing.

        Sessions without a latched release are not this gate's concern
        (returns True; the fallback-metadata branch handles them).  Callers
        log a suppression loudly with :attr:`supporting_row_count` /
        :attr:`closing_side_coverage` for diagnosis.
        """
        if self.album_release_id is None:
            return True
        # R5-05 (preserved): the closer must be a row OF the latched release —
        # a foreign single would otherwise cover its own one-row side and
        # phantom-credit the multi-track album it was latched to.
        closer = self.closing_track
        if closer is None or closer.discogs_release_id != self.album_release_id:
            return False
        side_rows = self._closing_side_row_indices(closer)
        if not side_rows:
            # Sideless (numbered / CD-only) tracklist — no vinyl-side concept
            # exists.  Fall back to the pre-R7 evidence rule (Lane 2026-08-11).
            # R6-07: count PLAYABLE (vinyl) rows, not len(tracklist), so a
            # hybrid LP+CD whose only vinyl side is one side-long piece still
            # reads as the single-row release it plays as.  (Reached only when
            # the closer's tracklist has no side letters at all, so the vinyl
            # count falls through to len().)
            return (
                self.supporting_row_count >= 2
                or _playable_row_count(closer.tracklist) == 1
            )
        # SIDE-COVERAGE: every vinyl row of the closing side must have been
        # identified this session (or inherited from a flip-resumed prior
        # session, R7-03).  A one-row closing side is covered by the closer alone
        # (the single-vinyl-row carve-out, as full coverage).
        covered = self._identified_side_rows(side_rows) | (self.inherited_side_rows & side_rows)
        return side_rows <= covered

    def _closing_side_row_indices(self, closer: "TrackMetadata") -> set[int]:
        """R7-01: the global indices of every VINYL row sharing *closer*'s side
        letter — the full row-set of the closing side.

        Empty set for a numbered / CD-only (sideless) tracklist, whose closer
        has no side letter; the caller reads that as "no side concept, fall
        back to the legacy evidence rule".  Side membership uses the same
        :func:`_match_side` predicate as the completion anchor and
        :func:`_playable_row_count`, so bonus CD/DVD/file rows (which are not
        real vinyl sides) never join a side.
        """
        letter = closer.side_letter
        if letter is None:
            return set()
        return {
            i
            for i, e in enumerate(closer.tracklist)
            if (m := _match_side(e.position)) is not None
            and m.group(1).upper() == letter
        }

    def _identified_side_rows(self, side_rows: set[int]) -> set[int]:
        """R7-01: the subset of *side_rows* actually identified this session for
        the latched release — distinct resolved global indices, restricted to
        the closing side.  An identification that resolves to no row
        (``global_index`` None) or to a row off the closing side contributes
        nothing; a decorated re-identification of a closing-side row collapses
        onto its single index, exactly as :attr:`supporting_row_count` does.
        """
        return {
            t.side_index.global_index
            for t in self.identified_tracks
            if t.discogs_release_id == self.album_release_id
            and t.side_index.global_index in side_rows
        }

    @property
    def closing_side_coverage(self) -> "Optional[tuple[int, int]]":
        """R7-01 diagnostic for the suppression log line: ``(identified, total)``
        vinyl rows of the closing side, or None when the gate does not use
        side-coverage (no latched release, no/foreign closer, or a sideless
        tracklist that falls back to the legacy rule)."""
        if self.album_release_id is None:
            return None
        closer = self.closing_track
        if closer is None or closer.discogs_release_id != self.album_release_id:
            return None
        side_rows = self._closing_side_row_indices(closer)
        if not side_rows:
            return None
        covered = self._identified_side_rows(side_rows) | (self.inherited_side_rows & side_rows)
        return (len(covered), len(side_rows))

    def _distinct_row_count(self, release_id: Optional[int]) -> int:
        """Distinct resolved tracklist rows of *release_id* identified this
        session (0 when *release_id* is None). An identification whose title
        resolves to NO row (global_index None) contributes nothing — it cannot
        vouch for a completed side."""
        if release_id is None:
            return 0
        return len({
            t.side_index.global_index
            for t in self.identified_tracks
            if t.discogs_release_id == release_id
            and t.side_index.global_index is not None
        })

    @property
    def supporting_row_count(self) -> int:
        """#182: distinct resolved tracklist rows of the latched release
        identified this session (0 when no release is latched)."""
        return self._distinct_row_count(self.album_release_id)

    @property
    def love_supported(self) -> bool:
        """R6-06: the gate for the Last.fm love.

        The love has no separate unlatched-skip branch (the Play Count credit
        does — a session with no latched release logs "release not in collection,
        skipping" and never writes), yet it reused :attr:`completion_supported`,
        whose ``album_release_id is None`` escape hatch returns True — so a lone
        DB-tier closer identification (an unowned compilation, or an owned record
        during a degraded blip that never latched) got Loved on session end with
        ZERO supporting rows.

        Latched sessions defer to :attr:`completion_supported` unchanged.  For an
        UNLATCHED session whose closer nonetheless carries a release id and a
        tracklist, require the SAME evidence of a completed side: ≥2 distinct
        resolved rows of the CLOSER's own release, or a genuine single-(vinyl-)row
        release.  A closer with no release id (a pure FALLBACK closer, no
        tracklist to verify against) preserves the prior love-the-closer
        behaviour — there is nothing to count.
        """
        if self.album_release_id is not None:
            return self.completion_supported
        closer = self.closing_track
        if closer is None or closer.discogs_release_id is None:
            return True
        if self._distinct_row_count(closer.discogs_release_id) >= 2:
            return True
        return _playable_row_count(closer.tracklist) == 1

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
            # #181: remember WHICH track armed the flag (the closer) so the
            # Last.fm love can target it even if more tracks are identified
            # afterwards.  The consecutive-dedup early-return above is
            # harmless here: the first occurrence already recorded itself.
            self.closing_track = track
        if track.discogs_release_id:
            self.last_release_id = track.discogs_release_id
            self.last_release_source = track.source          # #184
            self.last_release_resolve_key = track.resolve_key
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
            self.album_resolve_key = track.resolve_key
