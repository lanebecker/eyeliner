"""Standalone unit tests for PaletteTransition (ARCH-3).

The point of extracting the cross-fade out of DisplayRenderer is that the lerp
state machine can be exercised with NO DisplayRenderer instance, NO pygame
window, NO display surface — just a PaletteTransition built directly over a
palette cache. These drive queue()/animated() and assert the transition
decisions (retarget, same-target skip, snap-before-retarget) and the
interpolation (quantization mid-lerp, exact settle) directly.

Only src.display.palette (pure colour math) and a bounded cache are needed. The
cache impl is borrowed from renderer.py, exactly as test_typography borrows its
caches — importing renderer is headless (pygame is lazy), so no window is
created.
"""
import pytest

from src.display.palette_transition import (
    PaletteTransition,
    _lerp_palette,
    _quantize_palette,
    _PALETTE_LERP_QUANTIZE,
    _TRANSITION_SECS,
)
from src.display.palette import DisplayPalette, FALLBACK_PALETTE, contrast_ratio
from src.display.renderer import _BoundedCache, _PALETTE_CACHE_MAX


ALT_PALETTE = DisplayPalette(
    bg=(20, 10, 10), surface=(40, 22, 22), accent=(220, 80, 80),
    text=(240, 230, 230), muted=(150, 130, 130),
)
OTHER_PALETTE = DisplayPalette(
    bg=(10, 10, 30), surface=(22, 22, 44), accent=(90, 90, 230),
    text=(230, 230, 240), muted=(130, 130, 150),
)


def make_cache():
    return _BoundedCache(_PALETTE_CACHE_MAX)


@pytest.fixture
def clock(monkeypatch):
    """A settable monotonic clock for palette_transition."""
    state = {"t": 1000.0}
    monkeypatch.setattr("src.display.palette_transition.time.monotonic",
                        lambda: state["t"])
    return state


# ---------------------------------------------------------------------------
# queue() — retarget decisions
# ---------------------------------------------------------------------------

def test_disabled_theming_never_retargets(clock):
    pt = PaletteTransition()
    cache = make_cache()
    cache.put("http://x/cover.jpg", ALT_PALETTE)
    pt.queue("http://x/cover.jpg", cache, dynamic_theming=False)
    assert pt.target == FALLBACK_PALETTE
    assert pt.transition_start == 0.0


def test_none_url_targets_fallback_and_starts_transition(clock):
    pt = PaletteTransition(initial=ALT_PALETTE)  # some album palette is live
    pt.target = ALT_PALETTE
    pt.queue(None, make_cache(), dynamic_theming=True)
    assert pt.target == FALLBACK_PALETTE
    assert pt.transition_start == 1000.0          # real change → transition started


def test_uncached_url_targets_fallback_no_decode(clock):
    """P-9: queue() never decodes — an unknown, uncached URL targets FALLBACK and
    does NOT populate the cache (the async extractor does that off-loop)."""
    pt = PaletteTransition()
    cache = make_cache()
    pt.queue("http://x/never-downloaded.jpg", cache, dynamic_theming=True)
    assert pt.target == FALLBACK_PALETTE
    assert cache.get("http://x/never-downloaded.jpg") is None


def test_cache_hit_retargets_and_starts_transition(clock):
    pt = PaletteTransition()
    cache = make_cache()
    cache.put("http://x/cover.jpg", ALT_PALETTE)
    pt.queue("http://x/cover.jpg", cache, dynamic_theming=True)
    assert pt.target == ALT_PALETTE
    assert pt.transition_start == 1000.0


def test_same_target_does_not_restart_the_timer(clock):
    """v1.3.5 same-target skip: re-queuing an unchanged palette must NOT restart
    the 1s transition (every track commit re-queues; same-album tracks share a
    cover URL)."""
    pt = PaletteTransition()
    cache = make_cache()
    cache.put("http://x/cover.jpg", ALT_PALETTE)

    pt.queue("http://x/cover.jpg", cache, dynamic_theming=True)   # genuine change
    first_start = pt.transition_start
    clock["t"] = 1005.0
    pt.queue("http://x/cover.jpg", cache, dynamic_theming=True)   # same album again
    assert pt.transition_start == first_start                     # timer NOT restarted


def test_new_target_mid_steady_state_restarts_the_timer(clock):
    pt = PaletteTransition()
    cache = make_cache()
    cache.put("http://x/a.jpg", ALT_PALETTE)
    cache.put("http://x/b.jpg", OTHER_PALETTE)

    pt.queue("http://x/a.jpg", cache, dynamic_theming=True)
    first_start = pt.transition_start
    clock["t"] = 1200.0
    pt.queue("http://x/b.jpg", cache, dynamic_theming=True)        # different album
    assert pt.target == OTHER_PALETTE
    assert pt.transition_start == 1200.0
    assert pt.transition_start > first_start


