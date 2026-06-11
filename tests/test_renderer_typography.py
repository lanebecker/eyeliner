"""Unit tests for the v1.4.0 typography/fidelity helpers in DisplayRenderer.

Covers the design-translation behaviors added in the Phase 1 fidelity work:

  ✓ _wrap_lines — single source of truth for word-wrapping
  ✓ _fit_wrapped — shrink-instead-of-ellipsis for artist (1 line) and
    album (2 lines): long strings step DOWN in size, short strings keep
    the base size
  ✓ _ellipsize — the one sanctioned ellipsis (PREV/NEXT panel only)
  ✓ _chip_texts — genre chips capped at 3 with a '+N' overflow indicator
  ✓ _contrast_ratio / _ensure_contrast — WCAG math + the muted-role
    4.5:1 clamp from DESIGN.md's Full-Opacity Rule
  ✓ a full headless _compose_now_playing smoke test (dummy SDL driver)

pygame.font is initialized once per module; no real display is required
except for the smoke test, which uses SDL's dummy video driver.
"""
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from src.display.renderer import (  # noqa: E402
    DisplayRenderer,
    _BoundedCache,
    _LABEL_CACHE_MAX,
    _contrast_ratio,
    _ensure_contrast,
)
from src.display.layouts import get_now_playing_layout  # noqa: E402
from src.metadata.models import FALLBACK_PALETTE  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LONG_ALBUM = "Bachelor No. 2 or, the Last Remains of the Dodo"
LONG_ARTIST = "...And You Will Know Us by the Trail of Dead"


@pytest.fixture(scope="module", autouse=True)
def _pygame_font():
    pygame.font.init()
    yield
    # Leave pygame initialized — other modules may share the process.


def make_renderer():
    """Renderer skeleton with only what the typography helpers touch."""
    r = DisplayRenderer.__new__(DisplayRenderer)
    r._font_cache = {}
    r._label_cache = _BoundedCache(_LABEL_CACHE_MAX)
    return r


class FakeTrack:
    """Minimal stand-in for TrackMetadata, display fields only."""
    title = "Catholic Block"
    artist = "Sonic Youth"
    album = "Sister"
    genres = ["Noise Rock", "Alternative Rock", "Post-Punk", "Indie", "Rock"]
    year = "1987"
    label = "SST Records"
    catalog_number = "SST-134"
    cover_art_url = None
    side_letter = "A"
    side_position = 4
    side_total = 6
    track_display = "A4"
    prev_track_title = "Stereo Sanctity"
    next_track_title = "Beauty Lies in the Eye"


# ---------------------------------------------------------------------------
# _wrap_lines
# ---------------------------------------------------------------------------

def test_wrap_lines_short_text_is_one_line():
    r = make_renderer()
    font = r._font("text", 24)
    assert r._wrap_lines("Sister", font, 400) == ["Sister"]


def test_wrap_lines_long_text_wraps():
    r = make_renderer()
    font = r._font("text", 24)
    lines = r._wrap_lines(LONG_ALBUM, font, 200)
    assert len(lines) > 1
    # No line may exceed the available width
    assert all(font.size(line)[0] <= 200 for line in lines)


def test_wrap_lines_empty_text_is_empty():
    r = make_renderer()
    font = r._font("text", 24)
    assert r._wrap_lines("", font, 400) == []


# ---------------------------------------------------------------------------
# _fit_wrapped — shrink-instead-of-ellipsis
# ---------------------------------------------------------------------------

def test_fit_keeps_base_size_when_text_fits():
    r = make_renderer()
    size, lines = r._fit_wrapped("Sister", "title", 32, 440, max_lines=2)
    assert size == 32
    assert lines == ["Sister"]


def test_fit_shrinks_long_album_to_two_lines():
    r = make_renderer()
    size, lines = r._fit_wrapped(LONG_ALBUM, "title", 32, 440, max_lines=2)
    assert len(lines) <= 2
    assert size <= 32


def test_fit_shrinks_long_artist_to_single_line():
    r = make_renderer()
    size, lines = r._fit_wrapped(LONG_ARTIST, "text", 48, 440, max_lines=1, min_size=18)
    assert len(lines) == 1
    assert size < 48  # a 44-char artist cannot hold 48px in a 440px column
    assert size >= 18


