"""TQ-1 (#128) — construct the REAL DisplayRenderer and exercise its dispatch.

Every other renderer test builds its subject with ``DisplayRenderer.__new__(...)``
and hand-assigns attributes, so ``__init__``, ``start()``, ``_on_state_change``
and the ``_render()`` status dispatch were **0% executed** — the 83% coverage
figure was measured against an object the tests built themselves.  The failure
that hides in that gap: an ``__init__`` refactor drops
``self.state.on_change(self._on_state_change)``, the suite stays green, and on
the Pi the display shows the boot card and then never updates again.  These
tests build the real object and pin the wiring + dispatch that no test touched.
"""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # headless pygame

import pygame  # noqa: E402
import pytest  # noqa: E402

from src.config import DisplayConfig  # noqa: E402
from src.display.renderer import DisplayRenderer, EmptyState  # noqa: E402
from src.state.player_state import PlayerState, PlayerStatus  # noqa: E402


def _config(tmp_path):
    return DisplayConfig(
        width=1024,
        height=600,
        fullscreen=False,
        dynamic_theming=True,
        reduced_motion=False,
        cover_art_cache_dir=str(tmp_path / "cache"),
    )


def test_init_wires_the_state_subscription(tmp_path):
    # Prove __init__ actually calls state.on_change(self._on_state_change).
    # Behavioral, not introspective: fire a real state change through the
    # Signal and confirm the renderer's handler ran.  Drop the subscription
    # line and this fails (dirty stays False) — the exact refactor that would
    # freeze the display while every existing renderer test stayed green.
    state = PlayerState()
    r = DisplayRenderer(_config(tmp_path), state)
    assert r.state is state
    assert r._screen is None                      # constructed, not started

    r._dirty = False                              # clear the constructor's initial True
    r._listening_since = None
    state.set_status(PlayerStatus.LISTENING)      # real notify through the Signal
    assert r._dirty is True                        # _on_state_change fired
    assert r._listening_since is not None          # its LISTENING-specific side effect


@pytest.mark.parametrize("status, has_track, expected", [
    (PlayerStatus.IDLE,      False, ("empty", EmptyState.IDLE)),
    (PlayerStatus.LISTENING, False, ("empty", EmptyState.BOOT)),
    (PlayerStatus.ERROR,     False, ("empty", EmptyState.ERROR)),
    (PlayerStatus.PLAYING,   True,  ("now_playing", None)),
    (PlayerStatus.PLAYING,   False, ("empty", EmptyState.BOOT)),   # PLAYING w/o track → else branch
])
def test_render_dispatches_by_status(tmp_path, status, has_track, expected):
    # _render() picks a screen from (status, current_track).  Pin every arm,
    # including PLAYING-without-a-track falling through to the boot screen.
    state = PlayerState()
    r = DisplayRenderer(_config(tmp_path), state)
    calls = []
    r._render_empty = lambda s: calls.append(("empty", s))
    r._render_now_playing = lambda: calls.append(("now_playing", None))

    # Set the fields directly so this isolates dispatch, without firing the
    # on_change side effects (palette queue / cover prefetch).
    r.state.status = status
    r.state.current_track = object() if has_track else None
    r._render()

    assert calls == [expected]


def test_start_creates_a_headless_surface(tmp_path):
    # Covers start(): pygame.init() + display.set_mode(), under the dummy
    # video driver.  0% executed before TQ-1.
    state = PlayerState()
    r = DisplayRenderer(_config(tmp_path), state)
    try:
        r.start()
        assert r._screen is not None
        assert r._screen.get_size() == (1024, 600)
    finally:
        pygame.display.quit()


def test_cover_store_can_be_injected(tmp_path):
    """ARCH-8: an injected cover store is used verbatim, so tests/composition
    root can substitute one without monkeypatching the private attribute."""
    from unittest.mock import MagicMock
    state = PlayerState()
    sentinel = MagicMock(name="fake-cover-store")
    r = DisplayRenderer(_config(tmp_path), state, cover_store=sentinel)
    assert r._cover_store is sentinel


def test_on_bg_task_done_logs_and_discards_faulted_task(tmp_path, caplog):
    """#207/arch-6: an exception escaping a _spawn'd display background task must
    be RETRIEVED and logged with context, not left as a detached GC-time 'Task
    exception was never retrieved'. The done-callback also releases the strong
    ref."""
    import asyncio
    import logging
    state = PlayerState()
    r = DisplayRenderer(_config(tmp_path), state)

    class _FakeTask:
        def __init__(self, exc=None, cancelled=False):
            self._exc, self._cancelled = exc, cancelled
        def cancelled(self): return self._cancelled
        def exception(self):
            # A REAL asyncio.Task.exception() RAISES CancelledError when the task
            # was cancelled — so the callback's cancelled() guard MUST run first.
            # Modelling that here pins the ordering: an exception-first refactor
            # would raise out of the done-callback and this test would catch it.
            if self._cancelled:
                raise asyncio.CancelledError()
            return self._exc

    faulted = _FakeTask(exc=RuntimeError("cover decode blew up"))
    r._bg_tasks.add(faulted)
    with caplog.at_level(logging.ERROR, logger="src.display.renderer"):
        r._on_bg_task_done(faulted)
    assert faulted not in r._bg_tasks     # strong ref released
    assert any("cover decode blew up" in rec.getMessage() for rec in caplog.records)


