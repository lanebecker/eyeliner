"""Text rendering & typography for the display (ARCH-3 — TextRenderer).

Extracted from ``renderer.py`` so the pure text-layout logic — font loading,
letter-spaced label rendering, greedy word-wrap, shrink-to-fit sizing,
ellipsize, and wrapped-text draw/measure — can be unit-tested WITHOUT a
pygame-initialised ``DisplayRenderer`` (the ARCH-3 concern: this logic shares no
state with the render loop yet could previously only be reached through it).

``DisplayRenderer`` composes one ``TextRenderer`` and delegates to it via thin
shims, so its public/private method surface is unchanged.  The font and label
LRU caches are OWNED by the renderer and injected here, so cache bounds and
eviction behaviour are identical to before and the renderer's cache tests keep
passing.

pygame is imported lazily inside the methods that need it, so the module stays
importable on machines without SDL.
"""

import logging
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

# DISP-5 / R7-07: per-glyph letter-spacing (see TextRenderer.render_tracked) is
# visually correct for any script whose glyphs are INDEPENDENT and laid out
# left-to-right — Latin (incl. precomposed diacritics), Greek, Cyrillic, CJK,
# digits, punctuation, symbols, arrows.  It is WRONG only for text that needs
# complex shaping: contextual joining (Arabic), reordering / stacked conjuncts
# (Indic, Thai, …), floating combining marks, right-to-left runs, or
# ZWJ / variation-selector sequences (emoji).  The original DISP-5 gate used
# ``not text.isascii()``, which ALSO caught the renderer's own label glyphs — the
# mid-dot U+00B7 in "SIDE A · 04 OF 06", the ellipsis U+2026 in "STILL
# LISTENING…", the arrows U+2190/2192 in "← PREV" / "NEXT →" — and silently
# dropped their DESIGNED tracking on every frame (R7-07).  ``_needs_shaping``
# shapes ONLY genuinely complex text, so those labels keep their letter-spacing.

# Codepoint ranges of complex-shaping scripts, for the case of a character that
# is not itself combining/RTL yet belongs to a script that shapes (e.g. a base
# Indic consonant that forms a conjunct with a following virama).
_COMPLEX_SHAPING_RANGES = (
    (0x0590, 0x05FF),    # Hebrew
    (0x0600, 0x06FF),    # Arabic
    (0x0700, 0x074F),    # Syriac
    (0x0750, 0x077F),    # Arabic Supplement
    (0x0780, 0x07BF),    # Thaana
    (0x07C0, 0x07FF),    # NKo
    (0x0900, 0x0DFF),    # Indic block span: Devanagari … Sinhala
    (0x0E00, 0x0E7F),    # Thai
    (0x0E80, 0x0EFF),    # Lao
    (0x0F00, 0x0FFF),    # Tibetan
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0xFB1D, 0xFDFF),    # Hebrew + Arabic presentation forms-A
    (0xFE70, 0xFEFF),    # Arabic presentation forms-B
    (0x1F000, 0x1FAFF),  # emoji & pictographs (ZWJ sequences, VS16 bases)
)


def _needs_shaping(text: str) -> bool:
    """True when *text* must be rendered as ONE shaped run rather than per-glyph
    with manual tracking (DISP-5 / R7-07 — see the note above).

    The complex-script coverage is a curated allow-list, NOT exhaustive: a few
    shaping scripts that carry combining-class 0 and bidi class "L" and are absent
    from ``_COMPLEX_SHAPING_RANGES`` — decomposed (NFD) conjoining Hangul jamo,
    Mongolian, and some Brahmic scripts beyond the Indic span — fall through to
    the tracked path and would render per-glyph.  Accepted: Discogs metadata is
    effectively always NFC (precomposed Hangul U+AC00, which correctly needs no
    shaping), and these scripts are near-absent from vinyl catalog data; add a
    range if one ever shows up mis-spaced on the display.
    """
    for ch in text:
        o = ord(ch)
        if o < 0x0300:
            # Basic Latin + Latin-1 Supplement: all spacing glyphs, no shaping —
            # includes the label mid-dot U+00B7 and precomposed diacritics (é, ñ).
            continue
        if o in (0x200C, 0x200D):                      # ZWNJ / ZWJ (joiners)
            return True
        if 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:  # variation selectors
            return True
        if unicodedata.combining(ch):                  # floating marks / viramas
            return True
        if unicodedata.bidirectional(ch) in ("R", "AL", "AN"):  # RTL runs
            return True
        for lo, hi in _COMPLEX_SHAPING_RANGES:
            if lo <= o <= hi:
                return True
    return False

