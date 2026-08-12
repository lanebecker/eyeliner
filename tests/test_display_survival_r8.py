"""R8-06 (#353) + R8-07 (#354) — the display survives a video-loss episode.

R8-06: while ``convert()`` fails (video-mode loss), the `_cover_decode_deferred`
latch used to suppress only the LOG — each failed decode task cleared the
inflight guard and left the URL in `_cover_on_disk`, so `_load_cover`
re-spawned a full JPEG decode + SD read EVERY frame (~10 Hz) for the whole
fault episode (executed: 30 frames → 30 decodes).  The latch now gates the
WORK: one probe per `_COVER_DECODE_RETRY_SECONDS`, deadline re-armed on every
failed attempt, cleared on a clean decode / a new cover.

R8-07: a ``pygame.error`` escaping the per-frame render/flip used to fault the
display leg → FIRST_COMPLETED → whole-pipeline exit → systemd restart loop on
one flaky HDMI cable.  ``run()`` now logs once per episode, slows to ~1
attempt/s, re-tries ``set_mode`` every `_RENDER_FAULT_REINIT_SECONDS`, logs
recovery — and still lets NON-pygame exceptions kill the leg (fail-fast on
genuine bugs unchanged).
"""
import asyncio
import os
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

import src.display.renderer as rmod  # noqa: E402
from tests.test_renderer_robustness import make_renderer  # noqa: E402


