"""Display renderer — manages the pygame window on the HDMI output.

Listens for PlayerState changes and re-renders the appropriate layout.
Runs as an async task in the main event loop.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from src.state.player_state import PlayerState, PlayerStatus
from src.display.layouts import get_now_playing_layout, Rect

log = logging.getLogger(__name__)

# Suppress pygame audio (we're output-only) and point to the right display
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("DISPLAY", ":0")  # Needed when running headless / via SSH


class DisplayRenderer:
    """Renders now-playing info to an HDMI screen via pygame."""

    def __init__(self, config: dict, state: PlayerState):
        self.config = config["display"]
        self.state = state
        self.width: int = self.config["width"]
        self.height: int = self.config["height"]
        self.fullscreen: bool = self.config.get("fullscreen", True)
        self.bg_color: tuple = tuple(self.config["background_color"])
        self.font_color: tuple = tuple(self.config["font_color"])
        self.accent_color: tuple = tuple(self.config.get("accent_color", [180, 140, 80]))
        self.show_source_indicator: bool = self.config.get("show_source_indicator", True)
        self.cache_dir = Path(
            self.config.get("cover_art_cache_dir", "src/display/assets/cache")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._screen = None
        self._fonts: dict = {}
        self._running = True
        self._dirty = True  # Force initial render

        self.state.on_change(self._on_state_change)

    def start(self):
        """Initialize pygame. Must be called before run()."""
        import pygame
        pygame.init()
        flags = pygame.FULLSCREEN if self.fullscreen else 0
        self._screen = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption("vinyl-now-playing")
        pygame.mouse.set_visible(False)
        self._fonts = {
            36: pygame.font.SysFont("dejavu sans", 36, bold=True),
            26: pygame.font.SysFont("dejavu sans", 26),
            24: pygame.font.SysFont("dejavu sans", 24),
            16: pygame.font.SysFont("dejavu sans", 16),
        }
        log.info(f"Display initialized: {self.width}x{self.height} fullscreen={self.fullscreen}")

    def _on_state_change(self, state: PlayerState):
        """Called by PlayerState whenever anything changes."""
        self._dirty = True

    async def run(self):
        """Async display loop — re-renders when dirty, handles pygame quit events."""
        import pygame
        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.stop()
                    return
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self.stop()
                    return

            if self._dirty:
                self._render()
                self._dirty = False
                pygame.display.flip()

            await asyncio.sleep(1 / 30)  # ~30 fps

    def _render(self):
        """Dispatch to the appropriate layout based on current state."""
        if self.state.status in (PlayerStatus.IDLE, PlayerStatus.SESSION_ENDED):
            self._render_idle()
        elif self.state.status == PlayerStatus.LISTENING:
            self._render_listening()
        elif self.state.status == PlayerStatus.PLAYING and self.state.current_track:
            self._render_now_playing()
        else:
            self._render_listening()

    def _render_now_playing(self):
        """Render the full now-playing layout."""
        import pygame
        from src.metadata.models import MetadataSource

        track = self.state.current_track
        layout = get_now_playing_layout(self.width, self.height)
        self._screen.fill(self.bg_color)

        # Cover art
        cover = self._load_cover(track.cover_art_url, layout.cover_art.w, layout.cover_art.h)
        if cover:
            self._screen.blit(cover, (layout.cover_art.x, layout.cover_art.y))
        else:
            pygame.draw.rect(
                self._screen, (40, 40, 40),
                (layout.cover_art.x, layout.cover_art.y, layout.cover_art.w, layout.cover_art.h),
            )

        # Text fields
        self._draw_text(track.artist, layout.font_size_artist, layout.artist_text, self.font_color)
        self._draw_text(track.album, layout.font_size_album, layout.album_text, self.font_color)
        self._draw_text(track.title, layout.font_size_track, layout.track_text, self.accent_color)

        meta_parts = [p for p in [track.year, track.label, track.catalog_number] if p]
        if meta_parts:
            self._draw_text(
                " · ".join(meta_parts), layout.font_size_meta,
                layout.meta_text, (150, 150, 150),
            )

        if track.track_display:
            self._draw_text(
                track.track_display, layout.font_size_meta,
                layout.position_text, (120, 120, 120),
            )

        if self.show_source_indicator and track.source == MetadataSource.FALLBACK:
            self._draw_text(
                "⚠ metadata from fallback", 16,
                layout.source_badge, (100, 80, 60),
            )

    def _render_listening(self):
        """Render 'Listening...' while awaiting first recognition."""
        import pygame
        self._screen.fill(self.bg_color)
        font = self._fonts.get(26)
        if font:
            text = font.render("Listening…", True, (80, 80, 80))
            rect = text.get_rect(center=(self.width // 2, self.height // 2))
            self._screen.blit(text, rect)

    def _render_idle(self):
        """Render idle/standby screen."""
        self._screen.fill(self.bg_color)
        # TODO: add a nice idle layout — last played album, clock, logo, etc.

    def _draw_text(self, text: str, size: int, rect: Rect, color: tuple):
        """Render text clipped to a Rect."""
        font = self._fonts.get(size) or self._fonts.get(24)
        if not font or not text:
            return
        surface = font.render(text, True, color)
        self._screen.blit(surface, (rect.x, rect.y), area=(0, 0, rect.w, rect.h))

    def _load_cover(self, url: Optional[str], w: int, h: int):
        """Load and scale cover art from URL, with local file cache."""
        import hashlib
        import urllib.request
        import pygame

        if not url:
            return None

        cache_key = hashlib.md5(url.encode()).hexdigest() + ".jpg"
        cache_path = self.cache_dir / cache_key

        if not cache_path.exists():
            try:
                urllib.request.urlretrieve(url, cache_path)
            except Exception as e:
                log.warning(f"Failed to download cover art: {e}")
                return None

        try:
            img = pygame.image.load(str(cache_path)).convert()
            return pygame.transform.smoothscale(img, (w, h))
        except Exception as e:
            log.warning(f"Failed to load cached cover art: {e}")
            cache_path.unlink(missing_ok=True)  # Purge corrupted cache entry
            return None

    def stop(self):
        self._running = False
        import pygame
        pygame.quit()
        log.info("Display stopped.")
