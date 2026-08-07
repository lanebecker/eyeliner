"""Regression tests for B-12, B-17, B-18 (renderer robustness).

B-12 — extract_palette must not IndexError on a degenerate cover (solid colour
       / tiny image) that quantizes to fewer than 8 palette entries.
B-17 — the genre "+N" overflow chip must reflect how many chips ACTUALLY fit,
       not a fixed cap of 3.
B-18 — a corrupt cached cover is re-fetched within the track (not left as a
       placeholder until the next state change).
"""
import asyncio
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
from PIL import Image  # noqa: E402

from src.display.renderer import (  # noqa: E402
    DisplayRenderer, _BoundedCache, _COVER_MAX_LOAD_FAILURES,
)
from src.display.cover_cache import CoverArtCache  # noqa: E402
from src.display.palette import extract_palette  # noqa: E402
from src.display.layouts import get_now_playing_layout, Rect  # noqa: E402
from src.display.palette import DisplayPalette, FALLBACK_PALETTE  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _pygame_font():
    pygame.font.init()
    yield


def make_renderer():
    r = DisplayRenderer.__new__(DisplayRenderer)
    r._font_cache = _BoundedCache(64)   # P-8: matches the real bounded cache
    r._label_cache = _BoundedCache(64)
    r._dot_cache = _BoundedCache(64)
    # STAB-1 cover-loop state (mirrors DisplayRenderer.__init__)
    r._cover_load_failures = {}
    r._cover_bad_urls = set()
    r._cover_prefetch_inflight = set()
    r._cover_decode_inflight = set()
    r._cover_decode_deferred = False
    r._cover_version = 0
    return r


# ---------------------------------------------------------------------------
# B-12 — degenerate covers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size,color", [
    ((1, 1), (120, 40, 30)),      # 1×1
    ((80, 80), (20, 80, 160)),    # solid colour
    ((2, 2), (0, 0, 0)),          # tiny + pure black
])
def test_extract_palette_survives_degenerate_cover(tmp_path, size, color):
    p = tmp_path / "cover.png"
    Image.new("RGB", size, color).save(p)
    pal = extract_palette(p)           # must not raise
    assert isinstance(pal, DisplayPalette)
    # A real palette was derived, not the IndexError→FALLBACK degradation.
    assert pal is not FALLBACK_PALETTE


# ---------------------------------------------------------------------------
# B-17 — overflow reflects what fit
# ---------------------------------------------------------------------------

def test_genre_overflow_counts_what_actually_fit():
    r = make_renderer()
    layout = get_now_playing_layout(1024, 600)

    rendered = []
    wide_label = pygame.Surface((200, 20), pygame.SRCALPHA)  # 1 chip per row

    def fake_render(text, size, color, tracking):
        rendered.append(text)
        return wide_label

    r._render_tracked = fake_render
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)

    # A box only tall/wide enough for a single chip → only 1 genre fits.
    chips_rect = Rect(0, 0, 130, 26)
    r._draw_genre_chips(target, ["G1", "G2", "G3", "G4", "G5"], layout, FALLBACK_PALETTE,
                        chips_rect=chips_rect)

    # 1 genre fit → overflow must be "+4" (5 − 1), never the fixed-cap "+2".
    assert "+4" in rendered
    assert "+2" not in rendered


def test_genre_no_overflow_chip_when_all_fit():
    r = make_renderer()
    layout = get_now_playing_layout(1024, 600)
    rendered = []
    small_label = pygame.Surface((20, 16), pygame.SRCALPHA)
    r._render_tracked = lambda *a: (rendered.append(a[0]) or small_label)
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)

    chips_rect = Rect(0, 0, 1000, 200)  # plenty of room
    r._draw_genre_chips(target, ["Rock", "Jazz"], layout, FALLBACK_PALETTE,
                        chips_rect=chips_rect)

    assert rendered == ["Rock", "Jazz"]            # no overflow chip
    assert not any(t.startswith("+") for t in rendered)


# ---------------------------------------------------------------------------
# B-18 — corrupt cached cover is re-fetched within the track
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_corrupt_cached_cover_triggers_refetch(tmp_path):
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()

    url = "https://i.discogs.com/cover.jpg"
    cache_path = r._cover_store.path_for(url)
    cache_path.write_bytes(b"this is not a valid image")  # corrupt cover

    refetched = []

    async def fake_prefetch(u):
        refetched.append(u)

    r._prefetch_cover = fake_prefetch

    await r._decode_cover_async(url, 100, 100)

    assert r._cover_cache.get((url, 100, 100)) is None   # nothing cached (corrupt)
    assert not cache_path.exists()        # the corrupt file was unlinked
    await asyncio.sleep(0)                 # let the spawned re-fetch run
    assert refetched == [url]              # a re-fetch was scheduled in-track


