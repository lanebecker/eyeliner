"""Display renderer — manages the pygame window on the HDMI output.

Listens for PlayerState changes and re-renders the appropriate layout.
Runs as an async task in the main event loop.

v1.2.0 visual design
---------------------
Implements the "Museum Card" layout from the Claude Design Direction A mockups:
  - 5-color palette extracted from album art via Pillow (bg, surface, accent, text, muted)
  - Radial gradient background blended from palette.surface → palette.bg
  - Header strip: pulsing "NOW PLAYING" dot + "SIDE A · 04 OF 06"
  - Hero track title (large, bold, word-wrapped)
  - Short accent divider line
  - Artist name, album name (italic serif rendered via a separate font)
  - Genre/style pill badges
  - Meta footer: Year · Label · Catalog
  - Prev/next track strip

Color transitions lerp over ~1 second when a new track starts.
Palettes are cached per cover art URL so extraction only runs once per album.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from src.state.player_state import PlayerState, PlayerStatus
from src.display.layouts import get_now_playing_layout, NowPlayingLayout
from src.metadata.models import DisplayPalette, FALLBACK_PALETTE

log = logging.getLogger(__name__)

# Suppress pygame audio (we're output-only) and point to the right display
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("DISPLAY", ":0")  # Needed when running headless / via SSH

# How long (seconds) to lerp between palettes on track change
_TRANSITION_SECS = 1.0


def _lerp_color(a: tuple, b: tuple, t: float) -> tuple:
    """Linear interpolation between two RGB tuples. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _lerp_palette(a: DisplayPalette, b: DisplayPalette, t: float) -> DisplayPalette:
    """Interpolate all five channels of two DisplayPalettes."""
    return DisplayPalette(
        bg=_lerp_color(a.bg, b.bg, t),
        surface=_lerp_color(a.surface, b.surface, t),
        accent=_lerp_color(a.accent, b.accent, t),
        text=_lerp_color(a.text, b.text, t),
        muted=_lerp_color(a.muted, b.muted, t),
    )


def _clamp_luminance(color: tuple, min_lum: float = 0.25) -> tuple:
    """Ensure a color is bright enough to read against a dark background.

    Uses a simple perceived-brightness formula. If the color is too dark,
    it's brightened proportionally until it hits min_lum.
    """
    r, g, b = color
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    if lum < min_lum and lum > 0:
        scale = min_lum / lum
        return tuple(min(255, int(c * scale)) for c in (r, g, b))
    return color


def _extract_palette(image_path: Path) -> DisplayPalette:
    """Extract a 5-color display palette from a cached cover art image.

    Uses Pillow's color quantization to find the dominant hues, then
    derives the full palette (bg, surface, accent, text, muted) from them.

    Falls back to FALLBACK_PALETTE on any error.
    """
    try:
        from PIL import Image

        img = Image.open(image_path).convert("RGB")
        img = img.resize((80, 80), Image.LANCZOS)

        # Quantize to 8 colors; getpalette returns flat R,G,B,R,G,B,... list
        quantized = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        raw = quantized.getpalette()  # 768 values: 8 colors × 3 channels

        # Count how often each palette index appears (most → most dominant)
        pixel_data = list(quantized.getdata())
        counts = [0] * 8
        for idx in pixel_data:
            counts[idx] += 1

        # Build (count, RGB) tuples, sorted by dominance descending
        palette_colors = [
            (counts[i], (raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]))
            for i in range(8)
        ]
        palette_colors.sort(key=lambda x: x[0], reverse=True)
        colors = [c for _, c in palette_colors]

        # Most dominant color → tint for bg/surface
        dominant = colors[0]

        # Most *vibrant* color → accent (highest saturation)
        def saturation(rgb):
            r, g, b = [x / 255.0 for x in rgb]
            mx, mn = max(r, g, b), min(r, g, b)
            return (mx - mn) / mx if mx > 0 else 0

        accent_raw = max(colors, key=saturation)
        accent = _clamp_luminance(accent_raw, min_lum=0.30)

        # Derive bg: darken dominant significantly (target ~15% brightness)
        scale_bg = 0.18
        bg = tuple(max(8, int(c * scale_bg + dominant[i] * 0.04)) for i, c in enumerate(dominant))

        # Surface: slightly lighter than bg
        surface = tuple(min(255, int(c * 1.6)) for c in bg)

        # Text: near-white with a slight warm tint from dominant
        text = (
            min(255, 230 + int(dominant[0] * 0.04)),
            min(255, 225 + int(dominant[1] * 0.03)),
            min(255, 215 + int(dominant[2] * 0.03)),
        )

        # Muted: medium gray, very slightly tinted
        muted = (
            min(200, 120 + int(dominant[0] * 0.08)),
            min(200, 118 + int(dominant[1] * 0.07)),
            min(200, 115 + int(dominant[2] * 0.06)),
        )

        return DisplayPalette(bg=bg, surface=surface, accent=accent, text=text, muted=muted)

    except Exception as e:
        log.warning(f"Palette extraction failed for {image_path}: {e}")
        return FALLBACK_PALETTE