_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_FONT_FILES = {
    "display": "InterTight-SemiBold.ttf",
    "text": "InterTight-Medium.ttf",
    "title": "Newsreader-Italic.ttf",
    "mono": "JetBrainsMono-Regular.ttf",
}
# SysFont fallbacks if a bundled file is missing (name, bold, italic).
_SYSFONT_FALLBACKS = {
    "display": ("dejavu sans", True, False),
    "text": ("dejavu sans", False, False),
    "title": ("dejavu sans", False, True),
    "mono": ("dejavu sans mono", False, False),
}

# ---------------------------------------------------------------------------
# R8-03 (#352): per-run script fallback.
#
# The bundled faces cover Latin (+ precomposed diacritics); Inter Tight and
# JetBrains Mono also cover Cyrillic/Greek, but Newsreader-Italic does NOT —
# and none of the four covers CJK, Arabic, Hebrew or emoji.  pygame.font.Font
# renders ONE file with no fallback chain, so a Japanese pressing (坂本龍一)
# used to render every role as .notdef tofu boxes, and a Кино record rendered
# the artist fine but the album title as boxes (R8-03).
#
# Fix (Lane, 2026-08-12, mockup-approved): bundle Noto Sans JP — whose coverage
# includes CJK AND Cyrillic/Greek/extended Latin — in the three role weights,
# and render any run the primary face doesn't cover with the role's fallback
# face, upright (CJK convention; no faux-oblique for the album role).
# Coverage is read from each face's cmap ONCE via fontTools (pure-Python,
# startup-only); if fontTools or the fallback files are absent the composite
# degrades to exactly the old single-face behavior, with one WARNING.
#
# Arabic/Hebrew remain uncovered by choice (rare in vinyl metadata; the
# _needs_shaping gate above already routes them to a single shaped run, which
# would need a shaping engine AND a face — revisit if a pressing shows up).
_FALLBACK_DIR = _FONT_DIR / "fallback"
_FALLBACK_FONT_FILES = {
    "display": "NotoSansJP-SemiBold.ttf",   # hero — matches Inter Tight SemiBold
    "text": "NotoSansJP-Medium.ttf",        # artist — matches Inter Tight Medium
    "title": "NotoSansJP-Regular.ttf",      # album — UPRIGHT (no CJK italic)
    "mono": "NotoSansJP-Regular.ttf",       # catalog/labels — Regular
}

# Per-file codepoint coverage sets, loaded lazily via fontTools.  None is the
# "unknown" sentinel (fontTools missing / file unreadable): the composite then
# treats everything as primary-covered — the pre-R8-03 behavior.
_COVERAGE_CACHE: dict = {}
_coverage_warned = False


def _font_coverage(path) -> "set | None":
    """Codepoints covered by the font file at *path*, or None if unknowable.

    Cached per path.  Uses fontTools' best cmap (startup-only, pure Python);
    a missing file, a parse failure, or an absent fontTools all degrade to
    None so rendering falls back to the old single-face path rather than
    failing the display.
    """
    global _coverage_warned
    key = str(path)
    if key in _COVERAGE_CACHE:
        return _COVERAGE_CACHE[key]
    coverage = None
    try:
        from fontTools.ttLib import TTFont
        with TTFont(key, lazy=True) as tt:
            coverage = set(tt.getBestCmap().keys())
    except Exception as e:
        if not _coverage_warned:
            log.warning(
                "R8-03 font-fallback coverage unavailable (%s: %s) — non-Latin "
                "metadata may render as boxes (single-face rendering).",
                Path(key).name, e,
            )
            _coverage_warned = True
    _COVERAGE_CACHE[key] = coverage
    return coverage


