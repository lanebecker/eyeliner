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
    r.cache_dir = tmp_path
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()

    url = "https://i.discogs.com/cover.jpg"
    cache_path = tmp_path / r._url_to_cache_key(url)
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
    r.cache_dir = tmp_path
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    refetched = []
    r._prefetch_cover = lambda u: refetched.append(u)  # noqa: E731

    result = r._load_cover("https://i.discogs.com/missing.jpg", 100, 100)

    assert result is None
    assert refetched == []
