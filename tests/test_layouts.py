"""Unit tests for display layout calculations.

Pure geometry — no pygame window, no display hardware, no Pi required.
Verifies that get_now_playing_layout() produces sane proportions at
multiple resolutions, with no overlapping or out-of-bounds rects.
"""
import pytest
from src.display.layouts import get_now_playing_layout, Rect, NowPlayingLayout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEXT_PANEL_NAMES = (
    "artist_text",
    "album_text",
    "track_text",
    "meta_text",
    "position_text",
    "source_badge",
)

ALL_RECT_NAMES = ("cover_art",) + TEXT_PANEL_NAMES


def all_rects(layout):
    return {name: getattr(layout, name) for name in ALL_RECT_NAMES}


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

def test_returns_now_playing_layout_instance():
    assert isinstance(get_now_playing_layout(1024, 600), NowPlayingLayout)


def test_cover_art_is_rect():
    layout = get_now_playing_layout(1024, 600)
    assert isinstance(layout.cover_art, Rect)


def test_all_text_panels_are_rects():
    layout = get_now_playing_layout(1024, 600)
    for name in TEXT_PANEL_NAMES:
        assert isinstance(getattr(layout, name), Rect), f"{name} should be a Rect"


# ---------------------------------------------------------------------------
# Positive non-zero dimensions
# ---------------------------------------------------------------------------

def test_all_rects_have_positive_width_and_height():
    layout = get_now_playing_layout(1024, 600)
    for name, rect in all_rects(layout).items():
        assert rect.w > 0, f"{name}.w must be > 0, got {rect.w}"
        assert rect.h > 0, f"{name}.h must be > 0, got {rect.h}"


def test_all_rects_have_non_negative_coordinates():
    layout = get_now_playing_layout(1024, 600)
    for name, rect in all_rects(layout).items():
        assert rect.x >= 0, f"{name}.x must be >= 0, got {rect.x}"
        assert rect.y >= 0, f"{name}.y must be >= 0, got {rect.y}"


# ---------------------------------------------------------------------------
# Nothing bleeds off-screen at 1024x600 (primary target)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("width,height", [
    (1024, 600),   # Waveshare 7" HDMI LCD (H) — primary target
    (800, 480),    # Common 5" HDMI display
    (1280, 720),   # HD
    (640, 480),    # Small / edge case
])
def test_all_rects_fit_within_screen(width, height):
    layout = get_now_playing_layout(width, height)
    for name, rect in all_rects(layout).items():
        assert rect.x + rect.w <= width, (
            f"{name}: right edge {rect.x + rect.w} exceeds screen width {width}"
        )
        assert rect.y + rect.h <= height, (
            f"{name}: bottom edge {rect.y + rect.h} exceeds screen height {height}"
        )


# ---------------------------------------------------------------------------
# Cover art geometry
# ---------------------------------------------------------------------------

def test_cover_art_is_square():
    """Album art should be square — equal width and height."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.w == layout.cover_art.h


def test_cover_art_is_square_at_all_resolutions():
    for w, h in [(800, 480), (1024, 600), (1280, 720)]:
        layout = get_now_playing_layout(w, h)
        assert layout.cover_art.w == layout.cover_art.h, (
            f"Cover art not square at {w}x{h}"
        )


def test_cover_art_occupies_significant_screen_portion():
    """Cover art should be a generous size — at least 60% of screen height."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.h >= 600 * 0.60


def test_cover_art_starts_near_left_edge():
    """Cover art should begin close to the left margin, not centered."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.x < 1024 * 0.10  # Within 10% of left edge


# ---------------------------------------------------------------------------
# Text panels are to the right of the cover art
# ---------------------------------------------------------------------------

def test_text_panels_start_after_cover_art():
    """All text panels must start to the right of the cover art's right edge."""
    layout = get_now_playing_layout(1024, 600)
    cover_right = layout.cover_art.x + layout.cover_art.w
    for name in TEXT_PANEL_NAMES:
        rect = getattr(layout, name)
        assert rect.x >= cover_right, (
            f"{name}.x ({rect.x}) must be >= cover right edge ({cover_right})"
        )


def test_text_panels_have_meaningful_width():
    """Text panels need enough width to actually render text."""
    layout = get_now_playing_layout(1024, 600)
    for name in TEXT_PANEL_NAMES:
        rect = getattr(layout, name)
        assert rect.w >= 50, f"{name}.w ({rect.w}) is too narrow to render text"


# ---------------------------------------------------------------------------
# Font sizes
# ---------------------------------------------------------------------------

def test_artist_font_is_largest():
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_artist >= layout.font_size_album
    assert layout.font_size_artist >= layout.font_size_track
    assert layout.font_size_artist >= layout.font_size_meta


def test_meta_font_is_smallest():
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_meta <= layout.font_size_track
    assert layout.font_size_meta <= layout.font_size_album


def test_font_sizes_are_usable():
    """Minimum readable sizes on a 7" display."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_artist >= 20, "Artist font too small to read"
    assert layout.font_size_meta >= 10, "Meta font too small to read"


# ---------------------------------------------------------------------------
# Layout scales with resolution
# ---------------------------------------------------------------------------

def test_cover_art_larger_at_higher_resolution():
    small = get_now_playing_layout(800, 480)
    large = get_now_playing_layout(1280, 720)
    assert large.cover_art.h > small.cover_art.h


def test_text_panels_wider_at_wider_resolution():
    narrow = get_now_playing_layout(800, 480)
    wide = get_now_playing_layout(1280, 480)  # Same height, different width
    assert wide.artist_text.w > narrow.artist_text.w


# ---------------------------------------------------------------------------
# Vertical ordering of text panels (artist at top, meta at bottom)
# ---------------------------------------------------------------------------

def test_artist_text_is_above_album_text():
    layout = get_now_playing_layout(1024, 600)
    assert layout.artist_text.y < layout.album_text.y


def test_album_text_is_above_track_text():
    layout = get_now_playing_layout(1024, 600)
    assert layout.album_text.y < layout.track_text.y


def test_track_text_is_above_meta_text():
    layout = get_now_playing_layout(1024, 600)
    assert layout.track_text.y < layout.meta_text.y


def test_source_badge_is_near_bottom():
    """Source badge (fallback indicator) should be in the lower portion."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.source_badge.y > 600 * 0.70
