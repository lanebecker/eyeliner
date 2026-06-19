"""Unit tests for display layout calculations.

Pure geometry — no pygame window, no display hardware, no Pi required.
Verifies that get_now_playing_layout() produces sane proportions at
multiple resolutions, with no overlapping or out-of-bounds rects.

Updated for v1.2.0 "Museum Card" layout (Direction A from Claude Design).
"""
import pytest
from src.display.layouts import get_now_playing_layout, Rect, NowPlayingLayout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEXT_PANEL_NAMES = (
    "track_text",
    "divider",
    "artist_text",
    "album_text",
    "genre_chips",
    "meta_text",
    "prev_next",
)

ALL_RECT_NAMES = ("header_strip", "cover_art") + TEXT_PANEL_NAMES


def test_chip_and_tracking_style_live_on_the_layout():
    """A-14: genre-chip border alpha + per-context letter-spacing moved out of
    renderer.py onto the layout, so a restyle is 'edit layouts.py'."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.chip_border_alpha == 0x55
    assert layout.tracking_chip == 0.10
    assert layout.tracking_label == 0.16
    assert layout.tracking_catalog == 0.08
    assert layout.tracking_adjacent == 0.12
    assert layout.tracking_empty_label == 0.20


def test_tracking_is_resolution_independent():
    """Letter-spacing is em-relative, so it must NOT scale with resolution."""
    a = get_now_playing_layout(1024, 600)
    b = get_now_playing_layout(2048, 1200)
    assert a.tracking_chip == b.tracking_chip
    assert a.tracking_label == b.tracking_label
    assert a.chip_border_alpha == b.chip_border_alpha


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


def test_header_strip_is_rect():
    layout = get_now_playing_layout(1024, 600)
    assert isinstance(layout.header_strip, Rect)


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
# Nothing bleeds off-screen
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
# Header strip geometry
# ---------------------------------------------------------------------------

def test_header_strip_spans_full_width():
    layout = get_now_playing_layout(1024, 600)
    assert layout.header_strip.x == 0
    assert layout.header_strip.w == 1024


def test_header_strip_starts_at_top():
    layout = get_now_playing_layout(1024, 600)
    assert layout.header_strip.y == 0


def test_header_strip_is_thin():
    """Strip should be a slim bar, not a large region."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.header_strip.h < 600 * 0.10


# ---------------------------------------------------------------------------
# Cover art geometry
# ---------------------------------------------------------------------------

def test_cover_art_is_square():
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.w == layout.cover_art.h


def test_cover_art_is_square_at_all_resolutions():
    for w, h in [(800, 480), (1024, 600), (1280, 720)]:
        layout = get_now_playing_layout(w, h)
        assert layout.cover_art.w == layout.cover_art.h, f"Cover art not square at {w}x{h}"


