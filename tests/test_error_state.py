"""Unit tests for the v1.4.1 error state and empty-state rendering.

Covers the Phase 2 design-translation work:

  ✓ PlayerStatus.ERROR exists and PlayerState transitions through it
  ✓ RecognitionLoop._register_miss — consecutive misses while LISTENING
    surface ERROR; misses in other states don't; a hit resets the count
  ✓ DisplayRenderer._boot_label — the time-progressive boot label
  ✓ Empty-state compose smoke tests for all three kinds (headless)
  ✓ Empty-state static key changes when the boot label ticks

Same headless patterns as the rest of the suite: MagicMock-backed
RecognitionLoop, __new__-skeleton renderer, SDL dummy video driver.
"""
import os
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from src.audio.recognizer import RecognitionLoop  # noqa: E402
from src.display.renderer import (  # noqa: E402
    DisplayRenderer,
    _BoundedCache,
    _LABEL_CACHE_MAX,
    _FONT_CACHE_MAX,
    _DOT_CACHE_MAX,
    _EMPTY_STATES,
    _ERROR_RED,
    EmptyState,
)
from src.display.layouts import get_now_playing_layout  # noqa: E402
from src.metadata.models import FALLBACK_PALETTE  # noqa: E402
from tests.factories import make_recognition_config  # noqa: E402
from src.state.player_state import PlayerState, PlayerStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_loop(error_after_misses=3):
    config = make_recognition_config(
        confirmation_required=2,
        error_after_misses=error_after_misses,
    )
    state = PlayerState()

    async def commit(r):
        # Stand in for TrackCommitService.commit: a confirmed track lands as
        # PLAYING so any miss-streak-reset-on-commit assertions hold.
        state.set_track(MagicMock())
        state.set_raw(r)
        return True

    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, commit)
    return loop, state


def make_renderer():
    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = False
    r._font_cache = _BoundedCache(_FONT_CACHE_MAX)
    r._label_cache = _BoundedCache(_LABEL_CACHE_MAX)
    r._dot_cache = _BoundedCache(_DOT_CACHE_MAX)
    r._layout = get_now_playing_layout(1024, 600)
    r._gradient_key = None
    r._gradient_surface = None
    r._shadow_key = None
    r._shadow_surface = None
    r._static_key = None
    r._static_surface = None
    r._listening_since = None
    r._arc_segment = None
    return r


# ---------------------------------------------------------------------------
# PlayerStatus.ERROR transitions
# ---------------------------------------------------------------------------

def test_error_status_exists():
    assert PlayerStatus.ERROR is not None


def test_clear_recovers_from_error_to_idle():
    state = PlayerState()
    state.set_status(PlayerStatus.ERROR)
    state.clear()
    assert state.status == PlayerStatus.IDLE


def test_set_track_recovers_from_error_to_playing():
    state = PlayerState()
    state.set_status(PlayerStatus.ERROR)
    state.set_track(MagicMock())
    assert state.status == PlayerStatus.PLAYING


# ---------------------------------------------------------------------------
# RecognitionLoop miss counting
# ---------------------------------------------------------------------------

def test_misses_while_listening_surface_error():
    loop, state = make_loop(error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)
    for _ in range(3):
        loop._register_miss()
    assert state.status == PlayerStatus.ERROR


def test_fewer_misses_than_threshold_stay_listening():
    loop, state = make_loop(error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)
    for _ in range(2):
        loop._register_miss()
    assert state.status == PlayerStatus.LISTENING


def test_misses_while_playing_do_not_error():
    """Surface noise and quiet passages produce routine misses mid-album —
    they must never put NO MATCH FOUND over a correctly identified record."""
    loop, state = make_loop(error_after_misses=3)
    state.set_status(PlayerStatus.PLAYING)
    for _ in range(10):
        loop._register_miss()
    assert state.status == PlayerStatus.PLAYING


def test_misses_while_idle_do_not_error():
    loop, state = make_loop(error_after_misses=3)
    for _ in range(10):
        loop._register_miss()
    assert state.status == PlayerStatus.IDLE


def test_miss_count_resets_outside_listening():
    """Misses in other states clear the streak — re-entering LISTENING
    starts from zero rather than inheriting stale failures."""
    loop, state = make_loop(error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)
    loop._register_miss()
    loop._register_miss()
    state.set_status(PlayerStatus.PLAYING)
    loop._register_miss()          # resets the streak
    state.set_status(PlayerStatus.LISTENING)
    loop._register_miss()
    assert state.status == PlayerStatus.LISTENING  # 1 of 3, not 3 of 3


@pytest.mark.asyncio
async def test_same_as_current_hit_resets_miss_count():
    """A result that matches the currently-playing track means recognition is
    working again — it resets the miss streak (B-7)."""
    from tests.test_recognizer import make_raw
    loop, state = make_loop(error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)
    await loop._handle_result(None)
    await loop._handle_result(None)
    assert loop._miss_count == 2
    state.current_raw = make_raw()           # this track is now "playing"
    await loop._handle_result(make_raw())    # same as current → streak reset
    assert loop._miss_count == 0
    assert state.status == PlayerStatus.LISTENING