@pytest.fixture(autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((64, 64))
    yield


def _renderer_with_cover(tmp_path, monkeypatch):
    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = rmod._BoundedCache(8)
    url = "https://i.discogs.com/fault.jpg"
    r._cover_on_disk = {url}
    r._wanted_cover_url = url   # F3P-1: only the WANTED cover may latch the episode
    tasks = []
    monkeypatch.setattr(r, "_spawn", lambda coro: tasks.append(asyncio.ensure_future(coro)), raising=False)
    p = tmp_path / "fault.jpg"
    pygame.image.save(pygame.Surface((100, 100)), str(p))

    class Store:
        def path_for(self, _url):
            return p

    r._cover_store = Store()
    return r, url, tasks


# ---------------------------------------------------------------------------
# R8-06 — the deferred latch gates the WORK
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_06_convert_fault_episode_decodes_once_not_per_frame(tmp_path, monkeypatch):
    """RED before R8-06: 30 render frames during a convert() fault → 30 full
    decodes.  Now: the first failure latches + arms the retry deadline, and
    every subsequent frame inside the window skips the spawn — ≤2 decodes."""
    r, url, tasks = _renderer_with_cover(tmp_path, monkeypatch)
    loads = {"n": 0}
    real_load = pygame.image.load

    def counting_load(path):
        loads["n"] += 1
        return real_load(path)

    monkeypatch.setattr(pygame.image, "load", counting_load)
    monkeypatch.setattr(pygame.transform, "smoothscale",
                        lambda *a: (_ for _ in ()).throw(pygame.error("no video mode")))

    for _ in range(30):                      # 30 simulated frames
        r._load_cover(url, 40, 40)
        await asyncio.sleep(0.01)

    for t in tasks:
        t.cancel()
    assert loads["n"] <= 2, f"decode storm: {loads['n']} decodes over 30 fault frames"
    assert r._cover_decode_deferred is True
    assert r._cover_decode_retry_at > time.monotonic()   # deadline armed


@pytest.mark.asyncio
async def test_r8_06_deadline_rearms_on_every_failed_probe(tmp_path, monkeypatch):
    """After the deadline elapses, exactly one probe runs and RE-ARMS the
    deadline — the gate must not fall open permanently after the first window."""
    r, url, tasks = _renderer_with_cover(tmp_path, monkeypatch)
    monkeypatch.setattr(pygame.transform, "smoothscale",
                        lambda *a: (_ for _ in ()).throw(pygame.error("no video mode")))
    r._load_cover(url, 40, 40)
    await asyncio.sleep(0.01)                        # first failure: latch + arm
    first_deadline = r._cover_decode_retry_at
    r._cover_decode_retry_at = time.monotonic() - 0.1   # window elapses
    r._load_cover(url, 40, 40)                       # one probe spawns
    await asyncio.sleep(0.01)
    assert r._cover_decode_retry_at > time.monotonic(), "probe failure must re-arm"
    assert r._cover_decode_retry_at != first_deadline
    for t in tasks:
        t.cancel()


@pytest.mark.asyncio
async def test_r8_06_clean_decode_clears_latch_and_new_cover_clears_deadline(tmp_path, monkeypatch):
    r, url, tasks = _renderer_with_cover(tmp_path, monkeypatch)
    monkeypatch.setattr(pygame.transform, "smoothscale",
                        lambda *a: (_ for _ in ()).throw(pygame.error("no video mode")))
    r._load_cover(url, 40, 40)
    await asyncio.sleep(0.01)
    assert r._cover_decode_deferred is True
    monkeypatch.undo()                                # display "returns"
    pygame.init(); pygame.display.set_mode((64, 64))  # monkeypatch.undo tore env vars? no — restore mocks only
    r._cover_decode_retry_at = 0.0                    # window elapsed
    r._load_cover(url, 40, 40)
    await asyncio.sleep(0.05)
    assert r._cover_decode_deferred is False, "a clean decode must clear the latch"
    assert r._cover_cache.get((url, 40, 40)) is not None
    for t in tasks:
        t.cancel()


@pytest.mark.asyncio
async def test_f1_probe_is_clock_driven_not_render_driven(tmp_path, monkeypatch):
    """Cold-review F1 (caught pre-commit): under reduced_motion the now-playing
    frame doesn't self-dirty, so a render-driven probe never fired after a
    fault episode's last frame — the cover stayed a placeholder for the rest
    of the track.  run() itself must re-arm a frame once the retry deadline
    elapses (O(1) clock check per iteration)."""
    r, url, tasks = _renderer_with_cover(tmp_path, monkeypatch)
    r._running = True
    r._dirty = False                          # reduced_motion-like: nothing self-dirties
    r._transition_start = 0.0
    r.width, r.height, r.fullscreen = 64, 64, False
    r._static_key = None
    monkeypatch.setattr(r, "_maybe_retry_cover_download", lambda: None, raising=False)
    monkeypatch.setattr(r, "_render", lambda: r._load_cover(url, 40, 40), raising=False)
    monkeypatch.setattr("pygame.event.get", lambda: [])
    # A latched episode whose retry deadline has ALREADY elapsed:
    r._cover_decode_deferred = True
    r._cover_decode_retry_at = time.monotonic() - 0.1

    real_sleep = asyncio.sleep
    frames = {"n": 0}

    async def capped_sleep(_delay):
        frames["n"] += 1
        # Stop as soon as the probe's decode lands (or give up at 50 turns —
        # the executor round-trip needs a few real scheduler turns).
        if not r._cover_decode_deferred or frames["n"] >= 50:
            r._running = False
        await real_sleep(0.01)

    monkeypatch.setattr("asyncio.sleep", capped_sleep)
    await asyncio.wait_for(r.run(), timeout=5)

    # The loop must have re-armed a frame and driven the probe: the display is
    # healthy in this test, so the probe decodes cleanly and clears the latch.
    assert r._cover_decode_deferred is False, (
        "the run loop never drove the elapsed-deadline probe (F1): with no "
        "self-dirtying frames the cover would stay a placeholder forever"
    )
    assert r._cover_cache.get((url, 40, 40)) is not None
    for t in tasks:
        t.cancel()


# ---------------------------------------------------------------------------
# R8-07 — run() survives pygame.error; non-pygame stays fatal
# ---------------------------------------------------------------------------

def _loop_renderer(monkeypatch):
    r = make_renderer()
    r._running = True
    r._dirty = True
    r._transition_start = 0.0
    r.width, r.height, r.fullscreen = 64, 64, False
    r._static_key = None
    monkeypatch.setattr(r, "_maybe_retry_cover_download", lambda: None, raising=False)
    monkeypatch.setattr("pygame.event.get", lambda: [])
    return r


@pytest.mark.asyncio
async def test_r8_07_pygame_error_does_not_kill_the_loop(monkeypatch, caplog):
    """RED before R8-07: the first pygame.error propagated out of run() (the
    pipeline died).  Now the loop survives, logs ONCE per episode, and keeps
    attempting frames."""
    import logging
    r = _loop_renderer(monkeypatch)
    attempts = {"n": 0}

    def boom():
        attempts["n"] += 1
        if attempts["n"] >= 3:
            r._running = False               # end the test after 3 attempts
        raise pygame.error("blit on lost surface")

    monkeypatch.setattr(r, "_render", boom, raising=False)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)                  # collapse the 1s fault cadence

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(r.run(), timeout=2)

    assert attempts["n"] >= 3                # survived repeated faults
    episode_logs = [rec for rec in caplog.records if "Per-frame render failed" in rec.message]
    assert len(episode_logs) == 1, "one warning per episode, not per frame"


