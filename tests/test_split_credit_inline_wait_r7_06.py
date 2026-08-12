"""R7-06 — a creditable album-split whose credit honours a Discogs Retry-After
must not stall the recognition pipeline.

`on_track_identified` (the recognition leg that processes audio chunks) used to
`await asyncio.shield(task)` the split-credit finalize UNBOUNDED. When that
finalize honoured a Retry-After (#229 — up to _HONORED_RETRY_AFTER_CAP_SECONDS,
and up to _FINALIZE_WRITE_ATTEMPTS-1 times ⇒ ~180s), the leg blocked for the
whole wait: no chunks processed, the maxsize-5 queue draining ~50s of audio and
losing the next record's early tracks.

The fix bounds the inline wait to `_SPLIT_CREDIT_INLINE_WAIT_SECONDS` and lets a
slow credit finish in the background (it is in `_bg_tasks`, drained at shutdown).
The credit still lands; only the recognition leg is freed.
"""
import asyncio
import time

import pytest

import src.tracking.listen_tracker as lt
from src.audio.silence import AudioEvent
from src.metadata.discogs.transport import DiscogsRateLimited
from src.metadata.models import MetadataSource, TrackMetadata, TracklistEntry
from tests.test_listen_tracker import make_tracker, make_track


def _foreign(title):
    return TrackMetadata(
        title=title, artist="Other Band", album="Record Two",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=555, discogs_instance_id=556,
        tracklist=[TracklistEntry("A1", "X1"), TracklistEntry("A2", "X2")],
    )


async def _drain(tracker):
    if tracker._bg_tasks:
        await asyncio.gather(*list(tracker._bg_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_split_credit_honoring_retry_after_does_not_stall_the_leg(monkeypatch):
    """RED before R7-06: the swap's `on_track_identified` returns promptly even
    though the split credit honours a (here 0.4s) Retry-After — the honoured wait
    is served in the background, not inline on the recognition leg."""
    # Tiny inline bound so the test is fast; the honoured wait (0.4s) exceeds it.
    monkeypatch.setattr(lt, "_SPLIT_CREDIT_INLINE_WAIT_SECONDS", 0.05)

    tracker, writer = make_tracker()
    # First credit attempt honours a Retry-After, the retry succeeds.
    writer.increment_play_count.side_effect = [DiscogsRateLimited(0.4), True]

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))   # support
    await tracker.on_track_identified(make_track("Master-Dik"))       # closer → arms

    t0 = time.monotonic()
    await tracker.on_track_identified(_foreign("X1"))                 # swap → split credit
    elapsed = time.monotonic() - t0

    # DETERMINISTIC signal (no wall-clock threshold): when the leg returns, the
    # credit is still honouring its Retry-After in the background — the first
    # attempt has raised (1 call) but the retry has NOT yet landed. Had the leg
    # blocked on the unbounded await, the whole finalize would be done here (2).
    assert writer.increment_play_count.call_count == 1, (
        "the recognition leg blocked until the credit finished — R7-06 stall"
    )
    # Secondary (loose) sanity: the leg returned near the bound, not the 0.4s wait.
    assert elapsed < 0.35, f"recognition leg stalled {elapsed:.2f}s on the split credit"

    # The backgrounded credit still lands after draining.
    await _drain(tracker)
    assert writer.increment_play_count.call_count == 2  # raised once, then succeeded


@pytest.mark.asyncio
async def test_fast_split_credit_still_completes_inline(monkeypatch):
    """Control: a NORMAL (non-throttled) split credit completes within the inline
    bound, so common-case timing is unchanged — the credit has landed by the time
    `on_track_identified` returns, with no draining needed."""
    monkeypatch.setattr(lt, "_SPLIT_CREDIT_INLINE_WAIT_SECONDS", 5.0)
    tracker, writer = make_tracker()

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Master-Dik"))       # closer → arms
    await tracker.on_track_identified(_foreign("X1"))                 # swap → split credit (fast)

    # No drain: a fast credit completed inline within the bound.
    writer.increment_play_count.assert_called_once_with(12345, 67890)