class _CompositeFont:
    """A pygame-Font-compatible facade that renders per-run script fallback.

    Wraps a *primary* pygame Font and an optional *fallback* Font.  Exposes the
    exact surface of the pygame.font.Font API this codebase uses — ``render``,
    ``size``, ``get_height``, ``get_ascent``, ``get_descent`` — so every call
    site (wrap/fit/tracked/ellipsize/chips) gains fallback without changes.

    Run rule: a character renders with the FALLBACK only when the primary's
    cmap lacks it AND the fallback's cmap has it; everything else stays primary
    (so text neither face covers renders primary .notdef exactly as before, and
    an unknown coverage set — fontTools missing — degrades to all-primary).

    Metrics: ``get_height``/``get_ascent``/``get_descent`` report the PRIMARY's
    metrics — they are layout constants (line advance, baseline) and must not
    jump when a CJK run appears.  Mixed-run surfaces are baseline-aligned (each
    run blitted at ``max_ascent - run_ascent``) and may exceed get_height by a
    few pixels; callers already tolerate that (glyph surfaces are blitted, not
    packed).  ``size``/``render`` agree exactly by construction (same run
    arithmetic).
    """

    def __init__(self, primary, fallback, primary_coverage, fallback_coverage):
        self._primary = primary
        self._fallback = fallback
        self._pcov = primary_coverage
        self._fcov = fallback_coverage

    # -- run splitting ------------------------------------------------------

    def _use_fallback(self, ch: str) -> bool:
        if self._fallback is None or self._pcov is None or self._fcov is None:
            return False
        o = ord(ch)
        return o not in self._pcov and o in self._fcov

    def _runs(self, text: str):
        """Yield (font, chunk) runs.  Fast path: all-primary → one run."""
        if self._fallback is None or not text:
            yield (self._primary, text)
            return
        if text.isascii():
            yield (self._primary, text)
            return
        import unicodedata
        out = []
        for ch in text:
            fb = self._use_fallback(ch)
            # Cold-review F5: a COMBINING mark must stay attached to its base's
            # run when that run's face covers it — otherwise "Кино́"-style text
            # splits the mark into its own run and it renders detached as a
            # spacing glyph.  (U+0301 is in most Latin faces' cmaps, so the
            # plain "primary has it" rule pulled it out of the fallback run.)
            if out and unicodedata.combining(ch):
                prev_fb = out[-1][0]
                prev_cov = self._fcov if prev_fb else self._pcov
                if prev_cov is not None and ord(ch) in prev_cov:
                    out[-1][1] += ch
                    continue
            if out and out[-1][0] == fb:
                out[-1][1] += ch
            else:
                out.append([fb, ch])
        for fb, chunk in out:
            yield (self._fallback if fb else self._primary, chunk)

    def _char_font(self, ch: str):
        """The concrete pygame Font this composite would render *ch* with —
        for callers doing their own per-glyph layout (render_tracked's
        baseline alignment)."""
        return self._fallback if self._use_fallback(ch) else self._primary

    # -- pygame.font.Font API ----------------------------------------------

    def render(self, text: str, antialias: bool, color, background=None):
        import pygame

        runs = list(self._runs(text))
        if len(runs) == 1:
            font, chunk = runs[0]
            if background is not None:
                return font.render(chunk, antialias, color, background)
            return font.render(chunk, antialias, color)
        surfs = [(font, font.render(chunk, antialias, color)) for font, chunk in runs]
        max_ascent = max(font.get_ascent() for font, _ in surfs)
        height = max(
            max_ascent - font.get_ascent() + s.get_height() for font, s in surfs
        )
        width = sum(s.get_width() for _, s in surfs)
        out = pygame.Surface((max(1, width), max(1, height)), pygame.SRCALPHA)
        if background is not None:
            # Cold-review F4: honour the facade contract — a caller passing a
            # background color must get it on the multi-run path too (no
            # current caller does; latent-consistency fix).
            out.fill(background)
        x = 0
        for font, s in surfs:
            out.blit(s, (x, max_ascent - font.get_ascent()))
            x += s.get_width()
        return out

    def size(self, text: str):
        runs = list(self._runs(text))
        if len(runs) == 1:
            return runs[0][0].size(runs[0][1])
        width = sum(font.size(chunk)[0] for font, chunk in runs)
        height = max(font.size(chunk)[1] for font, chunk in runs)
        return (width, height)

    def get_height(self):
        return self._primary.get_height()

    def get_ascent(self):
        return self._primary.get_ascent()

    def get_descent(self):
        return self._primary.get_descent()

    def get_linesize(self):
        return self._primary.get_linesize()


