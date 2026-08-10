"""Regression tests for #180 (R4:gap1-2) — SideIndex title matching.

The old matcher located the current track by ``e.title.lower().strip() ==
title_key`` — exact equality with no further normalisation.  Shazam's
Apple-Music-backed catalogue systematically decorates titles that vinyl
Discogs tracklists carry plain (`` - 2011 Remastered Version``, ``(Remastered
2009)``, ``(feat. X)``, typographic ``’`` vs ``'``, NFD vs NFC accents, ``&``
vs ``and``), so any decorated closer yielded ``SideIndex.empty``:
``is_last_track`` never armed, and the Play Count, Last Played, AND the
Last.fm love were all silently skipped for a flawlessly identified complete
playthrough — while the side caption blanked and the log blamed the listener
("likely only one side played").

The fix (mirroring #179's philosophy): tier 1 matches on losslessly folded
text (punctuation fold + NFKC + casefold + whitespace collapse, shared helper
in ``src/metadata/normalize.py``); tier 2 retries with a keyword-gated
trailing decoration (parenthetical or dash suffix) stripped from ONE side at a
time, and must identify a UNIQUE folded title across the tracklist — on
ambiguity the conservative ``SideIndex.empty`` failure is kept, so the
META-4/#78 phantom-last-track class cannot resurface.  All three comparison
sites inside ``from_tracklist`` (current row, side ordinal, global index) use
the same matcher, so the positional facts can never desync.
"""
import unicodedata

from src.metadata.models import SideIndex, TracklistEntry


def _tl(*rows):
    return [TracklistEntry(position=p, title=t) for p, t in rows]


DSOTM = _tl(
    ("A1", "Speak to Me"), ("A2", "Breathe (In the Air)"), ("A3", "On the Run"),
    ("B1", "Money"), ("B2", "Us and Them"), ("B3", "Eclipse"),
)


# ---------------------------------------------------------------------------
# The headline failure: a decorated closer must still arm is_last_track.
# ---------------------------------------------------------------------------

def test_remaster_dash_suffix_on_the_closer_arms_is_last_track():
    si = SideIndex.from_tracklist(DSOTM, "Eclipse - 2011 Remastered Version")

    assert si.is_last_track is True
    assert si.track_display == "B3"
    assert si.side_letter == "B"
    assert si.side_position == 3
    assert si.prev_track_title == "Us and Them"


def test_remaster_parenthetical_matches():
    si = SideIndex.from_tracklist(DSOTM, "Eclipse (Remastered 2009)")
    assert si.is_last_track is True


# ---------------------------------------------------------------------------
# The divergence corpus (each drove a real-world miss in the audit repro).
# ---------------------------------------------------------------------------

def _matches(row_title, shazam_title):
    tl = _tl(("A1", "Opener"), ("A2", row_title))
    si = SideIndex.from_tracklist(tl, shazam_title)
    return si.track_display == "A2"


def test_divergence_corpus_matches():
    cases = [
        ("Song", "Song - 2011 Remastered Version"),     # dash remaster
        ("Song", "Song (Remastered 2009)"),             # paren remaster
        ("Song", "Song (Live)"),                        # live decoration
        ("Song", "Song - Live"),                        # dash live
        ("Song", "Song (Mono)"),                        # mono decoration
        ("Song", "Song (feat. Someone)"),               # feat credit
        ("Don't Stand Me Down", "Don’t Stand Me Down"), # typographic apostrophe
        ("Song…", "Song..."),                           # ellipsis forms
        ("Us and Them", "Us & Them"),                   # & vs and
        (unicodedata.normalize("NFD", "Café Song"),
         unicodedata.normalize("NFC", "Café Song")),    # NFD vs NFC accents
        ("SONG", "song  "),                             # case + whitespace
        ("Rock 'N' Roll", "Rock ｀N｀ Roll"),           # NFKC output is a fold key (U+FF40)
        ("Song (2019 Mix)", "Song"),                    # decorated INDEX side
    ]
    misses = [(r, s) for r, s in cases if not _matches(r, s)]
    assert misses == [], f"corpus misses: {misses}"


def test_pt_vs_part_remains_a_conservative_miss():
    """ACCEPTED RESIDUAL: 'Pt. 2' vs 'Part Two' is a rewording, not a
    decoration — no strip applies and equality fails.  Conservative empty."""
    assert not _matches("Song, Pt. 2", "Song, Part Two")


# ---------------------------------------------------------------------------
# Adversarial duplicate-title cases (the fix note's mandated guards).
# ---------------------------------------------------------------------------

