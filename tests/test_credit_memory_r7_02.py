"""R7-02 — silence-window credited-memory: one physical spin whose Shazam
attribution ping-pongs between two releases must not double-credit.

The #185 replay boundary guards re-identifications WITHIN one session, and the
#186 idempotency guard protects WITHIN one finalize — but when a foreign
confirmation intervenes, each swing SPLITS, and the split-off remainder gets a
fresh session with a fresh ``credited`` latch.  One physical spin, one silence
window, no SESSION_ENDED between the swings, so the PCONC-1 epoch guard cannot
see it either: the armed release is detached and credited once per swing (+N
Play Count for ONE play).

The fix — originally R7-02's 45s wall-clock window, SUPERSEDED by R8-02/#346
(Lane, 2026-08-12, LOCKED): the window expired between two real confirmation
cycles of the ping-pong, so the memory is now SILENCE-BOUNDARY keyed — a
per-spin :class:`SpinMemory` (R9-26) SWAPPED for a fresh one at the boundary
EVENT itself (R9-08: not "cleared when the finalize completes"; a boundary
finalize legally completes minutes late and judges against its own outgoing
spin) — and a credit for a release already credited THIS SPIN is suppressed
UNLESS the session was opened by a genuine #185 replay boundary (a real
re-drop, which earns its own credit).  A real back-to-back replay still credits
(the exemption); a genuine later spin credits after the silence boundary; two
different records never interfere (R9-01: a genuine credit for a different
record even advances the spin).  These tests pin the MECHANISM in compressed
time; the
realistic-cadence scenarios live in test_credit_cadence_r8.py (R8-04).
"""
import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, TrackMetadata, TracklistEntry
from tests.test_listen_tracker import make_tracker, make_track


# A DIFFERENT release (r2) whose tracks are all NON-closers, so r2 never arms a
# completion and can only ever act as the foreign confirmation that drives the
# swing — isolating r1's credit count as the thing under test.
_R2_TL = [
    TracklistEntry("A1", "X1"),
    TracklistEntry("A2", "X2"),
    TracklistEntry("B1", "X3-closer"),
]


def _r2(title):
    return TrackMetadata(
        title=title, artist="Other Band", album="Record Two",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=555, discogs_instance_id=556, tracklist=_R2_TL,
    )


def _r1_credits(writer):
    return [c.args for c in writer.increment_play_count.call_args_list
            if c.args == (12345, 67890)]


@pytest.mark.asyncio
async def test_attribution_pingpong_does_not_double_credit_one_spin():
    """RED before R7-02: r1's closer plays and arms; a swing to r2 splits and
    credits r1 (#1); Shazam swings back, r1 re-arms; another swing to r2 splits
    and credits r1 AGAIN (#2) — +2 Play Count for one physical spin.  The
    credited-memory must suppress the second split credit."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))   # r1 support (A1)
    await tracker.on_track_identified(make_track("Master-Dik"))       # r1 closer (B1) → arms
    await tracker.on_track_identified(_r2("X1"))                      # swing → split, credit #1 (r1)
    await tracker.on_track_identified(make_track("Master-Dik"))       # swing back → r1 re-arms
    await tracker.on_track_identified(_r2("X2"))                      # swing → split, credit #2? SUPPRESSED
    await tracker._end_session()

    assert _r1_credits(writer) == [(12345, 67890)], (
        f"one physical spin was double-credited: {_r1_credits(writer)}"
    )


@pytest.mark.asyncio
async def test_pingpong_ending_on_the_armed_release_does_not_double_credit():
    """Cold-review catch (2026-08-11): the spin ENDS on the armed release — a
    single blip to a foreign record, then back to r1, then silence.  Credit #1
    lands via the split; the terminal credit lands via SESSION_ENDED, which the
    original split-only guard did NOT cover (+2 for one spin).  The
    credited-memory must guard the terminal credit too."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))       # r1 closer → arms
    await tracker.on_track_identified(_r2("X1"))                      # blip → split, credit #1 (r1)
    await tracker.on_track_identified(make_track("Master-Dik"))       # back to r1, re-arms
    await tracker._end_session()                                     # SESSION_ENDED → credit #2? SUPPRESSED

    assert _r1_credits(writer) == [(12345, 67890)], (
        f"the terminal credit double-counted one spin: {_r1_credits(writer)}"
    )


