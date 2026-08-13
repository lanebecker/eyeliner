"""R8 Wave 3 (#356–#360) — cover pipeline residue.

R8-05/#356: the #306 sweep only ran on a DIRECT track change; the common album
boundary (PLAYING→IDLE→PLAYING, ≥45s of silence between records) nulled
`_wanted_cover_url` first, so the previous album's three failure-bookkeeping
entries survived forever — v1.5.26's "no longer grows unbounded" didn't hold on
the common path.

R8-11/#357: the #305 draft box (1600) required a minor axis ≥3200 to engage, so
near-square oversized scans (3400×3100) were permanently blacklisted (blank
cover) while the comment claimed only "unusual wide covers" were affected.  Box
is now 800: engages at minor ≥1600, reduced decode bounded at 2.56 MP (4×
stronger). (The downscale-matrix tests live in test_cover_cache.py.)

R8-12/#358: the #304 type-only throttle key hid a genuinely NEW error condition
of the same class for up to 30s and blurred mixed tallies; the key is now
(type, first message word after the errno bracket).

R8-18/#359 (E1): every cover is normalized to ≤NORMALIZED_COVER_SIDE RGB JPEG
at cache-write, and a one-shot startup sweep normalizes legacy files — killing
the Pi-scaled ~0.4–0.7s per-decode on-loop convert+smoothscale stall and the
~100MB palette executor decodes for 3000×3000-class scans.

R8-26/#360: the two event-loop `exists()` stats (prefetch, palette) moved
behind the executor (a dying SD card can block a stat for seconds).
"""
import asyncio
import os

import pytest
from PIL import Image

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

import src.display.renderer as rmod  # noqa: E402
import src.display.palette as palette  # noqa: E402
from src.display.cover_cache import CoverArtCache  # noqa: E402
from src.state.player_state import PlayerState  # noqa: E402
from src.metadata.models import MetadataSource, TrackMetadata  # noqa: E402
from tests.test_renderer_robustness import make_renderer  # noqa: E402


