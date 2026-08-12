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


def test_render_tracked_complex_script_is_a_single_shaped_run():
    """DISP-5 / R7-07: text that genuinely needs shaping (Arabic joining + RTL)
    renders as one shaped run — width matches a plain font.render, no per-glyph
    tracking that would mangle it."""
    t = make_text()
    text = "مرحبا"
    s = t.render_tracked(text, 16, (255, 255, 255), 0.2)
    plain = t.font("mono", 16).render(text, True, (255, 255, 255))
    assert s.get_width() == plain.get_width()


def test_render_tracked_app_labels_keep_letter_spacing():
    """R7-07: the app's own non-ASCII labels (·, …, ←, →) keep their designed
    tracking — rendered wider than a plain single-run layout."""
    t = make_text()
    font = t.font("mono", 13)
    for label in ("SIDE A · 04 OF 06", "← PREV", "NEXT →", "STILL LISTENING…"):
        assert t.render_tracked(label, 13, (255, 255, 255), 0.16).get_width() \
            > font.size(label)[0], f"{label!r} lost its letter-spacing (R7-07)"


def test_render_tracked_ellipsized_trims_overflow_to_fit():
    """R7-09: a catalog footer wider than its column is trimmed with a trailing …
    so its LETTER-SPACED width fits, instead of hard-clipping mid-glyph."""
    t = make_text()
    footer = "1998 · Deutsche Grammophon Gesellschaft mbH · 289 459 610-2"
    col = 440
    full = t.render_tracked(footer, 13, (255, 255, 255), 0.08).get_width()
    assert full > col                                    # genuinely overflows
    trimmed = t.render_tracked_ellipsized(footer, 13, (255, 255, 255), 0.08, col)
    assert trimmed.get_width() <= col                    # now fits the column
    assert trimmed.get_width() < full                    # something was trimmed


def test_render_tracked_ellipsized_leaves_fitting_label_alone():
    """R7-09: a footer that already fits is returned untouched (same width as a
    plain tracked render — no spurious ellipsis)."""
    t = make_text()
    footer = "1987 · SST · SST-134"
    col = 440
    plain = t.render_tracked(footer, 13, (255, 255, 255), 0.08).get_width()
    assert plain <= col
    assert t.render_tracked_ellipsized(footer, 13, (255, 255, 255), 0.08, col).get_width() == plain


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
