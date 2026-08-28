"""#468: a mid-side flip to a DIFFERENT Discogs release that shares the album's
MASTER (a collection/database tier flip driven by AudD album-string variance) must
NOT be treated as an album change / session split."""
import pytest

from src.audio.silence import AudioEvent
from tests.test_listen_tracker import make_tracker, make_track


@pytest.mark.asyncio
async def test_same_master_different_release_does_not_split():
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Catholic Block", release_id=100, master_id=500))
    sess = tracker._session
    # different pressing of the SAME album (same master) — the flip-flop
    await tracker.on_track_identified(
        make_track("Stereo Sanctity", release_id=200, master_id=500))
    assert tracker._session is sess                       # not split
    assert tracker._session.last_release_id == 200        # detector still tracks latest
    assert tracker._session.last_master_id == 500


@pytest.mark.asyncio
async def test_different_master_still_splits():
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Catholic Block", release_id=100, master_id=500))
    sess = tracker._session
    # a genuinely different album (different master) still splits
    await tracker.on_track_identified(
        make_track("Foreign Song", release_id=200, master_id=999))
    assert tracker._session is not sess


@pytest.mark.asyncio
async def test_masterless_flip_still_splits_conservatively():
    """No master on either side → no album-identity vouch → the release_id change
    stays a conservative split (unchanged pre-#468 behavior)."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Catholic Block", release_id=100, master_id=None))
    sess = tracker._session
    await tracker.on_track_identified(
        make_track("Foreign Song", release_id=200, master_id=None))
    assert tracker._session is not sess