@pytest.fixture(autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((64, 64))
    yield


def _track(cover_url):
    return TrackMetadata(
        title="T", artist="A", album="B",
        source=MetadataSource.DISCOGS_COLLECTION, cover_art_url=cover_url,
    )


def _state_renderer(monkeypatch):
    r = make_renderer()
    r._palette_cache = rmod._BoundedCache(8)
    monkeypatch.setattr(r, "_spawn", lambda coro: coro.close(), raising=False)
    monkeypatch.setattr(r, "_queue_palette", lambda url: None, raising=False)
    return r


# ---------------------------------------------------------------------------
# R8-05 (#356) — the IDLE-path sweep
# ---------------------------------------------------------------------------

def test_r8_05_idle_path_sweeps_the_outgoing_covers_bookkeeping(monkeypatch):
    """RED before R8-05: A→IDLE→B left all three of A's entries forever."""
    r = _state_renderer(monkeypatch)
    A = "https://i.discogs.com/A.jpg"
    r._wanted_cover_url = A
    r._cover_download_failures[A] = 2
    r._cover_download_retry_after[A] = 12345.0
    r._cover_decode_failures[A] = 1

    state = PlayerState()
    state.clear()                                  # → IDLE (the album boundary)
    r._on_state_change(state)
    state.set_track(_track("https://i.discogs.com/B.jpg"))
    r._on_state_change(state)                      # next record starts

    assert A not in r._cover_download_failures
    assert A not in r._cover_download_retry_after
    assert A not in r._cover_decode_failures


def test_r8_05_blacklist_is_not_swept_on_idle(monkeypatch):
    """Same policy as the PLAYING-branch sweep: `_cover_bad_urls` persists (the
    accepted STAB-1 residual — a permanently-bad cover stays blacklisted)."""
    r = _state_renderer(monkeypatch)
    A = "https://i.discogs.com/A.jpg"
    r._wanted_cover_url = A
    r._cover_bad_urls.add(A)

    state = PlayerState()
    state.clear()
    r._on_state_change(state)

    assert A in r._cover_bad_urls


# ---------------------------------------------------------------------------
# R8-12 (#358) — throttle key distinguishes conditions, not just classes
# ---------------------------------------------------------------------------

def test_r8_12_distinct_conditions_of_one_class_get_distinct_keys():
    from src.audio.capture import AudioCapture
    k1 = AudioCapture._capture_error_key(OSError("[Errno -9985] Device unavailable"))
    k2 = AudioCapture._capture_error_key(OSError("[Errno -9997] Invalid sample rate"))
    assert k1 != k2, "two different failure conditions must not share a key"
    assert k1 == "OSError:Device" and k2 == "OSError:Invalid"


def test_r8_12_portaudioerror_shape_distinguishes_conditions():
    """W3 cold-review F3: sounddevice's PortAudioError formats as
    'Error opening InputStream: <condition> [PaErrorCode N]' — a constant
    first word.  The first cut keyed every one of them 'PortAudioError:Error'
    (type-only in effect, for the DOMINANT capture error class)."""
    from src.audio.capture import AudioCapture

    class PortAudioError(Exception):
        pass

    k1 = AudioCapture._capture_error_key(
        PortAudioError("Error opening InputStream: Device unavailable [PaErrorCode -9985]"))
    k2 = AudioCapture._capture_error_key(
        PortAudioError("Error opening InputStream: Invalid sample rate [PaErrorCode -9997]"))
    assert k1 == "PortAudioError:Device" and k2 == "PortAudioError:Invalid"
    assert k1 != k2


def test_r8_12_varying_detail_does_not_mint_new_keys():
    """The #304 property is preserved: a changing device index / errno deeper in
    the message must NOT create per-variant keys (the pre-#304 flood/LRU bug)."""
    from src.audio.capture import AudioCapture
    keys = {
        AudioCapture._capture_error_key(OSError(f"[Errno -{n}] Device unavailable: hw:{n}"))
        for n in range(20)
    }
    assert keys == {"OSError:Device"}


def test_r8_12_new_condition_reports_immediately():
    """RED before R8-12: 'Invalid sample rate' arriving 1s after 'Device
    unavailable' was invisible for the rest of the 30s window."""
    from src.audio.capture import AudioCapture
    r = AudioCapture.__new__(AudioCapture)
    from src.util.logthrottle import LogThrottle
    r._capture_error_throttle = LogThrottle(30.0, per_message=True)
    import logging
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    h = H()
    logging.getLogger("src.audio.capture").addHandler(h)
    try:
        r._log_capture_error(OSError("[Errno -9985] Device unavailable"))
        r._log_capture_error(OSError("[Errno -9997] Invalid sample rate"))
    finally:
        logging.getLogger("src.audio.capture").removeHandler(h)

    assert any("Invalid sample rate" in m for m in records), (
        "a genuinely new condition must surface immediately, not after 30s"
    )


# ---------------------------------------------------------------------------
# R8-18 (#359, E1) — cache-write normalization + legacy sweep
# ---------------------------------------------------------------------------

def test_r8_18_normalize_shrinks_a_typical_scan_and_converts_cmyk(tmp_path):
    p = tmp_path / "scan.jpg"
    Image.new("CMYK", (3000, 3000), (10, 20, 30, 0)).save(p, "JPEG", quality=85)
    assert palette.normalize_cover_image(str(p)) is True
    with Image.open(p) as im:
        assert max(im.size) <= palette.NORMALIZED_COVER_SIDE
        assert im.mode == "RGB" and im.format == "JPEG"


def test_r8_18_already_normalized_cover_is_a_header_read_noop(tmp_path):
    p = tmp_path / "thumb.jpg"
    Image.new("RGB", (600, 600), (10, 20, 30)).save(p, "JPEG")
    before = p.read_bytes()
    assert palette.normalize_cover_image(str(p)) is False
    assert p.read_bytes() == before


def test_r8_18_download_wiring_normalizes_at_write(tmp_path, monkeypatch):
    """Pin the WIRING (the R7-14 lesson): CoverArtCache.download() itself — with
    the network leg mocked exactly like test_cover_cache's download tests —
    must leave a ≤NORMALIZED_COVER_SIDE RGB JPEG on disk for a typical
    3000×3000 in-cap scan.  Deleting the download-path normalize call fails
    HERE, not just in the unit tests of the helper."""
    import io
    import src.display.cover_cache as cc
    from tests.test_cover_cache import _FakeResp, _make_store

    buf = io.BytesIO()
    Image.new("RGB", (3000, 3000), (90, 90, 140)).save(buf, "JPEG", quality=85)
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "93.184.216.34"))
    resp = _FakeResp(headers={"Content-Type": "image/jpeg"}, body=buf.getvalue())
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    out = store.download("https://i.discogs.com/bigscan.jpg")

    with Image.open(out) as im:
        assert max(im.size) <= palette.NORMALIZED_COVER_SIDE
        assert im.mode == "RGB" and im.format == "JPEG"


