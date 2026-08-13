"""R9 Wave 2 (#385–#394) — display correctness.

R9-04/#385  fallback-font em-box match: a mixed-script line's composite surface
  has the SAME baseline and (within a pixel) height as a Latin-only line, so
  the accent divider no longer strikes hero descenders and a Cyrillic/CJK album
  title sits in its measured slot.  Fixed by loading the fallback face at the
  size whose ascent matches the primary's.
R9-16/#391  _CompositeFont.size() and render() compute height with the same
  baseline arithmetic (they had diverged).
R9-05/#386  the outgoing-cover sweep runs on ANY change of the wanted URL,
  including PLAYING→PLAYING(no-cover) — the third unswept branch of the #306
  bookkeeping class.
R9-09/#387  _decode_cover_async's two exists() stats and _handle_corrupt_cover's
  unlink run off the event loop.
R9-10/#388  a pump-only video fault does not flap WARNING+recovered pairs.
R9-18/#392  the decode-fault latch clears on entering an empty state.
"""
import asyncio
import os

import numpy as np
import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

import src.display.typography as typo  # noqa: E402
from src.display.renderer import _BoundedCache  # noqa: E402
from src.state.player_state import PlayerState  # noqa: E402
from src.metadata.models import MetadataSource, TrackMetadata  # noqa: E402
from tests.test_renderer_robustness import make_renderer  # noqa: E402


@pytest.fixture(autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((64, 64))
    yield


def _tr():
    return typo.TextRenderer(_BoundedCache(32), _BoundedCache(32))


def _ink_rows(surf):
    a = pygame.surfarray.pixels_alpha(surf)
    rows = np.where(a.any(axis=0))[0]
    return (int(rows.min()), int(rows.max())) if len(rows) else (None, None)


# ---------------------------------------------------------------------------
# R9-04 (#385) — em-box match
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (typo._FALLBACK_DIR / typo._FALLBACK_FONT_FILES["display"]).exists(),
    reason="Noto fallback files not present",
)
def test_r9_04_mixed_hero_matches_latin_height_and_baseline():
    """RED before R9-04: a mixed hero surface was ~17px taller than Latin-only
    and its Latin ink was pushed ~14px down (the divider then struck the
    descenders)."""
    f = _tr().font("display", 72)
    mixed = f.render("energy 東京", True, (255, 255, 255))
    latin = f.render("energy", True, (255, 255, 255))
    assert abs(mixed.get_height() - latin.get_height()) <= 1, (
        f"mixed height {mixed.get_height()} vs latin {latin.get_height()}"
    )
    # The Latin word's ink must sit at the same rows whether or not a CJK run
    # follows it (no push-down).
    latin_in_mixed = _ink_rows(f.render("energy", True, (255, 255, 255)))
    assert latin_in_mixed == _ink_rows(latin)


@pytest.mark.skipif(
    not (typo._FALLBACK_DIR / typo._FALLBACK_FONT_FILES["title"]).exists(),
    reason="Noto fallback files not present",
)
def test_r9_04_cyrillic_album_fits_its_cell():
    """A Cyrillic album title (Newsreader-Italic lacks Cyrillic → Noto fallback)
    renders within the primary's cell height, not ~13px taller."""
    f = _tr().font("title", 32)
    cyr = f.render("Кино", True, (255, 255, 255))
    lat = f.render("Sister", True, (255, 255, 255))
    assert cyr.get_height() <= lat.get_height() + 1


@pytest.mark.skipif(
    not (typo._FALLBACK_DIR / typo._FALLBACK_FONT_FILES["title"]).exists(),
    reason="Noto fallback files not present",
)
def test_r9_04_fallback_still_renders_real_glyphs_not_tofu():
    """Regression guard: the em-box scaling must not reintroduce tofu."""
    f = _tr().font("title", 32)
    notdef = pygame.image.tobytes(f.render("", True, (255, 255, 255)), "RGBA")
    tofu = [c for c in "Кино坂本"
            if pygame.image.tobytes(f.render(c, True, (255, 255, 255)), "RGBA") == notdef]
    assert tofu == []


