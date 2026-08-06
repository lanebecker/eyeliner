"""Album-cover palette extraction + WCAG colour science (A-8).

This was scattered through the pygame renderer, which is the wrong home for
colour maths and the DisplayPalette invariant.  It now lives in one module the
renderer *consumes*: `extract_palette()` is the factory that turns a cover image
into a DisplayPalette and **guarantees** the Full-Opacity Rule (muted ≥4.5:1 vs
bg) by construction, so the renderer never builds an invalid palette by hand.

Pillow / numpy imports are kept lazy (inside the functions that need them) so
the module stays importable on machines without the image stack.
"""
import colorsys
import logging
from pathlib import Path

from src.metadata.models import DisplayPalette, FALLBACK_PALETTE

log = logging.getLogger(__name__)

# Reject images larger than this many total pixels (decompression-bomb guard,
# S-2).  6000×6000 ≈ 36 MP comfortably exceeds any real album-cover scan.
MAX_IMAGE_PIXELS = 6000 * 6000


# ---------------------------------------------------------------------------
# Image validation (S-2)
# ---------------------------------------------------------------------------

def validate_image_file(path: str, *, return_image=False):
    """Validate that a file is a sane, bounded, fully-decodable image before it
    is cached.

    Reads the header to enforce a format allow-list and a pixel-count bound
    (decompression-bomb guard, S-2), then forces a **full decode** to reject
    truncated or corrupt payloads (DISP-3) — e.g. a cover whose download was cut
    off mid-scan by a dropped connection.  Raises ValueError on anything
    suspicious.

    When *return_image* is True the decoded (post-``load()``) ``Image`` is
    returned so a caller such as ``extract_palette`` can sample its pixels
    without decoding the same file a second time (#173); the caller then owns
    that image and must close it.  The validate-only callers (e.g.
    ``cover_cache.download``) leave it False and get ``None``.

    Note: Pillow's ``verify()`` is deliberately NOT used.  Only
    ``PngImageFile.verify()`` performs a real structural (CRC) check; the JPEG /
    WEBP / GIF / BMP readers inherit ``ImageFile.verify()``, which closes the
    file object without inspecting the pixel stream.  So a half-written JPEG
    (the format this cache is named for) passes ``verify()`` while failing a
    real ``load()``.  ``ImageFile.LOAD_TRUNCATED_IMAGES`` is left at its default
    (False) so that a short read raises instead of being silently zero-filled.
    """
    from PIL import Image

    # Belt-and-suspenders: bound Pillow's own decompression-bomb threshold for
    # the duration of this call, then restore the prior value.  This fixes the
    # test-isolation leak (#172): a test that lowered MAX_IMAGE_PIXELS no longer
    # sees that small cap persist into a later test.  The restore is NOT
    # thread-isolated — concurrent validations on the shared executor can race
    # and leave the global at MAX_IMAGE_PIXELS — but that is harmless: every
    # caller writes the identical bound, the value seen *during* any call is
    # always that intended bound, and MAX_IMAGE_PIXELS only ever *lowers*
    # Pillow's default, so the worst possible residual is the safe cap we want.
    _prev_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        # 1. Open and read the header only — no pixel decode yet, so a bomb is
        #    rejected below (step 2) before it is ever decoded.
        try:
            with Image.open(path) as probe:
                fmt = probe.format
                width, height = probe.size
        except Exception as e:
            raise ValueError(f"not a decodable image: {e}")

        if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
            raise ValueError(f"unexpected image format: {fmt!r}")
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"image dimensions out of bounds: {width}x{height}")

        # 2. Force a real decode.  This is the only structural check that
        #    actually bites for JPEG (verify() does not), and it is the last
        #    gate before the file is os.replace'd into the on-disk cover cache.
        #    Bounded above by the pixel check, so we never fully decode an
        #    oversized image.  No `with`: when return_image is True the caller
        #    needs the still-usable decoded image.
        decoded = None
        try:
            decoded = Image.open(path)
            decoded.load()
        except Exception as e:
            if decoded is not None:
                decoded.close()
            raise ValueError(f"not a decodable image: {e}")

        if return_image:
            return decoded          # caller owns it and must close it
        decoded.close()
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = _prev_max