@pytest.mark.asyncio
async def test_pingpong_ending_on_armed_release_does_not_double_love():
    """The terminal duplicate credit must not double-love the closer either."""
    from unittest.mock import MagicMock
    from src.tracking.listen_tracker import ListenTracker
    from tests.test_listen_tracker import make_writer_mock
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.enabled = True
    lastfm.love_on_completion = True
    lastfm.love = MagicMock(return_value=True)
    tracker = ListenTracker(writer, lastfm)

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))       # arms
    await tracker.on_track_identified(_r2("X1"))                      # split → credit + love #1
    await tracker.on_track_identified(make_track("Master-Dik"))       # re-arms
    await tracker._end_session()                                     # terminal dup → suppressed

    assert lastfm.love.call_count == 1, (
        f"the closer was loved twice for one spin: {lastfm.love.call_count}"
    )


@pytest.mark.asyncio
async def test_pingpong_suppression_is_logged(caplog):
    """The suppression is loud (R7-02 diagnosability)."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker.on_track_identified(_r2("X1"))
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level("INFO"):
        await tracker.on_track_identified(_r2("X2"))
    assert any("R8-02" in r.message for r in caplog.records), (
        "a suppressed duplicate split credit must be logged"
    )


@pytest.mark.asyncio
async def test_genuine_185_redrop_still_credits_each_spin():
    """Control: the #185 exception — a genuine re-drop of the SAME record
    (opener after closer) is a real second spin and must credit each time, even
    though the release was credited moments ago within the silence window."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))   # opener (A1)
    await tracker.on_track_identified(make_track("Master-Dik"))       # closer → arms
    await tracker.on_track_identified(make_track("Catholic Block"))   # #185 re-drop → split, credit #1
    await tracker.on_track_identified(make_track("Master-Dik"))       # closer → arms again
    await tracker.on_track_identified(make_track("Catholic Block"))   # #185 re-drop → split, credit #2
    await tracker._end_session()

    # Two genuine spins via #185 both credit — the memory must NOT suppress a
    # replay-boundary split.
    assert len(_r1_credits(writer)) == 2, (
        f"a genuine re-drop was wrongly suppressed: {_r1_credits(writer)}"
    )


@pytest.mark.asyncio
async def test_two_different_records_both_credit():
    """Control: cross-release independence — playing r1 to completion then a
    DIFFERENT owned record r3 to completion credits both; the memory is keyed by
    release and never suppresses a different one."""
    tracker, writer = make_tracker()
    r3_tl = [TracklistEntry("A1", "Y-open"), TracklistEntry("B1", "Y-closer")]

    def r3(title):
        return TrackMetadata(
            title=title, artist="Third Band", album="Record Three",
            source=MetadataSource.DISCOGS_COLLECTION,
            discogs_release_id=777, discogs_instance_id=778, tracklist=r3_tl,
        )

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))   # r1 support
    await tracker.on_track_identified(make_track("Master-Dik"))       # r1 closer
    await tracker.on_track_identified(r3("Y-open"))                   # album change → split, credit r1
    await tracker.on_track_identified(r3("Y-closer"))                 # r3 closer → arms
    await tracker._end_session()                                     # credit r3

    calls = [c.args for c in writer.increment_play_count.call_args_list]
    assert (12345, 67890) in calls and (777, 778) in calls, calls
    assert len(calls) == 2, f"expected exactly two distinct credits, got {calls}"
