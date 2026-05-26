"""Display layout definitions.

All pixel positions, font sizes, and spacing constants live here.
Change this file to restyle the display without touching renderer logic.

v1.2.0 layout — "Museum Card" (Direction A from Claude Design mockups)
=======================================================================
Geometry derived from DirectionA.jsx at 1024×600:

  ┌─────────────────────────────────────────────────────────────────┐
  │ ● NOW PLAYING                          SIDE A · 04 OF 06        │ ← header strip (30px)
  ├─────────────────────────────────────────────────────────────────┤
  │        │                                                         │
  │        │  Catholic Block                                         │ ← track (hero, 72px bold)
  │ cover  │  ────────────────                                       │ ← accent divider (2px)
  │  art   │  Sonic Youth                                            │ ← artist (48px)
  │ 440×440│  Sister                                                 │ ← album (32px italic serif)
  │        │  [Noise Rock] [Alt Rock] [Post-Punk]                   │ ← genre chips (12px mono)
  │        │                                                         │
  │        │  1987 · SST Records · SST-134                          │ ← meta (13px mono)
  │        │  ← PREV  Stereo Sanctity    Beauty Lies…  NEXT →       │ ← prev/next (11/14px)
  └─────────────────────────────────────────────────────────────────┘

Scale: all constants are expressed relative to 1024×600 and then scaled
proportionally, so the layout works at any resolution.
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

    All Rects are in absolute pixels for the given display resolution.
    The renderer consumes these directly — no layout arithmetic there.
    """
    # Full-width strip at the very top (NOW PLAYING indicator + SIDE info)
    header_strip: Rect

    # Album art panel (left side)
    cover_art: Rect

    # Text panel elements (right side), top to bottom
    track_text: Rect       # Hero track title (largest element)
    divider: Rect          # Short accent line below track title
    artist_text: Rect      # Artist name
    album_text: Rect       # Album name (italic serif)
    genre_chips: Rect      # Bounding box for genre/style pill badges
    meta_text: Rect        # Year · Label · Catalog
    prev_next: Rect        # Prev/next track footer strip

    # Font sizes (pixels — pygame SysFont treats these as pixel heights)
    font_size_track: int      # Hero track title
    font_size_artist: int     # Artist name
    font_size_album: int      # Album name
    font_size_chips: int      # Genre chip labels (monospace)
    font_size_meta: int       # Meta footer (monospace)
    font_size_header: int     # Header strip text (monospace)
    font_size_adjacent: int   # Prev/next track names

    # Divider geometry
    divider_width: int     # Width of the accent line in pixels

    # Chip geometry
    chip_padding_x: int    # Horizontal padding inside each chip badge
    chip_padding_y: int    # Vertical padding inside each chip badge
    chip_gap: int          # Gap between adjacent chips
    chip_radius: int       # Corner radius (0 = sharp-cornered, per design)


def get_now_playing_layout(width: int, height: int) -> NowPlayingLayout:
    """Calculate layout rects for the given display resolution.

    All constants scale proportionally from the 1024×600 reference used
    in the DirectionA.jsx Claude Design mockup.
    """
    # Scale factors from 1024×600 reference
    sx = width / 1024
    sy = height / 600
    s = min(sx, sy)  # uniform scale for fonts and fixed-size elements

    # --- Reference geometry in 1024×600 pixel space ---
    STRIP_H    = 30    # header strip height
    MARGIN_X   = 50    # left/right margin
    MARGIN_TOP = 60    # top of content area (sits below header strip)
    MARGIN_BOT = 40    # bottom margin
    COVER_SIZE = 440   # square cover art side length
    GAP        = 44    # horizontal gap between cover art and text panel
    DIVIDER_W  = 64    # accent divider line width

    # Derived reference values
    text_x    = MARGIN_X + COVER_SIZE + GAP       # 534
    text_w    = 1024 - text_x - MARGIN_X          # 440
    content_y = MARGIN_TOP                         # 60
    content_h = 600 - MARGIN_TOP - MARGIN_BOT      # 500

    # Vertical rhythm within the text panel (reference pixels):
    # The track title gets the biggest slice — up to ~2 wrapped lines at 72px.
    # Everything below it flows downward; meta + prev/next anchor from the bottom.
    track_y  = content_y + 6
    track_h  = int(content_h * 0.34)    # ~170px (room for 2 wrapped lines)

    div_y    = track_y + track_h + 4
    div_h    = 2

    artist_y = div_y + div_h + 20
    artist_h = int(content_h * 0.12)    # ~60px

    album_y  = artist_y + artist_h + 4
    album_h  = int(content_h * 0.09)    # ~45px

    chips_y  = album_y + album_h + 8
    chips_h  = int(content_h * 0.08)    # ~40px

    # Meta and prev/next anchor from the bottom of the content area
    pn_h   = 44
    pn_y   = content_y + content_h - pn_h
    meta_h = 20
    meta_y = pn_y - meta_h - 6

    # --- Scale to the actual display resolution ---
    return NowPlayingLayout(
        header_strip=Rect(0, 0, width, int(STRIP_H * sy)),

        # Cover art must be square — use the smaller of the two scaled dimensions
        # so it fits at non-16:9 resolutions without clipping.
        cover_art=Rect(
            int(MARGIN_X * sx),
            int(content_y * sy),
            min(int(COVER_SIZE * sx), int(COVER_SIZE * sy)),
            min(int(COVER_SIZE * sx), int(COVER_SIZE * sy)),
        ),

        track_text=Rect(int(text_x * sx), int(track_y * sy), int(text_w * sx), int(track_h * sy)),
        divider=Rect(int(text_x * sx), int(div_y * sy), int(DIVIDER_W * sx), int(div_h * sy)),
        artist_text=Rect(int(text_x * sx), int(artist_y * sy), int(text_w * sx), int(artist_h * sy)),
        album_text=Rect(int(text_x * sx), int(album_y * sy), int(text_w * sx), int(album_h * sy)),
        genre_chips=Rect(int(text_x * sx), int(chips_y * sy), int(text_w * sx), int(chips_h * sy)),
        meta_text=Rect(int(text_x * sx), int(meta_y * sy), int(text_w * sx), int(meta_h * sy)),
        prev_next=Rect(int(text_x * sx), int(pn_y * sy), int(text_w * sx), int(pn_h * sy)),

        font_size_track=max(24, int(72 * s)),
        font_size_artist=max(18, int(48 * s)),
        font_size_album=max(14, int(32 * s)),
        font_size_chips=max(10, int(12 * s)),
        font_size_meta=max(10, int(13 * s)),
        font_size_header=max(9, int(11 * s)),
        font_size_adjacent=max(10, int(14 * s)),

        divider_width=int(DIVIDER_W * sx),
        chip_padding_x=max(6, int(12 * s)),
        chip_padding_y=max(3, int(5 * s)),
        chip_gap=max(4, int(6 * s)),
        chip_radius=0,
    )
