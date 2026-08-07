"""Palette cross-fade state machine (ARCH-3 — PaletteTransition).

Extracted from ``renderer.py`` so the 1-second palette lerp on track change —
its interpolation, per-frame quantization (P-4), same-target skip (v1.3.5), and
snap-before-retarget behaviour — can be unit-tested WITHOUT a pygame-initialised
``DisplayRenderer`` (the ARCH-3 concern: this is a pure time-driven state machine
over five colours that shared nothing with the render loop yet could previously
only be reached through it).

``DisplayRenderer`` composes one ``PaletteTransition`` and delegates
``_queue_palette``/``_animated_palette`` to it via thin shims, so its method
surface is unchanged.  The transition STATE (current/target/start) lives here;
the palette cache and the ``dynamic_theming`` flag stay OWNED by the renderer and
are passed in per call, so the engine captures no renderer state and the
renderer's ``__new__`` skeleton tests keep working unchanged.

No pygame or PIL import: this module is pure colour arithmetic plus
``time.monotonic``, so it is importable and testable anywhere the headless suite
runs.
"""

import time

from src.display.palette import (
    DisplayPalette,
    FALLBACK_PALETTE,
    ensure_contrast,
    ensure_contrast_hue_preserving,
    text_background,
)

# Palette cross-fade duration (seconds) on a track change.
_TRANSITION_SECS = 1.0

# Quantization step (per RGB channel) applied to the in-flight lerp palette so
# the per-frame palette — and the static-frame + tracked-label cache keys that
# include it — only changes ~16 times over the 1s transition instead of every
# frame, avoiding thousands of glyph re-renders per track change (P-4).  The
# stepping is imperceptible at the gradient's low bg→surface contrast, and the
# final (settled) palette is the exact target, not a quantized value.
_PALETTE_LERP_QUANTIZE = 16


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


def _quantize_palette(p: DisplayPalette) -> DisplayPalette:
    q = _PALETTE_LERP_QUANTIZE

    def snap(color):
        return tuple((c // q) * q for c in color)

    bg = snap(p.bg)
    surface = snap(p.surface)
    # Re-assert the Full-Opacity Rule after quantizing: flooring a text role
    # toward black could otherwise transiently drop it below the 4.5:1 WCAG
    # floor during the lerp.  Clamp against the gradient's brightest pixel
    # (DISP-2), not flat bg; accent (the album title) is lifted hue-preserving
    # exactly as extract_palette does (DISP-1), muted blends toward white.  Both
    # are deterministic in their (quantized) inputs, so the cache key stays
    # stable while readability is preserved.
    tb = text_background(bg, surface)
    muted = ensure_contrast(snap(p.muted), tb, min_ratio=4.5)
    accent = ensure_contrast_hue_preserving(snap(p.accent), tb, min_ratio=4.5)
    return DisplayPalette(
        bg=bg, surface=surface, accent=accent,
        text=snap(p.text), muted=muted,
    )


class PaletteTransition:
    """The time-driven palette cross-fade for one display.

    Holds the three-field state (``current``, ``target``, ``transition_start``)
    and the two operations over it: ``queue`` (retarget on a track change) and
    ``animated`` (the interpolated palette for this render frame).  The palette
    cache and ``dynamic_theming`` are the renderer's, passed to ``queue`` per
    call rather than captured, so this class owns no renderer state.
    """

    def __init__(self, initial: DisplayPalette = FALLBACK_PALETTE):
        # current and target start as the SAME object (the shared FALLBACK_PALETTE
        # module constant by default) — safe because palettes are treated as
        # immutable throughout: queue()/animated()/_lerp_palette/_quantize_palette
        # only ever REASSIGN whole DisplayPalette objects, never mutate a channel
        # in place.  This preserves the pre-ARCH-3 __init__ semantics exactly
        # (which also seeded both fields from the one FALLBACK_PALETTE object).
        self.current: DisplayPalette = initial
        self.target: DisplayPalette = initial
        self.transition_start: float = 0.0

    def queue(self, cover_url, palette_cache, dynamic_theming: bool):
        """Set the target palette for a new track, triggering a transition.

        If a previous transition is still in flight, snap ``current`` to the
        currently-interpolated value before reassigning the target — that way the
        new lerp starts from what the user is *currently seeing* instead of
        jumping back to a stale starting point.

        The palette cache and ``dynamic_theming`` flag belong to the renderer and
        are passed in, keeping this engine free of renderer state.
        """
        if not dynamic_theming:
            return
        if cover_url is None:
            target = FALLBACK_PALETTE
        elif (cached := palette_cache.get(cover_url)) is not None:
            target = cached  # get() already refreshed its eviction position
        else:
            # P-9: _queue_palette must NEVER decode — it runs synchronously inside
            # set_track's Signal callback on the event loop, and Pillow decode +
            # quantize is tens of ms on the Pi.  When the palette isn't cached yet
            # we target FALLBACK for now; _extract_palette_async (in an executor)
            # extracts off-loop and re-queues with the real palette a frame later.
            target = FALLBACK_PALETTE

        # Skip the retarget entirely when nothing changed (v1.3.5): every
        # track commit notifies the renderer, and tracks from the same album
        # share a cover URL — without this guard each commit restarted the 1s
        # transition (30 fps cadence + per-frame gradient regeneration)
        # lerping a palette to itself.
        if target == self.target:
            return

        # Snap current to the live interpolated value before retargeting, so a
        # mid-transition track change doesn't lerp from a stale base palette.
        self.current = self.animated()
        self.target = target
        self.transition_start = time.monotonic()

    def animated(self) -> DisplayPalette:
        """Return the current interpolated palette for this render frame."""
        elapsed = time.monotonic() - self.transition_start
        t = min(1.0, elapsed / _TRANSITION_SECS)
        if t >= 1.0:
            self.current = self.target
            return self.target
        # Quantize the in-flight palette so the per-frame cache keys stay stable
        # across many frames during the lerp (P-4).
        return _quantize_palette(
            _lerp_palette(self.current, self.target, t)
        )