def test_queue_snaps_current_to_live_value_before_retargeting(clock):
    """A mid-transition track change must lerp from what's ON SCREEN now, not
    from the stale starting palette — queue() snaps current to animated() first."""
    a = DisplayPalette((0, 0, 0), (40, 40, 40), (255, 0, 0), (250, 250, 250), (200, 200, 200))
    b = DisplayPalette((48, 48, 48), (80, 80, 80), (0, 0, 255), (240, 240, 240), (210, 210, 210))
    pt = PaletteTransition(initial=a)
    pt.target = b
    pt.transition_start = 1000.0
    cache = make_cache()
    cache.put("http://x/c.jpg", OTHER_PALETTE)

    clock["t"] = 1000.0 + _TRANSITION_SECS * 0.5   # halfway through a→b
    live = pt.animated()
    pt.queue("http://x/c.jpg", cache, dynamic_theming=True)        # retarget to C mid-lerp

    assert pt.current == live                        # snapped to the on-screen value
    assert pt.current != a and pt.current != b       # genuinely mid-transition, not stale
    assert pt.target == OTHER_PALETTE


# ---------------------------------------------------------------------------
# animated() — interpolation
# ---------------------------------------------------------------------------

def test_animated_settles_to_exact_target(clock):
    pt = PaletteTransition(
        initial=DisplayPalette((1, 1, 1), (2, 2, 2), (3, 3, 3), (4, 4, 4), (5, 5, 5)))
    pt.target = DisplayPalette((17, 33, 250), (9, 9, 9), (255, 1, 1), (7, 7, 7), (3, 3, 3))
    pt.transition_start = 1000.0
    clock["t"] = 1000.0 + 10_000.0                   # long past the window
    assert pt.animated() == pt.target
    assert pt.animated().bg == (17, 33, 250)         # exact target, NOT quantized


def test_animated_mid_transition_is_quantized(clock):
    a = DisplayPalette((0, 0, 0), (40, 40, 40), (255, 0, 0), (250, 250, 250), (200, 200, 200))
    b = DisplayPalette((48, 48, 48), (80, 80, 80), (0, 0, 255), (240, 240, 240), (210, 210, 210))
    pt = PaletteTransition(initial=a)
    pt.target = b
    pt.transition_start = 1000.0
    clock["t"] = 1000.0 + _TRANSITION_SECS * 0.5     # t = 0.5

    pal = pt.animated()
    for channel in (pal.bg, pal.surface, pal.text):
        for v in channel:
            assert v % _PALETTE_LERP_QUANTIZE == 0    # gradient roles snapped to the step
    assert pal.bg != a.bg and pal.bg != b.bg          # genuinely mid-transition


def test_animated_at_start_returns_a_quantized_current(clock):
    """At t≈0 the lerp is ~current, but still routed through quantization so the
    per-frame cache key is stable from the first frame (P-4)."""
    a = DisplayPalette((10, 20, 33), (50, 60, 70), (255, 0, 0), (250, 250, 250), (200, 200, 200))
    b = DisplayPalette((200, 200, 200), (220, 220, 220), (0, 0, 255), (240, 240, 240), (210, 210, 210))
    pt = PaletteTransition(initial=a)
    pt.target = b
    pt.transition_start = 1000.0
    clock["t"] = 1000.0                               # t = 0 exactly (not >= 1.0)
    pal = pt.animated()
    for v in pal.bg:
        assert v % _PALETTE_LERP_QUANTIZE == 0


# ---------------------------------------------------------------------------
# pure helpers (module-level, shared with the renderer's gradient/divider)
# ---------------------------------------------------------------------------

def test_lerp_palette_interpolates_all_channels():
    black = DisplayPalette((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0))
    white = DisplayPalette((255, 255, 255), (254, 254, 254), (253, 253, 253),
                           (252, 252, 252), (251, 251, 251))
    mid = _lerp_palette(black, white, 0.5)
    assert mid.bg == (127, 127, 127)
    assert mid.surface == (127, 127, 127)
    assert mid.muted == (125, 125, 125)


def test_quantize_preserves_muted_wcag_floor():
    """Flooring muted toward black during the lerp must not drop it below the
    4.5:1 WCAG floor vs the gradient background — it is re-clamped."""
    p = DisplayPalette(
        bg=(20, 20, 20), surface=(44, 44, 44), accent=(200, 50, 50),
        text=(240, 240, 240), muted=(70, 70, 70),
    )
    q = _quantize_palette(p)
    assert contrast_ratio(q.muted, q.bg) >= 4.5
