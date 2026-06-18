"""Unit tests for the renderer's pure helpers — caches and color math.

DisplayRenderer itself needs pygame and a screen, but its hot-path caching
(v1.3.3) and palette math are deliberately pure Python/data so they can be
tested headlessly.  Importing src.display.renderer does NOT import pygame
(pygame imports live inside methods), so this module runs anywhere the rest
of the suite runs.

Verifies _BoundedCache (backing the palette, scaled-cover, and gradient caches):
  ✓ get() returns None on miss, the stored value on hit
  ✓ put() evicts the OLDEST entry beyond max_entries
  ✓ get() refreshes an entry's eviction position (LRU-ish)
  ✓ put() on an existing key replaces the value and refreshes position
  ✓ __contains__ / __len__

Verifies color helpers:
  ✓ _lerp_color endpoints and midpoint, with t clamped to [0, 1]
  ✓ _lerp_palette interpolates all five channels
  ✓ clamp_luminance brightens too-dark colors, leaves bright ones alone
"""
from src.display.renderer import (
    _BoundedCache,
    _lerp_color,
    _lerp_palette,
)
from src.display.palette import clamp_luminance
from src.metadata.models import DisplayPalette


# ---------------------------------------------------------------------------
# _BoundedCache
# ---------------------------------------------------------------------------

def test_get_returns_none_on_miss():
    cache = _BoundedCache(max_entries=2)
    assert cache.get("nope") is None


def test_put_then_get_round_trips():
    cache = _BoundedCache(max_entries=2)
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert "a" in cache
    assert len(cache) == 1


def test_eviction_drops_oldest_entry():
    cache = _BoundedCache(max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)  # Capacity 2 → "a" (oldest) evicted
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_get_refreshes_eviction_position():
    """Touching an entry protects it from the next eviction."""
    cache = _BoundedCache(max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")     # "a" is now most-recently-used
    cache.put("c", 3)  # Evicts "b", NOT "a"
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_put_existing_key_replaces_and_refreshes():
    cache = _BoundedCache(max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 99)  # Replace + refresh — "b" becomes oldest
    cache.put("c", 3)   # Evicts "b"
    assert cache.get("a") == 99
    assert cache.get("b") is None
    assert len(cache) == 2


def test_cache_of_one_holds_only_latest():
    cache = _BoundedCache(max_entries=1)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") is None
    assert cache.get("b") == 2


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def test_lerp_color_endpoints():
    a, b = (0, 0, 0), (100, 200, 50)
    assert _lerp_color(a, b, 0.0) == a
    assert _lerp_color(a, b, 1.0) == b


def test_lerp_color_midpoint():
    assert _lerp_color((0, 0, 0), (100, 200, 50), 0.5) == (50, 100, 25)


def test_lerp_color_clamps_t():
    a, b = (10, 10, 10), (20, 20, 20)
    assert _lerp_color(a, b, -5.0) == a
    assert _lerp_color(a, b, 5.0) == b


def test_lerp_palette_interpolates_all_channels():
    black = DisplayPalette(
        bg=(0, 0, 0), surface=(0, 0, 0), accent=(0, 0, 0),
        text=(0, 0, 0), muted=(0, 0, 0),
    )
    white = DisplayPalette(
        bg=(255, 255, 255), surface=(255, 255, 255), accent=(255, 255, 255),
        text=(255, 255, 255), muted=(255, 255, 255),
    )
    mid = _lerp_palette(black, white, 0.5)
    for channel in (mid.bg, mid.surface, mid.accent, mid.text, mid.muted):
        assert channel == (127, 127, 127)


def test_clamp_luminance_brightens_dark_colors():
    dark = (10, 10, 10)
    clamped = clamp_luminance(dark, min_lum=0.25)
    r, g, b = clamped
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    assert lum >= 0.24  # Allow for integer rounding


def test_clamp_luminance_leaves_bright_colors_alone():
    bright = (200, 180, 160)
    assert clamp_luminance(bright, min_lum=0.25) == bright


def test_clamp_luminance_leaves_pure_black_alone():
    """Black (lum == 0) can't be scaled up proportionally — documented behavior."""
    assert clamp_luminance((0, 0, 0), min_lum=0.25) == (0, 0, 0)
