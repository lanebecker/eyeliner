"""Album-cover palette extraction + WCAG colour science (A-8).

This was scattered through the pygame renderer, which is the wrong home for
colour maths and the DisplayPalette invariant.  It now lives in one module the
renderer *consumes*: `extract_palette()` is the factory that turns a cover image
into a DisplayPalette and **guarantees** the Full-Opacity Rule (muted ≥4.5:1 vs
solid `surface`, #206) by construction, so the renderer never builds an invalid
palette by hand.

Pillow / numpy imports are kept lazy (inside the functions that need them) so
the module stays importable on machines without the image stack.
"""
import colorsys
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class DisplayPalette:
    """5-color palette for dynamic theming, extracted from album art.

    All values are (R, G, B) tuples in 0-255 range.

    Matches the palette schema from the Claude Design mockups:
      bg      — main background tint (very dark, ~15-22% lightness)
      surface — slightly lighter card/panel tone for radial gradient
      accent  — vibrant extracted color (divider line, album name, badge borders)
      text    — primary text color (near-white, slightly tinted)
      muted   — secondary/meta text color (medium gray, slightly tinted)
    """
    bg: tuple
    surface: tuple
    accent: tuple
    text: tuple
    muted: tuple


# Used when no cover art is available or palette extraction fails.
FALLBACK_PALETTE = DisplayPalette(
    bg=(10, 10, 10),
    surface=(22, 22, 22),
    accent=(200, 200, 200),
    text=(235, 230, 220),
    muted=(138, 133, 124),
)

# Reject images larger than this many total pixels (decompression-bomb guard,
# S-2).  R6-20: lowered from 6000×6000 (36 MP) to 3200×3200 (~10 MP). Real album
# covers top out ~9 MP, but a 36 MP cap let a ~0.7 MB smooth-gradient JPEG off a
# (compromised/MITM'd) allow-listed CDN decode to ~400 MB transient RSS — three
# times over (validate / palette / pygame) — which is OOM-killer / SD-swap
# territory on a 512 MB–1 GB Pi. cover_cache validates at cache-WRITE, so an
# oversized image is now rejected before it is ever stored, decoded, or rendered.
MAX_IMAGE_PIXELS = 3200 * 3200

# #305: two-tier policy for an OVERSIZED-but-legitimate cover (a high-res Cover Art
# Archive scan whose full-size original exceeds MAX_IMAGE_PIXELS). Rather than
# reject it (which blanked the cover AND made R6-18 re-download the same huge file
# 5x), a JPEG in (MAX_IMAGE_PIXELS, MAX_DECODE_PIXELS] is DOWNSCALED to the display
# cap at cache-write via a REDUCED decode (see downscale_oversized_image). Anything
# ABOVE MAX_DECODE_PIXELS is a genuine decompression bomb and is rejected at the
# HEADER, never decoded (preserving the S-2 guarantee). ~36 MP (6000x6000) is well
# above any real album-art scan; it is the header sanity ceiling, NOT a claim that a
# full 36 MP decode is affordable — it is not (~400 MB RSS on a Pi, the R6-20 OOM),
# which is exactly why the decode is done reduced (draft) and the reduced size is
# re-checked against MAX_IMAGE_PIXELS before any pixels are materialized.
MAX_DECODE_PIXELS = 6000 * 6000

