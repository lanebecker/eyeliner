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
_DECORATION_WORD_RE = re.compile(
    r"(?:\b(?:remaster(?:ed)?|live|mono|stereo|featuring|version|edit|mix|"
    r"remix|demo|bonus|deluxe|single|acoustic|instrumental|sessions?|"
    r"(?:19|20)\d{2})\b|\bfeat\.?)"
)

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