def test_r9_04_ascii_and_single_face_paths_unaffected():
    """The scaling only touches the fallback face; a pure-ASCII render is
    byte-identical to the primary's."""
    f = _tr().font("display", 72)
    composite = f.render("Abbey Road", True, (240, 240, 240))
    primary = f._primary.render("Abbey Road", True, (240, 240, 240))
    assert pygame.image.tobytes(composite, "RGBA") == pygame.image.tobytes(primary, "RGBA")


# ---------------------------------------------------------------------------
# R9-16 (#391) — size()/render() height agree
# ---------------------------------------------------------------------------

class _StubFace:
    """A minimal pygame.font.Font stand-in with controllable ascent/height, so
    a composite run can have a HIGH ascent + LOW descent alongside one with a
    LOW ascent + HIGH descent — the exact geometry where render()'s baseline
    height (max_ascent + max_descent) diverges from the naive max-of-heights."""

    def __init__(self, ascent: int, height: int):
        self._a, self._h = ascent, height

    def get_ascent(self):
        return self._a

    def get_height(self):
        return self._h

    def size(self, s):
        return (max(1, len(s)) * 7, self._h)

    def render(self, s, antialias, color, background=None):
        surf = pygame.Surface((max(1, len(s)) * 7, self._h), pygame.SRCALPHA)
        if background is not None:
            surf.fill(background)
        return surf


def test_r9_16_size_height_matches_render_baseline_arithmetic_when_ascents_differ():
    """R9-16: size() must mirror render()'s BASELINE arithmetic, not the naive
    max-of-run-heights.  With the em-box match (R9-04) the bundled faces have
    equal ascent so the two agree trivially and the mutation is unreachable —
    this pins the divergent case directly.  prim: ascent 70 / descent 5;
    fb: ascent 40 / descent 25.  render() height = 70 + 25 = 95; the naive max
    would be max(75, 65) = 75."""
    from src.display.typography import _CompositeFont

    prim = _StubFace(ascent=70, height=75)   # descent 5
    fb = _StubFace(ascent=40, height=65)      # descent 25
    comp = _CompositeFont(prim, fb, {ord("A")}, {ord("Я")})
    # "AЯ" → primary run 'A' (tallest ascent) + fallback run 'Я' (deepest descent)
    assert comp.size("AЯ") == (14, 95), "size() must equal render()'s baseline height"
    r = comp.render("AЯ", True, (255, 255, 255))
    assert (r.get_width(), r.get_height()) == comp.size("AЯ")
    # sanity: the naive max (what the mutation reverts to) really would disagree
    assert max(prim.get_height(), fb.get_height()) == 75 != 95


@pytest.mark.skipif(
    not (typo._FALLBACK_DIR / typo._FALLBACK_FONT_FILES["display"]).exists(),
    reason="Noto fallback files not present",
)
def test_r9_16_size_matches_render_for_real_bundled_mixed_runs():
    f = _tr().font("display", 72)
    for text in ("Кино Rocks", "energy 東京", "坂本龍一", "plain ascii"):
        assert f.size(text) == (
            f.render(text, True, (255, 255, 255)).get_width(),
            f.render(text, True, (255, 255, 255)).get_height(),
        )


# ---------------------------------------------------------------------------
# R9-05 (#386) — the third unswept branch
# ---------------------------------------------------------------------------

def _state_renderer(monkeypatch):
    r = make_renderer()
    r._palette_cache = _BoundedCache(8)
    monkeypatch.setattr(r, "_spawn", lambda coro: coro.close(), raising=False)
    monkeypatch.setattr(r, "_queue_palette", lambda url: None, raising=False)
    return r


def _track(cover_url):
    return TrackMetadata(title="T", artist="A", album="B",
                         source=MetadataSource.DISCOGS_COLLECTION, cover_art_url=cover_url)