# #305 (R8, corrected; R8-11/#357 re-corrected): the box passed to JPEG draft()
# when reducing an oversized cover. draft() decodes to the smallest 1/2^n scale
# whose result is still >= this box, so a decoded axis lands in [side, 2*side) —
# meaning the reduced decode is <= 4*side^2 pixels REGARDLESS of the source's
# size. draft() also only halves while BOTH axes stay >= the box, so reduction
# engages only when the MINOR axis is >= 2*side.
#
# R8-11: side = floor(sqrt(cap)/2) (1600) required a minor axis >= 3200 to
# engage — which rejected NEAR-SQUARE oversized scans (3400x3100, ratio 1.10;
# 4000x3000) and permanently blacklisted their covers, while the accompanying
# comment claimed "album art is square, so this rejects only unusual wide
# covers". side = floor(sqrt(cap)/4) (800) engages for any minor axis >= 1600,
# bounds the reduced decode at 4*side^2 = 2.56 MP — FOUR TIMES stronger than
# the previous bound (== cap) — and still exceeds the 1024px display need.
# The rejected set shrinks to genuine extreme ratios (minor axis < 1600 with
# total pixels > the cap, i.e. roughly > 6.4:1 at the cap boundary); the
# post-draft re-check remains the backstop for those, which are blanked.
_DRAFT_TARGET_SIDE = int(math.isqrt(MAX_IMAGE_PIXELS) // 4)


class PermanentCoverError(ValueError):
    """A cover that is DEFINITIVELY unusable for THESE bytes - a decompression
    bomb beyond the decode ceiling, a corrupt/truncated payload, or a disallowed
    format. A ``ValueError`` subclass (so existing ``except ValueError`` catches
    still fire), but distinct so the download leg can BLACKLIST it immediately
    instead of re-downloading the same bad bytes up to 5x (#305)."""


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
    # sees that small cap persist into a later test.
    #
    # W3 cold-review F2: `Image.MAX_IMAGE_PIXELS` is a process GLOBAL, and this
    # function and `downscale_oversized_image` set DIFFERENT bounds (the 10.24MP
    # display cap here; the 36MP decode ceiling there).  Concurrent executor
    # threads — the R8-18 legacy sweep decoding a legal pre-v1.5.26 25MP file
    # while a download validates — raced: the sweep's decode ran under THIS
    # function's 10.24MP bound, tripped Pillow's DecompressionBombError, and the
    # sweep deleted a legitimate file.  (The old comment's "every caller writes
    # the identical bound" premise has been false since #305 introduced the
    # 36MP bump.)  All bomb-limit critical sections now serialize on
    # `_BOMB_LIMIT_LOCK`; these are blocking executor-side functions, so the
    # lock adds no event-loop stall.
    _BOMB_LIMIT_LOCK.acquire()
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
            raise PermanentCoverError(f"not a decodable image: {e}")

        if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
            raise PermanentCoverError(f"unexpected image format: {fmt!r}")
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise PermanentCoverError(f"image dimensions out of bounds: {width}x{height}")

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
            raise PermanentCoverError(f"not a decodable image: {e}")

        if return_image:
            return decoded          # caller owns it and must close it
        decoded.close()
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = _prev_max
        _BOMB_LIMIT_LOCK.release()


# W3 cold-review F2: serializes every mutation of Pillow's global
# Image.MAX_IMAGE_PIXELS (validate_image_file at the 10.24MP display cap;
# downscale_oversized_image at the 36MP decode ceiling).  Without it, two
# executor threads interleaving DIFFERENT bounds let a legitimate 10.24–36MP
# legacy file decode under the wrong (lower) bound and bomb out.
_BOMB_LIMIT_LOCK = threading.Lock()


def _classify_cover_error(e: Exception, stage: str) -> Exception:
    """W3 cold-review F1/F6: map a failure to the right exception class.

    A REAL disk/filesystem error carries an ``errno`` (ENOSPC, EIO, ENOENT from
    a racing prune, EACCES…) and is TRANSIENT — it must propagate as the
    OSError it is, so the legacy sweep SKIPS the file (retried next boot) and
    the download path backs off, instead of both treating it as
    permanently-bad bytes (the first cut deleted a GOOD cover on a full disk).
    Pillow's content failures (undecodable/truncated bytes, encode rejects)
    surface as errno-less OSErrors/ValueErrors — those genuinely condemn the
    BYTES and become :class:`PermanentCoverError`.
    """
    if isinstance(e, OSError) and e.errno is not None:
        return e
    return PermanentCoverError(f"{stage}: {e}")


def _probe_image_header(path: str) -> tuple:
    """Header-only probe → (format, mode, (w, h)), under the bomb-limit lock.

    W3 2nd-pass finding: Pillow's decompression-bomb check fires AT OPEN TIME
    from the header dimensions, so an UNLOCKED probe of a legitimate 25MP
    legacy file while a concurrent validate held the global at the 10.24MP
    display cap raised DecompressionBombError (errno-less → condemned → the
    sweep deleted the file) — the F2 race re-entering one call earlier.  The
    probe therefore takes the same lock, bound to the DECODE ceiling: a genuine
    >2×36MP bomb still errors here (errno-less → PermanentCoverError at the
    caller), a 36–72MP file opens with a warning and is rejected by the
    caller's explicit header check, and everything legitimate probes clean.
    No pixels are decoded (header read only).
    """
    from PIL import Image

    with _BOMB_LIMIT_LOCK:
        _prev = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS
        try:
            with Image.open(path) as probe:
                return probe.format, probe.mode, probe.size
        finally:
            Image.MAX_IMAGE_PIXELS = _prev


def _atomic_jpeg_save(img, path: str) -> None:
    """Atomic RGB-JPEG rewrite (tmp + os.replace) with tmp cleanup on ANY
    failure (W3 cold-review F4: an orphaned ``.norm-part`` was invisible to
    every cleanup mechanism — unmatched by ``_sweep_partials``'s glob, uncounted
    by the disk bound, unswept — and accumulated forever)."""
    tmp = f"{path}.norm-part"
    try:
        img.save(tmp, format="JPEG", quality=90)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def downscale_oversized_image(path: str) -> bool:
    """#305: if the image at *path* exceeds ``MAX_IMAGE_PIXELS`` but is within the
    ``MAX_DECODE_PIXELS`` bomb ceiling, reduce-decode it and overwrite it with a copy
    scaled to fit within ``MAX_IMAGE_PIXELS``. Returns True if it downscaled, False if
    the image was already within the display cap (header read only — no decode).

    Memory safety (R8 cold-audit fix). An oversized image can only be decoded
    affordably on a Pi if the decode itself is REDUCED — a full 36 MP decode is the
    ~400 MB RSS spike R6-20 exists to prevent. Pillow offers a reduced decode only
    for JPEG (``draft()`` → DCT-domain scaled decode), so:

    * A JPEG is drafted to the small ``_DRAFT_TARGET_SIDE`` box (NOT the display cap),
      which bounds the reduced decode to ``<= MAX_IMAGE_PIXELS`` regardless of the
      source's size. ``draft()`` merely SETS the decode scale — no pixels yet — so the
      post-draft size is re-checked against the cap and rejected if still over (a
      cover wider than roughly square can resist ``draft()``'s both-axes halving)
      BEFORE any decode. This closes the hole where drafting to the cap left a
      6000×6000 JPEG decoding at full resolution.
    * A non-JPEG (PNG/WEBP/GIF/BMP) that is oversized cannot be reduce-decoded, so it
      is rejected with :class:`PermanentCoverError` rather than risk the full-decode
      OOM. Such covers are rare (CAA/Discogs serve JPEG); the backstop blanks it.

    Raises :class:`PermanentCoverError` WITHOUT decoding for a genuine decompression
    bomb (dimensions above ``MAX_DECODE_PIXELS``) — the S-2 header guard is preserved
    — and for the oversized-non-JPEG and resist-reduction cases above. A decode/encode
    error on a within-ceiling JPEG also raises ``PermanentCoverError`` (corrupt bytes).
    Call this BEFORE :func:`validate_image_file` at cache-write: it either leaves a
    within-cap file for validate to check, or rejects up front.
    """
    from PIL import Image

    try:
        fmt, _mode, (w, h) = _probe_image_header(path)   # locked (2nd-pass fix)
    except Exception as e:
        # W3 F1/F6: a racing-prune FileNotFoundError (errno) propagates as the
        # transient OSError it is; only errno-less content failures condemn the bytes.
        raise _classify_cover_error(e, "not a decodable image")

    if w <= 0 or h <= 0:
        raise PermanentCoverError(f"image dimensions out of bounds: {w}x{h}")
    if w * h <= MAX_IMAGE_PIXELS:
        return False                        # within the display cap — nothing to do
    if w * h > MAX_DECODE_PIXELS:
        # Genuine decompression bomb — reject at the HEADER, never decode (S-2).
        raise PermanentCoverError(f"image dimensions out of bounds: {w}x{h}")
    if fmt != "JPEG":
        # No reduced decode available for this format — a full-resolution decode of
        # an oversized image is the R6-20 OOM. Reject rather than risk it.
        raise PermanentCoverError(
            f"oversized non-JPEG cover cannot be safely downscaled: {fmt!r} {w}x{h}"
        )

    box = (_DRAFT_TARGET_SIDE, _DRAFT_TARGET_SIDE)
    _BOMB_LIMIT_LOCK.acquire()              # W3 F2: serialize the global mutation
    _prev_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS
    try:
        with Image.open(path) as src:
            # draft() only ADJUSTS the decoder scale + src.size; no pixels are
            # materialized until convert()/load(). Re-check the post-draft size so a
            # source draft() could not reduce below the cap (a cover wider than
            # roughly square, whose minor axis is already below ~2x the draft box) is
            # rejected BEFORE the full-resolution decode it would otherwise trigger.
            src.draft("RGB", box)
            dw, dh = src.size
            if dw * dh > MAX_IMAGE_PIXELS:
                raise PermanentCoverError(
                    f"oversized cover could not be reduced below the cap: {dw}x{dh}"
                )
            img = src.convert("RGB")        # independent copy; triggers the reduced load
        try:
            img.thumbnail(box, Image.LANCZOS)   # only shrinks; result <= cap
            # R8-18: ATOMIC rewrite — the legacy sweep runs this against LIVE
            # cache files a concurrent decode may be reading.
            _atomic_jpeg_save(img, path)
        finally:
            img.close()
    except PermanentCoverError:
        raise
    except Exception as e:
        # W3 F1: real disk errors (errno) propagate as transient OSError —
        # the sweep skips (retries next boot) and the download path backs off;
        # only errno-less content failures condemn the bytes.
        raise _classify_cover_error(e, "could not downscale oversized cover")
    finally:
        Image.MAX_IMAGE_PIXELS = _prev_max
        _BOMB_LIMIT_LOCK.release()
    return True


# R8-18 (#359, E1): every cover is normalized to at most this side at
# cache-write. 2x the 440px display slot, so smoothscale keeps full quality
# headroom at 1024x600 while: the per-decode on-loop convert()+smoothscale
# stall shrinks from ~0.4-0.7s (Pi-scaled, 3000x3000 scan, ~118MB episode
# peak) to milliseconds; the palette executor decode shrinks the same way
# (~99MB -> small); the disk cache shrinks several-fold; and CMYK is converted
# to RGB defensively at write (SDL_image renders CMYK JPEGs unreliably).
NORMALIZED_COVER_SIDE = 880


def normalize_cover_image(path: str, max_side: int = NORMALIZED_COVER_SIDE) -> bool:
    """R8-18 (#359): normalize the (already within-cap, validated-decodable)
    cover at *path* to <= *max_side* on its longer axis, re-encoded as RGB JPEG.

    Returns True if the file was rewritten, False if it was already within
    bounds AND already an RGB JPEG (nothing to do — the overwhelmingly common
    Discogs-thumbnail case pays only a header read).  Call AFTER
    :func:`downscale_oversized_image` (which bounds the decode for >cap
    sources; anything reaching here decodes at <= MAX_IMAGE_PIXELS, a one-time
    <= ~40MB transient at write) and after :func:`validate_image_file`.

    Raises :class:`PermanentCoverError` on a decode/encode failure — by this
    point the bytes validated clean, so a failure here is corrupt-in-a-new-way
    and the caller's blacklist semantics apply.
    """
    from PIL import Image

    try:
        fmt, mode, (w, h) = _probe_image_header(path)    # locked (2nd-pass fix)
    except Exception as e:
        raise _classify_cover_error(e, "not a decodable image")

    if max(w, h) <= max_side and fmt == "JPEG" and mode == "RGB":
        return False                    # already normalized — header read only

    try:
        with Image.open(path) as src:
            if fmt == "JPEG":
                # Cheap reduced decode toward the target where available.
                src.draft("RGB", (max_side, max_side))
            img = src.convert("RGB")
        try:
            img.thumbnail((max_side, max_side), Image.LANCZOS)  # only shrinks
            # Atomic rewrite with tmp cleanup — the legacy sweep normalizes
            # LIVE cache files (W3 F4).
            _atomic_jpeg_save(img, path)
        finally:
            img.close()
    except PermanentCoverError:
        raise
    except Exception as e:
        # W3 F1: errno-carrying disk errors stay transient OSError (sweep
        # skips; download backs off); errno-less content failures condemn the
        # bytes as PermanentCoverError.
        raise _classify_cover_error(e, "could not normalize cover")
    return True


# ---------------------------------------------------------------------------
# WCAG colour science
# ---------------------------------------------------------------------------

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

def extract_palette(image_path: Path) -> Optional[DisplayPalette]:
    """Extract a 5-color DisplayPalette from a cached cover image.

    Quantizes the cover, derives (bg, surface, accent, text, muted), and
    GUARANTEES both text roles pass the Full-Opacity Rule (≥4.5:1), each against
    the brightest thing it lands on: `accent` (the album title on the gradient
    card) against the gradient's brightest pixel (`text_background`, DISP-1), and
    `muted` (secondary text on the solid status strip) against solid `surface`
    (#206 — surface ≥ the gradient peak, so this subsumes the DISP-2 guarantee).

    R6-17: returns None on ANY failure (a malformed/undecodable image, or a
    quantize that yields no colors) rather than FALLBACK_PALETTE. Its sole caller,
    ``DisplayRenderer._extract_palette_async``, treats None as "don't cache this
    URL's palette" — otherwise a transient decode error poisoned the URL to
    FALLBACK for the whole session and the corrupt-cover refetch's good bytes never
    re-themed. Callers wanting a guaranteed palette use ``extract_palette(p) or
    FALLBACK_PALETTE``.
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
            return None   # R6-17: signal failure (not FALLBACK) so it isn't cached

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
        # against SURFACE, the brightest thing muted text ever lands on (#206
        # /disp-1). The status strip is filled with SOLID `surface`
        # (renderer._draw_header), which is brighter than the gradient's
        # text-background peak `tb`; clamping against `tb` left the strip labels
        # ("NOW PLAYING", "SIDE A · NN OF MM") at ≈3.6–4.4:1 on bright covers —
        # below the WCAG AA floor the design commits to. surface ≥ tb always, so
        # clamping against surface subsumes the DISP-2 gradient-card guarantee in
        # one move and brightens secondary text everywhere (Lane approved the
        # global brightening, 2026-08-10). accent stays clamped on `tb` — the
        # album title is only ever drawn on the gradient card, never the strip.
        muted = (
            min(200, 120 + int(dominant[0] * 0.08)),
            min(200, 118 + int(dominant[1] * 0.07)),
            min(200, 115 + int(dominant[2] * 0.06)),
        )
        muted = ensure_contrast(muted, surface, min_ratio=4.5)

        return DisplayPalette(bg=bg, surface=surface, accent=accent, text=text, muted=muted)

    except Exception as e:
        log.warning(f"Palette extraction failed for {image_path}: {e}")
        return None   # R6-17: signal failure (not FALLBACK) so it isn't cached
