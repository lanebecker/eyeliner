"""Album-cover palette extraction + WCAG colour science (A-8).

This was scattered through the pygame renderer, which is the wrong home for
colour maths and the DisplayPalette invariant.  It now lives in one module the
renderer *consumes*: `extract_palette()` is the factory that turns a cover image
into a DisplayPalette and **guarantees** the Full-Opacity Rule (muted ≥4.5:1 vs
bg) by construction, so the renderer never builds an invalid palette by hand.

Pillow / numpy imports are kept lazy (inside the functions that need them) so
the module stays importable on machines without the image stack.
"""
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

def validate_image_file(path: str) -> None:
    """Verify a file is a sane, bounded image before it is decoded/cached.

    Uses Pillow's `verify()` to reject truncated / malformed files and caps the
    pixel count to guard against decompression bombs (S-2).  Raises ValueError
    on anything suspicious.
    """
    from PIL import Image

    # Belt-and-suspenders: bound Pillow's own decompression-bomb threshold too.
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

    try:
        with Image.open(path) as probe:
            fmt = probe.format
            width, height = probe.size
            probe.verify()  # structural integrity check; consumes the file object
    except Exception as e:
        raise ValueError(f"not a decodable image: {e}")

    if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
        raise ValueError(f"unexpected image format: {fmt!r}")
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"image dimensions out of bounds: {width}x{height}")


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


# ---------------------------------------------------------------------------
# Palette factory
# ---------------------------------------------------------------------------

def extract_palette(image_path: Path) -> DisplayPalette:
    """Extract a 5-color DisplayPalette from a cached cover image.

    Quantizes the cover, derives (bg, surface, accent, text, muted), and
    GUARANTEES the muted role passes the Full-Opacity Rule (≥4.5:1 vs bg).
    Falls back to FALLBACK_PALETTE on any error.
    """
    try:
        from PIL import Image

        # Validate before decoding (S-2): the download path already checks, but
        # palette extraction can also run against pre-existing cache files, so
        # guard here too against malformed images / decompression bombs.
        validate_image_file(str(image_path))

        img = Image.open(image_path).convert("RGB")
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
        accent = clamp_luminance(accent_raw, min_lum=0.30)

        # bg: darken dominant significantly (target ~15% brightness)
        scale_bg = 0.18
        bg = tuple(max(8, int(c * scale_bg + dominant[i] * 0.04)) for i, c in enumerate(dominant))

        # surface: slightly lighter than bg
        surface = tuple(min(255, int(c * 1.6)) for c in bg)

        # text: near-white with a slight warm tint from dominant
        text = (
            min(255, 230 + int(dominant[0] * 0.04)),
            min(255, 225 + int(dominant[1] * 0.03)),
            min(255, 215 + int(dominant[2] * 0.03)),
        )

        # muted: medium gray, slightly tinted — then contrast-clamped to ≥4.5:1
        # against this album's bg (Full-Opacity Rule guarantee).
        muted = (
            min(200, 120 + int(dominant[0] * 0.08)),
            min(200, 118 + int(dominant[1] * 0.07)),
            min(200, 115 + int(dominant[2] * 0.06)),
        )
        muted = ensure_contrast(muted, bg, min_ratio=4.5)

        return DisplayPalette(bg=bg, surface=surface, accent=accent, text=text, muted=muted)

    except Exception as e:
        log.warning(f"Palette extraction failed for {image_path}: {e}")
        return FALLBACK_PALETTE