@pytest.mark.asyncio
async def test_missing_cover_does_not_refetch(tmp_path):
    """A simply-absent cover (not yet downloaded) must NOT trigger a re-fetch —
    the off-loop decode early-returns on a missing file; the download path is
    owned by the state-change prefetch."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    refetched = []
    r._prefetch_cover = lambda u: refetched.append(u)  # noqa: E731

    url = "https://i.discogs.com/missing.jpg"
    assert r._load_cover(url, 100, 100) is None   # cache miss → placeholder
    await r._decode_cover_async(url, 100, 100)    # missing file → early return
    await asyncio.sleep(0)

    assert refetched == []


# ---------------------------------------------------------------------------
# STAB-1 — an un-decodable / display-faulted cover must NOT become an unbounded
#          download + unlink + log loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stab1_undecodable_cover_stops_after_one_refetch(tmp_path):
    """A cached cover that will NOT decode used to be unlinked + re-downloaded on
    every render frame (~8.7 Hz), re-landing the same bad bytes forever.  The
    refetch is now bounded: at most one unlink+refetch, then the URL is
    negative-cached and the disk/network/log loop stops."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/bad.jpg"
    cache_path = r._cover_store.path_for(url)
    cache_path.write_bytes(b"not a real image")

    spawns = []

    async def relanding_prefetch(u):
        spawns.append(u)
        cache_path.write_bytes(b"still not a real image")  # re-download re-lands bad bytes

    r._prefetch_cover = relanding_prefetch

    # Drive the off-loop decode repeatedly, as render-path cache misses would.
    for _ in range(6):
        await r._decode_cover_async(url, 100, 100)
        await asyncio.sleep(0)      # let the spawned refetch re-land the file

    assert len(spawns) <= 1, f"unbounded refetch loop: {len(spawns)} spawns"
    assert url in r._cover_bad_urls           # negative-cached → loop stopped
    # Once blacklisted, later frames early-return BEFORE re-attempting the decode,
    # so the failure tally stops climbing (no per-frame load / log.error spam).
    assert r._cover_load_failures.get(url) == _COVER_MAX_LOAD_FAILURES + 1


@pytest.mark.asyncio
async def test_stab1_display_fault_does_not_delete_a_good_cover(tmp_path):
    """A video-mode loss (HDMI hotplug / lost X) makes .convert() raise on a
    PERFECTLY GOOD cover.  That transient display fault must NOT be treated as a
    corrupt file: the good bytes must survive, no re-download is triggered, and
    the URL is not blacklisted."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/good.jpg"
    cache_path = r._cover_store.path_for(url)
    Image.new("RGB", (120, 120), (60, 110, 190)).save(cache_path)  # a VALID cover

    spawns = []

    async def fake_prefetch(u):
        spawns.append(u)

    r._prefetch_cover = fake_prefetch

    pygame.display.quit()   # no video mode → .convert() raises, whatever the test order
    await r._decode_cover_async(url, 100, 100)

    assert r._cover_cache.get((url, 100, 100)) is None
    assert cache_path.exists()             # a display fault must NOT delete a good file
    assert r._cover_decode_deferred is True   # …the defer flag latched (rate-limits the log)
    await asyncio.sleep(0)
    assert spawns == []                    # …and must NOT hammer the network
    assert url not in r._cover_bad_urls    # …and must NOT blacklist a good URL


@pytest.mark.asyncio
async def test_stab1_concurrent_prefetch_for_same_url_downloads_once(tmp_path, monkeypatch):
    """A state-change prefetch and a load-failure refetch for the SAME URL must
    not both hit the network — the second is deduped against the in-flight
    download."""
    import time

    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._bg_tasks = set()
    r.dynamic_theming = False        # skip palette extraction
    r._cover_version = 0
    r._dirty = False
    url = "https://i.discogs.com/x.jpg"

    downloads = []

    def counting_download(u):        # runs in the executor thread
        downloads.append(u)
        time.sleep(0.02)             # hold the download open so both tasks overlap

    monkeypatch.setattr(r._cover_store, "download", counting_download)
    monkeypatch.setattr(r._cover_store, "exists", lambda u: False)

    await asyncio.gather(r._prefetch_cover(url), r._prefetch_cover(url))

    assert downloads.count(url) == 1              # deduped — exactly one network fetch
    assert r._cover_prefetch_inflight == set()    # …and the in-flight guard cleared afterward


@pytest.mark.asyncio
async def test_stab1_transient_decode_failure_still_recovers(tmp_path):
    """Non-regression (B-18): a ONE-OFF decode failure (transient SD read) must
    still self-heal — unlink + one refetch, and when the re-download lands good
    bytes the cover loads cleanly and the URL is NOT permanently blacklisted."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/flaky.jpg"
    cache_path = r._cover_store.path_for(url)
    cache_path.write_bytes(b"corrupt just this once")

    async def healing_prefetch(u):
        Image.new("RGB", (100, 100), (30, 60, 90)).save(cache_path)  # good bytes this time

    r._prefetch_cover = healing_prefetch

    pygame.display.set_mode((64, 64))    # a video mode so a good cover can .convert()
    try:
        await r._decode_cover_async(url, 100, 100)      # first: corrupt → unlink + one refetch
        await asyncio.sleep(0)                          # healing refetch lands good bytes
        await r._decode_cover_async(url, 100, 100)      # now decodes cleanly
        assert r._cover_cache.get((url, 100, 100)) is not None
        assert url not in r._cover_bad_urls             # a transient glitch is NOT permanent
        assert url not in r._cover_load_failures        # tally cleared on a clean load
    finally:
        pygame.display.quit()


