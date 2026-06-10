"""Unit tests for DisplayRenderer._queue_palette — headless (new in v1.3.5).

_queue_palette never imports pygame (only _extract_palette touches PIL, and
these tests avoid that path or go through the in-memory palette cache), so
the renderer's palette-transition decisions can be pinned down anywhere the
rest of the suite runs.  The renderer is built via __new__ with just the
attributes _queue_palette reads — the same pattern test_resolver.py uses.

Verifies:
  ✓ dynamic_theming=False → no retarget at all
  ✓ Unknown URL with no cached cover file → FALLBACK_PALETTE target
  ✓ Palette-cache hit → cached palette becomes the target, transition starts
  ✓ Same-target skip (v1.3.5): re-queuing an unchanged palette does NOT
    restart the 1s transition (previously every track commit re-triggered a
    30 fps transition lerping a palette to itself)
  ✓ A genuinely new palette mid-steady-state retargets and restarts the timer
"""
from src.display.renderer import (
    DisplayRenderer,
    _BoundedCache,
    _PALETTE_CACHE_MAX,
)
from src.metadata.models import DisplayPalette, FALLBACK_PALETTE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALT_PALETTE = DisplayPalette(
    bg=(20, 10, 10), surface=(40, 22, 22), accent=(220, 80, 80),
    text=(240, 230, 230), muted=(150, 130, 130),
)


def make_renderer(tmp_path, dynamic_theming=True):
    """Build a renderer skeleton with only what _queue_palette touches."""
    r = DisplayRenderer.__new__(DisplayRenderer)
    r.dynamic_theming = dynamic_theming
    r.cache_dir = tmp_path                      # Empty dir → no cover files on disk
    r._palette_cache = _BoundedCache(_PALETTE_CACHE_MAX)
    r._current_palette = FALLBACK_PALETTE
    r._target_palette = FALLBACK_PALETTE
    r._transition_start = 0.0
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_disabled_theming_never_retargets(tmp_path):
    r = make_renderer(tmp_path, dynamic_theming=False)
    r._palette_cache.put("http://x/cover.jpg", ALT_PALETTE)
    r._queue_palette("http://x/cover.jpg")
    assert r._target_palette == FALLBACK_PALETTE
    assert r._transition_start == 0.0


def test_unknown_url_without_cached_file_targets_fallback(tmp_path):
    """No palette cached and no cover file on disk → FALLBACK_PALETTE.
    Since the target is already FALLBACK, the same-target skip also means
    the transition timer stays untouched."""
    r = make_renderer(tmp_path)
    r._queue_palette("http://x/never-downloaded.jpg")
    assert r._target_palette == FALLBACK_PALETTE
    assert r._transition_start == 0.0


def test_none_url_targets_fallback(tmp_path):
    r = make_renderer(tmp_path)
    r._target_palette = ALT_PALETTE  # Pretend an album palette is active
    r._queue_palette(None)
    assert r._target_palette == FALLBACK_PALETTE
    assert r._transition_start > 0.0  # Real change → transition started


def test_palette_cache_hit_retargets_and_starts_transition(tmp_path):
    r = make_renderer(tmp_path)
    r._palette_cache.put("http://x/cover.jpg", ALT_PALETTE)
    r._queue_palette("http://x/cover.jpg")
    assert r._target_palette == ALT_PALETTE
    assert r._transition_start > 0.0


def test_same_target_skip_does_not_restart_transition(tmp_path):
    """Regression (v1.3.5): tracks from the same album share a cover, so
    every commit used to restart the 1s transition — 30 fps rendering and
    per-frame gradient regeneration, lerping a palette to itself."""
    r = make_renderer(tmp_path)
    r._palette_cache.put("http://x/cover.jpg", ALT_PALETTE)

    r._queue_palette("http://x/cover.jpg")     # Track 1: genuine transition
    first_start = r._transition_start
    assert first_start > 0.0

    r._queue_palette("http://x/cover.jpg")     # Track 2, same album
    assert r._transition_start == first_start  # Timer NOT restarted


def test_new_palette_after_steady_state_retargets(tmp_path):
    r = make_renderer(tmp_path)
    r._palette_cache.put("http://x/a.jpg", ALT_PALETTE)
    r._queue_palette("http://x/a.jpg")
    first_start = r._transition_start

    other = DisplayPalette(
        bg=(10, 10, 20), surface=(22, 22, 44), accent=(80, 80, 220),
        text=(230, 230, 240), muted=(130, 130, 150),
    )
    r._palette_cache.put("http://x/b.jpg", other)
    r._queue_palette("http://x/b.jpg")         # Different album → new transition

    assert r._target_palette == other
    assert r._transition_start >= first_start
    assert r._transition_start != first_start or first_start == 0.0
