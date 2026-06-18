"""Regression tests for the renderer hot-loop perf fixes (P-3, P-4, P-8).

P-3 — the status dot Surface is pre-rendered per pulse-phase bucket and cached,
      not freshly allocated + drawn every frame.
P-4 — the in-flight lerp palette is quantized so per-frame cache keys stay
      stable across many frames (the settled palette is still the exact target).
P-8 — the font cache is bounded like every other cache.
P-5 is covered by the existing palette tests (degenerate + normal covers).
"""
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from src.display.renderer import (  # noqa: E402
    DisplayRenderer, _BoundedCache, _DOT_CACHE_MAX, _FONT_CACHE_MAX,
    _quantize_palette, _PALETTE_LERP_QUANTIZE, _TRANSITION_SECS,
)
from src.display.palette import contrast_ratio
from src.display.layouts import get_now_playing_layout  # noqa: E402
from src.metadata.models import DisplayPalette  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _pygame_font():
    pygame.font.init()
    yield


def make_dot_renderer():
    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = False
    r._dot_cache = _BoundedCache(_DOT_CACHE_MAX)
    r._layout = get_now_playing_layout(1024, 600)
    return r


def make_font_renderer():
    r = DisplayRenderer.__new__(DisplayRenderer)
    r._font_cache = _BoundedCache(_FONT_CACHE_MAX)
    return r


# ---------------------------------------------------------------------------
# P-3 — dot Surface caching
# ---------------------------------------------------------------------------

def test_status_dot_surface_is_cached_not_reallocated(monkeypatch):
    r = make_dot_renderer()
    # Freeze the clock so both calls land in the same pulse-phase bucket.
    monkeypatch.setattr("src.display.renderer.time.monotonic", lambda: 1000.0)

    renders = []
    real = DisplayRenderer._render_dot_surface

    def spy(color, k, glow, rr):
        renders.append((color, k, glow, rr))
        return real(color, k, glow, rr)

    monkeypatch.setattr(r, "_render_dot_surface", spy)
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)

    r._draw_status_dot(target, r._layout, (200, 100, 50), animate=True, glow=True)
    r._draw_status_dot(target, r._layout, (200, 100, 50), animate=True, glow=True)

    assert len(renders) == 1          # rendered once; 2nd frame hit the cache
    assert len(r._dot_cache) == 1


def test_status_dot_distinct_colors_cache_separately(monkeypatch):
    r = make_dot_renderer()
    monkeypatch.setattr("src.display.renderer.time.monotonic", lambda: 1000.0)
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)
    r._draw_status_dot(target, r._layout, (200, 100, 50), animate=True, glow=True)
    r._draw_status_dot(target, r._layout, (10, 220, 90), animate=True, glow=True)
    assert len(r._dot_cache) == 2


# ---------------------------------------------------------------------------
# P-4 — palette quantization
# ---------------------------------------------------------------------------

def test_quantize_palette_snaps_gradient_channels():
    # High-contrast muted vs bg so the WCAG re-clamp is a no-op here.
    p = DisplayPalette(
        bg=(17, 33, 40), surface=(1, 1, 1), accent=(255, 255, 255),
        text=(100, 100, 100), muted=(220, 220, 220),
    )
    q = _quantize_palette(p)
    for channel in (q.bg, q.surface, q.accent, q.text):
        for v in channel:
            assert v % _PALETTE_LERP_QUANTIZE == 0
    assert q.bg == (16, 32, 32)        # 17→16, 33→32, 40→32


def test_quantize_palette_preserves_muted_contrast():
    """Flooring muted toward black must not drop it below the 4.5:1 WCAG floor
    vs bg (Full-Opacity Rule) — it's re-clamped after quantization."""
    p = DisplayPalette(
        bg=(20, 20, 20), surface=(44, 44, 44), accent=(200, 50, 50),
        text=(240, 240, 240), muted=(70, 70, 70),   # low contrast vs bg
    )
    q = _quantize_palette(p)
    assert contrast_ratio(q.muted, q.bg) >= 4.5


def test_animated_palette_is_quantized_mid_transition(monkeypatch):
    """Mid-lerp, the gradient channels must be quantized so cache keys stay
    stable across frames (P-4), while muted stays WCAG-readable."""
    r = DisplayRenderer.__new__(DisplayRenderer)
    # High-contrast muted in both endpoints so quantization is the visible effect.
    a = DisplayPalette((0, 0, 0), (40, 40, 40), (255, 0, 0), (250, 250, 250), (200, 200, 200))
    b = DisplayPalette((48, 48, 48), (80, 80, 80), (0, 0, 255), (240, 240, 240), (210, 210, 210))
    r._current_palette = a
    r._target_palette = b
    r._transition_start = 0.0
    monkeypatch.setattr("src.display.renderer.time.monotonic",
                        lambda: _TRANSITION_SECS * 0.5)  # t = 0.5

    pal = r._animated_palette()
    for channel in (pal.bg, pal.surface, pal.accent, pal.text):
        for v in channel:
            assert v % _PALETTE_LERP_QUANTIZE == 0       # raw lerp would be odd
    assert pal.bg != a.bg and pal.bg != b.bg             # genuinely mid-transition
    assert contrast_ratio(pal.muted, pal.bg) >= 4.5     # invariant held


def test_animated_palette_settles_to_exact_target(monkeypatch):
    r = DisplayRenderer.__new__(DisplayRenderer)
    r._current_palette = DisplayPalette((1, 1, 1), (2, 2, 2), (3, 3, 3), (4, 4, 4), (5, 5, 5))
    r._target_palette = DisplayPalette((17, 33, 250), (9, 9, 9), (255, 1, 1), (7, 7, 7), (3, 3, 3))
    r._transition_start = 0.0

    # Long after the transition window → exact (un-quantized) target.
    monkeypatch.setattr("src.display.renderer.time.monotonic", lambda: 10_000.0)
    assert r._animated_palette() == r._target_palette
    assert r._animated_palette().bg == (17, 33, 250)   # not quantized to (16,32,240)


# ---------------------------------------------------------------------------
# P-8 — bounded font cache
# ---------------------------------------------------------------------------

def test_font_cache_is_bounded():
    r = make_font_renderer()
    assert isinstance(r._font_cache, _BoundedCache)
    for size in range(_FONT_CACHE_MAX + 20):
        r._font("mono", 8 + size)
    assert len(r._font_cache) <= _FONT_CACHE_MAX