# ---------------------------------------------------------------------------
# WCAG colour science
# ---------------------------------------------------------------------------

def clamp_luminance(color: tuple, min_lum: float = 0.25) -> tuple:
    """Brighten a color until it reads against a dark background.

    Uses a simple perceived-brightness formula; if too dark, brightens
    proportionally until it hits min_lum.
    """
    r, g, b = color
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    if lum < min_lum and lum > 0:
        scale = min_lum / lum
        return tuple(min(255, int(c * scale)) for c in (r, g, b))
    return color


def relative_luminance(color: tuple) -> float:
    """WCAG 2.x relative luminance of an sRGB color (0.0–1.0)."""
    def chan(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = color
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(a: tuple, b: tuple) -> float:
    """WCAG contrast ratio between two RGB colors (1.0–21.0)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def ensure_contrast(color: tuple, bg: tuple, min_ratio: float = 4.5) -> tuple:
    """Lighten *color* until it reaches min_ratio contrast against *bg*.

    DESIGN.md §2 (Full-Opacity Rule / muted role): secondary text must pass
    4.5:1 against its album background at full opacity.  Cool-dark backgrounds
    pull contrast down faster than neutral darks, so extracted muted values are
    clamped here rather than trusted.  Blends toward white in small steps; falls
    back to near-white if even that fails (cannot happen for the dark
    backgrounds this product produces, but cheap to guard).
    """
    if contrast_ratio(color, bg) >= min_ratio:
        return color
    r, g, b = color
    for step in range(1, 21):
        t = step / 20.0
        candidate = tuple(int(c + (255 - c) * t) for c in (r, g, b))
        if contrast_ratio(candidate, bg) >= min_ratio:
            return candidate
    return (235, 235, 235)


# The card background is a radial gradient from `bg` at the edges to a peak of
# ``_lerp(bg, surface, GRADIENT_TEXT_PEAK)`` at the centre (see
# renderer._draw_gradient_bg, which uses THIS SAME constant).  Text can land on
# any pixel up to that peak, so contrast for text roles is clamped against the
# peak — not flat `bg` — otherwise the guarantee is optimistic wherever the
# gradient is brightest (DISP-2, #126).  Keep this the single source of truth:
# if the gradient's blend factor ever changes, the clamp target follows.
GRADIENT_TEXT_PEAK = 0.55


def text_background(bg: tuple, surface: tuple) -> tuple:
    """Brightest colour the card gradient can put under text (DISP-2).

    Text roles are contrast-clamped against this rather than flat ``bg`` so the
    Full-Opacity Rule holds at the gradient's bright centre, not just its dark
    edges.
    """
    return tuple(
        int(bg[i] + (surface[i] - bg[i]) * GRADIENT_TEXT_PEAK) for i in range(3)
    )


def ensure_contrast_hue_preserving(color: tuple, bg: tuple, min_ratio: float = 4.5) -> tuple:
    """Raise *color*'s lightness until it reaches min_ratio against *bg*, keeping
    the cover's hue.

    Used for the ``accent`` role — the album title (DISP-1, #125).  Unlike
    ``ensure_contrast`` (which blends toward white and desaturates a neutral
    ``muted`` grey, where that is harmless), accent carries the artwork's colour,
    so we lift only HLS *lightness* and preserve hue + saturation — the smallest
    perceptual move that reaches 4.5:1, keeping it as faithful to the cover as
    the physics of a near-black background allow (Lane, 2026-07-30).  Falls back
    to near-white only if even full lightness cannot reach the ratio, which
    cannot happen for the dark backgrounds this product produces.
    """
    if contrast_ratio(color, bg) >= min_ratio:
        return color
    r, g, b = (c / 255.0 for c in color)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    for step in range(1, 101):
        cand_l = min(1.0, l + (1.0 - l) * (step / 100.0))
        rr, gg, bb = colorsys.hls_to_rgb(h, cand_l, s)
        candidate = tuple(round(x * 255) for x in (rr, gg, bb))
        if contrast_ratio(candidate, bg) >= min_ratio:
            return candidate
    return (235, 235, 235)


# ---------------------------------------------------------------------------
# Palette factory
# ---------------------------------------------------------------------------

def extract_palette(image_path: Path) -> DisplayPalette:
    """Extract a 5-color DisplayPalette from a cached cover image.

    Quantizes the cover, derives (bg, surface, accent, text, muted), and
    GUARANTEES both text roles pass the Full-Opacity Rule (≥4.5:1) against the
    gradient's brightest pixel: `muted` (secondary text) and `accent` (the album
    title, DISP-1).  Falls back to FALLBACK_PALETTE on any error.
    """
    try:
        from PIL import Image

        # Validate before decoding (S-2): the download path already checks, but
        # palette extraction can also run against pre-existing cache files, so
        # guard here too against malformed images / decompression bombs.  Reuse
        # the image the validator already decoded rather than opening and
        # decoding the same file a second time (#173).
        decoded = validate_image_file(str(image_path), return_image=True)
        try:
            img = decoded.convert("RGB")
        finally:
            decoded.close()
        img = img.resize((80, 80), Image.LANCZOS)

        # Quantize to up to 8 colors; getpalette returns a flat R,G,B,R,G,B,...
        # list — but a solid-colour or tiny cover can quantize to FEWER than 8
        # entries (or a different length depending on Pillow), so the palette
        # size must be read from the actual list, not hardcoded to 8 (B-12).
        quantized = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        raw = quantized.getpalette() or []
        n_colors = len(raw) // 3
        if n_colors == 0:
            return FALLBACK_PALETTE

        # Count palette-index frequency via numpy.bincount instead of a
        # 6,400-iteration Python loop; np.asarray reads indices directly,
        # avoiding the deprecated Image.getdata() (P-5 / #60).
        import numpy as np

        idx_array = np.asarray(quantized).ravel()
        counts = np.bincount(idx_array, minlength=n_colors).tolist()

        palette_colors = [
            (counts[i], (raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]))
            for i in range(n_colors)
        ]
        palette_colors.sort(key=lambda x: x[0], reverse=True)
        colors = [c for _, c in palette_colors]

        # Most dominant color → tint for bg/surface
        dominant = colors[0]

        # Most *vibrant* color → accent (highest saturation)
        def saturation(rgb):
            r, g, b = [x / 255.0 for x in rgb]
            mx, mn = max(r, g, b), min(r, g, b)
            return (mx - mn) / mx if mx > 0 else 0

        accent_raw = max(colors, key=saturation)

        # bg: darken dominant significantly (target ~15% brightness)
        scale_bg = 0.18
        bg = tuple(max(8, int(c * scale_bg + dominant[i] * 0.04)) for i, c in enumerate(dominant))

        # surface: slightly lighter than bg
        surface = tuple(min(255, int(c * 1.6)) for c in bg)

        # Brightest colour the gradient puts under text — clamp all text roles
        # against THIS, not flat bg, so the guarantee holds at the gradient's
        # bright centre too (DISP-2, #126).
        tb = text_background(bg, surface)

        # accent: the album title is drawn in accent (DISP-1, #125), so it is a
        # TEXT role and must meet 4.5:1.  Lifted hue-preserving — keeps the
        # artwork's colour — rather than the old perceived-brightness clamp,
        # which could not brighten a pure-black or already-saturated accent at
        # all (34/62 covers measured below 4.5:1 before this).
        accent = ensure_contrast_hue_preserving(accent_raw, tb, min_ratio=4.5)

        # text: near-white with a slight warm tint from dominant
        text = (
            min(255, 230 + int(dominant[0] * 0.04)),
            min(255, 225 + int(dominant[1] * 0.03)),
            min(255, 215 + int(dominant[2] * 0.03)),
        )

        # muted: medium gray, slightly tinted — then contrast-clamped to ≥4.5:1
        # against the gradient's brightest pixel (Full-Opacity Rule guarantee).
        muted = (
            min(200, 120 + int(dominant[0] * 0.08)),
            min(200, 118 + int(dominant[1] * 0.07)),
            min(200, 115 + int(dominant[2] * 0.06)),
        )
        muted = ensure_contrast(muted, tb, min_ratio=4.5)

        return DisplayPalette(bg=bg, surface=surface, accent=accent, text=text, muted=muted)

    except Exception as e:
        log.warning(f"Palette extraction failed for {image_path}: {e}")
        return FALLBACK_PALETTE
