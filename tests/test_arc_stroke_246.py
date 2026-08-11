"""R5-28 (#246) — the boot/error arc stroke must be a fine ~1.5px hairline.

`stroke` is the RADIUS of each stamped circle, so a radius-2 stamp produced a
~5px band (2*2+1) — over 3× the DESIGN.md:171 1.5px spec. Halving it yields a
3px band, the closest a pygame integer-radius circle approximates 1.5px while
keeping the round-cap look.
"""
import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from src.config import DisplayConfig  # noqa: E402
from src.display.renderer import DisplayRenderer  # noqa: E402
from src.state.player_state import PlayerState  # noqa: E402


def _renderer(tmp_path):
    return DisplayRenderer(
        DisplayConfig(width=1024, height=600, fullscreen=False,
                      dynamic_theming=True, reduced_motion=False,
                      cover_art_cache_dir=str(tmp_path / "cache")),
        PlayerState(),
    )


def _radial_thickness(surf):
    c = surf.get_width() // 2
    offs = [d for d in range(0, c) if surf.get_at((c + d, c))[3] > 0]
    return (max(offs) - min(offs) + 1) if offs else 0


def test_arc_stroke_is_a_fine_hairline_not_a_thick_band(tmp_path):
    r = _renderer(tmp_path)
    surf = r._get_arc_segment(32, (255, 255, 255))
    thickness = _radial_thickness(surf)
    assert thickness <= 3           # RED before R5-28: 5px
    assert thickness >= 1           # still visible (round cap preserved)
