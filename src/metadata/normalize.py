"""Shared text normalisation for matching Shazam strings against Discogs
strings (#180).

Shazam's Apple-Music-backed catalogue and Discogs' community-edited entries
render the same title differently in two independent ways:

1. **Rendering variants** — typographic vs ASCII punctuation (``’`` vs ``'``),
   NFD vs NFC accents, ``&`` vs ``and``, casing, stray whitespace.  These are
   lossless to fold: :func:`fold_text` collapses them so exact comparison
   works.
2. **Decoration** — a trailing parenthetical or dash suffix naming an edition
   rather than a work (``- 2011 Remastered Version``, ``(Live)``,
   ``(feat. X)``).  Stripping is LOSSY (``Song (Live)`` and ``Song`` may be
   different rows on one release), so :func:`strip_title_decoration` is
   keyword-gated and callers must apply it ONE SIDE AT A TIME and require a
   unique match — the #179/#180 refuse-to-guess discipline.

``RecognitionLoop._same_track`` deliberately does NOT use these helpers: it
compares Shazam output to Shazam output, where both sides carry identical
decoration, and folding there would only widen its dedup semantics (#180 fix
note, point 3).
"""

import re
import unicodedata

# NFKC does NOT fold typographic punctuation to ASCII (U+2019 "’" stays
# U+2019), and some characters' NFKC *outputs* are themselves table keys
# (fullwidth grave U+FF40 → U+0060, ``ŉ`` → ``'n``), so the table runs on both
# sides of NFKC (see fold_text; the #179 reviews established both directions).
# ``&`` folds to ``and`` because the two are interchangeable renderings in
# titles ("Us and Them" vs "Us & Them"); the surrounding spaces keep "R&B" →
# "r and b" symmetrical on both fold sides.
_PUNCT_FOLD = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "`": "'", "´": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    "&": " and ",
})

# Decoration keywords for the LOSSY strip (#180 tier 2).  A trailing
# parenthetical or dash suffix qualifies as decoration only when it contains
# one of these — "Song - Part 2" and "(Section I)" are title content, not
# decoration, and must never be stripped.  Matched against FOLDED text, so the
# patterns are lowercase ASCII.
#
# The CORE lexical vocabulary is shared verbatim by the track-level strip (#180)
# and the album-level collection matcher (#222) so the two cannot drift (#225).
_DECORATION_LEXICAL = (
    r"\b(?:remaster(?:ed)?|live|mono|stereo|featuring|version|edit|mix|"
    r"remix|demo|bonus|deluxe|single|acoustic|instrumental|sessions?)\b|\bfeat\.?"
)

# Album-ONLY edition vocabulary (#222).  Deliberately NOT added to the track-
# level regex below: #222 is scoped to the collection matcher, and the #180
# track matcher (its 19-mutant campaign) must not shift behaviour.  These words
# name whole-album editions ("Deluxe Edition", "30th Anniversary", "Expanded",
# "2011 Reissue") that Shazam/iTunes append; they are the album grammar #222's
# gate must let through while still rejecting title distinguishers.
_ALBUM_EXTRA = r"\b(?:edition|expanded|anniversary|reissue)\b"

# A bare 4-digit year is decoration at the TRACK level (a re-recording / version
# year — "Money (1975)") but a genuine DISTINGUISHER at the ALBUM level (a
# live-album pressing date — "Live (1975)" vs "Live (1980)").  So the two levels
# share the core vocabulary above but compose DIFFERENT year policies (#222):
# the track keeps the year as decoration, the album keeps it as a distinguisher.
_BARE_YEAR = r"\b(?:19|20)\d{2}\b"

# Track level (#180 — SideIndex): CORE lexical keywords OR a bare year.  This is
# byte-for-byte the pre-#222 track behaviour (no album-extra words, year kept).
_DECORATION_WORD_RE = re.compile(f"(?:{_DECORATION_LEXICAL}|{_BARE_YEAR})")

# Album level (#222 — collection matcher): core + album-edition words, NO bare
# year.  Because a bare-year parenthetical stays a distinguisher, a decorated
# query ("Live (1975)") can no longer collapse onto a plain-titled owned family
# member — the #179 residual documented at reader.py's tier 2.
_ALBUM_DECORATION_WORD_RE = re.compile(f"(?:{_DECORATION_LEXICAL}|{_ALBUM_EXTRA})")

# Anchored, single, innermost-last: interior parentheticals are part of the
# title ("(What's the Story) Morning Glory?" is untouched), and only the LAST
# dash segment is a candidate suffix ("Money - It's a Gas - Live" strips
# " - Live", keeping the interior dash).
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_TRAILING_DASH_RE = re.compile(r"\s-\s([^-]*)$")