def test_cover_art_occupies_significant_screen_portion():
    """Cover art should be a generous size — at least 60% of screen height."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.h >= 600 * 0.60


def test_cover_art_starts_near_left_edge():
    """Cover art should begin close to the left margin, not centered."""
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.x < 1024 * 0.10


def test_cover_art_starts_below_header_strip():
    layout = get_now_playing_layout(1024, 600)
    assert layout.cover_art.y >= layout.header_strip.h


# ---------------------------------------------------------------------------
# Text panels are to the right of the cover art
# ---------------------------------------------------------------------------

def test_text_panels_start_after_cover_art():
    layout = get_now_playing_layout(1024, 600)
    cover_right = layout.cover_art.x + layout.cover_art.w
    for name in TEXT_PANEL_NAMES:
        rect = getattr(layout, name)
        assert rect.x >= cover_right, (
            f"{name}.x ({rect.x}) must be >= cover right edge ({cover_right})"
        )


def test_text_panels_have_meaningful_width():
    layout = get_now_playing_layout(1024, 600)
    for name in TEXT_PANEL_NAMES:
        rect = getattr(layout, name)
        assert rect.w >= 50, f"{name}.w ({rect.w}) is too narrow to render text"


# ---------------------------------------------------------------------------
# Vertical ordering: track → divider → artist → album → chips → meta → prev/next
# ---------------------------------------------------------------------------

def test_track_is_above_divider():
    layout = get_now_playing_layout(1024, 600)
    assert layout.track_text.y < layout.divider.y


def test_divider_is_above_artist():
    layout = get_now_playing_layout(1024, 600)
    assert layout.divider.y < layout.artist_text.y


def test_artist_is_above_album():
    layout = get_now_playing_layout(1024, 600)
    assert layout.artist_text.y < layout.album_text.y


def test_album_is_above_genre_chips():
    layout = get_now_playing_layout(1024, 600)
    assert layout.album_text.y < layout.genre_chips.y


def test_meta_is_above_prev_next():
    layout = get_now_playing_layout(1024, 600)
    assert layout.meta_text.y < layout.prev_next.y


def test_prev_next_is_near_bottom():
    layout = get_now_playing_layout(1024, 600)
    assert layout.prev_next.y > 600 * 0.70


# ---------------------------------------------------------------------------
# Font sizes — new hierarchy: track > artist > album > meta/chips/header
# ---------------------------------------------------------------------------

def test_track_font_is_largest():
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_track >= layout.font_size_artist
    assert layout.font_size_track >= layout.font_size_album
    assert layout.font_size_track >= layout.font_size_meta


def test_artist_font_larger_than_album():
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_artist >= layout.font_size_album


def test_header_font_is_smallest_or_near_smallest():
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_header <= layout.font_size_meta
    assert layout.font_size_header <= layout.font_size_chips


def test_font_sizes_are_usable():
    layout = get_now_playing_layout(1024, 600)
    assert layout.font_size_track >= 24, "Track font too small"
    assert layout.font_size_artist >= 18, "Artist font too small"
    assert layout.font_size_meta >= 10, "Meta font too small"


# ---------------------------------------------------------------------------
# Divider geometry
# ---------------------------------------------------------------------------

def test_divider_is_thin():
    layout = get_now_playing_layout(1024, 600)
    assert layout.divider.h <= 4  # Should be a fine accent line


def test_divider_width_is_positive():
    layout = get_now_playing_layout(1024, 600)
    assert layout.divider_width > 0


# ---------------------------------------------------------------------------
# Chip geometry
# ---------------------------------------------------------------------------

def test_chip_padding_is_positive():
    layout = get_now_playing_layout(1024, 600)
    assert layout.chip_padding_x > 0
    assert layout.chip_padding_y > 0


def test_chip_gap_is_non_negative():
    layout = get_now_playing_layout(1024, 600)
    assert layout.chip_gap >= 0


# ---------------------------------------------------------------------------
# Layout scales with resolution
# ---------------------------------------------------------------------------

def test_cover_art_larger_at_higher_resolution():
    small = get_now_playing_layout(800, 480)
    large = get_now_playing_layout(1280, 720)
    assert large.cover_art.h > small.cover_art.h


def test_text_panels_wider_at_wider_resolution():
    narrow = get_now_playing_layout(800, 480)
    wide = get_now_playing_layout(1280, 480)
    assert wide.track_text.w > narrow.track_text.w


def test_font_sizes_larger_at_higher_resolution():
    small = get_now_playing_layout(640, 480)
    large = get_now_playing_layout(1280, 720)
    assert large.font_size_track > small.font_size_track


# ---------------------------------------------------------------------------
# D-2 (#74) — resolution-independence matrix
#
# Backs CLAUDE.md's "resolution-independent (A-10)" claim with coverage across
# tiny → 4K.  The renderer reads width/height from config and scales every
# constant by s = min(width/1024, height/600); there are no hard breakpoints,
# so the layout must stay sane at any size — not just the 1024x600 target.
# ---------------------------------------------------------------------------

RESOLUTION_MATRIX = [
    (480, 320),     # tiny — stresses the max() font/element floors
    (640, 480),
    (800, 480),     # common 5" HDMI
    (1024, 600),    # primary target (Waveshare 7")
    (1280, 720),
    (1920, 1080),   # FHD
    (3840, 2160),   # 4K
    # Non-16:9 / extreme aspect ratios — these are the whole reason the cover uses
    # min(sx, sy) and the layout splits sx (horizontal) from sy (vertical); a
    # landscape-only matrix never exercises the sx != sy branch.
    (1024, 1024),   # square
    (600, 1024),    # portrait
    (2560, 600),    # ultra-wide
    (1280, 1024),   # 5:4
]


@pytest.mark.parametrize("width,height", RESOLUTION_MATRIX)
def test_matrix_rects_positive_and_in_bounds(width, height):
    """No rect is negative, zero-sized, or off-screen at any resolution."""
    layout = get_now_playing_layout(width, height)
    for name, rect in all_rects(layout).items():
        assert rect.w > 0 and rect.h > 0, f"{name} non-positive at {width}x{height}: {rect}"
        assert rect.x >= 0 and rect.y >= 0, f"{name} negative coord at {width}x{height}: {rect}"
        assert rect.x + rect.w <= width, f"{name} right edge off-screen at {width}x{height}"
        assert rect.y + rect.h <= height, f"{name} bottom edge off-screen at {width}x{height}"


@pytest.mark.parametrize("width,height", RESOLUTION_MATRIX)
def test_matrix_cover_square_and_text_clear_of_cover(width, height):
    layout = get_now_playing_layout(width, height)
    assert layout.cover_art.w == layout.cover_art.h, f"cover not square at {width}x{height}"
    # The square side must be bound by the SMALLER scaled dimension (440px ref),
    # which is what keeps it from clipping at non-16:9 ratios — the property the
    # portrait/ultra-wide/square rows exist to exercise.
    expected_side = min(int(440 * width / 1024), int(440 * height / 600))
    assert layout.cover_art.w == expected_side, (
        f"cover side {layout.cover_art.w} != min-dimension {expected_side} at {width}x{height}"
    )
    cover_right = layout.cover_art.x + layout.cover_art.w
    for name in TEXT_PANEL_NAMES:
        assert getattr(layout, name).x >= cover_right, (
            f"{name} overlaps the cover horizontally at {width}x{height}"
        )


@pytest.mark.parametrize("width,height", RESOLUTION_MATRIX)
def test_matrix_vertical_flow_and_title_clears_bottom_block(width, height):
    """The top-flowing text block stays ordered, the bottom-anchored block stays
    ordered, and the title region never collides with the bottom meta/prev-next."""
    layout = get_now_playing_layout(width, height)
    # Top-down flow (each computed from the previous one).
    flow = ["track_text", "divider", "artist_text", "album_text", "genre_chips"]
    for upper, lower in zip(flow, flow[1:]):
        assert getattr(layout, upper).y < getattr(layout, lower).y, (
            f"{upper} not above {lower} at {width}x{height}"
        )
    # Bottom-anchored pair.
    assert layout.meta_text.y < layout.prev_next.y, f"meta below prev_next at {width}x{height}"
    # The title block must clear the bottom-anchored meta region (no overlap).
    title_bottom = layout.track_text.y + layout.track_text.h
    assert title_bottom <= layout.meta_text.y, (
        f"title block (bottom {title_bottom}) overlaps meta (top {layout.meta_text.y}) "
        f"at {width}x{height}"
    )


@pytest.mark.parametrize("width,height", RESOLUTION_MATRIX)
def test_matrix_font_floors_and_hierarchy(width, height):
    """Font floors hold and the size hierarchy is preserved at every resolution."""
    layout = get_now_playing_layout(width, height)
    assert layout.font_size_track >= 24
    assert layout.font_size_artist >= 18
    assert layout.font_size_album >= 14
    assert layout.font_size_header >= 9
    assert layout.font_size_track >= layout.font_size_artist >= layout.font_size_album
