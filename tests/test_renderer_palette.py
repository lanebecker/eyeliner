"""Unit tests for DisplayRenderer._queue_palette — headless (new in v1.3.5).

_queue_palette never imports pygame (only extract_palette touches PIL, and
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
import io

from PIL import Image

from src.display.renderer import (
    DisplayRenderer,
    _BoundedCache,
    _PALETTE_CACHE_MAX,
)
from src.display.cover_cache import CoverArtCache
from src.metadata.models import DisplayPalette, FALLBACK_PALETTE


def _write_cover(store, url, color=(180, 90, 40)):
    """Write a real (decodable) cover image to the store's path for *url*."""
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    path = store.path_for(url)
    path.write_bytes(buf.getvalue())
    return path


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
    # Empty dir → no cover files on disk; _queue_palette reads paths via the store.
    r._cover_store = CoverArtCache(tmp_path)
    r._palette_cache = _BoundedCache(_PALETTE_CACHE_MAX)
    r._current_palette = FALLBACK_PALETTE
    r._target_palette = FALLBACK_PALETTE
    r._transition_start = 0.0
    r._wanted_cover_url = None
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


def test_new_palette_after_steady_state_retargets(tmp_path, monkeypatch):
    r = make_renderer(tmp_path)
    r._palette_cache.put("http://x/a.jpg", ALT_PALETTE)

    # Deterministic clock so "retarget restarts the timer" is provable with a
    # strict >, instead of the old flaky `!= first_start or == 0.0` escape that
    # silently passed when both calls landed in the same monotonic tick (T-6).
    monkeypatch.setattr("src.display.renderer.time.monotonic", lambda: 100.0)
    r._queue_palette("http://x/a.jpg")
    first_start = r._transition_start
    assert first_start == 100.0

    other = DisplayPalette(
        bg=(10, 10, 20), surface=(22, 22, 44), accent=(80, 80, 220),
        text=(230, 230, 240), muted=(130, 130, 150),
    )
    r._palette_cache.put("http://x/b.jpg", other)
    monkeypatch.setattr("src.display.renderer.time.monotonic", lambda: 200.0)
    r._queue_palette("http://x/b.jpg")         # Different album → new transition

    assert r._target_palette == other
    assert r._transition_start == 200.0
    assert r._transition_start > first_start    # strict: the timer was restarted


# ---------------------------------------------------------------------------
# P-9 — palette extraction never decodes on the event loop
# ---------------------------------------------------------------------------

def test_queue_palette_does_not_decode_disk_cover(tmp_path):
    """P-9: _queue_palette runs inside set_track's Signal callback on the event
    loop, so it must NOT decode — even when the cover file is already on disk and
    the palette isn't cached yet.  It targets FALLBACK; the async path extracts."""
    r = make_renderer(tmp_path)
    url = "https://i.discogs.com/c.jpg"
    _write_cover(r._cover_store, url)          # file present, palette NOT cached
    r._queue_palette(url)
    assert r._target_palette == FALLBACK_PALETTE   # no inline extraction happened
    assert r._palette_cache.get(url) is None


async def test_extract_palette_async_extracts_off_loop_and_requeues(tmp_path):
    """The off-loop path decodes (in an executor), caches, and re-queues the
    transition to the real palette."""
    r = make_renderer(tmp_path)
    url = "https://i.discogs.com/c.jpg"
    _write_cover(r._cover_store, url)
    r._wanted_cover_url = url                   # this cover is the one on screen

    await r._extract_palette_async(url)

    cached = r._palette_cache.get(url)
    assert cached is not None
    assert cached != FALLBACK_PALETTE          # a real palette was extracted
    assert r._target_palette == cached         # and re-queued as the target


async def test_extract_palette_async_does_not_overwrite_newer_cover(tmp_path):
    """Stale-decode guard: a slow extraction for a PREVIOUS track must cache its
    palette but NOT retarget the live transition over the cover now on screen."""
    r = make_renderer(tmp_path)
    old_url = "https://i.discogs.com/a.jpg"
    _write_cover(r._cover_store, old_url)
    r._wanted_cover_url = "https://i.discogs.com/b.jpg"   # track B is now current

    await r._extract_palette_async(old_url)               # A's late decode lands

    assert r._palette_cache.get(old_url) is not None      # cached for later reuse
    assert r._target_palette == FALLBACK_PALETTE          # but NOT painted over B


async def test_extract_palette_async_noop_when_file_missing(tmp_path):
    """No cover on disk yet → no extraction, no cache entry (the prefetch
    download path will call back here once the file lands)."""
    r = make_renderer(tmp_path)
    url = "https://i.discogs.com/missing.jpg"
    await r._extract_palette_async(url)
    assert r._palette_cache.get(url) is None
    assert r._target_palette == FALLBACK_PALETTE


async def test_extract_palette_async_uses_cache_without_decoding(tmp_path):
    """Already-cached palette → just re-queue it; no file or decode needed."""
    r = make_renderer(tmp_path)
    url = "https://i.discogs.com/c.jpg"
    r._palette_cache.put(url, ALT_PALETTE)     # cached; deliberately no file on disk
    r._wanted_cover_url = url
    await r._extract_palette_async(url)
    assert r._target_palette == ALT_PALETTE


# ---------------------------------------------------------------------------
# B-22 — the static-frame key uses a stable cover-version token, not id(cover)
# ---------------------------------------------------------------------------

async def test_prefetch_cover_bumps_cover_version(tmp_path):
    """When a cover lands on disk, _prefetch_cover bumps a monotonic version
    counter (and marks dirty), so the static-frame cache key changes and the
    frame recomposes — a stable signal, unlike the old id(cover) which could be
    recycled after GC (B-22)."""
    r = make_renderer(tmp_path)
    r._cover_version = 0
    r._dirty = False
    url = "https://i.discogs.com/c.jpg"
    _write_cover(r._cover_store, url)          # warm cache: file already on disk

    await r._prefetch_cover(url)

    assert r._cover_version == 1               # version advanced → forces recompose
    assert r._dirty is True


async def test_prefetch_cover_warm_cache_themes_without_download(tmp_path):
    """Warm-cache path: a cover already on disk (no download) must still be
    extracted + themed by _prefetch_cover — the download block is skipped, but
    palette extraction is NOT (P-9 warm-cache promise; guards against a refactor
    that moves the extract behind the download)."""
    r = make_renderer(tmp_path)
    r._cover_version = 0
    r._dirty = False
    url = "https://i.discogs.com/c.jpg"
    r._wanted_cover_url = url                  # this cover is the one on screen
    _write_cover(r._cover_store, url)          # already on disk → no download leg

    await r._prefetch_cover(url)

    themed = r._palette_cache.get(url)
    assert themed is not None and themed != FALLBACK_PALETTE  # extracted off-loop
    assert r._target_palette == themed                        # and queued as target
