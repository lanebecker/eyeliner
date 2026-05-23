"""Display layout definitions.

All pixel positions, font sizes, and colour rules live here.
Change this file to restyle the display without touching renderer logic.
"""

from dataclasses import dataclass


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass
class NowPlayingLayout:
    """Layout for the main now-playing screen.

    All proportions are relative to the display dimensions passed into
    get_now_playing_layout(), so this works at any resolution. Default
    target is 1024x600 (Waveshare 7" HDMI LCD H).
    """
    cover_art: Rect       # Large album art panel
    artist_text: Rect     # Artist name
    album_text: Rect      # Album name
    track_text: Rect      # Track title
    meta_text: Rect       # Year / label / catalog number
    position_text: Rect   # Track position (e.g. "A1")
    source_badge: Rect    # Fallback source indicator

    font_size_artist: int
    font_size_album: int
    font_size_track: int
    font_size_meta: int


def get_now_playing_layout(width: int, height: int) -> NowPlayingLayout:
    """Calculate layout rects for the given display resolution."""
    cover_size = int(height * 0.85)
    margin = int(height * 0.075)
    text_x = cover_size + margin * 2
    text_w = width - text_x - margin

    return NowPlayingLayout(
        cover_art=Rect(margin, margin, cover_size, cover_size),
        artist_text=Rect(text_x, int(height * 0.08), text_w, int(height * 0.18)),
        album_text=Rect(text_x, int(height * 0.28), text_w, int(height * 0.14)),
        track_text=Rect(text_x, int(height * 0.46), text_w, int(height * 0.14)),
        meta_text=Rect(text_x, int(height * 0.65), text_w, int(height * 0.10)),
        position_text=Rect(text_x, int(height * 0.78), text_w, int(height * 0.10)),
        source_badge=Rect(text_x, int(height * 0.90), text_w, int(height * 0.08)),
        font_size_artist=36,
        font_size_album=26,
        font_size_track=24,
        font_size_meta=16,
    )