def test_fit_never_goes_below_min_size():
    r = make_renderer()
    absurd = "Supercalifragilisticexpialidocious " * 10
    size, _ = r._fit_wrapped(absurd, "text", 48, 200, max_lines=1, min_size=18)
    assert size == 18  # floor respected even when the text can't ever fit


# ---------------------------------------------------------------------------
# _ellipsize — PREV/NEXT panel only
# ---------------------------------------------------------------------------

def test_ellipsize_leaves_short_text_alone():
    r = make_renderer()
    font = r._font("text", 14)
    assert r._ellipsize("Schizophrenia", font, 400) == "Schizophrenia"


def test_ellipsize_truncates_with_ellipsis_and_fits():
    r = make_renderer()
    font = r._font("text", 14)
    out = r._ellipsize("The Diamond Sea (Live at the Continental Club)", font, 120)
    assert out.endswith("…")
    assert font.size(out)[0] <= 120


# ---------------------------------------------------------------------------
# _chip_texts — cap at 3 with +N overflow
# ---------------------------------------------------------------------------

def test_chip_texts_three_or_fewer_pass_through():
    r = make_renderer()
    assert r._chip_texts(["Folk"]) == ["Folk"]
    assert r._chip_texts(["A", "B", "C"]) == ["A", "B", "C"]


def test_chip_texts_overflow_collapses_to_plus_n():
    r = make_renderer()
    assert r._chip_texts(["A", "B", "C", "D", "E"]) == ["A", "B", "C", "+2"]


def test_chip_texts_empty_is_empty():
    r = make_renderer()
    assert r._chip_texts([]) == []


# ---------------------------------------------------------------------------
# Contrast clamp (DESIGN.md Full-Opacity Rule)
# ---------------------------------------------------------------------------

def test_contrast_ratio_black_vs_white_is_21():
    assert _contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)


def test_ensure_contrast_passes_through_compliant_colors():
    # Fallback muted (#8a857c) against fallback bg (#0a0a0a) already passes
    assert _ensure_contrast(FALLBACK_PALETTE.muted, FALLBACK_PALETTE.bg) == FALLBACK_PALETTE.muted


def test_ensure_contrast_lightens_failing_colors():
    dark_muted = (60, 60, 60)  # ~1.9:1 against near-black — fails
    fixed = _ensure_contrast(dark_muted, (10, 10, 10), min_ratio=4.5)
    assert fixed != dark_muted
    assert _contrast_ratio(fixed, (10, 10, 10)) >= 4.5


def test_ensure_contrast_on_cool_dark_background():
    # DESIGN.md calls out Cavetown's #0e1a2a as a contrast hazard
    cool_bg = (14, 26, 42)
    fixed = _ensure_contrast((90, 88, 84), cool_bg, min_ratio=4.5)
    assert _contrast_ratio(fixed, cool_bg) >= 4.5


# ---------------------------------------------------------------------------
# Full compose smoke test (headless)
# ---------------------------------------------------------------------------

def test_compose_now_playing_smoke():
    """The full static-frame composition runs headless without error and
    returns a screen-sized Surface — catches API drift across all the
    drawing helpers in one go."""
    pygame.display.init()
    pygame.display.set_mode((1024, 600))
    try:
        r = make_renderer()
        r.width, r.height = 1024, 600
        r.reduced_motion = False
        r._layout = get_now_playing_layout(1024, 600)
        r._gradient_key = None
        r._gradient_surface = None
        r._shadow_key = None
        r._shadow_surface = None

        frame = r._compose_now_playing(FakeTrack(), r._layout, FALLBACK_PALETTE, cover=None)
        assert frame.get_size() == (1024, 600)

        # The animated dot draws over the composed frame without error too
        screen = pygame.display.get_surface()
        screen.blit(frame, (0, 0))
        r._draw_status_dot(screen, r._layout, FALLBACK_PALETTE.accent, animate=True, glow=True)
    finally:
        pygame.display.quit()