def test_on_bg_task_done_silent_on_cancelled_and_clean(tmp_path, caplog):
    """A cancelled task is normal shutdown, and a clean completion has no
    exception — neither should log an error, but both must be discarded."""
    import asyncio
    import logging
    state = PlayerState()
    r = DisplayRenderer(_config(tmp_path), state)

    class _FakeTask:
        def __init__(self, exc=None, cancelled=False):
            self._exc, self._cancelled = exc, cancelled
        def cancelled(self): return self._cancelled
        def exception(self):
            # A REAL asyncio.Task.exception() RAISES CancelledError when the task
            # was cancelled — so the callback's cancelled() guard MUST run first.
            # Modelling that here pins the ordering: an exception-first refactor
            # would raise out of the done-callback and this test would catch it.
            if self._cancelled:
                raise asyncio.CancelledError()
            return self._exc

    cancelled_t = _FakeTask(cancelled=True)
    clean_t = _FakeTask(exc=None)
    r._bg_tasks.update({cancelled_t, clean_t})
    with caplog.at_level(logging.ERROR, logger="src.display.renderer"):
        r._on_bg_task_done(cancelled_t)
        r._on_bg_task_done(clean_t)
    assert cancelled_t not in r._bg_tasks and clean_t not in r._bg_tasks
    assert [rec for rec in caplog.records if rec.levelno >= logging.ERROR] == []


# ---------------------------------------------------------------------------
# #215 (R4:test-5) — DisplayRenderer.run()'s loop had ZERO coverage: the
# QUIT/ESC stop paths, the dirty-reset-BEFORE-render ordering (the P-3 "goes
# quiet at steady state" mechanism, and the #208 settle), and the 30-vs-10fps
# cadence were all unpinned. These drive the REAL loop under the SDL dummy
# driver. Each test owns its renderer + start()/quit() because stop() calls
# pygame.quit(); the cadence/dirty tests end the loop by clearing _running from a
# fake asyncio.sleep (never a second pygame.event.get() after teardown).
# ---------------------------------------------------------------------------

async def test_run_stops_on_quit_event(tmp_path):
    import asyncio
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r.start()
    try:
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        await asyncio.wait_for(r.run(), timeout=2.0)   # returns via the QUIT arm
        assert r._running is False
    finally:
        pygame.display.quit()


async def test_run_stops_on_escape_key(tmp_path):
    import asyncio
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r.start()
    try:
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
        await asyncio.wait_for(r.run(), timeout=2.0)
        assert r._running is False
    finally:
        pygame.display.quit()


async def test_run_sleeps_at_30fps_while_transitioning(tmp_path, monkeypatch):
    import asyncio
    import time
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r.start()
    try:
        r._render = lambda: None                  # isolate cadence from rendering
        r._transition_start = time.monotonic()    # a LIVE transition → transitioning True
        intervals = []
        async def fake_sleep(interval):
            intervals.append(interval)
            r._running = False                    # end the loop after one iteration
        monkeypatch.setattr("src.display.renderer.asyncio.sleep", fake_sleep)
        await asyncio.wait_for(r.run(), timeout=2.0)
        assert intervals == [1 / 30]              # smooth 30fps during the lerp
    finally:
        pygame.display.quit()


async def test_run_sleeps_at_10fps_at_rest(tmp_path, monkeypatch):
    import asyncio
    import time
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r.start()
    try:
        r._render = lambda: None
        r._dirty = False
        r._transition_start = time.monotonic() - 10.0   # transition long over → at rest
        intervals = []
        async def fake_sleep(interval):
            intervals.append(interval)
            r._running = False
        monkeypatch.setattr("src.display.renderer.asyncio.sleep", fake_sleep)
        await asyncio.wait_for(r.run(), timeout=2.0)
        assert intervals == [1 / 10]              # easy 10fps when nothing animates
    finally:
        pygame.display.quit()


async def test_run_resets_dirty_before_render_so_a_frame_can_request_another(tmp_path, monkeypatch):
    # The loop resets self._dirty BEFORE calling _render(), so a render that sets
    # _dirty=True (a pulsing dot / spinner asking for the next frame) is honoured.
    # Moving the reset to AFTER _render() would make animation frames self-cancel
    # — the P-3 "freezes at steady state" regression. Pin the ordering: after one
    # iteration whose render re-requests a frame, _dirty must still be True.
    import asyncio
    import time
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r.start()
    try:
        r._transition_start = time.monotonic() - 10.0   # at rest, so only _dirty drives render
        r._dirty = True
        rendered = []
        def fake_render():
            rendered.append(True)
            r._dirty = True                       # this frame asks for another
        r._render = fake_render
        async def fake_sleep(interval):
            r._running = False
        monkeypatch.setattr("src.display.renderer.asyncio.sleep", fake_sleep)
        await asyncio.wait_for(r.run(), timeout=2.0)
        assert rendered == [True]                 # it rendered exactly once
        assert r._dirty is True                   # the render's re-request survived the reset
    finally:
        pygame.display.quit()
