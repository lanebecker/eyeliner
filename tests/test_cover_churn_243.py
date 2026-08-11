"""R5-21 (#243) — a cover whose download is pending or failed must not respawn a
decode task every render frame.

`_load_cover` runs each frame; on a cache miss it spawned `_decode_cover_async`,
which returned on `not cache_path.exists()` BEFORE claiming the inflight guard —
so a not-on-disk cover spawned ~10 tasks/s (each a blocking exists() stat on the
loop) for the whole track, and never retried after a failed download. The fix
gates the spawn on `_cover_on_disk`, which `_prefetch_cover` populates only once
the file lands; a failed download records a bounded failure tally and blacklists
past the bound.
"""
import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from src.config import DisplayConfig  # noqa: E402
from src.display.renderer import DisplayRenderer  # noqa: E402
from src.state.player_state import PlayerState  # noqa: E402


def _config(tmp_path):
    return DisplayConfig(
        width=1024, height=600, fullscreen=False,
        dynamic_theming=True, reduced_motion=False,
        cover_art_cache_dir=str(tmp_path / "cache"),
    )


_URL = "https://coverartarchive.org/release/x/front"


def test_load_cover_does_not_spawn_a_decode_when_cover_not_on_disk(tmp_path):
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    spawned = []
    r._spawn = lambda coro: (spawned.append(coro), coro.close(), None)[-1]

    for _ in range(50):
        assert r._load_cover(_URL, 300, 300) is None
    assert spawned == []          # RED before R5-21: 50 spawns


def test_load_cover_spawns_once_when_on_disk_then_inflight_dedups(tmp_path):
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._cover_on_disk.add(_URL)     # prefetch has landed the file
    count = {"n": 0}

    def fake_spawn(coro):
        count["n"] += 1
        coro.close()
        # emulate _decode_cover_async claiming the inflight guard
        r._cover_decode_inflight.add((_URL, 300, 300))
        return None

    r._spawn = fake_spawn
    for _ in range(50):
        r._load_cover(_URL, 300, 300)
    assert count["n"] == 1        # one decode, then the inflight guard holds


@pytest.mark.asyncio
async def test_prefetch_marks_cover_on_disk_on_a_warm_cache_hit(tmp_path):
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL                     # R6-22: it's the cover we want
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = True      # already on disk (warm cache)
    r._extract_palette_async = MagicMock(return_value=None)

    async def _noop(url):
        return None
    r._extract_palette_async = _noop

    await r._prefetch_cover(_URL)
    assert _URL in r._cover_on_disk


@pytest.mark.asyncio
async def test_prefetch_download_failure_backs_off_then_blacklists(tmp_path, monkeypatch):
    """R6-18: a DOWNLOAD failure backs off (time-based) rather than blacklisting
    within the album — a transient network blip must be able to self-heal mid-album
    (all tracks share one cover_art_url, so the old blacklist never lifted). Only a
    persistently-dead URL (past _COVER_MAX_DOWNLOAD_FAILURES) is given up on. It is
    never marked on-disk."""
    import src.display.renderer as renderer_mod
    from src.display.renderer import _COVER_MAX_DOWNLOAD_FAILURES
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = False
    r._cover_store.download.side_effect = OSError("network down")
    clock = {"t": 1000.0}
    monkeypatch.setattr(renderer_mod.time, "monotonic", lambda: clock["t"])

    # First failure: backs off, NOT blacklisted (mid-album self-heal must remain possible).
    r._cover_prefetch_inflight.discard(_URL)
    await r._prefetch_cover(_URL)
    assert _URL not in r._cover_bad_urls
    assert _URL in r._cover_download_retry_after

    # Within the backoff window a re-attempt is skipped (no extra download / churn).
    r._cover_store.download.reset_mock()
    r._cover_prefetch_inflight.discard(_URL)
    await r._prefetch_cover(_URL)
    r._cover_store.download.assert_not_called()

    # Advance past each backoff and keep failing → eventually given up on.
    for _ in range(_COVER_MAX_DOWNLOAD_FAILURES + 1):
        clock["t"] += renderer_mod._COVER_DOWNLOAD_RETRY_BACKOFF_SECONDS + 1
        r._cover_prefetch_inflight.discard(_URL)
        await r._prefetch_cover(_URL)

    assert _URL not in r._cover_on_disk
    assert _URL in r._cover_bad_urls


@pytest.mark.asyncio
async def test_maybe_retry_cover_download_re_attempts_once_the_backoff_elapses(tmp_path, monkeypatch):
    """R6-18 driver: the render loop's per-frame retry re-spawns the prefetch once
    the backoff window elapses — and NOT before, nor for a cover on disk /
    blacklisted / in flight."""
    import src.display.renderer as renderer_mod
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL
    clock = {"t": 1000.0}
    monkeypatch.setattr(renderer_mod.time, "monotonic", lambda: clock["t"])
    spawned = []
    r._spawn = lambda coro: (spawned.append(coro), coro.close(), None)[-1]

    # No pending backoff → nothing to retry.
    r._maybe_retry_cover_download()
    assert spawned == []

    # A pending backoff, still inside the window → no retry.
    r._cover_download_retry_after[_URL] = clock["t"] + 30.0
    r._maybe_retry_cover_download()
    assert spawned == []

    # Past the window → exactly one re-attempt.
    clock["t"] += 31.0
    r._maybe_retry_cover_download()
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_prefetch_reaffirms_readiness_if_discarded_during_extract(tmp_path):
    """R5-21 cold-review LOW: an X→Y→X state flip DURING the palette-extract await
    discards X from _cover_on_disk, and the re-spawned prefetch(X) is deduped —
    so prefetch must RE-AFFIRM readiness before its version bump, or the cover
    sticks on the placeholder."""
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL                  # R6-22: X is (still) wanted — the X→Y→X case
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = True   # warm cache: no download

    async def _extract_then_discard(url):
        # Simulate _on_state_change firing mid-await for a rapid X→Y→X flip.
        r._cover_on_disk.discard(url)
    r._extract_palette_async = _extract_then_discard

    await r._prefetch_cover(_URL)
    assert _URL in r._cover_on_disk     # re-affirmed despite the mid-await discard