@pytest.mark.asyncio
async def test_stab1_deferred_flag_clears_when_display_returns(tmp_path):
    """The video-mode-loss defer state latches (so the warning is logged once,
    not per frame) and then CLEARS on the next clean decode — so a recovered
    display resumes normal per-frame logging behaviour."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/good.jpg"
    cache_path = r._cover_store.path_for(url)
    Image.new("RGB", (100, 100), (10, 20, 30)).save(cache_path)   # a VALID cover

    pygame.display.quit()                          # no video mode → .convert() fails
    await r._decode_cover_async(url, 100, 100)
    assert r._cover_decode_deferred is True        # latched (log emitted once)

    pygame.display.set_mode((64, 64))              # video mode returns
    try:
        await r._decode_cover_async(url, 100, 100)
        assert r._cover_cache.get((url, 100, 100)) is not None   # decodes cleanly now
        assert r._cover_decode_deferred is False    # …and the defer flag cleared
    finally:
        pygame.display.quit()


@pytest.mark.asyncio
async def test_stab1_unexpected_scale_error_fails_safe(tmp_path, monkeypatch):
    """An UNEXPECTED (non-pygame) error while converting/scaling already-decoded
    bytes must fail safe — placeholder, good file preserved, no refetch — and must
    NOT escape _load_cover to crash the unguarded render loop (cold-review #1)."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/good.jpg"
    cache_path = r._cover_store.path_for(url)
    Image.new("RGB", (100, 100), (5, 5, 5)).save(cache_path)   # a VALID cover

    spawns = []

    async def fake_prefetch(u):
        spawns.append(u)

    r._prefetch_cover = fake_prefetch

    def boom(surface, size):
        raise ValueError("simulated non-pygame scale failure")

    monkeypatch.setattr(pygame.transform, "smoothscale", boom)

    pygame.display.set_mode((64, 64))    # video mode so convert() succeeds → reach smoothscale
    try:
        await r._decode_cover_async(url, 100, 100)   # must NOT raise
        assert r._cover_cache.get((url, 100, 100)) is None
        assert cache_path.exists()               # good file preserved
        await asyncio.sleep(0)
        assert spawns == []                      # no refetch
        assert url not in r._cover_bad_urls      # not blacklisted
    finally:
        pygame.display.quit()


@pytest.mark.asyncio
async def test_stab1_persistent_scale_error_is_log_bounded(tmp_path, monkeypatch):
    """A PERSISTENT unexpected scale error must be latched like the video-mode
    case — logged ONCE per episode, not once per render frame (~10 Hz).  Without
    the latch the fail-safe ERROR branch becomes the very per-frame log flood
    STAB-1 eliminates, just moved to the scale path (cold-review narrow pass)."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/good.jpg"
    cache_path = r._cover_store.path_for(url)
    Image.new("RGB", (100, 100), (5, 5, 5)).save(cache_path)   # a VALID cover

    def boom(surface, size):
        raise ValueError("persistent non-pygame scale failure")

    monkeypatch.setattr(pygame.transform, "smoothscale", boom)
    errors = []
    monkeypatch.setattr("src.display.renderer.log.error", lambda *a, **k: errors.append(a))

    pygame.display.set_mode((64, 64))    # video mode so convert() succeeds → reach smoothscale
    try:
        for _ in range(10):              # 10 decode attempts on the same persistent fault
            await r._decode_cover_async(url, 100, 100)
    finally:
        pygame.display.quit()

    assert len(errors) == 1, f"unbounded ERROR log loop: {len(errors)} logs in 10 frames"
    assert r._cover_decode_deferred is True   # latched after the first attempt


# ---------------------------------------------------------------------------
# STAB-5 — the render-path _load_cover does NO blocking work; the decode is
#          moved off the event loop into _decode_cover_async.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_cover_does_not_decode_on_the_render_path(tmp_path, monkeypatch):
    """STAB-5: the per-frame _load_cover must NOT do the blocking SD read +
    decode itself (a worn card can stall pygame.image.load for seconds and
    freeze the whole event loop) — it returns the cache or None and defers the
    decode to an executor.  Proven by spying on pygame.image.load: it is NOT
    called on the calling thread during _load_cover, only later off-loop."""
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/x.jpg"
    Image.new("RGB", (80, 80), (40, 40, 40)).save(r._cover_store.path_for(url))

    import threading
    main_thread = threading.current_thread()
    load_threads = []
    real_load = pygame.image.load

    def spy(p):
        load_threads.append(threading.current_thread())
        return real_load(p)

    monkeypatch.setattr(pygame.image, "load", spy)

    pygame.display.set_mode((64, 64))
    try:
        result = r._load_cover(url, 100, 100)      # render-path call
        assert result is None                       # placeholder while it decodes
        assert load_threads == []                   # NO decode ran during _load_cover
        await asyncio.gather(*list(r._bg_tasks))    # run the off-loop decode
        assert len(load_threads) == 1
        assert load_threads[0] is not main_thread   # SD read ran OFF the event-loop thread
        assert r._cover_cache.get((url, 100, 100)) is not None
    finally:
        pygame.display.quit()


def test_load_cover_returns_cached_surface_without_scheduling(tmp_path):
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/cached.jpg"
    surf = pygame.Surface((10, 10))
    r._cover_cache.put((url, 100, 100), surf)
    assert r._load_cover(url, 100, 100) is surf     # cache hit
    assert r._bg_tasks == set()                     # …no decode scheduled


def test_load_cover_blacklisted_returns_none_without_scheduling(tmp_path):
    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._cover_cache = _BoundedCache(8)
    r._bg_tasks = set()
    url = "https://i.discogs.com/bad.jpg"
    r._cover_bad_urls.add(url)
    assert r._load_cover(url, 100, 100) is None
    assert r._bg_tasks == set()                     # blacklisted → no decode scheduled


def test_stab1_new_cover_state_change_lifts_blacklist(tmp_path):
    """A blacklist is not permanent for the whole process: a state change to a
    genuinely NEW wanted cover clears that URL's blacklist + tally, giving a
    later play a fresh bounded attempt (the finding's "until state changes")."""
    from src.state.player_state import PlayerState, PlayerStatus
    from src.metadata.models import TrackMetadata, MetadataSource

    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._bg_tasks = set()
    r._wanted_cover_url = "https://i.discogs.com/current.jpg"
    r._queue_palette = lambda u: None
    r._prefetch_cover = lambda u: None
    r._spawn = lambda coro: None

    url = "https://i.discogs.com/bad.jpg"
    r._cover_bad_urls.add(url)
    r._cover_load_failures[url] = _COVER_MAX_LOAD_FAILURES + 1

    st = PlayerState()
    st.current_track = TrackMetadata(
        title="t", artist="a", album="al", source=MetadataSource.FALLBACK,
        cover_art_url=url, tracklist=[],          # a NEW wanted cover
    )
    st.status = PlayerStatus.PLAYING

    r._on_state_change(st)

    assert url not in r._cover_bad_urls            # new wanted cover → blacklist lifted
    assert url not in r._cover_load_failures       # …and its failure tally reset


# ---------------------------------------------------------------------------
# DISP-8 — _spawn guards the running-loop requirement, so a synchronous
# PlayerState callback that runs without a loop degrades to a no-op instead of
# raising RuntimeError out of the display layer into the notifying pipeline
# ---------------------------------------------------------------------------

def test_disp8_spawn_without_a_running_loop_is_a_noop():
    """`_spawn` is reached from `_on_state_change`, a SYNCHRONOUS PlayerState
    callback that may run with no running event loop (an off-loop unit-test call,
    or a state change delivered before the loop starts). It must degrade to a
    no-op, not let `create_task` raise RuntimeError('no running event loop')."""
    import inspect

    r = make_renderer()
    r._bg_tasks = set()

    async def coro():
        return None

    c = coro()
    result = r._spawn(c)               # NO running loop here

    assert result is None              # nothing scheduled
    assert r._bg_tasks == set()
    # The un-started coroutine is closed, so it can't leak a "never awaited" warning.
    assert inspect.getcoroutinestate(c) == inspect.CORO_CLOSED


def test_disp8_on_state_change_off_loop_does_not_raise(tmp_path):
    """The integration: a PLAYING state change with a cover URL, delivered with NO
    running loop, must not raise out of the renderer's callback (DISP-8).  On the
    unguarded code create_task raised RuntimeError straight back into the caller."""
    from src.state.player_state import PlayerState, PlayerStatus
    from src.metadata.models import TrackMetadata, MetadataSource

    r = make_renderer()
    r._cover_store = CoverArtCache(tmp_path)
    r._bg_tasks = set()
    r._wanted_cover_url = None
    r._queue_palette = lambda u: None          # isolate the _spawn path
    r._listening_since = None

    st = PlayerState()
    st.current_track = TrackMetadata(
        title="t", artist="a", album="al", source=MetadataSource.FALLBACK,
        cover_art_url="https://i.discogs.com/x.jpg", tracklist=[],
    )
    st.status = PlayerStatus.PLAYING

    r._on_state_change(st)                      # must NOT raise (no running loop)
    assert r._bg_tasks == set()                # nothing scheduled off-loop


@pytest.mark.asyncio
async def test_disp8_spawn_under_a_running_loop_still_schedules():
    """The guard must not break the normal path: with a running loop, `_spawn`
    still schedules the task and tracks/reaps it (DISP-8)."""
    r = make_renderer()
    r._bg_tasks = set()
    ran = []

    async def coro():
        ran.append(True)

    task = r._spawn(coro())
    assert task is not None
    assert task in r._bg_tasks                 # tracked while running
    await task
    assert ran == [True]                       # actually scheduled + ran
    assert r._bg_tasks == set()                # done-callback reaped it


# ---------------------------------------------------------------------------
# P-10 — boot-arc rotation is cached by angle bucket (not re-rotated per frame)
# ---------------------------------------------------------------------------

def test_boot_arc_rotation_is_bucketed(monkeypatch):
    """The boot arc spins for the whole identification wait; rotating it every
    frame is wasteful.  Frames landing in the same angle bucket must reuse a
    cached rotated Surface (one rotate), and only a new bucket triggers another."""
    from src.display.renderer import (
        _BoundedCache as _BC,
        _ARC_ROT_BUCKETS,
        _ARC_ROT_CACHE_MAX,
        _ARC_SECS,
    )

    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = False
    r._arc_segment = None
    r._arc_rot_cache = _BC(_ARC_ROT_CACHE_MAX)

    layout = get_now_playing_layout(1024, 600)
    target = pygame.Surface((1024, 600))

    calls = []
    real_rotate = pygame.transform.rotate
    monkeypatch.setattr(
        pygame.transform, "rotate",
        lambda surf, angle: calls.append(angle) or real_rotate(surf, angle),
    )

    bucket_dt = _ARC_SECS / _ARC_ROT_BUCKETS
    # Two frames inside bucket 0 → exactly one rotate (second is a cache hit).
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, 0.0)
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, bucket_dt * 0.4)
    assert len(calls) == 1

    # A frame in bucket 1 → one additional rotate.
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, bucket_dt * 1.5)
    assert len(calls) == 2


