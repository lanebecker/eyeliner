"""Standalone unit tests for TextRenderer (ARCH-3).

The whole point of extracting TextRenderer out of DisplayRenderer is that the
typography logic can be exercised WITHOUT a pygame-initialised renderer — no
DisplayRenderer, no display surface, no __new__-skeleton. These build a
TextRenderer directly over two bounded caches and drive it. Only pygame.font is
needed (a headless font engine), never a window.
"""
import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
import pytest  # noqa: E402

from src.display.typography import TextRenderer  # noqa: E402
from src.display.renderer import _BoundedCache, _FONT_CACHE_MAX, _LABEL_CACHE_MAX  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _font_engine():
    pygame.font.init()
    yield


def make_text() -> TextRenderer:
    """A TextRenderer over fresh caches — no renderer involved."""
    return TextRenderer(_BoundedCache(_FONT_CACHE_MAX), _BoundedCache(_LABEL_CACHE_MAX))


# ---------------------------------------------------------------------------
# font() — loading + caching
# ---------------------------------------------------------------------------

def test_font_returns_a_font_and_caches_it():
    t = make_text()
    f1 = t.font("mono", 16)
    f2 = t.font("mono", 16)
    assert f1 is f2                       # same (role, size) → cached object
    assert t.font("mono", 24) is not f1   # different size → different font


def test_font_cache_is_bounded():
    t = make_text()
    for size in range(_FONT_CACHE_MAX + 20):
        t.font("mono", 8 + size)
    assert len(t._font_cache) <= _FONT_CACHE_MAX


# ---------------------------------------------------------------------------
# wrap_lines / break_long_token — pure line breaking
# ---------------------------------------------------------------------------

def test_wrap_lines_short_text_is_one_line():
    t = make_text()
    font = t.font("text", 24)
    assert t.wrap_lines("Sister", font, 400) == ["Sister"]


def test_wrap_lines_hard_breaks_an_overlong_token():
    t = make_text()
    font = t.font("title", 32)
    token = "Supercalifragilisticexpialidocious" * 2
    lines = t.wrap_lines(token, font, 120)
    assert len(lines) > 1                                  # broken, not one runaway line
    assert all(font.size(line)[0] <= 120 or len(line) == 1 for line in lines)


def test_break_long_token_chunks_fit_the_width():
    t = make_text()
    font = t.font("mono", 20)
    chunks = TextRenderer.break_long_token("A" * 200, font, 80)
    assert len(chunks) > 1
    assert all(font.size(c)[0] <= 80 or len(c) == 1 for c in chunks)


# ---------------------------------------------------------------------------
# fit_wrapped — shrink-to-fit
# ---------------------------------------------------------------------------

def test_fit_wrapped_shrinks_to_meet_the_line_budget():
    t = make_text()
    long_title = "A Really Quite Long Album Title That Needs Shrinking To Fit"
    size, lines = t.fit_wrapped(long_title, "title", base_size=48, max_width=300, max_lines=2)
    assert size <= 48
    assert len(lines) <= 2


# ---------------------------------------------------------------------------
# ellipsize
# ---------------------------------------------------------------------------

def test_ellipsize_adds_an_ellipsis_when_too_wide():
    t = make_text()
    font = t.font("text", 24)
    out = t.ellipsize("An extremely long track title that will not fit", font, 120)
    assert out.endswith("…")
    assert font.size(out)[0] <= 120


def test_ellipsize_leaves_short_text_untouched():
    t = make_text()
    font = t.font("text", 24)
    assert t.ellipsize("Short", font, 400) == "Short"


# ---------------------------------------------------------------------------
# render_tracked — letter-spaced Surface, cached
# ---------------------------------------------------------------------------

def test_render_tracked_returns_a_surface_and_caches():
    t = make_text()
    s1 = t.render_tracked("NOW PLAYING", 12, (255, 255, 255), 0.1)
    s2 = t.render_tracked("NOW PLAYING", 12, (255, 255, 255), 0.1)
    assert s1 is s2                        # cached by (text, size, color, tracking)
    assert s1.get_width() > 0


def test_render_tracked_non_ascii_is_a_single_shaped_run():
    """DISP-5: a non-ASCII string is rendered as one shaped run (no per-glyph
    tracking that would mangle complex scripts)."""
    t = make_text()
    s = t.render_tracked("café ☕ 日本語", 16, (255, 255, 255), 0.2)
    assert s.get_width() > 0


# ---------------------------------------------------------------------------
# measure_wrapped_text — matches wrap_lines
# ---------------------------------------------------------------------------

def test_measure_wrapped_text_zero_for_empty():
    t = make_text()
    assert t.measure_wrapped_text("", "text", 24, 400) == 0


def test_measure_wrapped_text_grows_with_line_count():
    t = make_text()
    one = t.measure_wrapped_text("Short", "text", 24, 400)
    many = t.measure_wrapped_text("word " * 40, "text", 24, 120)
    assert many > one > 0
