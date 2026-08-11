"""R5-09 (#245) — the ambient radial gradient must glow from BEHIND the record.

DESIGN.md:196: `radial-gradient(... at 25% 35%, surface, bg)` — the surface-tinted
peak sits at the 25%/35% origin (over the cover) and fades to bg at the edges.
The pre-fix loop tied brightness to the RADIUS, so the edge got the full peak and
the origin ended near bg — the exact inversion, shipped since v1.2.0. The fix
inverts the color fraction; crucially the BRIGHTEST drawn pixel is still exactly
GRADIENT_TEXT_PEAK (now at the origin), so the DISP-2 contrast guarantee holds.
"""
import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from src.config import DisplayConfig  # noqa: E402
from src.display.renderer import DisplayRenderer  # noqa: E402
from src.display.palette import DisplayPalette, GRADIENT_TEXT_PEAK  # noqa: E402
from src.display.palette_transition import _lerp_color  # noqa: E402
from src.state.player_state import PlayerState  # noqa: E402


def _config(tmp_path):
    return DisplayConfig(
        width=1024, height=600, fullscreen=False,
        dynamic_theming=True, reduced_motion=False,
        cover_art_cache_dir=str(tmp_path / "cache"),
    )


def _lum(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


_PAL = DisplayPalette(bg=(26, 20, 16), surface=(120, 88, 64),
                      accent=(200, 150, 90), text=(240, 235, 230), muted=(180, 170, 160))


def _rendered(tmp_path):
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    surf = pygame.Surface((1024, 600))
    r._draw_gradient_bg(surf, _PAL)
    return surf


def test_gradient_origin_is_brighter_than_the_edge(tmp_path):
    surf = _rendered(tmp_path)
    origin = surf.get_at((int(1024 * 0.25), int(600 * 0.35)))[:3]
    edge = surf.get_at((1010, 300))[:3]
    assert _lum(origin) > _lum(edge)


def test_gradient_origin_equals_the_surface_tinted_peak(tmp_path):
    surf = _rendered(tmp_path)
    origin = surf.get_at((int(1024 * 0.25), int(600 * 0.35)))[:3]
    peak = _lerp_color(_PAL.bg, _PAL.surface, GRADIENT_TEXT_PEAK)
    assert all(abs(origin[i] - peak[i]) <= 1 for i in range(3))


def test_brightest_drawn_pixel_still_equals_the_peak_wcag_invariant(tmp_path):
    """The DISP-2 guarantee: no drawn pixel is brighter than GRADIENT_TEXT_PEAK,
    which text roles are contrast-clamped against. Inverting the gradient moved
    the peak from the edge to the origin but must not have RAISED it."""
    surf = _rendered(tmp_path)
    peak = _lerp_color(_PAL.bg, _PAL.surface, GRADIENT_TEXT_PEAK)
    peak_lum = _lum(peak)
    max_seen = max(
        _lum(surf.get_at((x, y))[:3])
        for x in range(0, 1024, 32) for y in range(0, 600, 32)
    )
    assert max_seen <= peak_lum + 1.0