@pytest.mark.asyncio
async def test_r6_22_abandoned_download_is_not_marked_on_disk(tmp_path):
    """R6-22: a download that completes for a cover NO LONGER wanted (the track
    changed mid-download) must not add a _cover_on_disk marker (which would never
    be discarded again — a slow leak) nor repaint for a cover off-screen."""
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = "https://coverartarchive.org/release/OTHER/front"  # a different cover
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = True    # warm cache (no download leg)

    async def _noop(u):
        return None
    r._extract_palette_async = _noop

    before = r._cover_version
    await r._prefetch_cover(_URL)                # _URL != wanted → abandoned
    assert _URL not in r._cover_on_disk          # not marked ready
    assert r._cover_version == before            # no repaint for an off-screen cover


@pytest.mark.asyncio
async def test_r6_23_download_failure_does_not_touch_the_decode_tally(tmp_path, monkeypatch):
    """R6-23: download and decode failures use SEPARATE tallies — a download blip
    must not consume the corrupt-decode path's bounded retry budget (which shared
    one tally would, blacklisting too early and misattributing the count)."""
    import src.display.renderer as renderer_mod
    monkeypatch.setattr(renderer_mod.time, "monotonic", lambda: 1000.0)
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = False
    r._cover_store.download.side_effect = OSError("network down")

    await r._prefetch_cover(_URL)
    assert r._cover_download_failures.get(_URL) == 1   # counted as a DOWNLOAD failure
    assert _URL not in r._cover_decode_failures        # the decode tally is untouched


@pytest.mark.asyncio
async def test_r6_23_clean_download_must_not_reset_the_decode_bound(tmp_path):
    """Cold-review HIGH regression: the corrupt-decode recovery unlinks + re-DOWNLOADS
    the file, so a cover that downloads clean but decodes corrupt (Pillow-accepts /
    SDL-rejects) would loop forever if a clean download reset the decode tally. A
    successful download must therefore NOT touch _cover_decode_failures — only a
    successful DECODE clears it."""
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = False          # so the download leg runs
    r._cover_store.download.return_value = None         # download SUCCEEDS

    async def _noop(u):
        return None
    r._extract_palette_async = _noop

    # A prior corrupt decode had already tallied a decode failure.
    r._cover_decode_failures[_URL] = 1
    await r._prefetch_cover(_URL)                        # clean re-download
    assert r._cover_decode_failures.get(_URL) == 1, (
        "a clean download reset the decode bound → the corrupt-decode refetch loop "
        "would never blacklist (STAB-1 storm)"
    )


def test_r6_17_extract_palette_returns_none_on_a_corrupt_image(tmp_path):
    """R6-17: extract_palette signals failure with None (not FALLBACK_PALETTE), so
    the caller can decline to cache it."""
    from src.display.palette import extract_palette
    p = tmp_path / "bad.png"
    p.write_bytes(b"this is not a valid image")
    assert extract_palette(p) is None


@pytest.mark.asyncio
async def test_r6_17_failed_palette_extraction_is_not_cached(tmp_path, monkeypatch):
    """R6-17: a transient palette-extraction failure must NOT be cached as the
    URL's palette — otherwise the cache-hit short-circuit never re-extracts and the
    theme stays FALLBACK even after the corrupt-cover refetch lands good bytes."""
    import src.display.renderer as renderer_mod
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._wanted_cover_url = _URL
    cp = r._cover_store.path_for(_URL)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_bytes(b"present-but-unreadable")     # on disk, so _extract_palette_async proceeds
    monkeypatch.setattr(renderer_mod, "extract_palette", lambda path: None)   # extraction fails

    await r._extract_palette_async(_URL)
    # The key must be ABSENT from the cache (not present-but-None): a None entry
    # would still leave the `is not None` cache-hit gate open, but it pollutes the
    # bounded cache and could evict a real palette. Assert true non-caching.
    assert _URL not in r._palette_cache            # NOT cached → a later good decode re-extracts


def test_cover_on_disk_is_bounded_across_distinct_covers(tmp_path):
    """R5-21: readiness markers don't accumulate one-per-cover forever — the
    outgoing wanted cover is dropped when a new one arrives."""
    from src.state.player_state import PlayerStatus
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._spawn = lambda coro: (coro.close(), None)[-1]   # don't run prefetch

    class _Track:
        def __init__(self, u): self.cover_art_url = u
    class _St:
        def __init__(self, u):
            self.status = PlayerStatus.PLAYING; self.current_track = _Track(u)

    # Simulate many distinct covers becoming ready and then superseded.
    for i in range(100):
        u = f"https://coverartarchive.org/release/{i}/front"
        r._cover_on_disk.add(u)          # prefetch would have marked it
        r._on_state_change(_St(u))       # a new track arrives → drop the outgoing
    assert len(r._cover_on_disk) <= 2    # bounded to the covers in play, not 100
