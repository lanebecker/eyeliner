"""Display renderer — manages the pygame window on the HDMI output.

Listens for PlayerState changes and re-renders the appropriate layout.
Runs as an async task in the main event loop.

v1.2.1 visual design
---------------------
Implements the "Museum Card" layout from the Claude Design Direction A mockups:
  - 5-color palette extracted from album art via Pillow (bg, surface, accent, text, muted)
  - Radial gradient background blended from palette.surface → palette.bg
  - Header strip: pulsing "NOW PLAYING" dot + "SIDE A · 04 OF 06"
  - Hero track title (large, bold, word-wrapped)
  - Short accent divider line
  - Artist name, album name (italic serif rendered via a separate font)
  - Genre/style pill badges
  - Meta footer: Year · Label · Catalog
  - Prev/next track strip

Color transitions lerp over ~1 second when a new track starts.
Palettes are cached per cover art URL so extraction only runs once per album.
Cover art is downloaded asynchronously (_prefetch_cover) so the render loop
is never blocked by network I/O.

Render-loop caching (v1.3.3)
----------------------------
The now-playing screen re-renders continuously (~10 fps) to animate the
pulsing dot, so anything done per-frame is effectively done forever.  Two
hot-path costs are therefore cached:

  - Scaled cover art: pygame.image.load + smoothscale of a 440×440 JPEG every
    frame was the single biggest CPU cost on the Pi.  _load_cover now caches
    the scaled Surface keyed by (url, w, h) in a bounded cache.
  - Gradient background: 24 full-screen filled circles per frame.  The
    gradient is now rendered once per (palette, size) onto an offscreen
    Surface and re-blitted; it only regenerates while a palette transition
    is actively lerping.

Both use _BoundedCache, the same insertion-order/LRU-refresh strategy the
palette cache has used since v1.3.2 (which now also uses it).

Design fidelity (v1.4.0)
------------------------
Implements the full DESIGN.md type/visual spec from design/DirectionA.jsx:

  - Bundled fonts (src/display/assets/fonts/, all OFL-licensed):
    Inter Tight SemiBold (hero), Inter Tight Medium (artist, adjacent track
    names), Newsreader Italic (album title), JetBrains Mono (all labels).
    Falls back to DejaVu SysFonts if the files are missing.
  - Letter-spacing for mono labels via per-character rendering
    (_render_tracked), cached in a _BoundedCache.
  - Shrink-to-fit typography everywhere: the hero keeps its step-down
    behavior, and artist (single line) and album (two wrapped lines) now
    shrink rather than clip.  Ellipsis appears only in the PREV/NEXT panel
    (per design + product decision).
  - Cover Lift shadow (Pillow gaussian blur, cached) + hairline ring.
  - Status strip with solid `surface` background; status dot with spec
    pulse (opacity/scale, 1.6s ease-in-out) and accent glow.
  - Genre chips: transparent background, accent @ ~33% alpha border,
    capped at 3 with a "+N" overflow chip.
  - Muted palette role is contrast-clamped to ≥4.5:1 against bg at
    extraction time (DESIGN.md Full-Opacity Rule).
  - display.reduced_motion config flag freezes all animation
    (translation of the design's prefers-reduced-motion requirement).

Empty states (v1.4.1)
---------------------
Boot (LISTENING), idle, and error (the v1.4.1 PlayerStatus.ERROR) render in
the full DirectionA frame per DESIGN.md §5: fallback palette (lerped to
smoothly), state-labelled status strip with state-mapped dot, the cover area
replaced by the state's treatment (rotating accent arc + time-progressive
label / 135° stripes / static red arc + recovery hint), the hero at 48px
with a state-specific string, and all album metadata suppressed.  Idle and
error are fully static frames — the render loop goes quiet; boot animates.

Static-frame cache (v1.4.0)
---------------------------
The only animated element on the now-playing screen is the status dot, but
the loop previously redrew everything at ~10 fps to keep it pulsing.  The
full frame (gradient, cover, shadow, all text) is now composed once onto an
offscreen Surface keyed by (track content, palette); steady-state frames are
one blit plus the dot.  The layout is likewise computed once at startup
(self._layout) instead of per frame.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests

from src.state.player_state import PlayerState, PlayerStatus
from src.display.layouts import get_now_playing_layout, NowPlayingLayout, Rect
from src.metadata.models import DisplayPalette, FALLBACK_PALETTE

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cover-art download safety (findings S-1 / S-2)
# ---------------------------------------------------------------------------
# `cover_art_url` originates from untrusted external APIs (Discogs image `uri`,
# the MusicBrainz Cover Art Archive).  A poisoned entry — or a MITM, since we do
# not pin certificates — could otherwise point the fetch at an internal LAN host
# (SSRF), a multi-gigabyte response that fills the SD card, or a malicious image
# that exploits a stale decoder.  The download path therefore:
#   * requires https,
#   * restricts the host to an allow-list of known cover-art providers,
#   * refuses any hop whose host resolves to a private / loopback / link-local IP
#     (defends against DNS rebinding and redirect-to-internal),
#   * follows redirects manually so every hop is re-validated,
#   * aborts after _MAX_COVER_BYTES,
#   * requires an image/* Content-Type, and
#   * verifies the decoded image (type + pixel bounds) before it is cached.

# Apex domains we trust to serve cover art.  A host matches if it IS one of
# these or is a dotted subdomain of one — never merely a string that ends with
# one (so "evilcoverartarchive.org" is rejected, not allowed).  Cover Art
# Archive 307-redirects to the Internet Archive (archive.org), so that apex is
# included for the redirect hop.
_ALLOWED_COVER_APEX_DOMAINS = (
    "discogs.com",
    "coverartarchive.org",
    "archive.org",
    "mzstatic.com",
)

_MAX_COVER_BYTES = 10 * 1024 * 1024   # 10 MB ceiling on a downloaded cover
_MAX_COVER_REDIRECTS = 5              # cap redirect chains
_COVER_CONNECT_READ_TIMEOUT = 15     # seconds, per HTTP request
# Reject images larger than this many total pixels (decompression-bomb guard).
# 6000×6000 ≈ 36 MP comfortably exceeds any real album-cover scan.
_MAX_IMAGE_PIXELS = 6000 * 6000


def _host_is_allowed(host: Optional[str]) -> bool:
    """True if `host` is an allow-listed apex domain or a dotted subdomain of one.

    Matching is exact-or-dot-boundary (`host == apex` or `host.endswith("." +
    apex)`), never a bare suffix test — otherwise "evilcoverartarchive.org"
    would be accepted as if it were "coverartarchive.org".
    """
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(
        host == apex or host.endswith("." + apex)
        for apex in _ALLOWED_COVER_APEX_DOMAINS
    )


def _host_resolves_to_public_ip(host: str) -> bool:
    """True only if every DNS result for `host` is a global (public) address.

    Blocks an allow-listed name that resolves to a private / loopback /
    link-local / reserved address — the DNS-rebinding and redirect-to-internal
    SSRF vectors (S-1).  Fails closed: any resolution error returns False.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    return True


