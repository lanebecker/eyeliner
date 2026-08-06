"""Text-drawing correctness — DISP-5 (#129) and DISP-7 (#131).

DISP-5: `_render_tracked` rendered one codepoint at a time with a manual
advance, which destroys shaping for complex scripts (Arabic joining, Devanagari
conjuncts, combining marks, emoji ZWJ) that arrive in free-text Discogs fields.
ASCII metadata keeps its per-glyph letter-spacing (the design intent); non-ASCII
is rendered as a single shaped run instead of reversed/mis-spaced tofu.

DISP-7: `_wrap_lines` emitted a token wider than the column as one un-broken
line, and `_draw_wrapped_text` blitted with no horizontal clip, so a 120-char
run-on Discogs title ran off the right edge and off-screen.  Over-wide tokens
are now character-broken, and the blit is clipped to the column as a backstop.
"""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
import pytest  # noqa: E402

from src.display.renderer import (  # noqa: E402
    DisplayRenderer,
    _BoundedCache,
    _LABEL_CACHE_MAX,
    _FONT_CACHE_MAX,
)


@pytest.fixture(scope="module", autouse=True)
def _pygame_font():
    pygame.font.init()
    yield


def make_renderer():
    r = DisplayRenderer.__new__(DisplayRenderer)
    r._font_cache = _BoundedCache(_FONT_CACHE_MAX)
    r._label_cache = _BoundedCache(_LABEL_CACHE_MAX)
    return r


# ---------------------------------------------------------------------------
# DISP-7 — long-token wrapping + horizontal clip
# ---------------------------------------------------------------------------

def test_wrap_lines_hard_breaks_an_overlong_token():
    r = make_renderer()
    font = r._font("title", 32)
    token = "Supercalifragilisticexpialidocious" * 4     # ~136 chars, no spaces
    max_width = 300
    assert font.size(token)[0] > max_width                # precondition: over-wide
    lines = r._wrap_lines(token, font, max_width)
    assert len(lines) > 1                                 # broken, not one run-on line
    assert all(font.size(line)[0] <= max_width for line in lines)  # every piece fits


def test_wrap_lines_preserves_normal_word_wrapping():
    # The char-break path must not regress ordinary word wrapping: no word lost,
    # duplicated, or reordered, and every line still fits.
    r = make_renderer()
    font = r._font("title", 32)
    text = "And You Will Know Us by the Trail of Dead"
    lines = r._wrap_lines(text, font, 220)
    assert all(font.size(line)[0] <= 220 for line in lines)
    assert " ".join(lines).split() == text.split()


def test_wrap_lines_breaks_long_token_but_continues_following_words():
    # A run-on followed by normal words: the tail of the break shares a line with
    # what follows (greedy), and nothing is dropped.
    r = make_renderer()
    font = r._font("title", 32)
    text = "x" * 80 + " end"
    lines = r._wrap_lines(text, font, 200)
    assert all(font.size(line)[0] <= 200 for line in lines)
    assert "".join(lines).replace(" ", "") == ("x" * 80 + "end")


def test_draw_wrapped_text_clips_blit_to_column_width():
    # DISP-7 backstop: every line is blitted with an area= clip to rect.w, so
    # even a single glyph wider than the column can't paint past the edge.
    r = make_renderer()
    rect = pygame.Rect(10, 20, 120, 200)
    recorded = []

    class FakeTarget:
        def blit(self, source, dest, area=None):
            recorded.append(area)

    r._draw_wrapped_text(FakeTarget(), "Wax Trax Reissue Series Vol 3",
                         "title", 32, rect, (255, 255, 255))
    assert recorded, "nothing was drawn"
    assert all(area is not None for area in recorded)        # clip on every line
    assert all(area.width == rect.w for area in recorded)    # clipped to the column


# ---------------------------------------------------------------------------
# DISP-5 — shaping-safe tracked labels
# ---------------------------------------------------------------------------

def test_render_tracked_ascii_keeps_letter_spacing():
    # ASCII path is unchanged: per-glyph tracking still widens the label beyond a
    # plain single-run render (guards against a mutation that drops tracking).
    r = make_renderer()
    surf = r._render_tracked("SST-134", 13, (255, 255, 255), 0.16)
    plain_w = r._font("mono", 13).size("SST-134")[0]
    assert surf.get_width() > plain_w


def test_render_tracked_non_ascii_is_single_shaped_run():
    # DISP-5: a non-ASCII label renders as ONE shaped run (no manual tracking),
    # so its width matches a plain font.render — not the wider, per-character,
    # shaping-destroying layout the old code produced.
    r = make_renderer()
    text = "café-noir"                                    # é → not ASCII
    assert not text.isascii()
    surf = r._render_tracked(text, 13, (255, 255, 255), 0.16)
    plain = r._font("mono", 13).render(text, True, (255, 255, 255))
    assert surf.get_width() == plain.get_width()


def test_render_tracked_empty_string_is_safe():
    # Empty label must not raise (isascii() is True for "" → ASCII path).
    r = make_renderer()
    surf = r._render_tracked("", 13, (255, 255, 255), 0.16)
    assert surf.get_width() >= 1