class TextRenderer:
    """Font loading + text layout over two bounded LRU caches.

    The caches (``font_cache`` keyed by ``(role, size)`` → Font; ``label_cache``
    keyed by ``(text, size, color, tracking)`` → Surface) are owned by
    ``DisplayRenderer`` and passed in, so the renderer's existing cache-bound
    tests and ``__new__`` skeletons keep working and the bounds are unchanged;
    ``TextRenderer`` only reads/writes them.
    """

    def __init__(self, font_cache, label_cache):
        self._font_cache = font_cache
        self._label_cache = label_cache

    # R8-03: warn once (not per role×size) when the fallback faces are absent.
    _fallback_missing_warned = False

    def font(self, role: str, size: int):
        """Return the bundled font for a role at a pixel size, cached.

        Roles map to the DESIGN.md type hierarchy (see ``_FONT_FILES``).  Loading
        is lazy and cached per (role, size) in a bounded LRU (the renderer's
        ``_FONT_CACHE_MAX``); eviction is rare because the working set is small (a
        fixed handful of sizes).  Falls back to the DejaVu SysFont family if the
        bundled file is missing, so dev machines and CI without the assets still
        render.

        R8-03 (#352): the returned object is a :class:`_CompositeFont` wrapping
        the primary face plus the role's script-fallback face (Noto Sans JP in
        the matching weight), so every text path — wrap, fit, tracked,
        ellipsize, chips — renders non-Latin runs with real glyphs instead of
        .notdef boxes.  If the fallback file (or fontTools) is unavailable the
        composite degrades to single-face behavior identical to pre-R8-03,
        with one WARNING for the whole process.
        """
        import pygame

        key = (role, size)
        font = self._font_cache.get(key)
        if font is not None:
            return font

        path = _FONT_DIR / _FONT_FILES[role]
        try:
            primary = pygame.font.Font(str(path), size)
            primary_cov = _font_coverage(path)
        except (FileNotFoundError, OSError, pygame.error):
            name, bold, italic = _SYSFONT_FALLBACKS[role]
            primary = pygame.font.SysFont(name, size, bold=bold, italic=italic)
            primary_cov = None   # SysFont path unknown — degrade to single-face
            log.warning(f"Bundled font missing ({path.name}); using SysFont fallback")

        fallback = None
        fallback_cov = None
        fb_path = _FALLBACK_DIR / _FALLBACK_FONT_FILES[role]
        try:
            fallback = pygame.font.Font(str(fb_path), size)
            fallback_cov = _font_coverage(fb_path)
        except (FileNotFoundError, OSError, pygame.error):
            if not TextRenderer._fallback_missing_warned:
                log.warning(
                    "R8-03 script-fallback font missing (%s) — CJK/Cyrillic/Greek "
                    "metadata the primary faces don't cover will render as boxes. "
                    "See src/display/assets/fonts/fallback/README.md.",
                    fb_path.name,
                )
                TextRenderer._fallback_missing_warned = True
            fallback = None

        font = _CompositeFont(primary, fallback, primary_cov, fallback_cov)
        self._font_cache.put(key, font)
        return font

    def render_tracked(self, text: str, size: int, color: tuple, tracking: float):
        """Render a mono label with letter-spacing, returning a Surface.

        pygame/SDL_ttf has no tracking support, so each character is rendered
        individually and blitted with an extra advance of (tracking × size)
        pixels — the same arithmetic as CSS letter-spacing in em.  Surfaces
        are cached (labels are small and mostly static per track).
        """
        import pygame

        key = (text, size, color, tracking)
        cached = self._label_cache.get(key)
        if cached is not None:
            return cached

        font = self.font("mono", size)

        # DISP-5 / R7-07: per-glyph tracking assumes every codepoint is an
        # independent glyph — true for Latin/CJK/punctuation/arrows (including the
        # app's own ·, …, ←, → labels), but it destroys shaping for complex
        # scripts (Arabic joining, Devanagari conjuncts, floating combining marks,
        # emoji ZWJ) that arrive in free-text Discogs fields.  Only such text is
        # rendered as ONE shaped run (skipping the manual tracking) rather than
        # reversed/mis-spaced a glyph at a time; everything else — the designed
        # labels included — keeps its letter-spacing below.  (The pre-R7-07 gate
        # was ``not text.isascii()``, which wrongly shaped the app's own labels.)
        if _needs_shaping(text):
            surf = font.render(text, True, color)
            self._label_cache.put(key, surf)
            return surf

        extra = tracking * size
        # R8-03: per-glyph baseline alignment.  With script fallback a label can
        # mix faces (e.g. an ASCII catalog number + a CJK label name); each
        # glyph is blitted at (max_ascent − its face's ascent) so baselines
        # meet.  For an all-primary label this reduces exactly to the old
        # y=0 / font-height layout (uniform ascent).  `_char_font` is the
        # composite's per-char face; a plain injected Font (tests) has no
        # such method and gets the uniform path.
        char_font = getattr(font, "_char_font", None)
        glyphs = []
        for ch in text:
            g = font.render(ch, True, color)
            asc = char_font(ch).get_ascent() if char_font else font.get_ascent()
            glyphs.append((g, asc))
        max_ascent = max((asc for _, asc in glyphs), default=font.get_ascent())
        height = max(
            [max_ascent - asc + g.get_height() for g, asc in glyphs]
            + [font.get_height()]
        )
        # Total width: glyph advances + tracking between characters (CSS adds
        # tracking after every glyph including the last; trim it for cleaner
        # right-alignment).
        width = int(sum(g.get_width() for g, _ in glyphs) + extra * max(0, len(glyphs) - 1))
        surf = pygame.Surface((max(1, width), height), pygame.SRCALPHA)
        x = 0.0
        for g, asc in glyphs:
            surf.blit(g, (int(x), max_ascent - asc))
            x += g.get_width() + extra
        self._label_cache.put(key, surf)
        return surf

    def render_tracked_ellipsized(
        self, text: str, size: int, color: tuple, tracking: float, max_width: int
    ):
        """R7-09: render a tracked mono label, trimming it with a trailing … so the
        LETTER-SPACED width fits *max_width* — instead of hard-clipping mid-glyph.

        The catalog footer (`year · label · catalog`) is drawn tracked and can
        exceed its column; the old code blitted it with a width clip, cutting the
        last glyph in half.  Ellipsizing must measure the REAL tracked width (each
        glyph carries ~``tracking·size`` of extra advance), not the plain font
        width, or the trimmed string would still overflow — so this binary-searches
        on ``render_tracked`` output (cached) rather than ``font.size``.

        Degenerate case: if *max_width* is narrower than the ellipsis itself,
        nothing sensible fits, so the bare "…" surface is returned even though it
        exceeds *max_width* (the caller's area clip is the backstop; unreachable at
        the 1024×600 footer column).

        R8-23 (#355) — documented limitations for shaped/RTL text (data-field
        content only; all backstopped by the caller's area clip):
          * the appended "…" lands at the LOGICAL end of the string, which for
            an RTL run is the visual LEFT — unconventional but unambiguous;
          * truncating Arabic mid-word changes joining forms at the cut point
            (the last kept letter re-shapes to final/isolated form);
          * the binary search assumes rendered width is monotonic in prefix
            length — true for the tracked per-glyph path, not guaranteed for a
            shaped run (contextual forms can shrink as text grows), so a shaped
            candidate may land a few px under the optimum (never over: the
            final candidate's width was measured, not predicted).
        Moot in practice until shaped scripts get a fallback face with real
        coverage (deferred with Arabic/Hebrew under R8-03).
        """
        surf = self.render_tracked(text, size, color, tracking)
        if surf.get_width() <= max_width:
            return surf
        ell = "…"
        ell_surf = self.render_tracked(ell, size, color, tracking)
        if ell_surf.get_width() > max_width:
            return ell_surf  # degenerate column — nothing sensible fits
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            cand = self.render_tracked(text[:mid].rstrip() + ell, size, color, tracking)
            if cand.get_width() <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return self.render_tracked(text[:lo].rstrip() + ell, size, color, tracking)

    @staticmethod
    def break_long_token(token: str, font, max_width: int) -> list:
        """Character-break a single token too wide for max_width into chunks that
        each fit (DISP-7).

        Greedy: accumulate characters until the next would overflow, then start a
        new chunk.  A single glyph wider than max_width is emitted alone (it can't
        be broken further; the blit clip in draw_wrapped_text is the backstop).
        """
        chunks = []
        current = ""
        for ch in token:
            if current and font.size(current + ch)[0] > max_width:
                chunks.append(current)
                current = ch
            else:
                current += ch
        if current:
            chunks.append(current)
        return chunks

    def wrap_lines(self, text: str, font, max_width: int) -> list:
        """Greedy word-wrap; the single source of truth for line breaking.

        Used by both measurement and drawing so they can never disagree
        (previously duplicated in draw_wrapped_text/measure_wrapped_text).  A
        token wider than max_width on its own is character-broken (DISP-7) rather
        than emitted whole, which used to run off the right edge of the column.
        """
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            if font.size(test)[0] <= max_width:
                current = test
                continue
            # `test` overflows: flush the line in progress, then place `word`.
            if current:
                lines.append(current)
                current = ""
            if font.size(word)[0] <= max_width:
                current = word
            else:
                # A run-on wider than the column: hard-break it.  Full chunks
                # become their own lines; the trailing partial stays `current`
                # so the next word can continue on the same line (greedy).
                chunks = self.break_long_token(word, font, max_width)
                lines.extend(chunks[:-1])
                current = chunks[-1] if chunks else ""
        if current:
            lines.append(current)
        return lines

    def fit_wrapped(
        self, text: str, role: str, base_size: int, max_width: int,
        max_lines: int, min_size: int = 14, step: int = 2,
    ) -> tuple:
        """Find the largest font size ≤ base_size at which *text* wraps into
        ≤ max_lines within max_width.  Returns (size, lines).

        This is the shrink-instead-of-ellipsis behavior (product decision):
        long artist names and album titles reduce in size rather than
        truncate.  If even min_size can't fit the line count, returns the
        min_size wrap (caller clips — practically unreachable for real
        metadata).
        """
        size = base_size
        while size >= min_size:
            lines = self.wrap_lines(text, self.font(role, size), max_width)
            if len(lines) <= max_lines:
                return size, lines
            size -= step
        return min_size, self.wrap_lines(text, self.font(role, min_size), max_width)

    def ellipsize(self, text: str, font, max_width: int) -> str:
        """Trim *text* with a trailing ellipsis to fit max_width.

        Only used by the PREV/NEXT adjacent panel — everywhere else the
        design translation shrinks instead (see fit_wrapped).
        """
        if font.size(text)[0] <= max_width:
            return text
        ell = "…"
        # R5-38: if even the ellipsis alone overflows the box, return "" rather
        # than a "…" wider than max_width (the caller shows nothing instead of an
        # overhang). Unreachable at 1024×600 where the PREV/NEXT panel is wide,
        # but the correct backstop at degenerate widths.
        if font.size(ell)[0] > max_width:
            return ""
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.size(text[:mid].rstrip() + ell)[0] <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ell

    def draw_wrapped_text(
        self, target, text: str, role: str, size: int, rect, color: tuple,
        line_height: float = 0.98,
    ) -> int:
        """Render text with word-wrapping to fit within rect.w, clipped to rect.h.

        line_height is the CSS-style multiplier from DESIGN.md §3 (hero 0.98,
        artist 1.04, album 1.12).  Returns the actual rendered height in
        pixels (distance from rect.y to the bottom of the last drawn line);
        0 if nothing was drawn.
        """
        import pygame

        font = self.font(role, size)
        if not text:
            return 0

        y = rect.y
        line_h = int(font.get_height() * line_height)
        last_bottom = rect.y
        for line in self.wrap_lines(text, font, rect.w):
            if y + line_h > rect.y + rect.h:
                break
            surf = font.render(line, True, color)
            # DISP-7 backstop: clip the blit to the column width so a line that
            # still exceeds rect.w (e.g. a single glyph wider than the column)
            # can never paint past the right edge / off-screen.
            target.blit(surf, (rect.x, y),
                        area=pygame.Rect(0, 0, rect.w, surf.get_height()))
            last_bottom = y + line_h
            y += line_h + 2

        return max(0, last_bottom - rect.y)

    def measure_wrapped_text(
        self, text: str, role: str, size: int, available_width: int,
        line_height: float = 0.98,
    ) -> int:
        """Measure wrapped-text height without drawing anything.

        Uses wrap_lines — the same algorithm as draw_wrapped_text — so
        measurements exactly match render output.  Returns total pixel
        height; 0 if text is empty.
        """
        font = self.font(role, size)
        if not text:
            return 0
        lines = self.wrap_lines(text, font, available_width)
        if not lines:
            return 0
        line_h = int(font.get_height() * line_height)
        # n lines: n × (line_h + 2) minus the trailing gap after the last line
        return len(lines) * (line_h + 2) - 2