class DisplayRenderer:
    """Renders now-playing info to an HDMI screen via pygame."""

    def __init__(self, config: dict, state: PlayerState):
        self.config = config["display"]
        self.state = state
        self.width: int = self.config["width"]
        self.height: int = self.config["height"]
        self.fullscreen: bool = self.config.get("fullscreen", True)
        self.dynamic_theming: bool = self.config.get("dynamic_theming", True)
        self.cache_dir = Path(
            self.config.get("cover_art_cache_dir", "src/display/assets/cache")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._screen = None
        self._fonts: dict = {}          # size → pygame.font.Font
        self._italic_fonts: dict = {}   # size → italic pygame.font.Font
        self._mono_fonts: dict = {}     # size → monospace pygame.font.Font
        self._running = True
        self._dirty = True              # Force initial render

        # Palette transition state
        self._current_palette: DisplayPalette = FALLBACK_PALETTE
        self._target_palette: DisplayPalette = FALLBACK_PALETTE
        self._transition_start: float = 0.0
        self._palette_cache: dict = {}  # cover_art_url → DisplayPalette

        self.state.on_change(self._on_state_change)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self):
        """Initialize pygame. Must be called before run()."""
        import pygame
        pygame.init()
        flags = pygame.FULLSCREEN if self.fullscreen else 0
        self._screen = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption("vinyl-now-playing")
        pygame.mouse.set_visible(False)
        self._build_font_cache()
        log.info(f"Display initialized: {self.width}x{self.height} fullscreen={self.fullscreen}")

    def _build_font_cache(self):
        """Pre-build font objects for all sizes used by the layout."""
        import pygame
        layout = get_now_playing_layout(self.width, self.height)
        sizes = {
            layout.font_size_track,
            layout.font_size_artist,
            layout.font_size_album,
            layout.font_size_chips,
            layout.font_size_meta,
            layout.font_size_header,
            layout.font_size_adjacent,
            # Always include a couple of fallback sizes
            16, 14, 12,
        }
        for sz in sizes:
            self._fonts[sz] = pygame.font.SysFont("dejavu sans", sz, bold=False)
            self._italic_fonts[sz] = pygame.font.SysFont("dejavu sans", sz, bold=False, italic=True)
            self._mono_fonts[sz] = pygame.font.SysFont("dejavu sans mono", sz, bold=False)
        # Bold variant for the track title
        self._fonts[layout.font_size_track] = pygame.font.SysFont(
            "dejavu sans", layout.font_size_track, bold=True
        )

    def _on_state_change(self, state: PlayerState):
        """Called by PlayerState whenever anything changes."""
        self._dirty = True
        # When a new track arrives, queue a palette transition
        if state.status == PlayerStatus.PLAYING and state.current_track:
            self._queue_palette(state.current_track.cover_art_url)

    # -----------------------------------------------------------------------
    # Async render loop
    # -----------------------------------------------------------------------

    async def run(self):
        """Async display loop — re-renders when dirty or transitioning."""
        import pygame
        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.stop()
                    return
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self.stop()
                    return

            # Keep re-rendering during a palette transition even if not dirty
            transitioning = (time.monotonic() - self._transition_start) < _TRANSITION_SECS
            if self._dirty or transitioning:
                self._render()
                self._dirty = False
                pygame.display.flip()

            await asyncio.sleep(1 / 30)

    # -----------------------------------------------------------------------
    # Render dispatch
    # -----------------------------------------------------------------------

    def _render(self):
        """Dispatch to the appropriate layout based on current player status."""
        if self.state.status in (PlayerStatus.IDLE, PlayerStatus.SESSION_ENDED):
            self._render_idle()
        elif self.state.status == PlayerStatus.LISTENING:
            self._render_listening()
        elif self.state.status == PlayerStatus.PLAYING and self.state.current_track:
            self._render_now_playing()
        else:
            self._render_listening()

    # -----------------------------------------------------------------------
    # Now-playing screen
    # -----------------------------------------------------------------------

    def _render_now_playing(self):
        """Render the full now-playing layout with the new v1.2.0 design."""
        import pygame

        track = self.state.current_track
        layout = get_now_playing_layout(self.width, self.height)
        p = self._animated_palette()

        # --- Background ---
        self._draw_gradient_bg(p)

        # --- Cover art ---
        cover = self._load_cover(track.cover_art_url, layout.cover_art.w, layout.cover_art.h)
        if cover:
            self._screen.blit(cover, (layout.cover_art.x, layout.cover_art.y))
        else:
            pygame.draw.rect(
                self._screen, p.surface,
                (layout.cover_art.x, layout.cover_art.y,
                 layout.cover_art.w, layout.cover_art.h),
            )

        # --- Header strip ---
        self._draw_header(layout, p, track)

        # --- Track title (hero, word-wrapped) ---
        self._draw_wrapped_text(
            track.title, layout.font_size_track,
            layout.track_text, p.text,
            bold=True,
        )

        # --- Accent divider line ---
        pygame.draw.rect(
            self._screen, p.accent,
            (layout.divider.x, layout.divider.y, layout.divider_width, max(2, layout.divider.h)),
        )

        # --- Artist ---
        self._draw_text_clipped(
            track.artist, layout.font_size_artist,
            layout.artist_text, p.text,
        )

        # --- Album (italic) ---
        self._draw_text_clipped(
            track.album, layout.font_size_album,
            layout.album_text, p.accent,
            italic=True,
        )

        # --- Genre chips ---
        if track.genres:
            self._draw_genre_chips(track.genres, layout, p)

        # --- Meta footer ---
        meta_parts = [x for x in [track.year, track.label, track.catalog_number] if x]
        if meta_parts:
            self._draw_mono_text(
                " · ".join(meta_parts), layout.font_size_meta,
                layout.meta_text, p.muted,
            )

        # --- Prev / next ---
        self._draw_prev_next(layout, p, track)

    def _draw_header(self, layout: NowPlayingLayout, p: DisplayPalette, track):
        """Draw the full-width header strip: NOW PLAYING dot + SIDE info."""
        import pygame
        strip = layout.header_strip
        font = self._mono_fonts.get(layout.font_size_header) or self._mono_fonts.get(12)
        if not font:
            return

        # Pulsing dot — simple on/off every ~0.8s
        dot_on = int(time.monotonic() / 0.8) % 2 == 0
        dot_color = p.accent if dot_on else p.muted
        dot_r = max(4, int(strip.h * 0.27))
        dot_x = int(strip.h * 0.87)
        dot_y = strip.h // 2
        pygame.draw.circle(self._screen, dot_color, (dot_x, dot_y), dot_r)

        # "NOW PLAYING" label
        label_surf = font.render("NOW PLAYING", True, p.muted)
        self._screen.blit(label_surf, (dot_x + dot_r + 8, (strip.h - label_surf.get_height()) // 2))

        # SIDE info (right-aligned)
        side_str = self._side_string(track)
        if side_str:
            side_surf = font.render(side_str, True, p.muted)
            x = self.width - int(layout.header_strip.h * 0.87) - side_surf.get_width()
            self._screen.blit(side_surf, (x, (strip.h - side_surf.get_height()) // 2))

    def _side_string(self, track) -> str:
        """Build the 'SIDE A · 04 OF 06' string, or '' if data is unavailable."""
        letter = track.side_letter
        pos = track.side_position
        total = track.side_total
        if letter and pos and total:
            return f"SIDE {letter} · {pos:02d} OF {total:02d}"
        elif track.track_display:
            return track.track_display
        return ""

    def _draw_genre_chips(self, genres: list, layout: NowPlayingLayout, p: DisplayPalette):
        """Render genre/style pill badges, wrapping onto a second row if needed."""
        import pygame
        font = self._mono_fonts.get(layout.font_size_chips) or self._mono_fonts.get(12)
        if not font:
            return

        px = layout.chip_padding_x
        py = layout.chip_padding_y
        gap = layout.chip_gap
        x0 = layout.genre_chips.x
        y0 = layout.genre_chips.y
        x = x0
        y = y0
        chip_h = font.get_height() + py * 2

        for genre in genres:
            text_surf = font.render(genre, True, p.muted)
            chip_w = text_surf.get_width() + px * 2

            # Wrap to next row if we'd overflow
            if x + chip_w > x0 + layout.genre_chips.w and x > x0:
                x = x0
                y += chip_h + gap
                if y + chip_h > layout.genre_chips.y + layout.genre_chips.h + chip_h:
                    break  # Out of room

            # Draw border rect (sharp corners per design)
            border_rect = pygame.Rect(x, y, chip_w, chip_h)
            pygame.draw.rect(self._screen, p.surface, border_rect)
            pygame.draw.rect(self._screen, p.muted, border_rect, 1)

            # Draw label centered in chip
            self._screen.blit(text_surf, (x + px, y + py))
            x += chip_w + gap

    def _draw_prev_next(self, layout: NowPlayingLayout, p: DisplayPalette, track):
        """Draw the prev/next track navigation footer."""
        import pygame
        label_font = self._mono_fonts.get(layout.font_size_header) or self._mono_fonts.get(12)
        name_font = self._fonts.get(layout.font_size_adjacent) or self._fonts.get(14)
        if not label_font or not name_font:
            return

        strip = layout.prev_next
        half_w = strip.w // 2

        # PREV (left half)
        prev = track.prev_track_title
        if prev:
            label = label_font.render("← PREV", True, p.muted)
            self._screen.blit(label, (strip.x, strip.y))
            name = name_font.render(prev, True, p.text)
            clip = pygame.Rect(strip.x, strip.y + label.get_height() + 4, half_w - 8, name.get_height())
            self._screen.blit(name, clip.topleft, area=(0, 0, clip.w, clip.h))

        # NEXT (right half, right-aligned label)
        nxt = track.next_track_title
        if nxt:
            label = label_font.render("NEXT →", True, p.muted)
            lx = strip.x + half_w + 8
            self._screen.blit(label, (lx, strip.y))
            name = name_font.render(nxt, True, p.text)
            clip_w = half_w - 8
            self._screen.blit(name, (lx, strip.y + label.get_height() + 4), area=(0, 0, clip_w, name.get_height()))

    # -----------------------------------------------------------------------
    # Boot / idle / listening states
    # -----------------------------------------------------------------------

    def _render_listening(self):
        """Render 'Identifying…' boot state while awaiting first recognition."""
        import pygame
        p = FALLBACK_PALETTE
        self._draw_gradient_bg(p)

        # Spinning arc as a loading indicator (simple rotation)
        cx, cy = self.width // 2, self.height // 2
        r = int(min(self.width, self.height) * 0.08)
        angle = (time.monotonic() * 200) % 360

        pygame.draw.circle(self._screen, p.surface, (cx, cy), r + 4, 1)
        import math
        for deg in range(0, 50, 3):
            a = math.radians(angle + deg)
            x1 = int(cx + r * math.cos(a))
            y1 = int(cy + r * math.sin(a))
            pygame.draw.circle(self._screen, p.accent, (x1, y1), 2)

        font = self._mono_fonts.get(12) or list(self._mono_fonts.values())[0] if self._mono_fonts else None
        if font:
            surf = font.render("IDENTIFYING…", True, p.muted)
            self._screen.blit(surf, surf.get_rect(center=(cx, cy + r + 20)))
        self._dirty = True  # Keep animating

    def _render_idle(self):
        """Render idle/standby screen."""
        p = FALLBACK_PALETTE
        self._draw_gradient_bg(p)
        # TODO (v1.4.0): grid of recently played albums, clock, random suggestion

    # -----------------------------------------------------------------------
    # Drawing helpers
    # -----------------------------------------------------------------------

    def _draw_gradient_bg(self, p: DisplayPalette):
        """Fill the screen with a radial gradient from surface (centre) to bg (edges)."""
        import pygame

        self._screen.fill(p.bg)

        cx = int(self.width * 0.25)
        cy = int(self.height * 0.35)
        max_r = int(max(self.width, self.height) * 0.75)
        steps = 24

        for i in range(steps, 0, -1):
            t = i / steps
            color = _lerp_color(p.bg, p.surface, t * 0.55)
            r = int(max_r * t)
            pygame.draw.circle(self._screen, color, (cx, cy), r)

    def _draw_wrapped_text(
        self, text: str, size: int, rect, color: tuple, bold: bool = False
    ):
        """Render text with word-wrapping to fit within rect.w, clipped to rect.h."""
        import pygame
        font = self._fonts.get(size)
        if not font or not text:
            return

        words = text.split()
        lines = []
        current = ""

        for word in words:
            test = (current + " " + word).strip()
            if font.size(test)[0] <= rect.w:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)

        y = rect.y
        line_h = int(font.get_height() * 0.98)
        for line in lines:
            if y + line_h > rect.y + rect.h:
                break
            surf = font.render(line, True, color)
            self._screen.blit(surf, (rect.x, y))
            y += line_h + 2

    def _draw_text_clipped(
        self, text: str, size: int, rect, color: tuple, italic: bool = False
    ):
        """Render a single line of text, clipped to rect bounds."""
        import pygame
        font_dict = self._italic_fonts if italic else self._fonts
        font = font_dict.get(size)
        if not font or not text:
            return
        surf = font.render(text, True, color)
        self._screen.blit(surf, (rect.x, rect.y), area=(0, 0, rect.w, rect.h))

    def _draw_mono_text(self, text: str, size: int, rect, color: tuple):
        """Render a single line in the monospace font, clipped to rect."""
        import pygame
        font = self._mono_fonts.get(size)
        if not font or not text:
            return
        surf = font.render(text, True, color)
        self._screen.blit(surf, (rect.x, rect.y), area=(0, 0, rect.w, rect.h))

    # -----------------------------------------------------------------------
    # Palette management
    # -----------------------------------------------------------------------

    def _queue_palette(self, cover_url: Optional[str]):
        """Set the target palette for a new track, triggering a transition."""
        if not self.dynamic_theming:
            return
        if cover_url is None:
            target = FALLBACK_PALETTE
        elif cover_url in self._palette_cache:
            target = self._palette_cache[cover_url]
        else:
            cache_key = self._url_to_cache_key(cover_url)
            cache_path = self.cache_dir / cache_key
            if cache_path.exists():
                target = _extract_palette(cache_path)
                self._palette_cache[cover_url] = target
            else:
                target = FALLBACK_PALETTE

        self._target_palette = target
        self._transition_start = time.monotonic()

    def _animated_palette(self) -> DisplayPalette:
        """Return the current interpolated palette for this render frame."""
        elapsed = time.monotonic() - self._transition_start
        t = min(1.0, elapsed / _TRANSITION_SECS)
        if t >= 1.0:
            self._current_palette = self._target_palette
            return self._target_palette
        return _lerp_palette(self._current_palette, self._target_palette, t)

    # -----------------------------------------------------------------------
    # Cover art loading
    # -----------------------------------------------------------------------

    def _url_to_cache_key(self, url: str) -> str:
        import hashlib
        return hashlib.md5(url.encode()).hexdigest() + ".jpg"

    def _load_cover(self, url: Optional[str], w: int, h: int):
        """Load and scale cover art from URL, with local file cache."""
        import urllib.request
        import pygame

        if not url:
            return None

        cache_key = self._url_to_cache_key(url)
        cache_path = self.cache_dir / cache_key
        fresh = False

        if not cache_path.exists():
            try:
                urllib.request.urlretrieve(url, cache_path)
                fresh = True
            except Exception as e:
                log.warning(f"Failed to download cover art: {e}")
                return None

        if fresh and self.dynamic_theming:
            self._queue_palette(url)

        try:
            img = pygame.image.load(str(cache_path)).convert()
            return pygame.transform.smoothscale(img, (w, h))
        except Exception as e:
            log.warning(f"Failed to load cached cover art: {e}")
            cache_path.unlink(missing_ok=True)
            return None

    def stop(self):
        self._running = False
        import pygame
        pygame.quit()
        log.info("Display stopped.")