def test_boot_arc_reduced_motion_never_rotates(monkeypatch):
    """With reduced_motion the arc is static — no rotation at all (and no cache
    churn), matching the design's prefers-reduced-motion translation."""
    from src.display.renderer import _BoundedCache as _BC, _ARC_ROT_CACHE_MAX

    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = True
    r._arc_segment = None
    r._arc_rot_cache = _BC(_ARC_ROT_CACHE_MAX)

    layout = get_now_playing_layout(1024, 600)
    target = pygame.Surface((1024, 600))

    calls = []
    real_rotate = pygame.transform.rotate
    monkeypatch.setattr(
        pygame.transform, "rotate",
        lambda surf, angle: calls.append(angle) or real_rotate(surf, angle),
    )

    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, 0.0)
    r._draw_boot_arc(target, layout, FALLBACK_PALETTE, 9.9)

    assert calls == []                 # never rotated
    assert len(r._arc_rot_cache) == 0  # and never cached


def test_static_frame_recomposes_when_cover_version_bumps(tmp_path):
    """B-22: a freshly-landed cover must force the now-playing static frame to
    recompose.  The static-frame key includes the monotonic _cover_version, so a
    bump changes the key even when the on-screen `cover` object is identical
    (the old id(cover) token could be GC-recycled and falsely match a stale
    frame).  Renders with no cover file on disk (cover=None both times), so ONLY
    the version token can distinguish the two keys."""
    from src.display.cover_cache import CoverArtCache
    from src.display.renderer import (
        _BoundedCache as _BC, _PALETTE_CACHE_MAX, _COVER_CACHE_MAX,
        _LABEL_CACHE_MAX, _DOT_CACHE_MAX, _FONT_CACHE_MAX,
    )
    from src.state.player_state import PlayerState, PlayerStatus
    from src.metadata.models import TrackMetadata, MetadataSource

    r = DisplayRenderer.__new__(DisplayRenderer)
    r.width, r.height = 1024, 600
    r.reduced_motion = True
    r.dynamic_theming = False
    r._layout = get_now_playing_layout(1024, 600)
    r._screen = pygame.Surface((1024, 600))
    r._font_cache = _BC(_FONT_CACHE_MAX)
    r._label_cache = _BC(_LABEL_CACHE_MAX)
    r._dot_cache = _BC(_DOT_CACHE_MAX)
    r._cover_cache = _BC(_COVER_CACHE_MAX)
    r._palette_cache = _BC(_PALETTE_CACHE_MAX)
    r._cover_store = CoverArtCache(tmp_path)
    r._gradient_key = None
    r._gradient_surface = None
    # Pre-seed the cover-shadow cache so _cover_shadow returns early instead of
    # calling convert_alpha (which needs an initialized display); this test only
    # cares about the static-frame KEY, not the shadow pixels.
    _ca = r._layout.cover_art
    r._shadow_key = (_ca.w, _ca.h)
    r._shadow_surface = pygame.Surface((_ca.w + 200, _ca.h + 200), pygame.SRCALPHA)
    r._static_key = None
    r._static_surface = None
    r._arc_segment = None
    r._current_palette = FALLBACK_PALETTE
    r._target_palette = FALLBACK_PALETTE
    r._transition_start = 0.0
    r._cover_version = 0
    r._cover_load_failures = {}          # STAB-1 cover-loop state
    r._cover_bad_urls = set()
    r._cover_prefetch_inflight = set()
    r._cover_decode_inflight = set()
    r._cover_decode_deferred = False
    r._dirty = False

    state = PlayerState()
    state.current_track = TrackMetadata(
        title="So What", artist="Miles Davis", album="Kind of Blue",
        source=MetadataSource.FALLBACK,
        cover_art_url="https://i.discogs.com/x.jpg",  # never written to disk → cover=None
        tracklist=[],
    )
    state.status = PlayerStatus.PLAYING
    r.state = state

    r._render_now_playing()
    key_before = r._static_key
    assert key_before is not None

    r._cover_version += 1          # a cover for this track just landed
    r._render_now_playing()
    key_after = r._static_key

    assert key_before != key_after  # the static frame recomposed (B-22)


def test_draw_genre_chips_requires_chips_rect():
    """ARCH-9: chips_rect is a REQUIRED parameter now — the dead
    layout.genre_chips fallback (never taken; the sole caller always passes a
    rect) was removed. Omitting it must fail loudly, not silently fall back."""
    import pygame
    r = make_renderer()
    layout = get_now_playing_layout(1024, 600)
    target = pygame.Surface((1024, 600), pygame.SRCALPHA)
    with pytest.raises(TypeError):
        r._draw_genre_chips(target, ["Rock"], layout, FALLBACK_PALETTE)