def test_r8_18_sweep_normalizes_legacy_files_and_drops_corrupt(tmp_path):
    cache = CoverArtCache(tmp_path / "covers")
    legacy = cache.cache_dir / "aaaa.jpg"
    Image.new("RGB", (2400, 2400), (50, 60, 70)).save(legacy, "JPEG", quality=85)
    ok = cache.cache_dir / "bbbb.jpg"
    Image.new("RGB", (500, 500), (1, 2, 3)).save(ok, "JPEG")
    ok_bytes = ok.read_bytes()
    corrupt = cache.cache_dir / "cccc.jpg"
    corrupt.write_bytes(b"not an image at all")

    touched = cache.sweep_legacy_oversized()

    with Image.open(legacy) as im:
        assert max(im.size) <= palette.NORMALIZED_COVER_SIDE
    assert ok.read_bytes() == ok_bytes, "an already-normalized file is untouched"
    assert not corrupt.exists(), "an undecodable legacy file is dropped"
    assert touched == 2


@pytest.mark.asyncio
async def test_r8_18_renderer_spawns_the_sweep_once_at_loop_start(tmp_path, monkeypatch):
    """The R7-14 lesson: pin the WIRING — deleting the run()-start spawn must
    fail a test, not just slow the Pi."""
    from tests.test_display_survival_r8 import _loop_renderer
    r = _loop_renderer(monkeypatch)
    swept = {"n": 0}

    async def counting_sweep():
        swept["n"] += 1

    monkeypatch.setattr(r, "_sweep_legacy_covers", counting_sweep, raising=False)

    real_sleep = asyncio.sleep
    frames = {"n": 0}

    async def capped_sleep(_delay):
        frames["n"] += 1
        if frames["n"] >= 2:
            r._running = False
        await real_sleep(0)

    monkeypatch.setattr(r, "_render", lambda: None, raising=False)
    monkeypatch.setattr("pygame.display.flip", lambda: None)
    monkeypatch.setattr("asyncio.sleep", capped_sleep)
    await asyncio.wait_for(r.run(), timeout=2)

    assert swept["n"] == 1, "run() must spawn the legacy sweep exactly once"


# ---------------------------------------------------------------------------
# W3 cold-review catches (F1/F2/F4/F5) — regression pins
# ---------------------------------------------------------------------------