def _validate_cover_url(url: str) -> str:
    """Validate and normalize a single cover-art URL hop; return the URL to fetch.

    - The host must be allow-listed and resolve to a public address (S-1).
    - http is upgraded to https for allow-listed hosts (the MusicBrainz Cover
      Art Archive sometimes returns http URLs; upgrading only ever makes the
      request more secure, and avoids silently dropping every fallback cover).

    Raises ValueError if the scheme is not http(s), the host is not allow-listed,
    or the host resolves to a non-public address.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"cover URL scheme not allowed: {parts.scheme!r}")
    if not _host_is_allowed(host):
        raise ValueError(f"cover URL host not allow-listed: {host!r}")
    if not _host_resolves_to_public_ip(host):
        raise ValueError(f"cover URL host resolves to a non-public address: {host!r}")
    if parts.scheme == "http":
        url = parts._replace(scheme="https").geturl()
    return url


def _validate_image_file(path: str) -> None:
    """Verify a downloaded file is a sane, bounded image before it is cached.

    Uses Pillow's `verify()` to reject truncated / malformed files and caps the
    pixel count to guard against decompression bombs (S-2).  Raises ValueError
    on anything suspicious.
    """
    from PIL import Image

    # Belt-and-suspenders: bound Pillow's own decompression-bomb threshold too.
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS

    try:
        with Image.open(path) as probe:
            fmt = probe.format
            width, height = probe.size
            probe.verify()  # structural integrity check; consumes the file object
    except Exception as e:
        raise ValueError(f"not a decodable image: {e}")

    if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
        raise ValueError(f"unexpected image format: {fmt!r}")
    if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
        raise ValueError(f"image dimensions out of bounds: {width}x{height}")

# Suppress pygame audio (we're output-only) and point to the right display
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("DISPLAY", ":0")  # Needed when running headless / via SSH

# How long (seconds) to lerp between palettes on track change
_TRANSITION_SECS = 1.0

# Cap on the per-URL palette cache.  Extraction is fast (~ms per album), so
# re-running on a cache miss is fine; the cap just prevents unbounded growth
# on machines with very long uptimes or very large collections.
_PALETTE_CACHE_MAX = 200

# Cap on the scaled-cover-Surface cache.  Each 440×440 RGB surface is ~775 KB,
# so 16 entries ≈ 12 MB — generous for a device that shows one cover at a time,
# and tiny next to re-decoding a JPEG ten times a second.
_COVER_CACHE_MAX = 16

# Cap on the tracked-label Surface cache (letter-spaced mono labels).
# Labels are tiny surfaces and mostly static per track; 128 is plenty.
_LABEL_CACHE_MAX = 128

# Status dot pulse period (seconds) — DESIGN.md: 1.6s ease-in-out infinite.
_PULSE_SECS = 1.6

# Boot arc rotation period (seconds) — DESIGN.md: 1.4s linear infinite.
_ARC_SECS = 1.4

# Muted red for the error state (DESIGN.md §5: #c85050).
_ERROR_RED = (200, 80, 80)

# Status strip labels per state (DESIGN.md stateLabel mapping; paused and
# between-tracks arrive with their states in a later release).
_STATE_LABELS = {
    "playing": "NOW PLAYING",
    "boot": "IDENTIFYING…",
    "idle": "IDLE",
    "error": "NO MATCH FOUND",
}

# Hero placeholder strings for the empty states, rendered at 48px
# (DESIGN.md empty-state font size exception).
_EMPTY_HEROES = {
    "boot": "Listening…",
    "idle": "Waiting for a record",
    "error": "Couldn't identify",
}

# Bundled fonts (DESIGN.md §3).  Role → filename in assets/fonts/.
# "display" = hero track (Inter Tight 600), "text" = artist + adjacent names
# (Inter Tight 500), "title" = album (Newsreader italic 400), "mono" = all
# labels/metadata (JetBrains Mono 400).
_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_FONT_FILES = {
    "display": "InterTight-SemiBold.ttf",
    "text": "InterTight-Medium.ttf",
    "title": "Newsreader-Italic.ttf",
    "mono": "JetBrainsMono-Regular.ttf",
}
# SysFont fallbacks if a bundled file is missing (name, bold, italic).
_SYSFONT_FALLBACKS = {
    "display": ("dejavu sans", True, False),
    "text": ("dejavu sans", False, False),
    "title": ("dejavu sans", False, True),
    "mono": ("dejavu sans mono", False, False),
}

# Letter-spacing (em) per label context, from DESIGN.md §3.
_TRACKING_LABEL = 0.16     # status strip, side counter
_TRACKING_CHIP = 0.10      # genre chips
_TRACKING_CATALOG = 0.08   # year · label · catalog footer
_TRACKING_ADJACENT = 0.12  # PREV/NEXT labels


class _BoundedCache:
    """A small insertion-ordered cache with LRU-refresh-on-get and a size cap.

    Python dicts preserve insertion order, so the eviction candidate is always
    the first key.  get() re-inserts hits at the end ("LRU-ish"), matching the
    strategy the palette cache has used since v1.3.2.  Pure Python, no pygame
    dependency — unit-tested in tests/test_renderer_caches.py.
    """

    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._data: dict = {}

    def get(self, key):
        """Return the cached value (refreshing its eviction position), or None."""
        if key not in self._data:
            return None
        value = self._data.pop(key)
        self._data[key] = value
        return value

    def put(self, key, value):
        """Insert/replace a value, evicting oldest entries beyond the cap."""
        self._data.pop(key, None)
        self._data[key] = value
        while len(self._data) > self.max_entries:
            self._data.pop(next(iter(self._data)))

    def __contains__(self, key) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)


def _lerp_color(a: tuple, b: tuple, t: float) -> tuple:
    """Linear interpolation between two RGB tuples. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _lerp_palette(a: DisplayPalette, b: DisplayPalette, t: float) -> DisplayPalette:
    """Interpolate all five channels of two DisplayPalettes."""
    return DisplayPalette(
        bg=_lerp_color(a.bg, b.bg, t),
        surface=_lerp_color(a.surface, b.surface, t),
        accent=_lerp_color(a.accent, b.accent, t),
        text=_lerp_color(a.text, b.text, t),
        muted=_lerp_color(a.muted, b.muted, t),
    )


def _clamp_luminance(color: tuple, min_lum: float = 0.25) -> tuple:
    """Ensure a color is bright enough to read against a dark background.

    Uses a simple perceived-brightness formula. If the color is too dark,
    it's brightened proportionally until it hits min_lum.
    """
    r, g, b = color
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    if lum < min_lum and lum > 0:
        scale = min_lum / lum
        return tuple(min(255, int(c * scale)) for c in (r, g, b))
    return color


