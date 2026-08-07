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
from pathlib import Path

log = logging.getLogger(__name__)

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

        # DISP-5: per-glyph tracking assumes every codepoint is an independent
        # glyph — true for the ASCII metadata/labels this is designed for, but it
        # destroys shaping for complex scripts (Arabic joining, Devanagari
        # conjuncts, floating combining marks, emoji ZWJ) that arrive in
        # free-text Discogs fields.  For any non-ASCII string, render it as ONE
        # shaped run and skip the manual tracking, rather than reverse/mis-space
        # it a glyph at a time.  ASCII keeps its designed letter-spacing below.
        if not text.isascii():
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
