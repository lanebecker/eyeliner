"""Regression tests for B-12, B-17, B-18 (renderer robustness).

B-12 — extract_palette must not IndexError on a degenerate cover (solid colour
       / tiny image) that quantizes to fewer than 8 palette entries.
B-17 — the genre "+N" overflow chip must reflect how many chips ACTUALLY fit,
       not a fixed cap of 3.
B-18 — a corrupt cached cover is re-fetched within the track (not left as a
       placeholder until the next state change).
"""
import asyncio
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
from PIL import Image  # noqa: E402

from src.display.renderer import DisplayRenderer, _BoundedCache  # noqa: E402
from src.display.cover_cache import CoverArtCache  # noqa: E402
from src.display.palette import extract_palette  # noqa: E402
from src.display.layouts import get_now_playing_layout, Rect  # noqa: E402
from src.metadata.models import DisplayPalette, FALLBACK_PALETTE  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _pygame_font():
    pygame.font.init()
    yield


def make_renderer():
    r = DisplayRenderer.__new__(DisplayRenderer)
    r._font_cache = _BoundedCache(64)   # P-8: matches the real bounded cache
    r._label_cache = _BoundedCache(64)
    r._dot_cache = _BoundedCache(64)
    return r


# ---------------------------------------------------------------------------
# B-12 — degenerate covers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size,color", [
    ((1, 1), (120, 40, 30)),      # 1×1
    ((80, 80), (20, 80, 160)),    # solid colour
    ((2, 2), (0, 0, 0)),          # tiny + pure black
])
def test_extract_palette_survives_degenerate_cover(tmp_path, size, color):
    p = tmp_path / "cover.png"
    Image.new("RGB", size, color).save(p)
    pal = extract_palette(p)           # must not raise
    assert isinstance(pal, DisplayPalette)
    # A real palette was derived, not the IndexError→FALLBACK degradation.
    assert pal is not FALLBACK_PALETTE


# ---------------------------------------------------------------------------
# B-17 — overflow reflects what fit
# ---------------------------------------------------------------------------

def test_genre_overflow_counts_what_actually_fit():
    r = make_renderer()
    layout = get_now_playing_layout(1024, 600)

    rendered = []
    wide_label = pygame.Surface((200, 20), pygame.SRCALPHA)  # 1 chip per row

    def fake_render(text, size, color, tracking):
        rendered.append(text)
        return wide_label

    r._render_tracked = fake_render
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)

    # A box only tall/wide enough for a single chip → only 1 genre fits.
    chips_rect = Rect(0, 0, 130, 26)
    r._draw_genre_chips(target, ["G1", "G2", "G3", "G4", "G5"], layout, FALLBACK_PALETTE,
                        chips_rect=chips_rect)

    # 1 genre fit → overflow must be "+4" (5 − 1), never the fixed-cap "+2".
    assert "+4" in rendered
    assert "+2" not in rendered


def test_genre_no_overflow_chip_when_all_fit():
    r = make_renderer()
    layout = get_now_playing_layout(1024, 600)
    rendered = []
    small_label = pygame.Surface((20, 16), pygame.SRCALPHA)
    r._render_tracked = lambda *a: (rendered.append(a[0]) or small_label)
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)

    chips_rect = Rect(0, 0, 1000, 200)  # plenty of room
    r._draw_genre_chips(target, ["Rock", "Jazz"], layout, FALLBACK_PALETTE,
                        chips_rect=chips_rect)

    assert rendered == ["Rock", "Jazz"]            # no overflow chip
    assert not any(t.startswith("+") for t in rendered)


# ---------------------------------------------------------------------------
# B-18 — corrupt cached cover is re-fetched within the track
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_corrupt_cached_cover_triggers_refetch(tmp_path):
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()

    url = "https://i.discogs.com/cover.jpg"
    cache_path = r._cover_store.path_for(url)
    cache_path.write_bytes(b"this is not a valid image")  # corrupt cover

    refetched = []

    async def fake_prefetch(u):
        refetched.append(u)

    r._prefetch_cover = fake_prefetch

    result = r._load_cover(url, 100, 100)

    assert result is None
    assert not cache_path.exists()        # the corrupt file was unlinked
    await asyncio.sleep(0)                 # let the spawned re-fetch run
    assert refetched == [url]              # a re-fetch was scheduled in-track