def test_r9_05_playing_to_no_cover_sweeps_the_outgoing_bookkeeping(monkeypatch):
    """RED before R9-05: PLAYING(cover A) → PLAYING(track with no artwork) left
    A's on_disk marker + three failure dicts forever (the sweep block was
    url-gated)."""
    r = _state_renderer(monkeypatch)
    A = "https://i.discogs.com/A.jpg"
    r._wanted_cover_url = A
    r._cover_on_disk.add(A)
    r._cover_download_failures[A] = 2
    r._cover_download_retry_after[A] = 999.0
    r._cover_decode_failures[A] = 1

    state = PlayerState()
    state.set_track(_track(None))            # PLAYING, but no cover art
    r._on_state_change(state)

    assert A not in r._cover_on_disk
    assert A not in r._cover_download_failures
    assert A not in r._cover_download_retry_after
    assert A not in r._cover_decode_failures
    assert r._wanted_cover_url is None


def test_r9_05_blacklist_survives_the_no_cover_sweep(monkeypatch):
    r = _state_renderer(monkeypatch)
    A = "https://i.discogs.com/A.jpg"
    r._wanted_cover_url = A
    r._cover_bad_urls.add(A)
    state = PlayerState()
    state.set_track(_track(None))
    r._on_state_change(state)
    assert A in r._cover_bad_urls


def test_r9_05_empty_string_cover_is_treated_as_none(monkeypatch):
    """`cover_art_url == ""` normalizes to None (one no-cover case)."""
    r = _state_renderer(monkeypatch)
    A = "https://i.discogs.com/A.jpg"
    r._wanted_cover_url = A
    r._cover_download_failures[A] = 1
    state = PlayerState()
    state.set_track(_track(""))
    r._on_state_change(state)
    assert A not in r._cover_download_failures
    assert r._wanted_cover_url is None


# ---------------------------------------------------------------------------
# R9-18 (#392) — IDLE clears the decode-fault latch
# ---------------------------------------------------------------------------

def test_r9_18_idle_clears_the_decode_fault_latch(monkeypatch):
    r = _state_renderer(monkeypatch)
    r._cover_decode_deferred = True
    r._cover_decode_retry_at = 12345.0
    state = PlayerState()
    state.clear()                            # → IDLE
    r._on_state_change(state)
    assert r._cover_decode_deferred is False
    assert r._cover_decode_retry_at == 0.0