def test_f1_sweep_keeps_a_good_cover_on_a_transient_disk_error(tmp_path, monkeypatch):
    """Cold-review F1 (caught pre-commit): the first cut wrapped EVERY failure
    into PermanentCoverError, so the sweep DELETED a good, decodable cover when
    the disk hiccuped (ENOSPC/EIO at save) — on the exact flaky-SD hardware
    #360 exists for.  errno-carrying OSErrors now propagate; the sweep skips
    and retries next boot."""
    import errno
    cache = CoverArtCache(tmp_path / "covers")
    legacy = cache.cache_dir / "aaaa.jpg"
    Image.new("RGB", (2400, 2400), (50, 60, 70)).save(legacy, "JPEG", quality=85)

    real_replace = os.replace

    def enospc_replace(src, dst):
        if str(dst).endswith("aaaa.jpg"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(palette.os, "replace", enospc_replace)
    touched = cache.sweep_legacy_oversized()

    assert legacy.exists(), "a transient disk error must never delete a good cover"
    with Image.open(legacy) as im:
        assert im.size == (2400, 2400), "the original must be untouched"
    assert touched == 0
    assert not list(cache.cache_dir.glob("*.norm-part")), (
        "the failed rewrite's tempfile must be cleaned up (F4)"
    )


def test_f1_racing_prune_is_skipped_not_condemned(tmp_path):
    """F6: a file vanishing mid-sweep (racing prune) is a FileNotFoundError
    (errno) — skip, never the 'dropping undecodable' delete branch."""
    import logging
    cache = CoverArtCache(tmp_path / "covers")
    ghost = cache.cache_dir / "gone.jpg"
    Image.new("RGB", (2400, 2400), (1, 2, 3)).save(ghost, "JPEG")

    real_downscale = palette.downscale_oversized_image

    def vanish_then_call(path):
        os.unlink(path)                    # the racing prune
        return real_downscale(path)

    import src.display.cover_cache as cc
    orig = cc.downscale_oversized_image
    cc.downscale_oversized_image = vanish_then_call
    try:
        records = []

        class H(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        h = H()
        logging.getLogger("src.display.cover_cache").addHandler(h)
        try:
            touched = cache.sweep_legacy_oversized()
        finally:
            logging.getLogger("src.display.cover_cache").removeHandler(h)
    finally:
        cc.downscale_oversized_image = orig

    assert touched == 0
    assert not any("dropping undecodable" in m for m in records), (
        "a racing prune must take the transient-skip path, not the delete branch"
    )


def test_f2_both_bomb_limit_mutators_serialize_on_the_lock(tmp_path, monkeypatch):
    """Cold-review F2: Image.MAX_IMAGE_PIXELS is a process global mutated to
    DIFFERENT bounds by validate (10.24MP) and downscale (36MP); unsynchronized
    executor threads raced and a legitimate 25MP legacy file bombed out under
    the wrong bound (and the sweep then deleted it).  Pin that both critical
    sections hold _BOMB_LIMIT_LOCK."""
    class RecordingLock:
        def __init__(self):
            self.acquires = 0
            self.releases = 0

        def acquire(self):
            self.acquires += 1

        def release(self):
            self.releases += 1

        def __enter__(self):            # the locked probe uses `with`
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    lock = RecordingLock()
    monkeypatch.setattr(palette, "_BOMB_LIMIT_LOCK", lock)

    small = tmp_path / "small.jpg"
    Image.new("RGB", (100, 100), (5, 5, 5)).save(small, "JPEG")
    palette.validate_image_file(str(small))
    assert (lock.acquires, lock.releases) == (1, 1), "validate must hold the lock"

    big = tmp_path / "big.jpg"
    Image.new("RGB", (3400, 3100), (5, 5, 5)).save(big, "JPEG", quality=85)
    # 2nd-pass fix: downscale = LOCKED probe (Pillow's bomb check fires at
    # open time) + locked decode body = 2 more acquire/release pairs.
    palette.downscale_oversized_image(str(big))
    assert (lock.acquires, lock.releases) == (3, 3), (
        "downscale must hold the lock for BOTH the header probe and the decode"
    )

    ok = tmp_path / "ok.jpg"
    Image.new("RGB", (900, 900), (5, 5, 5)).save(ok, "JPEG")
    palette.normalize_cover_image(str(ok))
    assert lock.acquires == lock.releases >= 4, "normalize's probe must be locked too"


def test_f4_norm_part_orphans_are_swept_at_construction(tmp_path):
    """Cold-review F4: a hard kill mid-save strands a .norm-part that nothing
    matched — not _sweep_partials, not the *.jpg disk bound, not the legacy
    sweep.  Construction now sweeps them."""
    d = tmp_path / "covers"
    d.mkdir(parents=True)
    (d / "aaaa.jpg.norm-part").write_bytes(b"stranded")
    (d / ".cover-xyz.part").write_bytes(b"old-style orphan")

    CoverArtCache(d)

    assert not list(d.glob("*.norm-part")), "construction must sweep .norm-part orphans"
    assert not list(d.glob(".cover-*.part"))


def test_f5_sweep_leaves_a_working_oversized_legacy_png_in_place(tmp_path):
    """Cold-review F5: a legacy oversized non-JPEG (legal under the pre-v1.5.26
    36MP cap) RENDERS today — deleting it would blank a working cover whose URL
    then re-downloads into a permanent modern-policy reject.  The sweep skips
    it (it keeps rendering at its old cost)."""
    cache = CoverArtCache(tmp_path / "covers")
    png = cache.cache_dir / "bigpng.jpg"      # cache paths are always *.jpg
    Image.new("RGB", (4000, 4000), (20, 30, 40)).save(png, "PNG")  # 16MP > cap

    touched = cache.sweep_legacy_oversized()

    assert png.exists(), "a rendering legacy cover must not be deleted"
    with Image.open(png) as im:
        assert im.size == (4000, 4000)
    assert touched == 0
    surf = pygame.image.load(str(png))        # …and it does render
    assert surf.get_width() == 4000


# ---------------------------------------------------------------------------
# R8-26 (#360) — the two stats run off the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_26_prefetch_and_palette_stat_off_the_loop(tmp_path, monkeypatch):
    """Both coroutines must route their existence stat through run_in_executor
    (pinned by counting executor dispatches of a callable resolving to the
    stat), and still behave correctly."""
    r = make_renderer()
    r._bg_tasks = set()
    r._palette_cache = rmod._BoundedCache(8)
    r.dynamic_theming = True
    r._cover_store = CoverArtCache(tmp_path / "covers")
    url = "https://i.discogs.com/x.jpg"
    p = r._cover_store.path_for(url)
    Image.new("RGB", (300, 300), (200, 40, 40)).save(p, "JPEG")

    dispatched = []
    loop = asyncio.get_running_loop()
    real_rie = loop.run_in_executor

    def counting_rie(executor, fn, *args):
        dispatched.append(getattr(fn, "__name__", repr(fn)))
        return real_rie(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", counting_rie)
    monkeypatch.setattr(r, "_queue_palette", lambda u: None, raising=False)

    await r._extract_palette_async(url)
    assert "exists" in dispatched, "the palette stat must run in the executor"
    assert r._palette_cache.get(url) is not None, "palette still extracted"

    dispatched.clear()
    r._wanted_cover_url = url
    r._cover_version = 0
    await r._prefetch_cover(url)
    assert "exists" in dispatched, "the prefetch stat must run in the executor"
    assert url in r._cover_on_disk, "prefetch still marks readiness"
