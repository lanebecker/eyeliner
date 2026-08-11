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

import asyncio  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from src.config import DisplayConfig  # noqa: E402
from src.display.renderer import DisplayRenderer, _COVER_MAX_LOAD_FAILURES  # noqa: E402
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
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = True      # already on disk (warm cache)
    r._extract_palette_async = MagicMock(return_value=None)

    async def _noop(url):
        return None
    r._extract_palette_async = _noop

    await r._prefetch_cover(_URL)
    assert _URL in r._cover_on_disk


@pytest.mark.asyncio
async def test_prefetch_download_failure_does_not_mark_on_disk_and_blacklists(tmp_path):
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = False
    r._cover_store.download.side_effect = OSError("network down")

    # Fail more than the bound → blacklisted; never marked on-disk.
    for _ in range(_COVER_MAX_LOAD_FAILURES + 1):
        r._cover_prefetch_inflight.discard(_URL)   # allow re-entry per attempt
        await r._prefetch_cover(_URL)

    assert _URL not in r._cover_on_disk
    assert _URL in r._cover_bad_urls


@pytest.mark.asyncio
async def test_prefetch_reaffirms_readiness_if_discarded_during_extract(tmp_path):
    """R5-21 cold-review LOW: an X→Y→X state flip DURING the palette-extract await
    discards X from _cover_on_disk, and the re-spawned prefetch(X) is deduped —
    so prefetch must RE-AFFIRM readiness before its version bump, or the cover
    sticks on the placeholder."""
    r = DisplayRenderer(_config(tmp_path), PlayerState())
    r._cover_store = MagicMock()
    r._cover_store.exists.return_value = True   # warm cache: no download

    async def _extract_then_discard(url):
        # Simulate _on_state_change firing mid-await for a rapid X→Y→X flip.
        r._cover_on_disk.discard(url)
    r._extract_palette_async = _extract_then_discard

    await r._prefetch_cover(_URL)
    assert _URL in r._cover_on_disk     # re-affirmed despite the mid-await discard


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
