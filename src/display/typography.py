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

    def font(self, role: str, size: int):
        """Return the bundled font for a role at a pixel size, cached.

        Roles map to the DESIGN.md type hierarchy (see ``_FONT_FILES``).  Loading
        is lazy and cached per (role, size) in a bounded LRU (the renderer's
        ``_FONT_CACHE_MAX``); eviction is rare because the working set is small (a
        fixed handful of sizes).  Falls back to the DejaVu SysFont family if the
        bundled file is missing, so dev machines and CI without the assets still
        render.
        """
        import pygame

        key = (role, size)
        font = self._font_cache.get(key)
        if font is not None:
            return font

        path = _FONT_DIR / _FONT_FILES[role]
        try:
            font = pygame.font.Font(str(path), size)
        except (FileNotFoundError, OSError, pygame.error):
            name, bold, italic = _SYSFONT_FALLBACKS[role]
            font = pygame.font.SysFont(name, size, bold=bold, italic=italic)
            log.warning(f"Bundled font missing ({path.name}); using SysFont fallback")
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
        glyphs = [font.render(ch, True, color) for ch in text]
        # Total width: glyph advances + tracking between characters (CSS adds
        # tracking after every glyph including the last; trim it for cleaner
        # right-alignment).
        width = int(sum(g.get_width() for g in glyphs) + extra * max(0, len(glyphs) - 1))
        surf = pygame.Surface((max(1, width), font.get_height()), pygame.SRCALPHA)
        x = 0.0
        for g in glyphs:
            surf.blit(g, (int(x), 0))
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
