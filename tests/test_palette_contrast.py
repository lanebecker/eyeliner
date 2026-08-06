"""The contrast guarantee — DISP-1 (#125), DISP-2 (#126), MUT-3 (#127).

These pin the Full-Opacity Rule on the *output* of `extract_palette`, measured
against the colour the gradient actually puts under text — `text_background(bg,
surface)` = the brightest gradient pixel — NOT flat `bg`.  Both text roles are
covered: `muted` (secondary text) and `accent` (the album title, DISP-1).

Before this wave the suite only exercised covers that happened to satisfy the
guarantee for free; MUT-3 is the mutation-proof battery that makes halving the
4.5 threshold (or neutering the lightening loop) fail loudly.
"""

import colorsys

import pytest
from PIL import Image

import src.display.palette as palette
from src.display.palette import (
    extract_palette,
    contrast_ratio,
    ensure_contrast,
    ensure_contrast_hue_preserving,
    text_background,
)

AA = 4.5


def _hue(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    return colorsys.rgb_to_hls(r, g, b)[0]


def _cover(tmp_path, bands, name="cover.png"):
    """Write an 80x80 PNG stacked top→bottom from (fraction, rgb) bands."""
    img = Image.new("RGB", (80, 80), bands[0][1])
    y = 0
    for frac, col in bands:
        for yy in range(y, min(80, y + int(80 * frac))):
            for xx in range(80):
                img.putpixel((xx, yy), col)
        y += int(80 * frac)
    p = tmp_path / name
    img.save(str(p))
    return p


# Representative covers, including the finding's reproduced failures and MUT-3's
# requested "deliberately low-contrast dark-blue" case.
_COVERS = {
    "matte_black": [(1.0, (6, 6, 6))],
    "saturated_blue": [(0.7, (10, 10, 120)), (0.3, (20, 20, 230))],
    "low_contrast_dark_blue": [(1.0, (18, 20, 70))],
    "saturated_yellow": [(0.7, (150, 140, 10)), (0.3, (240, 225, 30))],
    "deep_red": [(0.8, (90, 10, 10)), (0.2, (210, 20, 25))],
    "pop_bright": [(0.5, (220, 90, 40)), (0.5, (240, 200, 60))],
}


@pytest.mark.parametrize("name", list(_COVERS))
def test_accent_title_meets_AA_on_gradient(tmp_path, name):
    # DISP-1 + DISP-2: the album title is drawn in `accent`, over the gradient.
    pal = extract_palette(_cover(tmp_path, _COVERS[name]))
    tb = text_background(pal.bg, pal.surface)
    ratio = contrast_ratio(pal.accent, tb)
    assert ratio >= AA, f"{name}: accent {pal.accent} vs gradient {tb} = {ratio:.2f}:1"


@pytest.mark.parametrize("name", list(_COVERS))
def test_muted_meets_AA_on_gradient(tmp_path, name):
    # DISP-2: muted was clamped against flat bg but is drawn on the brighter
    # gradient, so the guarantee was systematically optimistic.
    pal = extract_palette(_cover(tmp_path, _COVERS[name]))
    tb = text_background(pal.bg, pal.surface)
    ratio = contrast_ratio(pal.muted, tb)
    assert ratio >= AA, f"{name}: muted {pal.muted} vs gradient {tb} = {ratio:.2f}:1"


def test_text_background_is_brighter_than_bg(tmp_path):
    # The whole point of DISP-2: the clamp target must be brighter than bg.
    pal = extract_palette(_cover(tmp_path, _COVERS["saturated_yellow"]))
    tb = text_background(pal.bg, pal.surface)
    assert sum(tb) > sum(pal.bg)


# ---------------------------------------------------------------------------
# ensure_contrast_hue_preserving — the accent (album-title) clamp (DISP-1)
# ---------------------------------------------------------------------------

def test_hue_preserving_passthrough_when_already_compliant():
    # A colour already ≥4.5:1 is returned untouched (no gratuitous lightening).
    white = (240, 240, 240)
    assert contrast_ratio(white, (10, 10, 10)) >= AA
    assert ensure_contrast_hue_preserving(white, (10, 10, 10)) == white


@pytest.mark.parametrize("color,bg", [
    ((0, 0, 200), (10, 10, 34)),      # saturated blue on dark blue
    ((180, 20, 20), (25, 10, 10)),    # red on dark red
    ((0, 0, 0), (8, 8, 8)),           # pure black — clamp_luminance CAN'T lift this
])
def test_hue_preserving_lifts_below_to_at_or_above_floor(color, bg):
    # MUT-3: input is BELOW 4.5:1, output is AT/ABOVE — pins both the threshold
    # and the lightening loop's success branch.
    assert contrast_ratio(color, bg) < AA          # precondition: genuinely failing
    out = ensure_contrast_hue_preserving(color, bg, min_ratio=AA)
    assert contrast_ratio(out, bg) >= AA


@pytest.mark.parametrize("color,bg", [
    ((0, 0, 200), (10, 10, 34)),
    ((180, 20, 20), (25, 10, 10)),
])
def test_hue_preserving_keeps_the_hue(color, bg):
    # The whole reason accent uses this instead of blend-to-white: the cover's
    # hue survives the lift (blend-to-white would drift it toward grey/white).
    out = ensure_contrast_hue_preserving(color, bg, min_ratio=AA)
    assert abs(_hue(out) - _hue(color)) < 0.02     # hue within ~7°


def test_ensure_contrast_input_below_output_above():
    # MUT-3 for the muted clamp (blend-to-white): input < 4.5, output >= 4.5.
    dark = (70, 70, 70)
    bg = (12, 12, 12)
    assert contrast_ratio(dark, bg) < AA
    assert contrast_ratio(ensure_contrast(dark, bg, min_ratio=AA), bg) >= AA