@pytest.mark.asyncio
async def test_non_confirming_hit_no_longer_wipes_miss_streak():
    """B-7: a single *new* non-confirming result is unconfirmable churn, not
    progress.  It used to reset the miss count to 0 on every non-None result —
    so persistent failure (or alternating churn) could never surface ERROR.
    Now it counts toward the streak instead of hiding it."""
    from tests.test_recognizer import make_raw
    loop, state = make_loop(error_after_misses=3)
    state.set_status(PlayerStatus.LISTENING)
    await loop._handle_result(None)          # no-progress 1
    await loop._handle_result(None)          # no-progress 2
    await loop._handle_result(make_raw())    # churn → no-progress 3 → ERROR
    assert state.status == PlayerStatus.ERROR


# ---------------------------------------------------------------------------
# Boot label progression
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("elapsed,expected", [
    (0, "WARMING UP"),
    (19.9, "WARMING UP"),
    (20, "STILL LISTENING…"),
    (59.9, "STILL LISTENING…"),
    (60, "IDENTIFYING… 1:00"),
    (75, "IDENTIFYING… 1:15"),
    (130, "IDENTIFYING… 2:10"),
])
def test_boot_label_progression(elapsed, expected):
    assert DisplayRenderer._boot_label(elapsed) == expected


# ---------------------------------------------------------------------------
# Empty-state compose smoke tests (headless)
# ---------------------------------------------------------------------------

@pytest.fixture()
def _display():
    pygame.init()
    pygame.display.set_mode((1024, 600))
    yield
    pygame.display.quit()


@pytest.mark.parametrize("state,boot_label", [
    (EmptyState.BOOT, "WARMING UP"),
    (EmptyState.IDLE, None),
    (EmptyState.ERROR, None),
])
def test_compose_empty_smoke(_display, state, boot_label):
    """Each empty-state frame composes headlessly at full screen size."""
    r = make_renderer()
    frame = r._compose_empty(state, r._layout, FALLBACK_PALETTE, boot_label)
    assert frame.get_size() == (1024, 600)


def test_render_empty_caches_until_boot_label_ticks(_display):
    """The static frame is reused while the boot label is unchanged and
    recomposed when it ticks to the next string."""
    r = make_renderer()
    r._screen = pygame.display.get_surface()

    class _State:  # minimal stand-in; _render_empty reads no state fields
        pass

    r.state = _State()
    r.dynamic_theming = False
    r._current_palette = FALLBACK_PALETTE
    r._target_palette = FALLBACK_PALETTE
    r._transition_start = 0.0
    r._dirty = False

    r._listening_since = time.monotonic() - 5   # WARMING UP bucket
    r._render_empty(EmptyState.BOOT)
    first = r._static_surface
    r._render_empty(EmptyState.BOOT)
    assert r._static_surface is first            # same label → cache hit

    r._listening_since = time.monotonic() - 30  # STILL LISTENING… bucket
    r._render_empty(EmptyState.BOOT)
    assert r._static_surface is not first        # label ticked → recompose


# ---------------------------------------------------------------------------
# Empty-state descriptor table (A-7) — one enum-keyed table replaced the
# stringly-typed "kind" plus three parallel dicts.
# ---------------------------------------------------------------------------

def test_every_empty_state_has_a_spec():
    """The table is total over the enum — no state can render without a row."""
    assert set(_EMPTY_STATES) == set(EmptyState)


def test_empty_state_dot_colors_resolve_from_palette():
    """boot pulses in accent, idle sits in muted, error sits in muted red —
    all driven by the descriptor's dot_color resolver."""
    p = FALLBACK_PALETTE
    assert _EMPTY_STATES[EmptyState.BOOT].dot_color(p) == p.accent
    assert _EMPTY_STATES[EmptyState.IDLE].dot_color(p) == p.muted
    assert _EMPTY_STATES[EmptyState.ERROR].dot_color(p) == _ERROR_RED


def test_only_boot_animates():
    """boot is the lone animated empty state (boot spins; idle/error sit)."""
    assert _EMPTY_STATES[EmptyState.BOOT].animates is True
    assert _EMPTY_STATES[EmptyState.IDLE].animates is False
    assert _EMPTY_STATES[EmptyState.ERROR].animates is False


@pytest.mark.parametrize("state,expect_dirty", [
    (EmptyState.BOOT, True),
    (EmptyState.IDLE, False),
    (EmptyState.ERROR, False),
])
def test_render_empty_sets_dirty_only_for_animated_states(_display, state, expect_dirty):
    """_render_empty re-arms the render loop iff the state animates."""
    r = make_renderer()
    r._screen = pygame.display.get_surface()

    class _State:
        pass

    r.state = _State()
    r._current_palette = FALLBACK_PALETTE
    r._target_palette = FALLBACK_PALETTE
    r._transition_start = 0.0
    r._listening_since = time.monotonic() - 5
    r._dirty = False

    r._render_empty(state)
    assert r._dirty is expect_dirty