def test_exact_decorated_row_wins_over_stripping():
    """When the tracklist itself carries the decorated form, tier 1 matches it
    directly — no stripping, no ambiguity."""
    tl = _tl(("A1", "Song"), ("A2", "Song (Live)"), ("A3", "Closer"))
    si = SideIndex.from_tracklist(tl, "Song (Live)")
    assert si.track_display == "A2"


def test_contested_base_refuses_rather_than_guessing_the_plain_twin():
    """'Song (Remastered)' with rows 'Song' and 'Song (Live)': the plain row
    matches the one-side strip, but 'Song (Live)' shares the same stripped
    base — the base is contested, and guessing the plain twin is forbidden
    (refuse-to-guess).  Conservative empty."""
    tl = _tl(("A1", "Song"), ("A2", "Song (Live)"), ("A3", "Closer"))
    assert SideIndex.from_tracklist(tl, "Song (Remastered)") == SideIndex.empty()


def test_syntax_divergent_decorated_sibling_cannot_arm_a_phantom_last_track():
    """#180 cold-review headline: rows 'Song (Demo)' (bonus track) and a plain
    closer 'Song'; Shazam reports the demo as 'Song - Demo' (dash syntax).
    Neither one-side branch sees the paren-form sibling, so the old logic
    confidently matched the CLOSER and armed a phantom is_last_track — a
    Play Count for an album play that never happened.  The contested-base
    refusal must keep this empty."""
    tl = _tl(
        ("A1", "Song (Demo)"), ("A2", "Filler"),
        ("B1", "Another"), ("B2", "Song"),
    )
    si = SideIndex.from_tracklist(tl, "Song - Demo")
    assert si == SideIndex.empty()
    assert si.is_last_track is False

    tl2 = _tl(("A1", "Song (Remastered 2009)"), ("A2", "Song"))
    assert SideIndex.from_tracklist(tl2, "Song - 2011 Remastered Version") == SideIndex.empty()

    tl3 = _tl(("A1", "Song"), ("A2", "Song (Live)"), ("A3", "Closer"))
    assert SideIndex.from_tracklist(tl3, "Song - Live") == SideIndex.empty()


def test_bracket_and_stacked_siblings_also_contest_the_base():
    """#180 second-pass regression: the contested scan must see decoration
    grammar the single-strip matcher cannot — a bracket-decorated sibling
    ('Song [Demo]') and a stacked sibling ('Song (Demo) (Live)') both conceal
    the same base, and the plain closer must not be phantom-credited."""
    tl = _tl(
        ("A1", "Song [Demo]"), ("A2", "Filler"),
        ("B1", "Another"), ("B2", "Song"),
    )
    si = SideIndex.from_tracklist(tl, "Song - Demo")
    assert si == SideIndex.empty()
    assert si.is_last_track is False

    tl2 = _tl(("A1", "Song (Demo) (Live)"), ("A2", "Song"))
    assert SideIndex.from_tracklist(tl2, "Song - Demo") == SideIndex.empty()


def test_mixed_branch_distinct_titles_are_ambiguous():
    """Uniqueness-by-folded-title is load-bearing even where the contested-base
    check can't see: a stacked-decoration row matches branch 2 while the plain
    row matches branch 1 — two DISTINCT folded titles qualify, and neither
    branch's stripped base exposes the other to the contested check.  Refuse."""
    tl = _tl(("A1", "Song"), ("A2", "Song (Remastered) (Live)"), ("A3", "Closer"))
    assert SideIndex.from_tracklist(tl, "Song (Remastered)") == SideIndex.empty()


def test_uniqueness_guard_holds_when_the_stripped_key_still_carries_decoration():
    """Adversarial pin for the uniqueness guard specifically: a stacked query
    strips (once) to a key that STILL carries decoration, so the contested
    scan's fixpoint base ('song') diverges from the stripped key ('song
    (live)') and cannot arm — only the distinct-folded-title uniqueness rule
    stands between this and guessing the first of two qualifying rows."""
    tl = _tl(
        ("A1", "Song (Live)"),
        ("A2", "Song (Live) (Remastered) (Demo)"),
        ("A3", "Closer"),
    )
    assert SideIndex.from_tracklist(tl, "Song (Live) (Remastered)") == SideIndex.empty()


def test_empty_query_title_matches_nothing():
    """Defence in depth: an empty title (upstream REC-3 should prevent it)
    must not match a decoration-only row via an empty stripped base."""
    tl = _tl(("A1", "(Live)"), ("A2", "Closer"))
    assert SideIndex.from_tracklist(tl, "") == SideIndex.empty()


def test_degenerate_pure_decoration_query_matches_nothing():
    """A query that strips to the empty base must not match a whitespace-only
    row (truthiness guards, mirroring reader.py's #179 equivalents)."""
    tl = _tl(("A1", "   "), ("A2", "Closer"))
    assert SideIndex.from_tracklist(tl, "(Live)") == SideIndex.empty()