@pytest.mark.asyncio
async def test_r8_07_reinit_attempted_after_deadline_and_recovery_logged(monkeypatch, caplog):
    import logging
    r = _loop_renderer(monkeypatch)
    state = {"n": 0, "reinits": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise pygame.error("lost surface")
        r._running = False                   # second attempt succeeds → recovery

    monkeypatch.setattr(r, "_render", flaky, raising=False)
    monkeypatch.setattr("pygame.display.flip", lambda: None)

    def counting_set_mode(size, flags=0):
        state["reinits"] += 1
        return pygame.Surface(size)

    monkeypatch.setattr("pygame.display.set_mode", counting_set_mode)
    monkeypatch.setattr(rmod, "_RENDER_FAULT_REINIT_SECONDS", 0.0)   # immediate
    # Cold-review F2: seed a NON-None static key so the "forced recompose"
    # assertion below actually pins the reinit path's clear (a None seed made
    # it vacuous — the mutant deleting the clear survived).
    r._static_key = ("sentinel",)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(r.run(), timeout=2)

    assert state["reinits"] >= 1, "set_mode re-init must be attempted after the deadline"
    assert any("Display recovered" in rec.message for rec in caplog.records)
    assert r._static_key is None, "reinit must force a static-frame recompose (F2)"


@pytest.mark.asyncio
async def test_f3_event_pump_pygame_error_survives_too(monkeypatch, caplog):
    """Cold-review F3: pygame.event.get() itself raises pygame.error once the
    video subsystem dies ("video system not initialized") — it must ride the
    same survival policy, not kill the pipeline from outside the render try."""
    import logging
    r = _loop_renderer(monkeypatch)
    r._dirty = False                          # isolate the pump path
    pumps = {"n": 0}

    def pump_boom():
        pumps["n"] += 1
        if pumps["n"] >= 3:
            r._running = False
        raise pygame.error("video system not initialized")

    monkeypatch.setattr("pygame.event.get", pump_boom)
    # The 2nd-pass fix routes pump faults into the render try — give the
    # skeleton a harmless render so the survival-under-pump-faults property
    # stays isolated (reaching reinit/recovery is pinned by the next test).
    monkeypatch.setattr(r, "_render", lambda: None, raising=False)
    monkeypatch.setattr("pygame.display.flip", lambda: None)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(r.run(), timeout=2)

    assert pumps["n"] >= 3, "the loop must survive event-pump faults"
    assert any("Event pump failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_f3_pump_only_fault_reaches_reinit_and_recovers(monkeypatch, caplog):
    """2nd-pass fix: a PUMP-only fault on a static frame (nothing self-dirties)
    must still flow into the render try — where the set_mode reinit and the
    recovery logging live — instead of pinning the loop in a permanent 1s
    fault cadence that never re-initializes and never ends."""
    import logging
    r = _loop_renderer(monkeypatch)
    r._dirty = False                          # static frame: only the pump fires
    state = {"pumps": 0, "reinits": 0, "renders": 0}

    def flaky_pump():
        state["pumps"] += 1
        if state["pumps"] == 1:
            raise pygame.error("video system not initialized")
        return []                             # pump healthy from the 2nd call

    monkeypatch.setattr("pygame.event.get", flaky_pump)

    def render_ok():
        state["renders"] += 1
        r._running = False               # one successful frame ends the test
        # (the recovery log still fires later in this same iteration)

    monkeypatch.setattr(r, "_render", render_ok, raising=False)
    monkeypatch.setattr("pygame.display.flip", lambda: None)

    def counting_set_mode(size, flags=0):
        state["reinits"] += 1
        return pygame.Surface(size)

    monkeypatch.setattr("pygame.display.set_mode", counting_set_mode)
    monkeypatch.setattr(rmod, "_RENDER_FAULT_REINIT_SECONDS", 0.0)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(r.run(), timeout=2)

    assert state["renders"] >= 1, (
        "a pump fault must dirty a frame so the episode reaches the render try"
    )
    assert any("Display recovered" in rec.message for rec in caplog.records), (
        "a pump-started episode must END once frames succeed again"
    )


@pytest.mark.asyncio
async def test_2p_probe_rearms_itself_when_no_decode_path_exists(monkeypatch):
    """2nd-pass fix: a latched episode with NO decode path (cover not on disk —
    new download pending / vanished file) must cost ONE probe frame per window,
    not a permanent ~10fps dirty loop of an unchanged frame."""
    r = _loop_renderer(monkeypatch)
    r._dirty = False
    r._cover_decode_deferred = True
    r._cover_decode_retry_at = time.monotonic() - 0.1   # elapsed
    renders = {"n": 0}

    def counting_render():
        renders["n"] += 1

    monkeypatch.setattr(r, "_render", counting_render, raising=False)
    monkeypatch.setattr("pygame.display.flip", lambda: None)

    real_sleep = asyncio.sleep
    frames = {"n": 0}

    async def capped_sleep(_delay):
        frames["n"] += 1
        if frames["n"] >= 8:
            r._running = False
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", capped_sleep)
    await asyncio.wait_for(r.run(), timeout=2)

    assert renders["n"] == 1, (
        f"probe must re-arm its own deadline: {renders['n']} renders over 8 "
        f"iterations (want exactly 1 — one probe per window)"
    )
    assert r._cover_decode_retry_at > time.monotonic()


@pytest.mark.asyncio
async def test_3p_stale_tasks_convert_failure_does_not_latch_the_episode(tmp_path, monkeypatch):
    """3rd-pass F3P-1: an inflight decode for a cover that is NO LONGER wanted
    (the track changed mid-decode) failing convert must NOT latch the global
    episode flag — a cover-specific failure would otherwise gate the NEW
    cover's first decode ~5s on a healthy display."""
    r, url, tasks = _renderer_with_cover(tmp_path, monkeypatch)
    r._wanted_cover_url = "https://i.discogs.com/the-new-one.jpg"   # url is now stale
    monkeypatch.setattr(pygame.transform, "smoothscale",
                        lambda *a: (_ for _ in ()).throw(pygame.error("no video mode")))

    await r._decode_cover_async(url, 40, 40)

    assert r._cover_decode_deferred is False, (
        "a stale task's failure must not latch the episode (F3P-1)"
    )
    assert r._cover_decode_retry_at == 0.0
    for t in tasks:
        t.cancel()


@pytest.mark.asyncio
async def test_2p_new_cover_state_change_clears_the_episode_latch(tmp_path, monkeypatch):
    """2nd-pass fix: switching to a NEW cover while a fault episode is latched
    must clear `_cover_decode_deferred` (not just the deadline) — otherwise the
    F1 probe stays armed with no decode path while the download pends."""
    from src.state.player_state import PlayerState
    from src.metadata.models import MetadataSource, TrackMetadata

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = rmod._BoundedCache(8)
    r._palette_cache = rmod._BoundedCache(8)
    monkeypatch.setattr(r, "_spawn", lambda coro: coro.close(), raising=False)
    monkeypatch.setattr(r, "_queue_palette", lambda url: None, raising=False)
    r._dirty = False
    r._cover_decode_deferred = True
    r._cover_decode_retry_at = time.monotonic() + 99.0
    r._wanted_cover_url = "https://i.discogs.com/old.jpg"

    state = PlayerState()
    state.set_track(TrackMetadata(
        title="T", artist="A", album="B",
        source=MetadataSource.DISCOGS_COLLECTION,
        cover_art_url="https://i.discogs.com/new.jpg",
    ))
    r._on_state_change(state)

    assert r._cover_decode_deferred is False, (
        "a new cover must clear the fault-episode latch"
    )
    assert r._cover_decode_retry_at == 0.0


@pytest.mark.asyncio
async def test_r8_07_non_pygame_exception_is_still_fatal(monkeypatch):
    """Fail-fast on genuine bugs is unchanged: only pygame.error is survivable."""
    r = _loop_renderer(monkeypatch)

    def bug():
        raise RuntimeError("genuine bug")

    monkeypatch.setattr(r, "_render", bug, raising=False)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(r.run(), timeout=2)