# ---------------------------------------------------------------------------
# R9-09 (#387) — decode-path I/O off the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r9_09_decode_exists_stat_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """R9-09: the pre-decode existence stat must run on an EXECUTOR thread, not
    the event-loop (main) thread — pinned by recording the thread Path.exists
    runs on (a sync revert would run it on the main thread)."""
    import threading
    from PIL import Image
    from src.display.cover_cache import CoverArtCache

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = _BoundedCache(8)
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/x.jpg"
    p = r._cover_store.path_for(url)
    Image.new("RGB", (80, 80), (200, 40, 40)).save(p, "JPEG")
    r._cover_on_disk.add(url)

    main_thread = threading.current_thread()
    threads = []
    orig_exists = type(p).exists

    def tracking_exists(self):
        threads.append(threading.current_thread())
        return orig_exists(self)

    monkeypatch.setattr(type(p), "exists", tracking_exists)
    await r._decode_cover_async(url, 40, 40)

    assert threads, "the pre-decode stat must have called exists()"
    assert all(t is not main_thread for t in threads), (
        "the existence stat ran on the event-loop thread (must be in the executor)"
    )
    assert r._cover_cache.get((url, 40, 40)) is not None, "cover still decodes"


@pytest.mark.asyncio
async def test_r9_09_corrupt_cover_unlink_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """R9-09: the corrupt-cover unlink (a WRITE) must run on an executor thread,
    and complete BEFORE the refetch spawns (ordering)."""
    import threading
    from src.display.cover_cache import CoverArtCache

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/bad.jpg"
    p = r._cover_store.path_for(url)
    p.write_bytes(b"not a decodable image")   # corrupt bytes

    main_thread = threading.current_thread()
    unlink_threads = []
    orig_unlink = type(p).unlink

    def tracking_unlink(self, *a, **k):
        unlink_threads.append(threading.current_thread())
        return orig_unlink(self, *a, **k)

    monkeypatch.setattr(type(p), "unlink", tracking_unlink)
    spawned = {"after_unlink": None}
    monkeypatch.setattr(
        r, "_spawn",
        lambda coro: (spawned.__setitem__("after_unlink", not p.exists()), coro.close()),
        raising=False,
    )

    await r._handle_corrupt_cover(url, p, ValueError("corrupt"))

    assert len(unlink_threads) == 1, "the corrupt file must be unlinked exactly once"
    assert unlink_threads[0] is not main_thread, "the unlink must run in the executor"
    assert spawned["after_unlink"] is True, "the refetch spawns AFTER the unlink completed"
    assert not p.exists()


@pytest.mark.asyncio
async def test_r9_09_vanished_recheck_stat_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """R9-09: the SECOND exists() stat — the vanished-vs-corrupt re-check in the
    decode except handler (R7-13) — must also run off the loop thread.  Only the
    corrupt-load path reaches it, so it needs its own decode; the good-cover test
    above pins the pre-decode stat, this pins the re-check."""
    import threading
    from src.display.cover_cache import CoverArtCache

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = _BoundedCache(8)
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/bad.jpg"
    p = r._cover_store.path_for(url)
    p.write_bytes(b"not a decodable image")   # exists, but pygame.image.load raises
    r._cover_on_disk.add(url)

    main_thread = threading.current_thread()
    recheck_threads = []
    orig_exists = type(p).exists
    # The pre-decode stat sees the file present and passes; count only the stats
    # that run AFTER the load has raised (the vanished re-check).
    load_raised = {"v": False}

    def boom_load(path):
        load_raised["v"] = True
        raise pygame.error("corrupt")

    def tracking_exists(self):
        if load_raised["v"]:
            recheck_threads.append(threading.current_thread())
        return orig_exists(self)

    monkeypatch.setattr(pygame.image, "load", boom_load)
    monkeypatch.setattr(type(p), "exists", tracking_exists)
    monkeypatch.setattr(r, "_spawn", lambda coro: coro.close(), raising=False)

    await r._decode_cover_async(url, 40, 40)

    assert recheck_threads, "the vanished re-check exists() must have run"
    assert all(t is not main_thread for t in recheck_threads), (
        "the vanished re-check ran on the event-loop thread (must be in the executor)"
    )


@pytest.mark.asyncio
async def test_r9_09_concurrent_decodes_of_same_cover_dedup_across_the_exists_await(
    tmp_path, monkeypatch
):
    """R9-09 cold-review regression (INTRODUCED by moving exists() off-loop): the
    inflight guard must be claimed BEFORE the off-loop exists() await, or two
    frames racing 100ms apart each pass the still-empty check and spawn a
    DUPLICATE decode.  A SLOW stat (the dying-card case the fix targets) widens
    the window.  Pinned by making exists() slow and running two decodes
    concurrently: exactly one pygame.image.load must occur."""
    import time as _time
    from PIL import Image
    from src.display.cover_cache import CoverArtCache

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = _BoundedCache(8)
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/x.jpg"
    p = r._cover_store.path_for(url)
    Image.new("RGB", (80, 80), (10, 120, 200)).save(p, "JPEG")
    r._cover_on_disk.add(url)

    orig_exists = type(p).exists

    def slow_exists(self):
        _time.sleep(0.15)   # runs in the executor thread — models a slow-card stat
        return orig_exists(self)

    loads = {"n": 0}
    orig_load = pygame.image.load

    def counting_load(path):
        loads["n"] += 1
        return orig_load(path)

    monkeypatch.setattr(type(p), "exists", slow_exists)
    monkeypatch.setattr(pygame.image, "load", counting_load)

    # Two overlapping decodes of the SAME cover, as two 10 fps frames would spawn.
    await asyncio.gather(
        r._decode_cover_async(url, 40, 40),
        r._decode_cover_async(url, 40, 40),
    )

    assert loads["n"] == 1, f"same cover decoded {loads['n']}× — inflight dedup raced the await"
    assert r._cover_cache.get((url, 40, 40)) is not None
    assert (url, 40, 40) not in r._cover_decode_inflight, "the claim must be released"


@pytest.mark.asyncio
async def test_r9_09_vanished_predecode_branch_releases_the_inflight_claim(
    tmp_path, monkeypatch
):
    """R9-09 cold-review regression: the pre-decode exists()==False branch returns
    BEFORE the decode body's try/finally, so it must discard the early inflight
    claim itself — otherwise a warm-start cover whose file was LRU-pruned leaks
    its key in _cover_decode_inflight forever, and _load_cover can never re-decode
    it once the refetch lands (permanently blank cover until process restart)."""
    from src.display.cover_cache import CoverArtCache

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = _BoundedCache(8)
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/pruned.jpg"
    # marked on disk (warm-start) but the file was pruned → exists() is False
    r._cover_on_disk.add(url)
    monkeypatch.setattr(r, "_spawn", lambda coro: coro.close(), raising=False)

    key = (url, 40, 40)
    await r._decode_cover_async(url, 40, 40)

    assert key not in r._cover_decode_inflight, (
        "the vanished pre-decode branch leaked the inflight claim"
    )
    assert url not in r._cover_on_disk, "the stale on-disk marker must be dropped (R7-12)"


@pytest.mark.asyncio
async def test_r9_09_exists_await_raising_releases_the_inflight_claim(tmp_path, monkeypatch):
    """R9-09 2nd-pass regression: the off-loop exists() stat can RAISE on a
    failing card (pathlib does NOT swallow EIO), and that await sits before the
    decode body.  A single try/finally spanning from the claim must release the
    key even on this escape — else the OSError strands the key and locks this
    cover out of every future decode until restart (worse than the dup-decode
    bug the claim relocation fixed)."""
    import errno
    from src.display.cover_cache import CoverArtCache

    r = make_renderer()
    r._bg_tasks = set()
    r._cover_cache = _BoundedCache(8)
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/eio.jpg"
    key = (url, 40, 40)
    p = r._cover_store.path_for(url)
    r._cover_on_disk.add(url)

    def boom_exists(self):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(type(p), "exists", boom_exists)

    with pytest.raises(OSError):
        await r._decode_cover_async(url, 40, 40)

    assert key not in r._cover_decode_inflight, (
        "an OSError from the exists() await stranded the inflight claim"
    )


# ---------------------------------------------------------------------------
# R9-10 (#388) — pump-fault no-flap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r9_10_pump_only_fault_does_not_flap_recovery(monkeypatch, caplog):
    """RED before R9-10: a render succeeding while the pump still faults cleared
    fault_since and logged 'recovered', so the next iteration reopened the
    episode → WARNING+recovered pairs every iteration.  Recovery must wait until
    the pump is also clean."""
    import logging
    from tests.test_display_survival_r8 import _loop_renderer

    r = _loop_renderer(monkeypatch)
    r._dirty = False
    state = {"pumps": 0}

    def always_faulting_pump():
        state["pumps"] += 1
        if state["pumps"] >= 6:
            r._running = False               # stop after several iterations
        raise pygame.error("video system not initialized")

    monkeypatch.setattr("pygame.event.get", always_faulting_pump)
    monkeypatch.setattr(r, "_render", lambda: None, raising=False)
    monkeypatch.setattr("pygame.display.flip", lambda: None)

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(r.run(), timeout=2)

    warnings = [rec for rec in caplog.records if "Event pump failed" in rec.getMessage()]
    recovered = [rec for rec in caplog.records if "Display recovered" in rec.getMessage()]
    assert len(warnings) == 1, "the pump WARNING is once per episode, not per frame"
    assert recovered == [], "no recovery may be declared while the pump still faults"