def test_bracket_and_stacked_decorations_remain_conservative_misses():
    """ACCEPTED RESIDUAL: square-bracket decorations and stacked double
    decorations are not stripped — conservative empty, never a guess."""
    tl = _tl(("A1", "Song"), ("A2", "Closer"))
    assert SideIndex.from_tracklist(tl, "Song [Live]") == SideIndex.empty()
    assert SideIndex.from_tracklist(tl, "Song (Live) [2011 Remaster]") == SideIndex.empty()


def test_two_decorated_siblings_are_ambiguous_and_refuse():
    """Shazam 'Eclipse' against rows 'Eclipse (Live)' and 'Eclipse (Mono)':
    both strip to 'eclipse' — no unique target, conservative empty (the
    META-4/#78 phantom-last-track guard)."""
    tl = _tl(("A1", "Eclipse (Live)"), ("A2", "Eclipse (Mono)"))
    si = SideIndex.from_tracklist(tl, "Eclipse")
    assert si == SideIndex.empty()


def test_distinct_decorations_are_different_tracks():
    """'Song (Live)' vs a row 'Song (Mono)': one-side-at-a-time stripping
    never equates two distinct decorated forms (#179's cold-review class)."""
    tl = _tl(("A1", "Song (Mono)"), ("A2", "Closer"))
    si = SideIndex.from_tracklist(tl, "Song (Live)")
    assert si == SideIndex.empty()


def test_reprise_first_occurrence_still_wins_under_tier2():
    """B-5 parity in tier 2: the SAME folded title on two rows is one match
    group, resolved to its first occurrence — not an ambiguity refusal."""
    tl = _tl(("A1", "Theme"), ("B1", "Theme"), ("B2", "Closer"))
    si = SideIndex.from_tracklist(tl, "Theme (Remastered)")
    assert si.track_display == "A1"
    assert si.is_last_track is False


def test_phantom_last_track_cannot_arm_via_stripping():
    """A decorated NON-closer must never be mistaken for the closer."""
    tl = _tl(("A1", "Intro"), ("A2", "Closer"))
    si = SideIndex.from_tracklist(tl, "Intro (Live)")
    assert si.track_display == "A1"
    assert si.is_last_track is False


def test_non_decoration_suffixes_are_not_stripped():
    """A dash suffix without a decoration keyword is part of the title —
    'Song - Part 2' is not a decorated 'Song'."""
    tl = _tl(("A1", "Song"), ("A2", "Closer"))
    assert SideIndex.from_tracklist(tl, "Song - Part 2") == SideIndex.empty()

    tl2 = _tl(("A1", "Music for Airports (Section I)"), ("A2", "Closer"))
    assert SideIndex.from_tracklist(tl2, "Music for Airports") == SideIndex.empty()


# ---------------------------------------------------------------------------
# Site synchronisation: the side-ordinal and global-index loops must use the
# SAME matcher as the current-row lookup (fix-note requirement 1).
# ---------------------------------------------------------------------------

def test_positional_facts_stay_coherent_under_tier2_matching():
    """A decorated query must produce the full, consistent positional picture
    — side ordinal, global index, neighbours — not just a matched title."""
    tl = _tl(
        ("A1", "One"), ("A2", "Two"),
        ("B1", "Three"), ("B2", "Four"), ("B3", "Five"),
    )
    si = SideIndex.from_tracklist(tl, "Four (Remastered)")

    assert si.track_display == "B2"
    assert si.side_letter == "B"
    assert si.side_position == 2
    assert si.side_total == 3
    assert si.is_last_track is False
    assert si.prev_track_title == "Three"
    assert si.next_track_title == "Five"


def test_out_of_order_side_rows_rank_correctly_under_tier2():
    """META-8 parity: rows listed out of sequence still rank by track number
    when matched via tier 2."""
    tl = _tl(("A2", "Two"), ("A1", "One"))
    si = SideIndex.from_tracklist(tl, "One (Live)")
    assert si.side_position == 1
    assert si.side_total == 2


def test_side_position_follows_the_display_row_for_a_duplicate_title_out_of_order():
    # #224: a duplicated title on one side with out-of-order rows. track_display
    # is the row-order first match ("A2"); side_position must be THAT row's
    # number-order ordinal (2), not the other "Theme" row's (1) — otherwise the
    # caption reads an incoherent "A2 · 01 OF 02".
    tl = _tl(("A2", "Theme"), ("A1", "Theme"))
    si = SideIndex.from_tracklist(tl, "Theme")
    assert si.track_display == "A2"
    assert si.side_position == 2
    assert si.side_total == 2