@pytest.mark.asyncio
async def test_missing_cover_does_not_refetch(tmp_path):
    """A simply-absent cover (not yet downloaded) must NOT spawn a re-fetch from
    _load_cover — that path is owned by the state-change prefetch."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    refetched = []
    r._prefetch_cover = lambda u: refetched.append(u)  # noqa: E731

    result = r._load_cover("https://i.discogs.com/missing.jpg", 100, 100)

    assert result is None
    assert refetched == []


# ---------------------------------------------------------------------------
# P-10 — boot-arc rotation is cached by angle bucket (not re-rotated per frame)
# ---------------------------------------------------------------------------

def test_boot_arc_rotation_is_bucketed(monkeypatch):
    """The boot arc spins for the whole identification wait; rotating it every
    frame is wasteful.  Frames landing in the same angle bucket must reuse a
    cached rotated Surface (one rotate), and only a new bucket triggers another."""
    from src.display.renderer import (
        _BoundedCache as _BC,
        _ARC_ROT_BUCKETS,
        _ARC_ROT_CACHE_MAX,
        _ARC_SECS,
    )

    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = False
    r._arc_segment = None
    r._arc_rot_cache = _BC(_ARC_ROT_CACHE_MAX)

    layout = get_now_playing_layout(1024, 600)
    target = pygame.Surface((1024, 600))

    calls = []
    real_rotate = pygame.transform.rotate
    monkeypatch.setattr(
        pygame.transform, "rotate",
        lambda surf, angle: calls.append(angle) or real_rotate(surf, angle),
    )

    bucket_dt = _ARC_SECS / _ARC_ROT_BUCKETS
    # Two frames inside bucket 0 → exactly one rotate (second is a cache hit).
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, 0.0)
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, bucket_dt * 0.4)
    assert len(calls) == 1

    # A frame in bucket 1 → one additional rotate.
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, bucket_dt * 1.5)
    assert len(calls) == 2


def test_boot_arc_reduced_motion_never_rotates(monkeypatch):
    """With reduced_motion the arc is static — no rotation at all (and no cache
    churn), matching the design's prefers-reduced-motion translation."""
    from src.display.renderer import _BoundedCache as _BC, _ARC_ROT_CACHE_MAX

    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = True
    r._arc_segment = None
    r._arc_rot_cache = _BC(_ARC_ROT_CACHE_MAX)

    layout = get_now_playing_layout(1024, 600)
    target = pygame.Surface((1024, 600))

    calls = []
    real_rotate = pygame.transform.rotate
    monkeypatch.setattr(
        pygame.transform, "rotate",
        lambda surf, angle: calls.append(angle) or real_rotate(surf, angle),
    )

    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, 0.0)
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, 9.9)

    assert calls == []                 # never rotated
    assert len(r._arc_rot_cache) == 0  # and never cached


def test_static_frame_recomposes_when_cover_version_bumps(tmp_path):
    """B-22: a freshly-landed cover must force the now-playing static frame to
    recompose.  The static-frame key includes the monotonic _cover_version, so a
    bump changes the key even when the on-screen `cover` object is identical
    (the old id(cover) token could be GC-recycled and falsely match a stale
    frame).  Renders with no cover file on disk (cover=None both times), so ONLY
    the version token can distinguish the two keys."""
    from src.display.cover_cache import CoverArtCache
    from src.display.renderer import (
        _BoundedCache as _BC, _PALETTE_CACHE_MAX, _COVER_CACHE_MAX,
        _LABEL_CACHE_MAX, _DOT_CACHE_MAX, _FONT_CACHE_MAX,
    )
    from src.state.player_state import PlayerState, PlayerStatus
    from src.metadata.models import TrackMetadata, MetadataSource

    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = True
    r.dynamic_theming = False
    r._layout = get_now_playing_layout(1024, 600)
    r._screen = pygame.Surface((1024, 600))
    r._font_cache = _BC(_FONT_CACHE_MAX)
    r._label_cache = _BC(_LABEL_CACHE_MAX)
    r._dot_cache = _BC(_DOT_CACHE_MAX)
    r._cover_cache = _BC(_COVER_CACHE_MAX)
    r._palette_cache = _BC(_PALETTE_CACHE_MAX)
    r._cover_store = CoverArtCache(tmp_path)
    r._gradient_key = None
    r._gradient_surface = None
    # Pre-seed the cover-shadow cache so _cover_shadow returns early instead of
    # calling convert_alpha (which needs an initialized display); this test only
    # cares about the static-frame KEY, not the shadow pixels.
    _ca = r._layout.cover_art
    r._shadow_key = (_ca.w, _ca.h)
    r._shadow_surface = pygame.Surface((_ca.w + 200, _ca.h + 200), pygame.SRCALPHA)
    r._static_key = None
    r._static_surface = None
    r._arc_segment = None
    r._current_palette = FALLBACK_PALETTE
    r._target_palette = FALLBACK_PALETTE
    r._transition_start = 0.0
    r._cover_version = 0
    r._dirty = False

    state = PlayerState()
    state.current_track = TrackMetadata(
        title="So What", artist="Miles Davis", album="Kind of Blue",
        source=MetadataSource.FALLBACK,
        cover_art_url="https://i.discogs.com/x.jpg",  # never written to disk → cover=None
        tracklist=[],
    )
    state.status = PlayerStatus.PLAYING
    r.state = state

    r._render_now_playing()
    key_before = r._static_key
    assert key_before is not None

    r._cover_version += 1          # a cover for this track just landed
    r._render_now_playing()
    key_after = r._static_key

    assert key_before != key_after  # the static frame recomposed (B-22)