def fold_text(s: str) -> str:
    """Losslessly fold rendering variants for exact comparison (#180 tier 1).

    Punctuation-fold → NFKC → punctuation-fold again → casefold → collapse
    whitespace.  The pre-NFKC pass catches table inputs NFKC would decompose
    first (``´`` becomes space + combining acute); the post-NFKC pass catches
    characters whose NFKC output is a table key (fullwidth grave).  The
    table's ASCII outputs are NFKC-stable, so the second pass cannot regress
    the first.
    """
    s = unicodedata.normalize("NFKC", s.translate(_PUNCT_FOLD)).translate(_PUNCT_FOLD)
    # R5-17: drop Unicode format (Cf) characters that carry no matching intent —
    # zero-width space (U+200B), soft hyphen (U+00AD), zero-width no-break space /
    # BOM (U+FEFF), word joiner (U+2060), the bidi marks, etc.  NFKC does NOT
    # remove them, so a single invisible character in a community-edited Discogs
    # title or a Shazam string made an owned album permanently unmatchable — and,
    # being invisible, undiagnosable from a log where both sides print
    # identically.  This WIDENS folding (it can merge two strings that differed
    # ONLY by such a char) — acceptable because these characters are accidental
    # copy-paste contaminants with no lexical meaning in this domain.  ZWNJ
    # (U+200C) and ZWJ (U+200D) are the exception: they ARE lexically
    # load-bearing in Persian/Arabic/Indic scripts (they can distinguish
    # different words), so they are KEPT — folding them away could merge two
    # genuinely different titles into a wrong match (the phantom-credit direction
    # this core refuses).  Keeping them means a title contaminated with a ZWNJ
    # still misses — the fail-safe (missed-credit) direction (cold-review LOW).
    _KEEP = {"\u200c", "\u200d"}
    s = "".join(c for c in s if c in _KEEP or unicodedata.category(c) != "Cf")
    return " ".join(s.casefold().split())


_TRAILING_BRACKET_RE = re.compile(r"\s*\[([^\[\]]*)\]\s*$")


def decoration_base(folded: str) -> str:
    """CONTESTED-SCAN-ONLY base: strip keyword-gated trailing decorations to a
    fixpoint, including square-bracket forms (#180 second pass).

    Deliberately more aggressive than :func:`strip_title_decoration` (which
    strips exactly one paren/dash form and is used for MATCHING): this helper
    is used only to decide whether a tier-2 stripped base is CONTESTED by
    another tracklist row, i.e. to REFUSE a match.  Extra aggression here can
    only add refusals — never a wrong match — so bracket-decorated
    ("Song [Demo]") and stacked ("Song (Demo) (Live)") siblings contest the
    base their decoration conceals from the single-strip matcher.
    """
    prev = None
    while prev != folded:
        prev = folded
        for pattern in (_TRAILING_PAREN_RE, _TRAILING_BRACKET_RE, _TRAILING_DASH_RE):
            m = pattern.search(folded)
            if m and _DECORATION_WORD_RE.search(m.group(1)):
                folded = folded[: m.start()].rstrip()
                break
    return folded


def strip_title_decoration(folded: str) -> str:
    """Strip ONE trailing decoration from an already-folded title (#180 tier 2).

    Returns *folded* unchanged when no keyword-gated trailing parenthetical or
    dash suffix is present.  LOSSY — see the module docstring for the
    one-side-at-a-time + unique-match rules callers must enforce.
    """
    m = _TRAILING_PAREN_RE.search(folded)
    if m and _DECORATION_WORD_RE.search(m.group(1)):
        return folded[: m.start()].rstrip()
    m = _TRAILING_DASH_RE.search(folded)
    if m and _DECORATION_WORD_RE.search(m.group(1)):
        return folded[: m.start()].rstrip()
    return folded


def strip_album_decoration(folded: str) -> str:
    """Strip ONE trailing paren- OR bracket-decoration from a folded ALBUM
    title, gated on a decoration keyword and EXCLUDING a bare year (#222).

    Distinct from :func:`strip_title_decoration` in two deliberate ways the
    collection matcher (reader.py) needs and the track-level matcher does not:

    * **Bracket AND dash forms.** Shazam/iTunes render album decoration in
      brackets ("Rumours [Deluxe Edition]", "Nevermind [30th Anniversary]") and,
      for singles/EPs, in the trailing dash form ("Blinding Lights - Single") —
      "single" is already in the vocabulary, so the dash candidate is stripped
      exactly like the paren/bracket ones (R6-14). Without the dash pattern an
      owned 7"/12" was never matched by its "- Single" query — a permanent missed
      collection match for a whole record class. (The track-level strip handles
      paren/dash; this one adds bracket + dash on top of the album vocabulary.)
    * **Bare-year exclusion.** A trailing "(1975)" on an ALBUM is a pressing
      distinguisher, not decoration (see ``_ALBUM_DECORATION_WORD_RE``), so it
      is never stripped — otherwise a decorated query collapses onto a plain
      owned family member.

    Operates on already-folded text, so NFKC has already mapped fullwidth parens
    "（）"→"()" — closing #222's fullwidth wrong-miss for free.  LOSSY: the caller
    must apply it ONE SIDE AT A TIME and require a unique match (the #179/#180
    refuse-to-guess discipline).  Returns *folded* unchanged when no
    keyword-gated trailing paren/bracket/dash suffix is present.  Note "- EP" is
    NOT stripped: "ep" is deliberately absent from the vocabulary (an album can
    legitimately be titled "... EP"), so only "- Single" and other keyworded dash
    forms strip.
    """
    for pattern in (_TRAILING_PAREN_RE, _TRAILING_BRACKET_RE, _TRAILING_DASH_RE):
        m = pattern.search(folded)
        if m and _ALBUM_DECORATION_WORD_RE.search(m.group(1)):
            return folded[: m.start()].rstrip()
    return folded