def _relative_luminance(color: tuple) -> float:
    """WCAG 2.x relative luminance of an sRGB color (0.0–1.0)."""
    def chan(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = color
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast_ratio(a: tuple, b: tuple) -> float:
    """WCAG contrast ratio between two RGB colors (1.0–21.0)."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _ensure_contrast(color: tuple, bg: tuple, min_ratio: float = 4.5) -> tuple:
    """Lighten *color* until it reaches min_ratio contrast against *bg*.

    DESIGN.md §2 (Full-Opacity Rule / muted role): secondary text must pass
    4.5:1 against its album background at full opacity.  Cool-dark
    backgrounds pull contrast down faster than neutral darks, so extracted
    muted values are clamped here rather than trusted.  Blends toward white
    in small steps; falls back to near-white if even that fails (cannot
    happen for the dark backgrounds this product produces, but cheap to
    guard).
    """
    if _contrast_ratio(color, bg) >= min_ratio:
        return color
    r, g, b = color
    for step in range(1, 21):
        t = step / 20.0
        candidate = tuple(int(c + (255 - c) * t) for c in (r, g, b))
        if _contrast_ratio(candidate, bg) >= min_ratio:
            return candidate
    return (235, 235, 235)


def _extract_palette(image_path: Path) -> DisplayPalette:
    """Extract a 5-color display palette from a cached cover art image.

    Uses Pillow's color quantization to find the dominant hues, then
    derives the full palette (bg, surface, accent, text, muted) from them.

    Falls back to FALLBACK_PALETTE on any error.
    """
    try:
        from PIL import Image

        # Validate before decoding (S-2): the download path already checks, but
        # palette extraction can also run against pre-existing cache files, so
        # guard here too against malformed images / decompression bombs.
        _validate_image_file(str(image_path))

        img = Image.open(image_path).convert("RGB")
        img = img.resize((80, 80), Image.LANCZOS)

        # Quantize to 8 colors; getpalette returns flat R,G,B,R,G,B,... list
        quantized = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        raw = quantized.getpalette()  # 768 values: 8 colors × 3 channels

        # Count how often each palette index appears (most → most dominant)
        pixel_data = list(quantized.getdata())
        counts = [0] * 8
        for idx in pixel_data:
            counts[idx] += 1

        # Build (count, RGB) tuples, sorted by dominance descending
        palette_colors = [
            (counts[i], (raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]))
            for i in range(8)
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
        accent = _clamp_luminance(accent_raw, min_lum=0.30)

        # Derive bg: darken dominant significantly (target ~15% brightness)
        scale_bg = 0.18
        bg = tuple(max(8, int(c * scale_bg + dominant[i] * 0.04)) for i, c in enumerate(dominant))

        # Surface: slightly lighter than bg
        surface = tuple(min(255, int(c * 1.6)) for c in bg)

        # Text: near-white with a slight warm tint from dominant
        text = (
            min(255, 230 + int(dominant[0] * 0.04)),
            min(255, 225 + int(dominant[1] * 0.03)),
            min(255, 215 + int(dominant[2] * 0.03)),
        )

        # Muted: medium gray, very slightly tinted — then contrast-clamped
        # to ≥4.5:1 against this album's bg (DESIGN.md Full-Opacity Rule).
        muted = (
            min(200, 120 + int(dominant[0] * 0.08)),
            min(200, 118 + int(dominant[1] * 0.07)),
            min(200, 115 + int(dominant[2] * 0.06)),
        )
        muted = _ensure_contrast(muted, bg, min_ratio=4.5)

        return DisplayPalette(bg=bg, surface=surface, accent=accent, text=text, muted=muted)

    except Exception as e:
        log.warning(f"Palette extraction failed for {image_path}: {e}")
        return FALLBACK_PALETTE


class DisplayRenderer:
    """Renders now-playing info to an HDMI screen via pygame."""

    def __init__(self, config: dict, state: PlayerState):
        self.config = config["display"]
        self.state = state
        self.width: int = self.config["width"]
        self.height: int = self.config["height"]
        self.fullscreen: bool = self.config.get("fullscreen", True)
        self.dynamic_theming: bool = self.config.get("dynamic_theming", True)
        # Translation of the design's prefers-reduced-motion requirement:
        # pygame has no OS media query, so it's a config flag.  When set,
        # the status dot renders static (no pulse, no glow animation).
        self.reduced_motion: bool = self.config.get("reduced_motion", False)
        self.cache_dir = Path(
            self.config.get("cover_art_cache_dir", "src/display/assets/cache")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._screen = None
        self._font_cache: dict = {}     # (role, size) → pygame.font.Font
        self._running = True
        self._dirty = True              # Force initial render

        # Layout is a pure function of (width, height) — compute once
        # instead of once per frame (v1.4.0).
        self._layout: NowPlayingLayout = get_now_playing_layout(self.width, self.height)

        # Palette transition state
        self._current_palette: DisplayPalette = FALLBACK_PALETTE
        self._target_palette: DisplayPalette = FALLBACK_PALETTE
        self._transition_start: float = 0.0
        self._palette_cache = _BoundedCache(_PALETTE_CACHE_MAX)  # cover_art_url → DisplayPalette

        # Render hot-path caches (v1.3.3)
        self._cover_cache = _BoundedCache(_COVER_CACHE_MAX)  # (url, w, h) → scaled Surface
        self._gradient_key: Optional[tuple] = None           # (bg, surface, w, h)
        self._gradient_surface = None                        # pygame.Surface

        # Render hot-path caches (v1.4.0)
        self._label_cache = _BoundedCache(_LABEL_CACHE_MAX)  # tracked-label Surfaces
        self._shadow_key: Optional[tuple] = None             # (w, h)
        self._shadow_surface = None                          # Cover Lift shadow
        self._static_key: Optional[tuple] = None             # (track content, palette)
        self._static_surface = None                          # composed frame (any screen)

        # Empty-state machinery (v1.4.1)
        self._listening_since: Optional[float] = None        # boot-label elapsed clock
        self._arc_segment = None                             # pre-rendered boot/error arc

        # Strong references to fire-and-forget tasks (cover prefetches).
        # asyncio only keeps weak references to tasks, so without this a
        # running download could in principle be garbage-collected mid-flight.
        self._bg_tasks: set = set()

        self.state.on_change(self._on_state_change)

    def _spawn(self, coro):
        """create_task() with a strong reference held until the task completes."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self):
        """Initialize pygame. Must be called before run()."""
        import pygame
        pygame.init()
        flags = pygame.FULLSCREEN if self.fullscreen else 0
        self._screen = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption("vinyl-now-playing")
        pygame.mouse.set_visible(False)
        log.info(f"Display initialized: {self.width}x{self.height} fullscreen={self.fullscreen}")

    def _font(self, role: str, size: int):
        """Return the bundled font for a role at a pixel size, cached.

        Roles map to the DESIGN.md type hierarchy (see _FONT_FILES).  Loading
        is lazy — a TTF is opened once per (role, size) and held forever
        (fonts are a small, fixed set of sizes).  Falls back to the DejaVu
        SysFont family if the bundled file is missing, so dev machines and
        CI without the assets still render.
        """
        import pygame

        key = (role, size)
        font = self._font_cache.get(key)
        if font is not None:
            return font

        path = _FONT_DIR / _FONT_FILES[role]
        try:
            font = pygame.font.Font(str(path), size)
        except (FileNotFoundError, OSError, pygame.error):
            name, bold, italic = _SYSFONT_FALLBACKS[role]
            font = pygame.font.SysFont(name, size, bold=bold, italic=italic)
            log.warning(f"Bundled font missing ({path.name}); using SysFont fallback")
        self._font_cache[key] = font
        return font

    def _on_state_change(self, state: PlayerState):
        """Called by PlayerState whenever anything changes."""
        self._dirty = True
        # Boot-label elapsed clock (v1.4.1): starts when LISTENING begins,
        # cleared on any other state so the next session starts fresh.
        if state.status == PlayerStatus.LISTENING:
            if self._listening_since is None:
                self._listening_since = time.monotonic()
        else:
            self._listening_since = None
        # When a new track arrives, queue a palette transition and prefetch cover art
        if state.status == PlayerStatus.PLAYING and state.current_track:
            url = state.current_track.cover_art_url
            self._queue_palette(url)
            if url:
                self._spawn(self._prefetch_cover(url))
        elif state.status in (PlayerStatus.IDLE, PlayerStatus.ERROR, PlayerStatus.LISTENING):
            # Empty states always use the fallback palette (DESIGN.md §2);
            # lerp back smoothly rather than jump-cutting.
            self._queue_palette(None)

    # -----------------------------------------------------------------------
    # Async render loop
    # -----------------------------------------------------------------------

    async def run(self):
        """Async display loop — re-renders when dirty or transitioning."""
        import pygame
        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.stop()
                    return
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self.stop()
                    return

            # Keep re-rendering during a palette transition even if not dirty
            transitioning = (time.monotonic() - self._transition_start) < _TRANSITION_SECS
            if self._dirty or transitioning:
                # Reset BEFORE rendering so _render_now_playing /
                # _render_empty can set self._dirty = True to request
                # another frame for animations (pulsing dot, spinner).
                self._dirty = False
                self._render()
                pygame.display.flip()

            # Sleep cadence: 30 fps while transitioning (smooth lerp), otherwise
            # ~10 fps — fast enough for the 1.6s pulsing dot, but easy on the Pi.
            await asyncio.sleep(1 / 30 if transitioning else 1 / 10)

    # -----------------------------------------------------------------------
    # Render dispatch
    # -----------------------------------------------------------------------

    def _render(self):
        """Dispatch to the appropriate layout based on current player status."""
        if self.state.status == PlayerStatus.IDLE:
            self._render_empty("idle")
        elif self.state.status == PlayerStatus.LISTENING:
            self._render_empty("boot")
        elif self.state.status == PlayerStatus.ERROR:
            self._render_empty("error")
        elif self.state.status == PlayerStatus.PLAYING and self.state.current_track:
            self._render_now_playing()
        else:
            self._render_empty("boot")

    # -----------------------------------------------------------------------
    # Now-playing screen
    # -----------------------------------------------------------------------

    def _render_now_playing(self):
        """Render the now-playing screen: cached static frame + animated dot.

        Everything except the status dot is composed once per (track content,
        palette) onto an offscreen Surface (_compose_now_playing).  Steady-
        state frames — the overwhelming majority — are one full-screen blit
        plus a few alpha circles for the dot, instead of re-rendering every
        text element at 10 fps (v1.4.0).  During the 1s palette lerp the key
        changes each frame, so the frame recomposes — same cost profile as
        the pre-cache code, and only for one second per track change.
        """
        track = self.state.current_track
        layout = self._layout
        p = self._animated_palette()

        cover = self._load_cover(track.cover_art_url, layout.cover_art.w, layout.cover_art.h)

        key = (
            track.title, track.artist, track.album,
            tuple(track.genres or ()),
            track.year, track.label, track.catalog_number,
            self._side_string(track),
            track.prev_track_title, track.next_track_title,
            id(cover),  # changes when the async cover download lands
            p.bg, p.surface, p.accent, p.text, p.muted,
        )
        if self._static_key != key or self._static_surface is None:
            self._static_surface = self._compose_now_playing(track, layout, p, cover)
            self._static_key = key

        self._screen.blit(self._static_surface, (0, 0))
        self._draw_status_dot(self._screen, layout, p.accent, animate=True, glow=True)

        # Keep re-rendering so the dot stays animated; with reduced_motion
        # the frame is fully static, so the loop can go quiet.
        if not self.reduced_motion:
            self._dirty = True

    def _compose_now_playing(self, track, layout: NowPlayingLayout, p: DisplayPalette, cover):
        """Compose the full static now-playing frame onto a new Surface.

        Implements the v1.2.1 dynamic push-down design with v1.4.0 fidelity:
        the hero claims the space it needs (stepping down in size as a last
        resort), and the divider/artist/album/chips flow from its actual
        bottom edge.  Artist and album use shrink-to-fit (never clipped, no
        ellipsis); meta footer and prev/next stay bottom-anchored.
        """
        import pygame

        surf = pygame.Surface((self.width, self.height))
        self._draw_gradient_bg(surf, p)

        # --- Cover: Lift shadow beneath, hairline ring above (DESIGN.md §4) ---
        ca = layout.cover_art
        shadow = self._cover_shadow(ca.w, ca.h)
        pad = (shadow.get_width() - ca.w) // 2
        offset_y = max(4, int(30 * min(self.width / 1024, self.height / 600)))
        surf.blit(shadow, (ca.x - pad, ca.y - pad + offset_y))
        if cover:
            surf.blit(cover, (ca.x, ca.y))
        else:
            pygame.draw.rect(surf, p.surface, (ca.x, ca.y, ca.w, ca.h))
        ring = pygame.Surface((ca.w, ca.h), pygame.SRCALPHA)
        pygame.draw.rect(ring, (255, 255, 255, 10), ring.get_rect(), 1)  # 0.04 alpha
        surf.blit(ring, (ca.x, ca.y))

        # --- Status strip (minus the animated dot) ---
        self._draw_header(surf, layout, p, _STATE_LABELS["playing"], self._side_string(track))

        # -----------------------------------------------------------------
        # Dynamic push-down geometry
        # -----------------------------------------------------------------
        sy = self.height / 600
        GAP_BEFORE_DIV    = max(2, int(4  * sy))   # title bottom → divider top
        GAP_AFTER_DIV     = max(8, int(20 * sy))   # divider bottom → artist top
        GAP_AFTER_ARTIST  = max(2, int(4  * sy))   # artist bottom → album top
        GAP_AFTER_ALBUM   = max(3, int(8  * sy))   # album bottom → chips top
        GAP_CHIPS_TO_META = max(8, int(16 * sy))   # chips bottom → meta top (min)

        div_h   = max(2, layout.divider.h)
        chips_h = layout.genre_chips.h

        # Shrink-to-fit (v1.4.0): artist stays on one line, album wraps to
        # at most two (DESIGN.md 2-line clamp) — both reduce font size
        # instead of clipping.  Heights are measured, not assumed, so the
        # push-down geometry stays honest when the album takes two lines.
        artist = track.artist or ""
        album = track.album or ""
        artist_size, _ = self._fit_wrapped(
            artist, "text", layout.font_size_artist, layout.artist_text.w,
            max_lines=1, min_size=18,
        )
        artist_h = self._measure_wrapped_text(
            artist, "text", artist_size, layout.artist_text.w, line_height=1.04
        )
        album_size, _ = self._fit_wrapped(
            album, "title", layout.font_size_album, layout.album_text.w,
            max_lines=2, min_size=14,
        )
        album_h = self._measure_wrapped_text(
            album, "title", album_size, layout.album_text.w, line_height=1.12
        )

        secondary_h = (
            GAP_BEFORE_DIV + div_h + GAP_AFTER_DIV
            + artist_h + GAP_AFTER_ARTIST
            + album_h + GAP_AFTER_ALBUM
            + chips_h + GAP_CHIPS_TO_META
        )

        # Maximum pixels the title can occupy before crowding the secondary block
        title_top   = layout.track_text.y
        meta_y      = layout.meta_text.y
        max_title_h = max(layout.font_size_track + 8, meta_y - title_top - secondary_h)

        # Hero size: try the layout default, step down 4px at a time if needed
        font_size = layout.font_size_track
        title_h   = self._measure_wrapped_text(track.title, "display", font_size, layout.track_text.w)
        if title_h > max_title_h:
            for smaller in range(font_size - 4, 17, -4):
                candidate_h = self._measure_wrapped_text(
                    track.title, "display", smaller, layout.track_text.w
                )
                if candidate_h <= max_title_h:
                    font_size = smaller
                    title_h   = candidate_h
                    break
            else:
                title_h = max_title_h   # absolute last resort: clip bottom lines

        title_rect = Rect(layout.track_text.x, title_top, layout.track_text.w, max_title_h)
        actual_title_h = self._draw_wrapped_text(
            surf, track.title, "display", font_size, title_rect, p.text
        )
        title_bottom = title_top + actual_title_h

        # Accent divider — fixed 64px-reference width (a punctuation mark,
        # not a full-width divider; DESIGN.md §5)
        div_y = title_bottom + GAP_BEFORE_DIV
        pygame.draw.rect(surf, p.accent, (layout.divider.x, div_y, layout.divider_width, div_h))

        # Artist (Inter Tight Medium, lh 1.04)
        artist_y    = div_y + div_h + GAP_AFTER_DIV
        artist_rect = Rect(layout.artist_text.x, artist_y, layout.artist_text.w, max(artist_h, 1))
        self._draw_wrapped_text(surf, artist, "text", artist_size, artist_rect, p.text, line_height=1.04)

        # Album (Newsreader italic in accent, lh 1.12, ≤2 lines)
        album_y    = artist_y + artist_h + GAP_AFTER_ARTIST
        album_rect = Rect(layout.album_text.x, album_y, layout.album_text.w, max(album_h, 1))
        self._draw_wrapped_text(surf, album, "title", album_size, album_rect, p.accent, line_height=1.12)

        # Genre chips
        chips_y    = album_y + album_h + GAP_AFTER_ALBUM
        chips_rect = Rect(layout.genre_chips.x, chips_y, layout.genre_chips.w, chips_h)
        if track.genres:
            self._draw_genre_chips(surf, track.genres, layout, p, chips_rect)

        # Bottom-anchored: catalog footer (tracked mono) + adjacent panel
        meta_parts = [str(x) for x in [track.year, track.label, track.catalog_number] if x]
        if meta_parts:
            label = self._render_tracked(
                " · ".join(meta_parts), layout.font_size_meta, p.muted, _TRACKING_CATALOG
            )
            surf.blit(label, (layout.meta_text.x, layout.meta_text.y),
                      area=(0, 0, layout.meta_text.w, layout.meta_text.h))

        self._draw_prev_next(surf, layout, p, track)
        return surf

    def _strip_pad_x(self) -> int:
        """Status strip horizontal padding (26px at 1024-wide reference)."""
        return max(8, int(26 * self.width / 1024))

    def _dot_radius(self) -> int:
        """Status dot radius — 8×8px dot at reference scale (DESIGN.md §5)."""
        return max(3, int(4 * min(self.width / 1024, self.height / 600)))

    def _draw_header(self, target, layout: NowPlayingLayout, p: DisplayPalette,
                     label_text: str, side_str: Optional[str] = None):
        """Draw the status strip: solid surface background, tracked state
        label, optional right-aligned side counter (suppressed in the empty
        states, which have no side to count).

        The animated dot is deliberately NOT drawn here — it's the one
        per-frame element (_draw_status_dot), so the strip can live in the
        cached static frame.
        """
        import pygame

        strip = layout.header_strip
        # Solid surface background (DESIGN.md §5: grounds the strip without a border)
        pygame.draw.rect(target, p.surface, (strip.x, strip.y, strip.w, strip.h))

        pad_x = self._strip_pad_x()
        dot_r = self._dot_radius()

        label = self._render_tracked(label_text, layout.font_size_header, p.muted, _TRACKING_LABEL)
        target.blit(label, (pad_x + dot_r * 2 + 8, (strip.h - label.get_height()) // 2))

        if side_str:
            side = self._render_tracked(side_str, layout.font_size_header, p.muted, _TRACKING_LABEL)
            target.blit(side, (self.width - pad_x - side.get_width(),
                               (strip.h - side.get_height()) // 2))

    def _draw_status_dot(self, target, layout: NowPlayingLayout, color: tuple,
                         animate: bool = True, glow: bool = True):
        """Draw the status dot — the strip's animated element.

        DESIGN.md §5: 8×8 circle; color maps to playback state (accent while
        playing/boot, muted in idle, muted red in error).  Pulse keyframes
        0%/100% {opacity 1, scale 1} → 50% {opacity 0.55, scale 0.9}, 1.6s
        ease-in-out infinite — a raised cosine reproduces the eased triangle
        exactly.  The glow approximates `box-shadow: 0 0 8px` with two soft
        alpha circles; per the spec it appears only in glowing states
        (playing/boot).  `animate=False` (idle, error, reduced_motion)
        renders the dot static at full opacity.
        """
        import math
        import pygame

        strip = layout.header_strip
        r = self._dot_radius()
        if not animate or self.reduced_motion:
            k = 0.0
        else:
            phase = (time.monotonic() % _PULSE_SECS) / _PULSE_SECS
            k = 0.5 - 0.5 * math.cos(2 * math.pi * phase)   # 0→1→0, eased
        opacity = 1.0 - 0.45 * k
        scale   = 1.0 - 0.10 * k

        cx = self._strip_pad_x() + r
        cy = strip.y + strip.h // 2

        size = r * 6  # room for the glow halo
        dot = pygame.Surface((size, size), pygame.SRCALPHA)
        c = size // 2
        if glow:
            glow_alpha = int(70 * opacity)
            pygame.draw.circle(dot, (*color, glow_alpha // 2), (c, c), int(r * 2.5))
            pygame.draw.circle(dot, (*color, glow_alpha), (c, c), int(r * 1.6))
        pygame.draw.circle(dot, (*color, int(255 * opacity)), (c, c), max(2, int(r * scale)))
        target.blit(dot, (cx - c, cy - c))

    def _side_string(self, track) -> str:
        """Build the 'SIDE A · 04 OF 06' string, or '' if data is unavailable."""
        letter = track.side_letter
        pos = track.side_position
        total = track.side_total
        if letter and pos and total:
            return f"SIDE {letter} · {pos:02d} OF {total:02d}"
        elif track.track_display:
            return track.track_display
        return ""

    def _chip_texts(self, genres: list) -> list:
        """Cap chips at 3 with a '+N' overflow indicator (DESIGN.md §6).

        Pure helper, unit-tested directly — Discogs can return 0 or 5+
        genres and the catalog footer must stay anchored regardless.
        """
        shown = list(genres[:3])
        if len(genres) > 3:
            shown.append(f"+{len(genres) - 3}")
        return shown

    def _draw_genre_chips(
        self,
        target,
        genres: list,
        layout: NowPlayingLayout,
        p: DisplayPalette,
        chips_rect=None,
    ):
        """Render genre chips per DESIGN.md §5: transparent background,
        1px border in accent at ~33% alpha (the JSX `{accent}55`), tracked
        muted mono text, sharp corners, max 3 + '+N' overflow.

        If *chips_rect* is supplied it overrides ``layout.genre_chips`` for the
        bounding box, allowing the caller to position chips dynamically.
        """
        import pygame

        rect = chips_rect if chips_rect is not None else layout.genre_chips
        px = layout.chip_padding_x
        py = layout.chip_padding_y
        gap = layout.chip_gap
        x, y = rect.x, rect.y
        border = (*p.accent, 0x55)

        for text in self._chip_texts(genres):
            label = self._render_tracked(text, layout.font_size_chips, p.muted, _TRACKING_CHIP)
            chip_w = label.get_width() + px * 2
            chip_h = label.get_height() + py * 2

            # Wrap to next row if we'd overflow the bounding box width
            if x + chip_w > rect.x + rect.w and x > rect.x:
                x = rect.x
                y += chip_h + gap
                if y + chip_h > rect.y + rect.h:
                    break  # Out of room

            # Per-chip SRCALPHA surface so the border alpha actually blends
            chip = pygame.Surface((chip_w, chip_h), pygame.SRCALPHA)
            pygame.draw.rect(chip, border, chip.get_rect(), 1)
            chip.blit(label, (px, py))
            target.blit(chip, (x, y))
            x += chip_w + gap

    def _draw_prev_next(self, target, layout: NowPlayingLayout, p: DisplayPalette, track):
        """Draw the adjacent-track panel (DESIGN.md §5 PREV/NEXT spec).

        Top divider in `surface`, PREV left-aligned, NEXT right-aligned so it
        hangs from the metadata column's right edge.  Track names are Inter
        Tight Medium with ellipsis truncation — the one place ellipsis is
        sanctioned (product decision: everywhere else shrinks instead).
        """
        import pygame

        prev = track.prev_track_title
        nxt = track.next_track_title
        if not prev and not nxt:
            return

        strip = layout.prev_next
        # border-top divider.  The design spec says 1px `surface`, but
        # surface-on-gradient is nearly invisible on the physical display
        # at room distance — production blends 40% toward `muted` (still
        # album-tinted, deliberately just-visible).  Product decision
        # 2026-06-11.
        divider = _lerp_color(p.surface, p.muted, 0.40)
        pygame.draw.rect(target, divider, (strip.x, strip.y, strip.w, 1))

        name_font = self._font("text", layout.font_size_adjacent)
        half_w = strip.w // 2 - 16
        y0 = strip.y + max(3, int(8 * self.height / 600))

        if prev:
            label = self._render_tracked("← PREV", layout.font_size_header, p.muted, _TRACKING_ADJACENT)
            target.blit(label, (strip.x, y0))
            name = name_font.render(self._ellipsize(prev, name_font, half_w), True, p.text)
            target.blit(name, (strip.x, y0 + label.get_height() + 4))

        if nxt:
            label = self._render_tracked("NEXT →", layout.font_size_header, p.muted, _TRACKING_ADJACENT)
            right = strip.x + strip.w
            target.blit(label, (right - label.get_width(), y0))
            name = name_font.render(self._ellipsize(nxt, name_font, half_w), True, p.text)
            target.blit(name, (right - name.get_width(), y0 + label.get_height() + 4))

    # -----------------------------------------------------------------------
    # Boot / idle / listening states
    # -----------------------------------------------------------------------

    @staticmethod
    def _boot_label(elapsed: float) -> str:
        """Time-progressive boot label (DESIGN.md §5).

        Lets the room listener distinguish active identification from a hung
        process without walking to the Pi: WARMING UP (0–19s), STILL
        LISTENING… (20–59s), IDENTIFYING… M:SS (60s+).
        """
        if elapsed < 20:
            return "WARMING UP"
        if elapsed < 60:
            return "STILL LISTENING…"
        m = int(elapsed // 60)
        s = int(elapsed % 60)
        return f"IDENTIFYING… {m}:{s:02d}"

    def _render_empty(self, kind: str):
        """Render a boot/idle/error empty state (v1.4.1, DESIGN.md §5).

        Full DirectionA frame on the (lerped-to-)fallback palette: status
        strip with state label (no side counter), the 440×440 cover area
        replaced by the state's empty-cover treatment, the hero at 48px with
        a state-specific string, and all album metadata suppressed.

        Animation budget per state: boot animates (rotating arc + pulsing
        dot + ticking label); idle and error are fully static, so the render
        loop goes quiet — the stillness of the error arc is the signal
        (boot spins; error sits).
        """
        layout = self._layout
        p = self._animated_palette()

        elapsed = time.monotonic() - self._listening_since if self._listening_since else 0.0
        boot_label = self._boot_label(elapsed) if kind == "boot" else None

        key = ("empty", kind, boot_label, p.bg, p.surface, p.accent, p.text, p.muted)
        if self._static_key != key or self._static_surface is None:
            self._static_surface = self._compose_empty(kind, layout, p, boot_label)
            self._static_key = key

        self._screen.blit(self._static_surface, (0, 0))

        # State-mapped dot (DESIGN.md §5): boot pulses+glows in accent;
        # idle sits static in muted; error sits static in muted red.
        if kind == "boot":
            self._draw_status_dot(self._screen, layout, p.accent, animate=True, glow=True)
            self._draw_boot_arc(self._screen, layout, p, elapsed)
            self._dirty = True  # arc + dot + label all tick
        elif kind == "error":
            self._draw_status_dot(self._screen, layout, _ERROR_RED, animate=False, glow=False)
        else:  # idle
            self._draw_status_dot(self._screen, layout, p.muted, animate=False, glow=False)

    def _compose_empty(self, kind: str, layout: NowPlayingLayout,
                       p: DisplayPalette, boot_label: Optional[str]):
        """Compose the static portion of an empty-state frame.

        Includes everything except the dot and (in boot) the rotating arc:
        gradient, cover shadow + treatment + ring, strip, hero, labels.
        """
        import pygame

        surf = pygame.Surface((self.width, self.height))
        self._draw_gradient_bg(surf, p)

        s = min(self.width / 1024, self.height / 600)
        ca = layout.cover_art

        # Cover Lift shadow stays — the empty cover is still the physical
        # object slot (the JSX applies the container shadow in all states).
        shadow = self._cover_shadow(ca.w, ca.h)
        pad = (shadow.get_width() - ca.w) // 2
        surf.blit(shadow, (ca.x - pad, ca.y - pad + max(4, int(30 * s))))

        # --- Empty-cover treatment ---
        if kind == "idle":
            self._draw_stripes(surf, ca, p)
            label = self._render_tracked("NO RECORD ON PLATTER", layout.font_size_header,
                                         p.muted, _TRACKING_LABEL)
            surf.blit(label, (ca.x + (ca.w - label.get_width()) // 2,
                              ca.y + (ca.h - label.get_height()) // 2))
        else:
            pygame.draw.rect(surf, p.surface, (ca.x, ca.y, ca.w, ca.h))
            cx = ca.x + ca.w // 2
            arc_r = int(32 * s)
            arc_cy = ca.y + ca.h // 2 - int(24 * s)   # arc sits above its label(s)
            # Ghost ring: stable circular frame so the arc reads as contained
            # rotation (muted @ 40% opacity, 1px)
            ring = pygame.Surface((arc_r * 2 + 4, arc_r * 2 + 4), pygame.SRCALPHA)
            pygame.draw.circle(ring, (*p.muted, 102), (arc_r + 2, arc_r + 2), arc_r, 1)
            surf.blit(ring, (cx - arc_r - 2, arc_cy - arc_r - 2))

            label_y = arc_cy + arc_r + int(18 * s)
            if kind == "boot":
                label = self._render_tracked(boot_label or "WARMING UP",
                                             layout.font_size_header, p.muted, 0.20)
                surf.blit(label, (cx - label.get_width() // 2, label_y))
            else:  # error — static arc + primary label + recovery hint
                arc = self._get_arc_segment(arc_r, _ERROR_RED)
                surf.blit(arc, arc.get_rect(center=(cx, arc_cy)))
                label = self._render_tracked("NO MATCH FOUND", layout.font_size_header,
                                             p.muted, 0.20)
                surf.blit(label, (cx - label.get_width() // 2, label_y))
                hint = self._render_tracked("REPOSITION NEEDLE TO RETRY",
                                            layout.font_size_header, p.muted, _TRACKING_ADJACENT)
                surf.blit(hint, (cx - hint.get_width() // 2,
                                 label_y + label.get_height() + int(8 * s)))

        # Hairline ring on the cover edge (all treatments)
        ring = pygame.Surface((ca.w, ca.h), pygame.SRCALPHA)
        pygame.draw.rect(ring, (255, 255, 255, 10), ring.get_rect(), 1)
        surf.blit(ring, (ca.x, ca.y))

        # --- Status strip (no side counter in empty states) ---
        self._draw_header(surf, layout, p, _STATE_LABELS[kind])

        # --- Hero at 48px (empty-state font size exception) + accent rule;
        #     all album metadata suppressed ---
        hero_size = max(18, int(48 * s))
        hero_rect = Rect(layout.track_text.x, layout.track_text.y,
                         layout.track_text.w, layout.track_text.h)
        hero_h = self._draw_wrapped_text(surf, _EMPTY_HEROES[kind], "display",
                                         hero_size, hero_rect, p.text)
        div_y = layout.track_text.y + hero_h + max(2, int(4 * self.height / 600))
        pygame.draw.rect(surf, p.accent,
                         (layout.divider.x, div_y, layout.divider_width,
                          max(2, layout.divider.h)))
        return surf

    def _draw_stripes(self, target, ca, p: DisplayPalette):
        """Idle empty cover: repeating 135° diagonal stripes, 12px bands
        alternating surface/bg (DESIGN.md §5 idle treatment)."""
        import pygame

        s = min(self.width / 1024, self.height / 600)
        band = max(6, int(12 * s))
        tile = pygame.Surface((ca.w, ca.h))
        tile.fill(p.bg)
        # 135° stripes: lines running bottom-left → top-right, advancing
        # along the x axis at 2-band spacing
        for off in range(-ca.h, ca.w + ca.h, band * 2):
            pygame.draw.line(tile, p.surface, (off, ca.h), (off + ca.h, 0), band)
        target.blit(tile, (ca.x, ca.y))

    def _get_arc_segment(self, radius: int, color: tuple):
        """Pre-render the quarter-circle arc segment (dasharray 50/200 ≈ 89°
        of a r=32 circle, round caps, 1.5px stroke), used static for error
        and rotated per frame for boot.  Stamped as small filled circles
        along the path — pygame.draw.arc moirés at thin widths.
        """
        import math
        import pygame

        key = (radius, color)
        if self._arc_segment is not None and self._arc_segment[0] == key:
            return self._arc_segment[1]

        size = radius * 2 + 6
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        c = size // 2
        stroke = max(1, round(radius / 21))   # ≈1.5px at the 32px reference
        for deg in range(0, 90):              # ~quarter circle
            a = math.radians(deg)
            x = c + radius * math.cos(a)
            y = c + radius * math.sin(a)
            pygame.draw.circle(surf, color, (int(x), int(y)), stroke)
        self._arc_segment = (key, surf)
        return surf

    def _draw_boot_arc(self, target, layout: NowPlayingLayout,
                       p: DisplayPalette, elapsed: float):
        """Rotate the accent arc segment over the boot empty cover
        (1.4s linear infinite; static under reduced_motion)."""
        import pygame

        s = min(self.width / 1024, self.height / 600)
        ca = layout.cover_art
        arc_r = int(32 * s)
        cx = ca.x + ca.w // 2
        cy = ca.y + ca.h // 2 - int(24 * s)

        arc = self._get_arc_segment(arc_r, p.accent)
        if not self.reduced_motion:
            angle = -(elapsed % _ARC_SECS) / _ARC_SECS * 360.0
            arc = pygame.transform.rotate(arc, angle)
        target.blit(arc, arc.get_rect(center=(cx, cy)))

    # -----------------------------------------------------------------------
    # Drawing helpers
    # -----------------------------------------------------------------------

    def _render_tracked(self, text: str, size: int, color: tuple, tracking: float):
        """Render a mono label with letter-spacing, returning a Surface.

        pygame/SDL_ttf has no tracking support, so each character is rendered
        individually and blitted with an extra advance of (tracking × size)
        pixels — the same arithmetic as CSS letter-spacing in em.  Surfaces
        are cached (labels are small and mostly static per track).
        """
        import pygame

        key = (text, size, color, tracking)
        cached = self._label_cache.get(key)
        if cached is not None:
            return cached

        font = self._font("mono", size)
        extra = tracking * size
        glyphs = [font.render(ch, True, color) for ch in text]
        # Total width: glyph advances + tracking between characters (CSS adds
        # tracking after every glyph including the last; trim it for cleaner
        # right-alignment).
        width = int(sum(g.get_width() for g in glyphs) + extra * max(0, len(glyphs) - 1))
        surf = pygame.Surface((max(1, width), font.get_height()), pygame.SRCALPHA)
        x = 0.0
        for g in glyphs:
            surf.blit(g, (int(x), 0))
            x += g.get_width() + extra
        self._label_cache.put(key, surf)
        return surf

    def _wrap_lines(self, text: str, font, max_width: int) -> list:
        """Greedy word-wrap; the single source of truth for line breaking.

        Used by both measurement and drawing so they can never disagree
        (previously duplicated in _draw_wrapped_text/_measure_wrapped_text).
        """
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            if font.size(test)[0] <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def _fit_wrapped(
        self, text: str, role: str, base_size: int, max_width: int,
        max_lines: int, min_size: int = 14, step: int = 2,
    ) -> tuple:
        """Find the largest font size ≤ base_size at which *text* wraps into
        ≤ max_lines within max_width.  Returns (size, lines).

        This is the shrink-instead-of-ellipsis behavior (product decision):
        long artist names and album titles reduce in size rather than
        truncate.  If even min_size can't fit the line count, returns the
        min_size wrap (caller clips — practically unreachable for real
        metadata).
        """
        size = base_size
        while size >= min_size:
            lines = self._wrap_lines(text, self._font(role, size), max_width)
            if len(lines) <= max_lines:
                return size, lines
            size -= step
        return min_size, self._wrap_lines(text, self._font(role, min_size), max_width)

    def _ellipsize(self, text: str, font, max_width: int) -> str:
        """Trim *text* with a trailing ellipsis to fit max_width.

        Only used by the PREV/NEXT adjacent panel — everywhere else the
        design translation shrinks instead (see _fit_wrapped).
        """
        if font.size(text)[0] <= max_width:
            return text
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.size(text[:mid].rstrip() + ell)[0] <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ell

    def _draw_gradient_bg(self, target, p: DisplayPalette):
        """Fill *target* with a radial gradient from surface (centre) to bg (edges).

        Pygame has no built-in radial gradient, so we approximate with concentric
        circles drawn from the outer edge inward. The gradient is anchored at
        roughly 25% from the left (over the cover art area), matching the JSX.

        The rendered gradient is cached per (bg, surface, size) and re-blitted
        on subsequent frames (v1.3.3).  Steady-state frames — the overwhelming
        majority, since the palette only changes during the 1s track-change
        lerp — therefore cost one blit instead of 24 full-screen circle fills.
        """
        import pygame

        key = (p.bg, p.surface, self.width, self.height)
        if self._gradient_key != key or self._gradient_surface is None:
            surface = pygame.Surface((self.width, self.height))
            surface.fill(p.bg)

            # Overlay a soft radial highlight using a small number of concentric circles
            cx = int(self.width * 0.25)
            cy = int(self.height * 0.35)
            max_r = int(max(self.width, self.height) * 0.75)
            steps = 24

            for i in range(steps, 0, -1):
                t = i / steps
                # Blend from surface (centre) toward bg (edge)
                color = _lerp_color(p.bg, p.surface, t * 0.55)
                r = int(max_r * t)
                pygame.draw.circle(surface, color, (cx, cy), r)

            self._gradient_key = key
            self._gradient_surface = surface

        target.blit(self._gradient_surface, (0, 0))

    def _cover_shadow(self, w: int, h: int):
        """Pre-render the Cover Lift shadow (DESIGN.md §4) for a w×h cover.

        CSS reference: `0 30px 60px rgba(0,0,0,0.55)` — a 30px downward
        offset, 60px blur, 55% black.  Pillow renders a filled rect with a
        gaussian blur once per cover size (a single size in practice);
        the result is cached and blitted beneath the cover every compose.
        The offset is applied at blit time, not baked into the surface.
        """
        import pygame
        from PIL import Image, ImageDraw, ImageFilter

        key = (w, h)
        if self._shadow_key == key and self._shadow_surface is not None:
            return self._shadow_surface

        s = min(self.width / 1024, self.height / 600)
        blur = max(8, int(30 * s))      # CSS 60px blur ≈ gaussian radius 30
        pad = blur * 2                  # room for the blur to breathe
        img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle((pad, pad, pad + w, pad + h), fill=(0, 0, 0, 140))  # 0.55 alpha
        img = img.filter(ImageFilter.GaussianBlur(blur))
        surf = pygame.image.frombuffer(img.tobytes(), img.size, "RGBA").convert_alpha()

        self._shadow_key = key
        self._shadow_surface = surf
        return surf

    def _draw_wrapped_text(
        self, target, text: str, role: str, size: int, rect, color: tuple,
        line_height: float = 0.98,
    ) -> int:
        """Render text with word-wrapping to fit within rect.w, clipped to rect.h.

        line_height is the CSS-style multiplier from DESIGN.md §3 (hero 0.98,
        artist 1.04, album 1.12).  Returns the actual rendered height in
        pixels (distance from rect.y to the bottom of the last drawn line);
        0 if nothing was drawn.
        """
        font = self._font(role, size)
        if not text:
            return 0

        y = rect.y
        line_h = int(font.get_height() * line_height)
        last_bottom = rect.y
        for line in self._wrap_lines(text, font, rect.w):
            if y + line_h > rect.y + rect.h:
                break
            surf = font.render(line, True, color)
            target.blit(surf, (rect.x, y))
            last_bottom = y + line_h
            y += line_h + 2

        return max(0, last_bottom - rect.y)

    def _measure_wrapped_text(
        self, text: str, role: str, size: int, available_width: int,
        line_height: float = 0.98,
    ) -> int:
        """Measure wrapped-text height without drawing anything.

        Uses _wrap_lines — the same algorithm as _draw_wrapped_text — so
        measurements exactly match render output.  Returns total pixel
        height; 0 if text is empty.
        """
        font = self._font(role, size)
        if not text:
            return 0
        lines = self._wrap_lines(text, font, available_width)
        if not lines:
            return 0
        line_h = int(font.get_height() * line_height)
        # n lines: n × (line_h + 2) minus the trailing gap after the last line
        return len(lines) * (line_h + 2) - 2

    # -----------------------------------------------------------------------
    # Palette management
    # -----------------------------------------------------------------------

    def _queue_palette(self, cover_url: Optional[str]):
        """Set the target palette for a new track, triggering a transition.

        If a previous transition is still in flight, snap _current_palette to
        the currently-interpolated value before reassigning the target — that
        way the new lerp starts from what the user is *currently seeing*
        instead of jumping back to a stale starting point.
        """
        if not self.dynamic_theming:
            return
        if cover_url is None:
            target = FALLBACK_PALETTE
        elif (cached := self._palette_cache.get(cover_url)) is not None:
            target = cached  # get() already refreshed its eviction position
        else:
            # Extraction happens synchronously here; cover art is already cached
            # on disk from _prefetch_cover(), so no network I/O.
            cache_key = self._url_to_cache_key(cover_url)
            cache_path = self.cache_dir / cache_key
            if cache_path.exists():
                target = _extract_palette(cache_path)
                self._palette_cache.put(cover_url, target)  # put() handles eviction
            else:
                target = FALLBACK_PALETTE

        # Skip the retarget entirely when nothing changed (v1.3.5): every
        # track commit notifies the renderer, and tracks from the same album
        # share a cover URL — without this guard each commit restarted the 1s
        # transition (30 fps cadence + per-frame gradient regeneration)
        # lerping a palette to itself.
        if target == self._target_palette:
            return

        # Snap current to the live interpolated value before retargeting, so a
        # mid-transition track change doesn't lerp from a stale base palette.
        self._current_palette = self._animated_palette()
        self._target_palette = target
        self._transition_start = time.monotonic()

    def _animated_palette(self) -> DisplayPalette:
        """Return the current interpolated palette for this render frame."""
        elapsed = time.monotonic() - self._transition_start
        t = min(1.0, elapsed / _TRANSITION_SECS)
        if t >= 1.0:
            self._current_palette = self._target_palette
            return self._target_palette
        return _lerp_palette(self._current_palette, self._target_palette, t)

    # -----------------------------------------------------------------------
    # Cover art — async fetch + sync load from cache
    # -----------------------------------------------------------------------

    def _url_to_cache_key(self, url: str) -> str:
        import hashlib
        return hashlib.md5(url.encode()).hexdigest() + ".jpg"

    async def _prefetch_cover(self, url: str):
        """Download cover art to the local cache without blocking the render loop.

        Scheduled via asyncio.create_task() from _on_state_change() so the
        download runs in a thread-pool executor and never stalls the event loop.
        Once the file is written, palette extraction is (re-)queued and the
        display is marked dirty so the next frame picks up the fresh image.

        Implementation details:
          - Uses requests.get with an explicit timeout so a hung CDN connection
            can't tie up an executor thread indefinitely.
          - Writes to a tempfile in the cache directory first, then atomically
            renames into place — partial downloads (network drop, process kill)
            never leave a half-written file that _load_cover would fail on.
        """
        cache_key = self._url_to_cache_key(url)
        cache_path = self.cache_dir / cache_key
        if cache_path.exists():
            return  # Already cached — nothing to do

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._download_cover_blocking, url, cache_path)
            log.debug(f"Cover art cached: {cache_path.name}")
            # File is now on disk — extract palette and trigger a redraw
            if self.dynamic_theming:
                self._queue_palette(url)
            self._dirty = True
        except Exception as e:
            log.warning(f"Failed to download cover art from {url}: {e}")

    def _download_cover_blocking(self, url: str, cache_path: Path):
        """Synchronous cover-art download — must run in an executor.

        Hardened against SSRF, oversized responses, and malicious images
        (findings S-1 / S-2):

          - Every URL hop is validated (https + allow-listed host + public IP)
            via _validate_cover_url before any request is made.
          - Redirects are followed manually (allow_redirects=False) so each hop
            is re-validated; the chain is capped at _MAX_COVER_REDIRECTS.
          - The final response must carry an image/* Content-Type.
          - The body is streamed with a running byte counter that aborts past
            _MAX_COVER_BYTES.
          - The written file is image-verified before it is atomically renamed
            into the cache, so neither pygame nor Pillow ever decodes an
            unvalidated file.

        The 15s timeout covers both connection setup and per-chunk reads.
        """
        # Walk the redirect chain ourselves, validating every hop.
        current_url = url
        resp = None
        try:
            for _ in range(_MAX_COVER_REDIRECTS + 1):
                current_url = _validate_cover_url(current_url)
                resp = requests.get(
                    current_url,
                    timeout=_COVER_CONNECT_READ_TIMEOUT,
                    stream=True,
                    allow_redirects=False,
                )
                if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    resp.close()
                    if not location:
                        raise ValueError("redirect with no Location header")
                    # Resolve relative redirects against the current URL.
                    current_url = requests.compat.urljoin(current_url, location)
                    resp = None
                    continue
                break
            else:
                raise ValueError("too many redirects fetching cover art")

            if resp is None:
                raise ValueError("no response fetching cover art")

            resp.raise_for_status()

            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                raise ValueError(f"unexpected Content-Type for cover art: {content_type!r}")

            # delete=False so we can rename after closing; we clean up manually on error
            tmp = tempfile.NamedTemporaryFile(
                dir=str(self.cache_dir),
                prefix=".cover-",
                suffix=".part",
                delete=False,
            )
            try:
                total = 0
                with tmp as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_COVER_BYTES:
                            raise ValueError(
                                f"cover art exceeds {_MAX_COVER_BYTES} byte cap"
                            )
                        f.write(chunk)
                # Validate the decoded image before exposing it to the cache (S-2).
                _validate_image_file(tmp.name)
                os.replace(tmp.name, str(cache_path))  # atomic on POSIX
            except Exception:
                # Clean up partial / rejected file before re-raising
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
        finally:
            if resp is not None:
                resp.close()

    def _load_cover(self, url: Optional[str], w: int, h: int):
        """Load and scale cover art from the local file cache.

        Returns None if the URL is absent or the file hasn't been downloaded
        yet — _prefetch_cover() handles the async download and will set
        self._dirty = True once the file arrives.

        The scaled Surface is cached keyed by (url, w, h) (v1.3.3): the render
        loop calls this every frame, and re-decoding + smoothscaling a JPEG at
        10 fps was the largest constant CPU cost on the Pi.  Disk load and
        scaling now happen exactly once per cover per resolution.
        """
        import pygame

        if not url:
            return None

        cached = self._cover_cache.get((url, w, h))
        if cached is not None:
            return cached

        cache_key = self._url_to_cache_key(url)
        cache_path = self.cache_dir / cache_key
        if not cache_path.exists():
            return None

        try:
            img = pygame.image.load(str(cache_path)).convert()
            scaled = pygame.transform.smoothscale(img, (w, h))
            self._cover_cache.put((url, w, h), scaled)
            return scaled
        except Exception as e:
            log.warning(f"Failed to load cached cover art: {e}")
            cache_path.unlink(missing_ok=True)
            return None

    def stop(self):
        self._running = False
        import pygame
        pygame.quit()
        log.info("Display stopped.")
