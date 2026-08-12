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
  - Shrink-to-fit typography for the hero (step-down), artist (single line),
    and album (two wrapped lines) — they shrink rather than clip.  Ellipsis is
    used for fixed-size DATA labels that can't shrink: the PREV/NEXT adjacent
    track names and (R7-09) the catalog footer, which is trimmed to its column
    with a trailing … rather than hard-clipped mid-glyph.
  - Cover Lift shadow (Pillow gaussian blur, cached) + hairline ring.
  - Status strip with solid `surface` background; status dot with spec
    pulse (opacity/scale, 1.6s ease-in-out) and accent glow.
  - Genre chips: transparent background, accent @ ~33% alpha border,
    capped at 3 with a "+N" overflow chip.
  - Muted palette role is contrast-clamped to ≥4.5:1 against solid `surface`
    (the status-strip fill it lands on, brighter than the gradient peak — #206)
    at extraction time (DESIGN.md Full-Opacity Rule).
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
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple, TYPE_CHECKING
# NOTE: pathlib.Path is no longer imported here — the only user was the font
# constants, which moved to typography.py with TextRenderer (ARCH-3).

from src.state.player_state import PlayerState, PlayerStatus
from src.display.layouts import get_now_playing_layout, NowPlayingLayout, Rect
from src.display.palette import DisplayPalette
from src.display.palette import (
    extract_palette,
    GRADIENT_TEXT_PEAK,
    PermanentCoverError,
)
# ensure_contrast / ensure_contrast_hue_preserving / text_background were used
# only by _quantize_palette, which moved to palette_transition.py (ARCH-3); they
# are imported there now, not here.
from src.display.cover_cache import CoverArtCache
from src.display.typography import TextRenderer
# ARCH-3: the palette cross-fade state machine lives in its own module now.  Re-
# exported here so `from src.display.renderer import _lerp_palette` / `_quantize_
# palette` / `_PALETTE_LERP_QUANTIZE` / `_TRANSITION_SECS` keep resolving for the
# render loop (which reads _TRANSITION_SECS) and the existing tests.
from src.display.palette_transition import (  # noqa: F401
    PaletteTransition,
    _lerp_color,
    _lerp_palette,
    _quantize_palette,
    _PALETTE_LERP_QUANTIZE,
    _TRANSITION_SECS,
)

if TYPE_CHECKING:
    import pygame
    from src.config import DisplayConfig

log = logging.getLogger(__name__)

# Cover-art fetching, the SSRF-hardened download (S-1/S-2/S-7), and the bounded,
# self-cleaning on-disk cache (R-1/R-2) live in src/display/cover_cache.py
# (A-15).  The renderer holds a CoverArtCache and only asks it for paths /
# triggers downloads; it keeps the scaled-Surface cache and palette transition,
# which are render-loop concerns.

# Suppress pygame audio (we're output-only) and point to the right display
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("DISPLAY", ":0")  # Needed when running headless / via SSH

# _TRANSITION_SECS (palette cross-fade duration) moved to palette_transition.py
# with the state machine that owns it; it is re-imported at the top of this
# module (the render loop still reads it).

# Cap on the per-URL palette cache.  Extraction is fast (~ms per album), so
# re-running on a cache miss is fine; the cap just prevents unbounded growth
# on machines with very long uptimes or very large collections.
_PALETTE_CACHE_MAX = 200

# Cap on the scaled-cover-Surface cache.  Each 440×440 RGB surface is ~775 KB,
# so 16 entries ≈ 12 MB — generous for a device that shows one cover at a time,
# and tiny next to re-decoding a JPEG ten times a second.
_COVER_CACHE_MAX = 16

# STAB-1: how many times a cached cover that won't DECODE may be unlinked +
# re-downloaded before the URL is given up on (negative-cached).  A genuinely
# corrupt/unsupported file re-lands the same bad bytes on refetch, so one retry
# is enough to distinguish a transient partial-write (recovers) from a permanent
# bad object (blacklisted) — without a per-frame download/unlink/log loop.
_COVER_MAX_LOAD_FAILURES = 1

# R6-18 / R6-23: DOWNLOAD failures are tracked and recovered SEPARATELY from
# decode failures. A decode failure means the same bytes re-land, so a bounded
# unlink+refetch then blacklist (_COVER_MAX_LOAD_FAILURES) is right. A download
# failure is usually a transient network blip on a URL that succeeds a minute
# later — and every track on an album shares ONE cover_art_url, so the old
# "blacklist until the track changes" never lifted within an album and blanked
# the cover for the rest of it. Instead, back off and retry: skip re-attempting
# for _COVER_DOWNLOAD_RETRY_BACKOFF_SECONDS (the render loop's per-frame
# _maybe_retry_cover_download re-attempts once the window elapses), and only after
# _COVER_MAX_DOWNLOAD_FAILURES give up (blacklist) so a genuinely dead URL stops.
_COVER_DOWNLOAD_RETRY_BACKOFF_SECONDS = 30.0
_COVER_MAX_DOWNLOAD_FAILURES = 5

# R8-06 (#353): while a convert() fault is latched (_cover_decode_deferred),
# re-attempt the decode at most once per this many seconds instead of every
# frame — a video-loss episode used to cost a full JPEG decode + SD read at
# ~10 Hz for its whole duration.
_COVER_DECODE_RETRY_SECONDS = 5.0

# R8-07 (#354): a pygame.error escaping the per-frame render/flip no longer
# kills the pipeline (one flaky HDMI cable used to mean a process restart
# loop).  During a fault episode the loop slows to one attempt per second and
# re-tries pygame.display.set_mode() every this many seconds; non-pygame
# exceptions remain fatal (FIRST_COMPLETED tears the pipeline down as before).
_RENDER_FAULT_REINIT_SECONDS = 5.0

# Cap on the tracked-label Surface cache (letter-spaced mono labels).
# Labels are tiny surfaces and mostly static per track; 128 is plenty.
_LABEL_CACHE_MAX = 128

# Cap on the font cache.  The shrink-to-fit logic probes many sizes per role,
# so over long uptime this accumulates dozens of Font objects; bound it like
# every other cache rather than leaving it the one unbounded dict (P-8).  Fonts
# are a small, fixed working set, so eviction is rare.
_FONT_CACHE_MAX = 64

# Cap on the pre-rendered status-dot Surface cache: one Surface per
# (colour, glow, pulse-phase bucket).  Steady state needs _DOT_PULSE_BUCKETS
# per colour; the rest is churn during the 1s palette lerp (P-3).
_DOT_CACHE_MAX = 64
# Pulse phase buckets — the dot pulse (1.6s) is sampled at ~10 fps (≈16 frames
# per pulse), so 16 pre-rendered phases match the cadence with no visible step.
_DOT_PULSE_BUCKETS = 16

# Status dot pulse period (seconds) — DESIGN.md: 1.6s ease-in-out infinite.
_PULSE_SECS = 1.6

# Boot arc rotation period (seconds) — DESIGN.md: 1.4s linear infinite.
_ARC_SECS = 1.4

# Pre-rendered boot-arc rotation buckets (P-10).  The arc spins once per
# _ARC_SECS, sampled at the boot cadence (~10 fps ≈ 14 frames/turn), so 24
# evenly-spaced angles are smoother than the eye can resolve while letting the
# rotated Surface be cached + reused instead of re-rotated every frame.
_ARC_ROT_BUCKETS = 24
# Cap on the rotated-arc cache: _ARC_ROT_BUCKETS per accent colour; the rest is
# churn during the 1s palette lerp.  Bounded like every other cache (P-8 ethos).
_ARC_ROT_CACHE_MAX = 64

# Muted red for the error state (DESIGN.md §5: #c85050).
_ERROR_RED = (200, 80, 80)

# Status-strip label for the now-playing screen (DESIGN.md stateLabel mapping;
# paused and between-tracks arrive with their states in a later release).
_NOW_PLAYING_LABEL = "NOW PLAYING"


class EmptyState(Enum):
    """The three non-playing screens (DESIGN.md §5).

    Replaces the former stringly-typed ``kind`` argument: an enum makes the
    valid set closed and explicit, so a typo is a NameError at author time
    rather than a silent KeyError (or wrong branch) at render time.
    """
    BOOT = "boot"      # listening / identifying
    IDLE = "idle"      # no record on the platter
    ERROR = "error"    # recognition gave up


@dataclass(frozen=True)
class _EmptyStateSpec:
    """Table-driven presentation for one :class:`EmptyState`.

    Everything that differs *as data* between the empty screens lives here, so
    the render code reads one descriptor instead of consulting three parallel
    dicts plus a chain of ``kind == "…"`` comparisons:

      * ``status_label`` — status-strip text (DESIGN.md stateLabel)
      * ``hero``         — 48px hero placeholder (empty-state font exception)
      * ``dot_color``    — resolves the status-dot colour from the live
                           (lerped) palette; only ERROR is palette-independent
      * ``dot_animate`` / ``dot_glow`` — dot behaviour (boot pulses+glows;
                           idle/error sit still — "boot spins; error sits")
      * ``animates``     — whether the frame must keep re-rendering (boot only)
    """
    status_label: str
    hero: str
    dot_color: Callable[[DisplayPalette], Tuple[int, int, int]]
    dot_animate: bool
    dot_glow: bool
    animates: bool


# The single source of truth for empty-state presentation.  Adding a state =
# adding one row here; the render path needs no new branches except for the
# genuinely-different cover treatments (stripes vs. spinner vs. static arc).
_EMPTY_STATES = {
    EmptyState.BOOT: _EmptyStateSpec(
        status_label="IDENTIFYING…",
        hero="Listening…",
        dot_color=lambda p: p.accent,
        dot_animate=True,
        dot_glow=True,
        animates=True,
    ),
    EmptyState.IDLE: _EmptyStateSpec(
        status_label="IDLE",
        hero="Waiting for a record",
        dot_color=lambda p: p.muted,
        dot_animate=False,
        dot_glow=False,
        animates=False,
    ),
    EmptyState.ERROR: _EmptyStateSpec(
        status_label="NO MATCH FOUND",
        hero="Couldn't identify",
        dot_color=lambda p: _ERROR_RED,
        dot_animate=False,
        dot_glow=False,
        animates=False,
    ),
}

# Bundled fonts (DESIGN.md §3).  Role → filename in assets/fonts/.
# "display" = hero track (Inter Tight 600), "text" = artist + adjacent names
# (Inter Tight 500), "title" = album (Newsreader italic 400), "mono" = all
# labels/metadata (JetBrains Mono 400).
# Font files + SysFont fallbacks moved to src/display/typography.py with the
# TextRenderer that uses them (ARCH-3).

# Letter-spacing (em) per label context now lives on NowPlayingLayout
# (layouts.py) so a restyle is "edit layouts.py" — see tracking_* fields (A-14).


# _BoundedCache moved to src/util/cache.py (arch-4 / #220) so the resolver's
# album cache shares the ONE implementation instead of a hand-rolled copy. Kept
# as a module-level alias under the old private name so the six renderer caches
# below and every `from src.display.renderer import _BoundedCache` importer
# (tests/test_renderer_caches.py et al.) keep resolving unchanged.
from src.util.cache import BoundedCache as _BoundedCache  # noqa: E402


# _lerp_color / _lerp_palette / _quantize_palette / _PALETTE_LERP_QUANTIZE moved
# to palette_transition.py (ARCH-3) with the PaletteTransition state machine that
# uses them; they are re-imported at the top of this module so existing importers
# (`from src.display.renderer import _lerp_palette`) keep resolving.


class DisplayRenderer:
    """Renders now-playing info to an HDMI screen via pygame."""

    def __init__(self, config: "DisplayConfig", state: PlayerState, cover_store=None):
        self.state = state
        self.width: int = config.width
        self.height: int = config.height
        self.fullscreen: bool = config.fullscreen
        self.dynamic_theming: bool = config.dynamic_theming
        # Translation of the design's prefers-reduced-motion requirement:
        # pygame has no OS media query, so it's a config flag.  When set,
        # the status dot renders static (no pulse, no glow animation).
        self.reduced_motion: bool = config.reduced_motion
        # The on-disk cover cache + SSRF-hardened fetch live in CoverArtCache
        # (A-15); it mkdir's the dir, sweeps stale .part files (R-1), bounds the
        # cache (R-2), and is the only thing that knows cover paths now.
        # ARCH-8: optional injection seam — defaults to the real CoverArtCache,
        # but a test (or the composition root) can pass a substitute instead of
        # monkeypatching the private attribute after construction.
        self._cover_store = (
            cover_store if cover_store is not None
            else CoverArtCache(config.cover_art_cache_dir)
        )

        self._screen: Optional["pygame.Surface"] = None
        self._font_cache = _BoundedCache(_FONT_CACHE_MAX)  # (role, size) → Font (P-8)
        self._running = True
        self._dirty = True              # Force initial render

        # Layout is a pure function of (width, height) — compute once
        # instead of once per frame (v1.4.0).
        self._layout: NowPlayingLayout = get_now_playing_layout(self.width, self.height)

        # Palette transition state — the cross-fade state machine (ARCH-3).  The
        # current/target/start fields it owns are still reachable as
        # self._current_palette / _target_palette / _transition_start via
        # delegating properties, so the render loop and the __new__-skeleton tests
        # are unchanged.  The palette cache stays renderer-owned (below) and is
        # passed into _queue_palette per call.
        self._palette_impl = PaletteTransition()
        self._palette_cache = _BoundedCache(_PALETTE_CACHE_MAX)  # cover_art_url → DisplayPalette

        # Render hot-path caches (v1.3.3)
        self._cover_cache = _BoundedCache(_COVER_CACHE_MAX)  # (url, w, h) → scaled Surface
        # Monotonic token bumped whenever a cover lands on disk (B-22).  The
        # static-frame key includes it so a freshly-downloaded cover forces a
        # recompose — a stable replacement for the old id(cover), whose ids could
        # be recycled after GC and falsely match a stale frame.
        self._cover_version: int = 0
        # STAB-1: bound the corrupt-cover recovery loop.  A cached cover that
        # will not decode used to be unlinked + re-downloaded every render frame
        # (~8.7 Hz), re-landing the same bad bytes forever (~31k GETs/hour,
        # ~9 GB/hour of SD writes, ~31k WARNING lines/hour).  Track per-URL
        # decode failures and, after _COVER_MAX_LOAD_FAILURES fruitless
        # refetches, mark the URL bad and stop touching disk/network/log for it.
        self._cover_decode_failures: dict = {}   # url → consecutive DECODE failures (R6-23)
        # R6-18: download failures are tracked separately, with time-based backoff
        # (see the constants above), so a transient network blip neither hammers
        # the CDN nor permanently blanks the cover for the rest of an album.
        self._cover_download_failures: dict = {}      # url → consecutive download failures
        self._cover_download_retry_after: dict = {}   # url → monotonic deadline before re-attempt
        self._cover_bad_urls: set = set()        # urls given up on (negative cache)
        # Dedupe concurrent downloads for one URL: a state-change prefetch and a
        # load-failure refetch must not both hit the network for the same cover.
        self._cover_prefetch_inflight: set = set()
        # STAB-5: dedupe concurrent off-loop cover DECODES for one (url, w, h),
        # so the render path (which spawns a decode on each cache miss) can't
        # stack up duplicate load+scale tasks for the same cover.
        self._cover_decode_inflight: set = set()
        # R5-21: urls whose cover file is CONFIRMED on disk this session.
        # _load_cover only spawns an off-loop decode for a url in this set, so a
        # cover whose download is still in flight OR failed never triggers a fresh
        # decode task every render frame (a not-on-disk cover used to spawn ~10
        # tasks/s for the whole track — each doing a blocking exists() stat on the
        # loop and returning before the inflight guard could engage). _prefetch_cover
        # adds the url once the download lands (and bumps _cover_version → repaint,
        # so the gated decode then spawns); _on_state_change drops it for a NEW
        # cover so a stale entry can't mislead the next track.
        self._cover_on_disk: set = set()
        # True while the display surface is unavailable (video-mode loss).
        # R8-06 (#353): the latch now gates the WORK, not just the log —
        # pre-R8-06 each failed convert() cleared the inflight guard and left
        # the URL in _cover_on_disk, so _load_cover respawned a full JPEG
        # decode + SD read EVERY frame (~10 Hz) for the whole fault episode.
        # While latched, _load_cover skips the decode spawn until
        # _cover_decode_retry_at elapses (~one attempt per
        # _COVER_DECODE_RETRY_SECONDS); cleared on a clean decode and on a
        # state change to a new cover.
        self._cover_decode_deferred: bool = False
        self._cover_decode_retry_at: float = 0.0
        self._gradient_key: Optional[tuple] = None           # (bg, surface, w, h)
        self._gradient_surface: Optional["pygame.Surface"] = None

        # Render hot-path caches (v1.4.0)
        self._label_cache = _BoundedCache(_LABEL_CACHE_MAX)  # tracked-label Surfaces
        self._dot_cache = _BoundedCache(_DOT_CACHE_MAX)      # pre-rendered dot phases (P-3)
        self._shadow_key: Optional[tuple] = None             # (w, h)
        self._shadow_surface: Optional["pygame.Surface"] = None   # Cover Lift shadow
        self._static_key: Optional[tuple] = None             # (track content, palette)
        self._static_surface: Optional["pygame.Surface"] = None   # composed frame (any screen)

        # Empty-state machinery (v1.4.1)
        self._listening_since: Optional[float] = None        # boot-label elapsed clock
        self._arc_segment: Optional[tuple] = None            # (key, surf) pre-rendered boot/error arc
        self._arc_rot_cache = _BoundedCache(_ARC_ROT_CACHE_MAX)  # rotated boot arcs (P-10)

        # Strong references to fire-and-forget tasks (cover prefetches).
        # asyncio only keeps weak references to tasks, so without this a
        # running download could in principle be garbage-collected mid-flight.
        self._bg_tasks: set = set()

        # The cover URL the display currently WANTS to show (set on every state
        # change).  An off-loop palette extraction (P-9) re-queues its result
        # only if this still matches — otherwise a slow decode for a previous
        # track could retarget the palette over the track now on screen.
        self._wanted_cover_url: Optional[str] = None

        self.state.on_change(self._on_state_change)

    def _spawn(self, coro):
        """create_task() with a strong reference held until the task completes.

        DISP-8: guard the running-loop requirement HERE — the single place a
        display background task is scheduled — so every caller is protected once.
        `_on_state_change` reaches this from a SYNCHRONOUS PlayerState callback
        that may run without a running loop (an off-loop unit test, or a state
        change delivered before the loop starts). Without the guard,
        ``create_task`` raises ``RuntimeError('no running event loop')`` out of the
        display callback and back into the notifying recognition pipeline, which
        does not expect the display layer to raise. Degrade to a no-op instead,
        closing the un-started coroutine so it doesn't warn about never being
        awaited.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            log.debug("No running event loop — skipping display background task (DISP-8).")
            return None
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_task_done)
        return task

    def _on_bg_task_done(self, task: "asyncio.Task") -> None:
        """Done-callback for _spawn'd display tasks (#207 / arch-6).

        Discards the strong ref AND retrieves the task's exception, matching the
        tracker's CONC-3 registry. Without the retrieval, a raise escaping a
        display background task — a path not covered by the coroutine's own
        try/excepts (e.g. a future edit to _decode_cover_async, or an OSError from
        the cover store on a worn SD card) — surfaced only as asyncio's detached
        "Task exception was never retrieved" at GC time, with none of the context
        that turns a Pi debugging session into one journal line. A cancelled task
        is normal shutdown, not a fault.
        """
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Display background task failed: %r", exc)

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

    @property
    def _text(self) -> TextRenderer:
        """The typography engine (ARCH-3), composed lazily over THIS renderer's
        own font/label caches.

        Lazy and built from ``self._font_cache`` / ``self._label_cache`` so the
        many ``__new__``-skeleton tests that set those two caches and then call
        the ``_font`` / ``_render_tracked`` / ``_wrap_lines`` … shims below keep
        working without constructing a TextRenderer themselves.  In normal use
        ``__init__`` has already set both caches, so the first typography call
        binds the engine to the real caches (and the same objects the renderer
        holds, so cache bounds/eviction are unchanged).
        """
        impl = self.__dict__.get("_text_impl")
        # Rebuild if never built OR if either cache object was swapped since
        # (a __new__-skeleton test may assign a fresh _font_cache/_label_cache
        # after the engine was first bound) — so the engine always uses THIS
        # renderer's current caches, never a stale memoized pair.
        if (
            impl is None
            or impl._font_cache is not self._font_cache
            or impl._label_cache is not self._label_cache
        ):
            impl = TextRenderer(self._font_cache, self._label_cache)
            self.__dict__["_text_impl"] = impl
        return impl

    # ---- Typography shims: the logic lives in TextRenderer (ARCH-3); these
    # keep the renderer's existing (role, size) → Surface method surface. ----
    def _font(self, role: str, size: int):
        return self._text.font(role, size)

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
            if url and url != self._wanted_cover_url:
                # STAB-1: a genuinely NEW cover is the "state change" that lifts a
                # prior blacklist — give it a fresh bounded decode attempt.  (A
                # repeat state-change for the SAME cover does not, so a permanently
                # bad cover can't be re-triggered into the loop by state churn.)
                self._cover_bad_urls.discard(url)
                self._cover_decode_failures.pop(url, None)
                # R6-18: also lift any download-failure backoff/blacklist for the
                # genuinely new cover, so it gets a clean first attempt.
                self._cover_download_failures.pop(url, None)
                self._cover_download_retry_after.pop(url, None)
                self._cover_on_disk.discard(url)   # R5-21: re-confirm on this track's prefetch
                # R5-21: bound _cover_on_disk — drop the OUTGOING cover's readiness
                # marker. Its scaled surface is already in the (bounded) _cover_cache,
                # so the gate is only consulted on a cache miss; keeping the marker
                # would grow the set by one per distinct cover over 24/7 uptime.
                self._cover_on_disk.discard(self._wanted_cover_url)
                # #306 (R7 scope-extension): also sweep the OUTGOING cover's failure
                # bookkeeping. Its download/decode tally + retry deadline otherwise
                # linger forever for a URL that failed 1–4× then was never revisited,
                # growing unbounded over 24/7 uptime keyed by distinct cover URL.
                # `_cover_bad_urls` is deliberately NOT swept — a permanently-bad
                # cover stays blacklisted so a return to it isn't re-attempted (the
                # accepted STAB-1 residual).
                outgoing = self._wanted_cover_url
                if outgoing is not None and outgoing != url:
                    self._cover_download_failures.pop(outgoing, None)
                    self._cover_download_retry_after.pop(outgoing, None)
                    self._cover_decode_failures.pop(outgoing, None)
                # R8-06 (#353): a NEW cover gets an immediate decode attempt —
                # clear the episode latch AND the deadline (the latch re-sets on
                # the next failure if the display is still gone).  2nd-pass fix:
                # the latch clear was missing, leaving the F1 probe armed with
                # no decode path to resolve it while the new download pended.
                self._cover_decode_deferred = False
                self._cover_decode_retry_at = 0.0
            self._wanted_cover_url = url
            self._queue_palette(url)
            if url:
                self._spawn(self._prefetch_cover(url))
        elif state.status in (PlayerStatus.IDLE, PlayerStatus.ERROR, PlayerStatus.LISTENING):
            # Empty states always use the fallback palette (DESIGN.md §2);
            # lerp back smoothly rather than jump-cutting.
            self._cover_on_disk.discard(self._wanted_cover_url)   # R5-21: bound the set
            self._wanted_cover_url = None
            self._queue_palette(None)

    # -----------------------------------------------------------------------
    # Async render loop
    # -----------------------------------------------------------------------

    async def run(self):
        """Async display loop — re-renders when dirty or transitioning.

        R8-07 (#354): a ``pygame.error`` escaping the per-frame render/flip —
        an SDL surface lost to a video fault (HDMI hotplug/blanking, the same
        episode class the cover convert() branch already treats as transient) —
        no longer faults the display leg and thereby the whole pipeline
        (FIRST_COMPLETED → cancel all → systemd restart loop on one flaky
        cable).  During a fault episode the loop logs once, slows to ~1 attempt
        per second, and re-tries ``pygame.display.set_mode`` every
        ``_RENDER_FAULT_REINIT_SECONDS``; recovery is logged with the episode
        duration.  Non-pygame exceptions stay FATAL — fail-fast on genuine
        bugs is unchanged.
        """
        import pygame
        prev_transitioning = False
        fault_since: Optional[float] = None
        fault_last_reinit = 0.0
        while self._running:
            # R8-07 (cold-review F3): the event pump can ALSO raise pygame.error
            # once the video subsystem is gone ("video system not initialized") —
            # keep it inside the same survival policy as the render below.  A
            # pump fault starts/continues the episode; events are retried next
            # iteration.
            try:
                events = pygame.event.get()
            except pygame.error as e:
                events = []
                if fault_since is None:
                    fault_since = time.monotonic()
                    fault_last_reinit = fault_since
                    log.warning(
                        "Event pump failed (%s) — video fault? Keeping the "
                        "pipeline alive (R8-07).", e,
                    )
                # 2nd-pass fix: route the episode into the render try below
                # (where retry, set_mode reinit and recovery-logging live) —
                # without this a pump-only fault on a static frame never
                # reached the reinit and never ended the episode.
                self._dirty = True
            for event in events:
                if event.type == pygame.QUIT:
                    self.stop()
                    return
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self.stop()
                    return

            # R6-18: re-attempt a backed-off cover download once its window elapses.
            # Cheap O(1) check per loop iteration (~10 fps even when idle), so a
            # transient download failure self-heals within ~the backoff — no timer.
            self._maybe_retry_cover_download()

            # R8-06 (cold-review F1): the decode probe must be CLOCK-driven, not
            # render-driven.  Under reduced_motion the now-playing frame doesn't
            # self-dirty, so once a convert-fault episode's last frame passed,
            # nothing would ever call _load_cover again and the cover stayed a
            # placeholder for the rest of the track (the pre-R8-06 storm was,
            # accidentally, also the recovery mechanism).  O(1) per iteration.
            # 2nd-pass fix, take two: the probe frame is BOUNDED — one per
            # window — by re-arming the deadline AFTER the probe frame renders
            # (see below).  Re-arming HERE (take one) deadlocked the probe:
            # _load_cover's gate saw the freshly-future deadline and skipped
            # the very decode this frame existed to spawn.
            probe_frame = (
                self._cover_decode_deferred
                and time.monotonic() >= self._cover_decode_retry_at
            )
            if probe_frame:
                self._dirty = True

            # Keep re-rendering during a palette transition even if not dirty
            transitioning = (time.monotonic() - self._transition_start) < _TRANSITION_SECS
            # #208 (disp-2): when a transition JUST ended (True→False edge), force
            # one more frame so the EXACT target palette is composed. Otherwise the
            # loop's "<1.0s" check and PaletteTransition.animated()'s ">=1.0s" check
            # straddle by ~1ms, so on a static screen (IDLE/ERROR, or now-playing
            # under reduced_motion) the last frame ever drawn holds the QUANTIZED
            # lerp palette forever — e.g. bg (0,0,0) instead of the intended
            # (10,10,10) the design's 8–10 floor guarantees. On the edge iteration
            # elapsed ≥ _TRANSITION_SECS, so animated() returns the exact target
            # and snaps current.
            if prev_transitioning and not transitioning:
                self._dirty = True
            prev_transitioning = transitioning

            if self._dirty or transitioning:
                # Reset BEFORE rendering so _render_now_playing /
                # _render_empty can set self._dirty = True to request
                # another frame for animations (pulsing dot, spinner).
                self._dirty = False
                try:
                    self._render()
                    pygame.display.flip()
                    if fault_since is not None:
                        log.info(
                            "Display recovered after a %.0fs video-fault "
                            "episode (R8-07).", time.monotonic() - fault_since,
                        )
                        fault_since = None
                except pygame.error as e:
                    now = time.monotonic()
                    if fault_since is None:
                        fault_since = now
                        fault_last_reinit = now
                        log.warning(
                            "Per-frame render failed (%s) — video fault? "
                            "Keeping the pipeline alive; retrying ~1/s and "
                            "re-initializing the display every %.0fs (R8-07).",
                            e, _RENDER_FAULT_REINIT_SECONDS,
                        )
                    self._dirty = True   # keep attempting frames
                    if now - fault_last_reinit >= _RENDER_FAULT_REINIT_SECONDS:
                        fault_last_reinit = now
                        try:
                            flags = pygame.FULLSCREEN if self.fullscreen else 0
                            self._screen = pygame.display.set_mode(
                                (self.width, self.height), flags
                            )
                            # Composed surfaces may hold the old pixel format;
                            # force a recompose on the next successful frame.
                            self._static_key = None
                        except pygame.error:
                            pass   # display still gone; next window retries
                if probe_frame:
                    # R8-06 2nd-pass: re-arm AFTER the probe frame ran, whether
                    # or not it managed to spawn a decode — bounds the
                    # no-decode-path case (cover not on disk, blacklisted,
                    # IDLE screen) to ONE frame per window instead of a
                    # permanent ~10fps dirty loop.  A spawned probe's own
                    # failure/success re-arms/clears independently.
                    self._cover_decode_retry_at = (
                        time.monotonic() + _COVER_DECODE_RETRY_SECONDS
                    )

            # Sleep cadence: 30 fps while transitioning (smooth lerp), otherwise
            # ~10 fps — fast enough for the 1.6s pulsing dot, but easy on the Pi.
            # R8-07: ~1 fps during a video-fault episode (each attempt raises;
            # don't burn CPU rendering into a dead surface at full cadence).
            if fault_since is not None:
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(1 / 30 if transitioning else 1 / 10)

    # -----------------------------------------------------------------------
    # Render dispatch
    # -----------------------------------------------------------------------

    def _render(self):
        """Dispatch to the appropriate layout based on current player status."""
        if self.state.status == PlayerStatus.IDLE:
            self._render_empty(EmptyState.IDLE)
        elif self.state.status == PlayerStatus.LISTENING:
            self._render_empty(EmptyState.BOOT)
        elif self.state.status == PlayerStatus.ERROR:
            self._render_empty(EmptyState.ERROR)
        elif self.state.status == PlayerStatus.PLAYING and self.state.current_track:
            self._render_now_playing()
        else:
            self._render_empty(EmptyState.BOOT)

    # -----------------------------------------------------------------------
    # Now-playing screen
    # -----------------------------------------------------------------------

    def _render_now_playing(self):
        """Render the now-playing screen: cached static frame + animated dot.

        Everything except the status dot is composed once per (track content,
        palette) onto an offscreen Surface (_compose_now_playing).  Steady-
        state frames — the overwhelming majority — are one full-screen blit
        plus a few alpha circles for the dot, instead of re-rendering every
        text element at 10 fps (v1.4.0).  During the 1s palette lerp the
        composite key changes only when the QUANTIZED palette does (P-4 /
        `_PALETTE_LERP_QUANTIZE`) — a dozen-plus times over the second rather than
        every frame.  The exact count is data-dependent (bounded by the frame
        count, driven by how far each channel travels — roughly 12–25 for typical
        vs high-contrast transitions), not a fixed function of the step constant;
        either way it is far below the pre-cache "re-render everything each frame"
        cost this cache replaced.
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
            self._cover_version,  # bumps when a cover lands (stable; no id() reuse — B-22)
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
        self._draw_header(surf, layout, p, _NOW_PLAYING_LABEL, self._side_string(track))

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

        # Bottom-anchored: catalog footer (tracked mono) + adjacent panel.
        # R7-09: ellipsize the joined string to the column width rather than
        # hard-clipping it mid-glyph (a third overflow policy the design didn't
        # sanction). The area clip stays as a vertical backstop only — the
        # ellipsize already guarantees the width fits.
        meta_parts = [str(x) for x in [track.year, track.label, track.catalog_number] if x]
        if meta_parts:
            label = self._render_tracked_ellipsized(
                " · ".join(meta_parts), layout.font_size_meta, p.muted,
                layout.tracking_catalog, layout.meta_text.w,
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

        label = self._render_tracked(label_text, layout.font_size_header, p.muted, layout.tracking_label)
        target.blit(label, (pad_x + dot_r * 2 + 8, (strip.h - label.get_height()) // 2))

        if side_str:
            side = self._render_tracked(side_str, layout.font_size_header, p.muted, layout.tracking_label)
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

        strip = layout.header_strip
        r = self._dot_radius()
        # Quantize the pulse to a phase bucket so the dot Surface is rendered
        # once per (colour, glow, bucket) and reused — not freshly allocated +
        # drawn every frame, ~10×/sec forever (P-3).
        if not animate or self.reduced_motion:
            bucket = -1            # static, full opacity
            k = 0.0
        else:
            phase = (time.monotonic() % _PULSE_SECS) / _PULSE_SECS
            bucket = int(phase * _DOT_PULSE_BUCKETS) % _DOT_PULSE_BUCKETS
            k = 0.5 - 0.5 * math.cos(2 * math.pi * (bucket / _DOT_PULSE_BUCKETS))

        cache_key = (color, glow, r, bucket)
        dot = self._dot_cache.get(cache_key)
        if dot is None:
            dot = self._render_dot_surface(color, k, glow, r)
            self._dot_cache.put(cache_key, dot)

        c = dot.get_width() // 2
        cx = self._strip_pad_x() + r
        cy = strip.y + strip.h // 2
        target.blit(dot, (cx - c, cy - c))

    @staticmethod
    def _render_dot_surface(color: tuple, k: float, glow: bool, r: int):
        """Render one status-dot Surface for pulse value k (0→1).  Cached and
        reused by _draw_status_dot (P-3); k=0 is the full-opacity resting dot."""
        import pygame

        opacity = 1.0 - 0.45 * k
        scale   = 1.0 - 0.10 * k
        size = r * 6  # room for the glow halo
        dot = pygame.Surface((size, size), pygame.SRCALPHA)
        c = size // 2
        if glow:
            glow_alpha = int(70 * opacity)
            pygame.draw.circle(dot, (*color, glow_alpha // 2), (c, c), int(r * 2.5))
            pygame.draw.circle(dot, (*color, glow_alpha), (c, c), int(r * 1.6))
        pygame.draw.circle(dot, (*color, int(255 * opacity)), (c, c), max(2, int(r * scale)))
        return dot

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

    def _draw_genre_chips(
        self,
        target,
        genres: list,
        layout: NowPlayingLayout,
        p: DisplayPalette,
        chips_rect,
    ):
        """Render genre chips per DESIGN.md §5: transparent background,
        1px border in accent at ~33% alpha (the JSX `{accent}55`), tracked
        muted mono text, sharp corners, max 3 + '+N' overflow.

        *chips_rect* is the bounding box to lay the chips out in (required —
        the sole caller computes it from the pushed-down layout; ARCH-9 removed
        a dead ``layout.genre_chips`` fallback nothing used).
        """
        import pygame

        rect = chips_rect
        px = layout.chip_padding_x
        py = layout.chip_padding_y
        gap = layout.chip_gap
        x, y = rect.x, rect.y
        border = (*p.accent, layout.chip_border_alpha)

        def draw_chip(text) -> bool:
            """Draw one chip at the running (x, y), wrapping rows as needed.

            R7-08: the target position is computed on LOCALS and the cursor is
            committed (and the chip blitted) ONLY when the chip fits the bounding
            box both horizontally (after any wrap) AND vertically.  Returns False
            WITHOUT mutating the cursor or drawing otherwise.  The old code
            checked the vertical bound only INSIDE the wrap branch and moved
            ``x``/``y`` before its early return, so a chip that fit horizontally on
            an over-low row — or ANY chip when the box was shorter than a chip
            (the extreme push-down case) — blitted BELOW the box, the "+N" grazing
            the catalog footer's row.  A chip that cannot fit is now suppressed
            rather than drawn out of bounds (the fail-safe direction: a missing
            overflow chip beats one bleeding into the footer).
            """
            nonlocal x, y
            label = self._render_tracked(text, layout.font_size_chips, p.muted, layout.tracking_chip)
            chip_w = label.get_width() + px * 2
            chip_h = label.get_height() + py * 2

            nx, ny = x, y
            # Wrap to the next row if the chip would run past the column's right
            # edge — but not when already at the row start (a chip wider than the
            # whole column can't wrap away).
            if nx + chip_w > rect.x + rect.w and nx > rect.x:
                nx = rect.x
                ny = y + chip_h + gap
            # Must fit the box VERTICALLY at its final row — checked on EVERY path,
            # not just after a wrap (the R7-08 fix).
            if ny + chip_h > rect.y + rect.h:
                return False

            x, y = nx, ny
            # Per-chip SRCALPHA surface so the border alpha actually blends
            chip = pygame.Surface((chip_w, chip_h), pygame.SRCALPHA)
            pygame.draw.rect(chip, border, chip.get_rect(), 1)
            chip.blit(label, (px, py))
            # #344: clip the blit to the column's right edge so a chip wider than
            # the whole column (a genre string longer than the metadata column can
            # hold — unreachable with realistic names, but R7-08 bounded only the
            # vertical axis) is clipped instead of bleeding past the right edge.
            avail_w = (rect.x + rect.w) - x
            target.blit(chip, (x, y), area=(0, 0, min(chip_w, avail_w), chip_h))
            x += chip_w + gap
            return True

        # Draw up to 3 genre chips (DESIGN.md §6 cap), stopping at the first
        # that doesn't fit.  The "+N" overflow then reflects what ACTUALLY fit,
        # not a fixed cap of 3 — so it can't read "+2" while 3 are hidden (B-17).
        drawn = 0
        for genre in genres[:3]:
            if not draw_chip(genre):
                break
            drawn += 1

        hidden = len(genres) - drawn
        if hidden > 0:
            draw_chip(f"+{hidden}")

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
            label = self._render_tracked("← PREV", layout.font_size_header, p.muted, layout.tracking_adjacent)
            target.blit(label, (strip.x, y0))
            name = name_font.render(self._ellipsize(prev, name_font, half_w), True, p.text)
            target.blit(name, (strip.x, y0 + label.get_height() + 4))

        if nxt:
            label = self._render_tracked("NEXT →", layout.font_size_header, p.muted, layout.tracking_adjacent)
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

    def _render_empty(self, state: EmptyState):
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
        spec = _EMPTY_STATES[state]
        layout = self._layout
        p = self._animated_palette()

        elapsed = time.monotonic() - self._listening_since if self._listening_since else 0.0
        boot_label = self._boot_label(elapsed) if state is EmptyState.BOOT else None

        key = ("empty", state, boot_label, p.bg, p.surface, p.accent, p.text, p.muted)
        if self._static_key != key or self._static_surface is None:
            self._static_surface = self._compose_empty(state, layout, p, boot_label)
            self._static_key = key

        self._screen.blit(self._static_surface, (0, 0))

        # State-mapped dot (DESIGN.md §5), driven entirely from the descriptor:
        # boot pulses+glows in accent; idle sits static in muted; error sits
        # static in muted red.
        self._draw_status_dot(self._screen, layout, spec.dot_color(p),
                              animate=spec.dot_animate, glow=spec.dot_glow)

        # Boot is the only state with a rotating-arc overlay (boot spins; error
        # sits) — that draw is state-specific.
        if state is EmptyState.BOOT:
            self._draw_boot_arc(self._screen, layout, p, elapsed)

        # Animated states keep the loop awake so the arc + pulsing dot + ticking
        # label advance; static states let it go quiet (driven by the table).
        if spec.animates:
            self._dirty = True

    def _compose_empty(self, state: EmptyState, layout: NowPlayingLayout,
                       p: DisplayPalette, boot_label: Optional[str]):
        """Compose the static portion of an empty-state frame.

        Includes everything except the dot and (in boot) the rotating arc:
        gradient, cover shadow + treatment + ring, strip, hero, labels.
        """
        import pygame

        spec = _EMPTY_STATES[state]
        surf = pygame.Surface((self.width, self.height))
        self._draw_gradient_bg(surf, p)

        s = min(self.width / 1024, self.height / 600)
        ca = layout.cover_art

        # Cover Lift shadow stays — the empty cover is still the physical
        # object slot (the JSX applies the container shadow in all states).
        shadow = self._cover_shadow(ca.w, ca.h)
        pad = (shadow.get_width() - ca.w) // 2
        surf.blit(shadow, (ca.x - pad, ca.y - pad + max(4, int(30 * s))))

        # --- Empty-cover treatment (irreducibly different per state) ---
        if state is EmptyState.IDLE:
            self._draw_stripes(surf, ca, p)
            label = self._render_tracked("NO RECORD ON PLATTER", layout.font_size_header,
                                         p.muted, layout.tracking_label)
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
            if state is EmptyState.BOOT:
                label = self._render_tracked(boot_label or "WARMING UP",
                                             layout.font_size_header, p.muted,
                                             layout.tracking_empty_label)
                surf.blit(label, (cx - label.get_width() // 2, label_y))
            else:  # error — static arc + primary label + recovery hint
                arc = self._get_arc_segment(arc_r, _ERROR_RED)
                surf.blit(arc, arc.get_rect(center=(cx, arc_cy)))
                label = self._render_tracked("NO MATCH FOUND", layout.font_size_header,
                                             p.muted, layout.tracking_empty_label)
                surf.blit(label, (cx - label.get_width() // 2, label_y))
                hint = self._render_tracked("REPOSITION NEEDLE TO RETRY",
                                            layout.font_size_header, p.muted, layout.tracking_adjacent)
                surf.blit(hint, (cx - hint.get_width() // 2,
                                 label_y + label.get_height() + int(8 * s)))

        # Hairline ring on the cover edge (all treatments)
        ring = pygame.Surface((ca.w, ca.h), pygame.SRCALPHA)
        pygame.draw.rect(ring, (255, 255, 255, 10), ring.get_rect(), 1)
        surf.blit(ring, (ca.x, ca.y))

        # --- Status strip (no side counter in empty states) ---
        self._draw_header(surf, layout, p, spec.status_label)

        # --- Hero at 48px (empty-state font size exception) + accent rule;
        #     all album metadata suppressed ---
        hero_size = max(18, int(48 * s))
        hero_rect = Rect(layout.track_text.x, layout.track_text.y,
                         layout.track_text.w, layout.track_text.h)
        hero_h = self._draw_wrapped_text(surf, spec.hero, "display",
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
        of a r=32 circle, round caps, ~1.5px spec → 3px stamped band, R5-28), used static for error
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
        # R5-28: `stroke` is the RADIUS of each stamped circle, so a stamp radius
        # of 2 produced a ~5px band (2*2+1) — over 3× the DESIGN.md:171 1.5px
        # spec.  Halve it: a radius-1 stamp yields a 3px band, the closest a
        # pygame integer-radius circle can approximate 1.5px while keeping the
        # round-cap look (radius 0 would be a 1px aliased dot with no cap).  A
        # true 1.5px would need an antialiased line-strip rewrite (deferred; the
        # stamped-circle path exists specifically to avoid pygame.draw.arc's
        # thin-width moiré).
        stroke = max(1, round(radius / 21 / 2))   # ≈1.5px spec → 3px stamp at r=32
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
        (1.4s linear infinite; static under reduced_motion).

        The rotated Surface is quantized to one of _ARC_ROT_BUCKETS angles and
        cached per (radius, colour, bucket), so the boot screen reuses a handful
        of pre-rotated arcs instead of calling pygame.transform.rotate on every
        frame for the whole (possibly minute-plus) identification wait (P-10) —
        the same bucketing the status dot uses (P-3)."""
        import pygame

        s = min(self.width / 1024, self.height / 600)
        ca = layout.cover_art
        arc_r = int(32 * s)
        cx = ca.x + ca.w // 2
        cy = ca.y + ca.h // 2 - int(24 * s)

        base = self._get_arc_segment(arc_r, p.accent)
        if self.reduced_motion:
            arc = base
        else:
            bucket = int((elapsed % _ARC_SECS) / _ARC_SECS * _ARC_ROT_BUCKETS) % _ARC_ROT_BUCKETS
            key = (arc_r, p.accent, bucket)
            arc = self._arc_rot_cache.get(key)
            if arc is None:
                angle = -(bucket / _ARC_ROT_BUCKETS) * 360.0
                arc = pygame.transform.rotate(base, angle)
                self._arc_rot_cache.put(key, arc)
        target.blit(arc, arc.get_rect(center=(cx, cy)))

    # -----------------------------------------------------------------------
    # Drawing helpers
    # -----------------------------------------------------------------------

    def _render_tracked(self, text: str, size: int, color: tuple, tracking: float):
        return self._text.render_tracked(text, size, color, tracking)

    def _render_tracked_ellipsized(self, text: str, size: int, color: tuple,
                                   tracking: float, max_width: int):
        return self._text.render_tracked_ellipsized(text, size, color, tracking, max_width)

    @staticmethod
    def _break_long_token(token: str, font, max_width: int) -> list:
        return TextRenderer.break_long_token(token, font, max_width)

    def _wrap_lines(self, text: str, font, max_width: int) -> list:
        return self._text.wrap_lines(text, font, max_width)

    def _fit_wrapped(
        self, text: str, role: str, base_size: int, max_width: int,
        max_lines: int, min_size: int = 14, step: int = 2,
    ) -> tuple:
        return self._text.fit_wrapped(
            text, role, base_size, max_width, max_lines, min_size, step
        )

    def _ellipsize(self, text: str, font, max_width: int) -> str:
        return self._text.ellipsize(text, font, max_width)

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
                # R5-09: draw largest circle first (darkest, ~bg) and overpaint
                # inward toward the surface-tinted PEAK at the centre, so the light
                # emanates from BEHIND the record (origin 25%/35%) and fades to bg
                # at the edges — the DESIGN.md:196 spec. The pre-R5-09 code tied
                # brightness to the RADIUS (t), so the biggest circle (the edge)
                # got the full peak and the centre ended at ~bg: the exact
                # inversion. Brightness now decreases OUTWARD via (1-(i-1)/steps):
                # the innermost circle (i=1) is exactly GRADIENT_TEXT_PEAK and the
                # outermost (~i=steps) is ~bg. The brightest pixel we draw is still
                # exactly GRADIENT_TEXT_PEAK (now at the origin, not the edge), so
                # the DISP-2 contrast guarantee — text roles clamped against
                # palette.text_background — is unchanged.
                bright = 1 - (i - 1) / steps
                color = _lerp_color(p.bg, p.surface, bright * GRADIENT_TEXT_PEAK)
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
        return self._text.draw_wrapped_text(
            target, text, role, size, rect, color, line_height
        )

    def _measure_wrapped_text(
        self, text: str, role: str, size: int, available_width: int,
        line_height: float = 0.98,
    ) -> int:
        return self._text.measure_wrapped_text(
            text, role, size, available_width, line_height
        )

    # -----------------------------------------------------------------------
    # Palette management (ARCH-3: delegates to the PaletteTransition engine)
    # -----------------------------------------------------------------------

    @property
    def _palette(self) -> PaletteTransition:
        """The cross-fade engine, built lazily so __new__-skeleton tests that set
        _current_palette / _target_palette / _transition_start directly (without
        calling __init__) still work.  The engine holds no renderer state — the
        palette cache and dynamic_theming flag are passed into queue() per call —
        so it never needs rebinding when a skeleton swaps the cache."""
        impl = self.__dict__.get("_palette_impl")
        if impl is None:
            impl = PaletteTransition()
            self.__dict__["_palette_impl"] = impl
        return impl

    # The three transition-state fields live on the engine now; these delegating
    # properties preserve the old attribute surface for the render loop (which
    # reads _transition_start) and every __new__-skeleton test that assigns them.
    @property
    def _current_palette(self) -> DisplayPalette:
        return self._palette.current

    @_current_palette.setter
    def _current_palette(self, value: DisplayPalette):
        self._palette.current = value

    @property
    def _target_palette(self) -> DisplayPalette:
        return self._palette.target

    @_target_palette.setter
    def _target_palette(self, value: DisplayPalette):
        self._palette.target = value

    @property
    def _transition_start(self) -> float:
        return self._palette.transition_start

    @_transition_start.setter
    def _transition_start(self, value: float):
        self._palette.transition_start = value

    def _queue_palette(self, cover_url: Optional[str]):
        """Set the target palette for a new track, triggering a transition.

        Thin delegator to the PaletteTransition engine; the renderer owns the
        palette cache and the dynamic_theming flag and passes them in.
        """
        return self._palette.queue(cover_url, self._palette_cache, self.dynamic_theming)

    def _animated_palette(self) -> DisplayPalette:
        """Return the current interpolated palette for this render frame."""
        return self._palette.animated()

    # -----------------------------------------------------------------------
    # Cover art — async fetch (via CoverArtCache) + sync load from cache
    # -----------------------------------------------------------------------

    async def _prefetch_cover(self, url: str):
        """Ensure the cover for *url* is on disk and its palette extracted, all
        off the event loop.

        Scheduled via asyncio.create_task() from _on_state_change() so the
        download (SSRF-hardened fetch + atomic write + disk bounding in
        CoverArtCache, A-15) and the palette extraction both run in a thread-pool
        executor and never stall the event loop.  Note the palette step runs even
        when the cover is ALREADY on disk (e.g. cached from a previous session) —
        that warm-cache case is exactly what used to decode inline on the loop
        (P-9).

        STAB-1: deduped against in-flight downloads for the same URL — a
        state-change prefetch and a load-failure refetch must not both hit the
        network for one cover.
        """
        # #165 / R7-15: skip a URL already given up on (blacklisted), whichever of
        # the two routes blacklisted it. The DECODE route (_handle_corrupt_cover
        # past _COVER_MAX_LOAD_FAILURES) LEAVES its corrupt bytes on disk, so
        # exists() is true and — without this guard — the palette step below would
        # re-decode them and log one "Palette extraction failed" WARNING on every
        # track-change prefetch; skipping avoids that flood. The DOWNLOAD route
        # (past _COVER_MAX_DOWNLOAD_FAILURES) leaves NO bytes on disk, so exists()
        # is false and the palette step wouldn't fire anyway — but skipping is
        # still correct (a dead URL must not be re-attempted, and the download
        # branch below would just re-fail). Either way, a genuinely NEW cover was
        # already discard()'d from _cover_bad_urls by _on_state_change before this
        # task is spawned, so it still gets a fresh attempt; only a
        # still-blacklisted URL is short-circuited here.
        if url in self._cover_bad_urls:
            return
        if url in self._cover_prefetch_inflight:
            return
        self._cover_prefetch_inflight.add(url)
        try:
            if not self._cover_store.exists(url):
                # R6-18: honour the download backoff window — a recently-failed URL
                # is not re-attempted until its retry_after deadline, so a transient
                # network blip neither hammers the CDN nor blanks the cover for the
                # rest of the album. The render loop's _maybe_retry_cover_download
                # re-spawns this prefetch once the window elapses.
                deadline = self._cover_download_retry_after.get(url)
                if deadline is not None and time.monotonic() < deadline:
                    return
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._cover_store.download, url)
                    log.debug(f"Cover art cached: {self._cover_store.path_for(url).name}")
                    # A clean download clears only the DOWNLOAD-failure state. It
                    # must NOT clear the decode tally (cold-review HIGH): the
                    # corrupt-decode recovery (_handle_corrupt_cover) works by
                    # unlink + re-DOWNLOAD, so a clean re-download of bytes that
                    # still fail to DECODE (Pillow-accepts / SDL-rejects: progressive
                    # or CMYK JPEG, 16-bit PNG, …) would reset the bound every cycle
                    # and loop the unlink→download→decode-fail storm forever — the
                    # exact churn _COVER_MAX_LOAD_FAILURES exists to stop. The decode
                    # tally is cleared only on a successful DECODE (_decode_cover_async).
                    self._cover_download_failures.pop(url, None)
                    self._cover_download_retry_after.pop(url, None)
                except PermanentCoverError as e:
                    # #305: a validation reject (decompression bomb beyond the decode
                    # ceiling, corrupt/truncated bytes, or a disallowed format) is
                    # PERMANENT for these bytes — re-downloading re-lands the same
                    # reject. Blacklist immediately instead of R6-18's up-to-5×
                    # re-download of the same (often large) file. Drop any pending
                    # download bookkeeping for it too (#306).
                    self._cover_bad_urls.add(url)
                    self._cover_download_failures.pop(url, None)
                    self._cover_download_retry_after.pop(url, None)
                    log.error(
                        f"Cover art at {url} is permanently unusable ({e}) — "
                        f"blacklisting immediately, no re-download (a same-album track "
                        f"change reuses this URL and won't lift it)."
                    )
                    return
                except Exception as e:
                    # R6-18: a failed download backs off (time-based) rather than
                    # blacklisting within the album; only a persistently-dead URL
                    # (past _COVER_MAX_DOWNLOAD_FAILURES) is given up on. The url is
                    # NOT marked on-disk, so _load_cover never spawns a decode for
                    # it — no per-frame churn.
                    failures = self._cover_download_failures.get(url, 0) + 1
                    self._cover_download_failures[url] = failures
                    if failures >= _COVER_MAX_DOWNLOAD_FAILURES:
                        self._cover_bad_urls.add(url)
                        # #306: once blacklisted, the download tally + retry deadline
                        # are dead weight — drop them (the URL is now gated by
                        # _cover_bad_urls). Bounds the dicts to actively-retrying
                        # covers.
                        self._cover_download_failures.pop(url, None)
                        self._cover_download_retry_after.pop(url, None)
                        log.error(
                            f"Cover art download for {url} failed {failures} times "
                            f"— giving up until a different cover is requested "
                            f"(a same-album track change reuses this URL and won't "
                            f"lift the blacklist): {e}"
                        )
                    else:
                        self._cover_download_retry_after[url] = (
                            time.monotonic() + _COVER_DOWNLOAD_RETRY_BACKOFF_SECONDS
                        )
                        log.warning(
                            f"Failed to download cover art from {url} "
                            f"(attempt {failures}); retrying after backoff: {e}"
                        )
                    return

            # R6-22: mark readiness / repaint ONLY if this cover is still the one
            # the display wants. A download that completed for an ABANDONED cover
            # (the track changed mid-download) leaves the file on disk, but adding a
            # _cover_on_disk marker for it would never be discarded again (a slow
            # leak), and repainting for a cover no longer shown is pointless.
            # (Extraction still runs unconditionally below — a pre-cached palette is
            # useful if the user returns to this cover — but it self-guards the
            # live transition on _wanted_cover_url.)
            if url == self._wanted_cover_url:
                # R5-21: mark readiness before extraction so a frame rendered DURING
                # it can already spawn the decode.
                self._cover_on_disk.add(url)
            await self._extract_palette_async(url)
            # Re-affirm readiness right before the repaint-triggering version bump:
            # a rapid X->Y->X state flip DURING the await above runs _on_state_change
            # (discarding X from _cover_on_disk), and the re-spawned prefetch(X) is
            # deduped by _cover_prefetch_inflight so it would NOT re-add it — leaving
            # the gate shut when this repaint lands (cold-review LOW). The file is
            # confirmed on disk, so re-adding is correct WHEN X is wanted again.
            if url == self._wanted_cover_url:
                self._cover_on_disk.add(url)
                # A cover for `url` is now on disk — bump the version so the next
                # render recomposes the static frame and picks it up (B-22).
                self._cover_version += 1
                self._dirty = True
        finally:
            self._cover_prefetch_inflight.discard(url)

    async def _extract_palette_async(self, url: str):
        """Extract a cover's palette in an executor, cache it, and re-queue the
        transition — keeping Pillow decode + quantize off the event loop (P-9).

        A no-op if dynamic theming is off, the palette is already cached (just
        re-queue, no decode), or the cover file isn't on disk yet (the prefetch
        download path will call back here once it lands).
        """
        if not self.dynamic_theming:
            return
        if self._palette_cache.get(url) is not None:
            if url == self._wanted_cover_url:
                self._queue_palette(url)  # cache hit → real palette, no decode
            return
        cache_path = self._cover_store.path_for(url)
        if not cache_path.exists():
            return
        try:
            loop = asyncio.get_running_loop()
            palette = await loop.run_in_executor(None, extract_palette, cache_path)
        except Exception as e:
            log.warning(f"Palette extraction failed for {url}: {e}")
            return
        if palette is None:
            # R6-17: a transient decode failure must NOT be cached as this URL's
            # palette — the cache-hit short-circuit above would then never
            # re-extract, so even after the corrupt-cover machinery refetches good
            # bytes the theme would stay FALLBACK until restart. Leave it uncached;
            # the refetch's own _extract_palette_async re-runs and caches the real
            # palette. (The live transition already targets FALLBACK for an
            # uncached URL, so the screen is correct meanwhile.)
            log.warning(f"Palette extraction produced no palette for {url} — not caching")
            return
        # Cache the real palette (valid for this URL regardless of timing)...
        self._palette_cache.put(url, palette)
        # ...but only retarget the live transition if this cover is still the one
        # the display wants — a slow decode for a previous track must not paint
        # its palette over whatever is on screen now.
        if url == self._wanted_cover_url:
            self._queue_palette(url)

    def _maybe_retry_cover_download(self, now: Optional[float] = None):
        """R6-18: re-attempt a backed-off cover download once its 30s window has
        elapsed. Called once per render-loop iteration; a no-op unless the WANTED
        cover has a pending, now-elapsed download backoff and isn't already on
        disk, blacklisted, or in flight. ``now`` is injectable for tests."""
        url = self._wanted_cover_url
        if not url or url in self._cover_on_disk or url in self._cover_bad_urls:
            return
        if url in self._cover_prefetch_inflight:
            return
        deadline = self._cover_download_retry_after.get(url)
        if deadline is None:
            return   # no pending download failure to retry
        if (now if now is not None else time.monotonic()) < deadline:
            return   # still backing off
        self._spawn(self._prefetch_cover(url))

    def _load_cover(self, url: Optional[str], w: int, h: int):
        """Return the scaled cover for *url* from the in-memory cache, or None.

        STAB-5: this runs on the event loop every render frame, so it does NO
        blocking work — no SD read, no decode.  On a cache miss it schedules an
        OFF-loop decode (`_decode_cover_async`) and returns None so the caller
        shows the placeholder; that task caches the scaled Surface and bumps
        `_cover_version` to trigger a repaint once it lands.  Returns None for an
        absent, blacklisted, or not-yet-decoded cover.

        The scaled Surface is cached keyed by (url, w, h) (v1.3.3): decode +
        smoothscale then happen exactly once per cover per resolution, off the
        loop.
        """
        if not url or url in self._cover_bad_urls:
            # STAB-1: a blacklisted URL gets no disk read, no decode, no refetch,
            # no log.  A state change to a NEW cover clears the blacklist (see
            # _on_state_change), giving a fresh play a clean try.
            return None

        cached = self._cover_cache.get((url, w, h))
        if cached is not None:
            return cached

        # Not decoded yet — kick off the off-loop load+scale (deduped inside
        # _decode_cover_async and via the inflight guard here) and show the
        # placeholder until it lands.  _spawn no-ops without a running loop.
        # R5-21: only spawn a decode once the file is known on disk — otherwise a
        # pending/failed download would respawn a (blocking-stat, no-op) decode
        # task every frame for the whole track. _prefetch_cover marks readiness
        # and bumps _cover_version, so a landed download triggers a repaint that
        # reaches here with url in _cover_on_disk and the decode spawns once.
        # R8-06 (#353): while a convert() fault is latched, the WORK is gated —
        # not just the log.  Without this, every failed decode task cleared the
        # inflight guard and this cache-miss respawned a full JPEG decode + SD
        # read per frame (~10 Hz) for the entire video-loss episode.  One
        # retry per _COVER_DECODE_RETRY_SECONDS probes for the display coming
        # back; a clean decode or a new cover clears the latch.
        if self._cover_decode_deferred and time.monotonic() < self._cover_decode_retry_at:
            return None
        if url in self._cover_on_disk and (url, w, h) not in self._cover_decode_inflight:
            self._spawn(self._decode_cover_async(url, w, h))
        return None

    async def _decode_cover_async(self, url: str, w: int, h: int):
        """Load + scale a cached cover OFF the event loop, cache it, repaint (STAB-5).

        The SD read + decode (`pygame.image.load`) is the step that can stall for
        SECONDS on a worn card, so it runs in the default executor.  `.convert()`
        + `smoothscale` stay ON the loop: they act on already-decoded bytes (fast
        CPU), `.convert()` needs the display's pixel format, and SDL video ops
        belong on the main thread.  This split also separates STAB-1's two failure
        classes by WHERE they raise:
          * `pygame.image.load()` failing (off-loop) → corrupt/partial bytes:
            one bounded unlink + refetch, then blacklist.
          * `.convert()` failing (on-loop) → no video mode (transient display
            fault, e.g. HDMI hotplug) on GOOD bytes: never delete or refetch;
            latch so the warning logs once per episode, not ~10×/second.
        """
        import pygame

        if not url or url in self._cover_bad_urls:
            return
        key = (url, w, h)
        if self._cover_cache.get(key) is not None:
            return                              # already decoded (raced with another spawn)
        if key in self._cover_decode_inflight:
            return                              # a decode for this cover is already running
        cache_path = self._cover_store.path_for(url)
        if not cache_path.exists():
            if url in self._cover_on_disk:
                # R7-12: the URL was MARKED on disk but the file has VANISHED — the
                # mtime-LRU pruned it. A warm-start cover's mtime is never refreshed
                # after the download short-circuits, so it is the prune's first
                # victim once any later download crosses the cache bound. The old
                # code returned WITHOUT dropping the marker, so `url in
                # _cover_on_disk` stayed true and _load_cover respawned this
                # (blocking-stat, no-op) decode EVERY frame for the rest of the
                # album, while the R6-18 retry driver stayed gated off (it too skips
                # a URL still in _cover_on_disk). Drop the readiness marker and
                # refetch — exactly as the corrupt-bytes path does (R6-19) — so the
                # churn stops and the cover recovers within the track.
                self._cover_on_disk.discard(url)
                if url not in self._cover_prefetch_inflight:
                    self._spawn(self._prefetch_cover(url))
                self._dirty = True   # repaint to the placeholder now the cover is gone
            # else: the decode raced ahead of a still-PENDING download (the file was
            # never on disk). The state-change prefetch owns landing it; do NOT
            # refetch here (that download path is not this method's job).
            return

        self._cover_decode_inflight.add(key)
        try:
            try:
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(None, pygame.image.load, str(cache_path))
            except Exception as e:
                # R7-13: distinguish a file PRUNED between the exists() check above
                # and the executor load (a race) from genuinely CORRUPT bytes.
                # pygame raises FileNotFoundError for a missing file; re-checking
                # existence also covers any load error on a now-absent file
                # whatever its exception type. A vanished file is treated as
                # not-on-disk (drop the marker + refetch, NO decode tally), never as
                # a corrupt-decode failure — which would unlink an already-gone file
                # and burn a _COVER_MAX_LOAD_FAILURES attempt toward a spurious
                # blacklist that a same-album track change could never lift.
                if isinstance(e, FileNotFoundError) or not cache_path.exists():
                    if url in self._cover_on_disk:
                        self._cover_on_disk.discard(url)
                        if url not in self._cover_prefetch_inflight:
                            self._spawn(self._prefetch_cover(url))
                    self._dirty = True
                    return
                # Corrupt/partial bytes (the SD read is off-loop, so its failure
                # surfaces here): STAB-1 bounded unlink + refetch, then blacklist.
                self._handle_corrupt_cover(url, cache_path, e)
                self._dirty = True
                return

            try:
                scaled = pygame.transform.smoothscale(raw.convert(), (w, h))
            except Exception as e:
                # convert()/scale failed on ALREADY-decoded, good bytes — a
                # display fault (no video mode), NOT a corrupt file, so never
                # delete or refetch.  Latch so a persistent fault logs ONCE per
                # episode; the latch clears on the next clean decode.  Catch
                # Exception (not just pygame.error) so a stray non-pygame error
                # fails safe to the placeholder instead of escaping the task.
                # 3rd-pass F3P-1: a STALE task (its cover no longer wanted —
                # the track changed mid-decode) must not latch the GLOBAL
                # episode flag: a cover-specific failure would otherwise gate
                # the NEW cover's first decode ~5s on a healthy display.  A
                # real video fault re-latches via the new cover's own decode.
                if url != self._wanted_cover_url:
                    return
                if not self._cover_decode_deferred:
                    if isinstance(e, pygame.error):
                        log.warning(f"Cover decode deferred — display not ready: {e}")
                    else:
                        log.error(f"Unexpected error scaling cover art for {url}: {e}")
                    self._cover_decode_deferred = True
                    self._dirty = True   # paint the placeholder once
                # R8-06: (re)arm the retry deadline on EVERY failed attempt —
                # first or repeat — so _load_cover's gate holds between probes
                # instead of re-opening once the first deadline passes.
                self._cover_decode_retry_at = (
                    time.monotonic() + _COVER_DECODE_RETRY_SECONDS
                )
                return

            self._cover_cache.put(key, scaled)
            self._cover_decode_failures.pop(url, None)   # a clean load clears the tally
            self._cover_decode_deferred = False         # display is back
            self._cover_version += 1                    # recompose the static frame with the cover
            self._dirty = True
        finally:
            self._cover_decode_inflight.discard(key)

    def _handle_corrupt_cover(self, url: str, cache_path, error) -> None:
        """STAB-1: a cached cover failed to DECODE (corrupt/partial bytes).

        Unlink + re-fetch it at most ``_COVER_MAX_LOAD_FAILURES`` times so a
        transient partial write can still self-heal within the track (B-18);
        past that the re-download keeps re-landing the same bad bytes, so mark
        the URL bad and stop the per-frame disk/network/log loop.  Always
        returns None (the caller shows the placeholder).
        """
        failures = self._cover_decode_failures.get(url, 0) + 1
        self._cover_decode_failures[url] = failures
        if failures > _COVER_MAX_LOAD_FAILURES:
            self._cover_bad_urls.add(url)
            log.error(
                f"Cover art at {url} still undecodable after {failures} "
                f"attempts — giving up until a different cover is requested "
                f"(a same-album track change reuses this URL and won't lift the "
                f"blacklist): {error}"
            )
            return None
        log.warning(f"Failed to load cached cover art (attempt {failures}): {error}")
        cache_path.unlink(missing_ok=True)
        # R6-19: drop the readiness marker along with the file, so _load_cover does
        # NOT keep spawning a (no-op, blocking-stat) decode task every frame during
        # the refetch window — the per-frame churn R5-21 closed. _prefetch_cover
        # re-adds the marker when the good bytes land.
        self._cover_on_disk.discard(url)
        # Re-fetch so the cover can recover within the track (B-18), but only if
        # a download for this URL isn't already in flight (STAB-1 dedup).  `_spawn`
        # now owns the running-loop guard (DISP-8), so an off-loop unit-test call
        # degrades to a no-op there rather than needing a duplicate guard here.
        if url not in self._cover_prefetch_inflight:
            self._spawn(self._prefetch_cover(url))
        return None

    def stop(self):
        self._running = False
        import pygame
        pygame.quit()
        log.info("Display stopped.")
