"""Unit tests for ListenTracker — the most business-logic-heavy component.

Covers every edge case from the architecture doc:
  ✓ Full album played → increment_play_count called
  ✓ Only Side A played → NOT incremented
  ✓ Last track recognition missed → NOT incremented
  ✓ Album not in collection (fallback metadata, no release_id) → NOT incremented
  ✓ increment_play_count returns False → no crash
  ✓ SESSION_ENDED with no active session → no crash
  ✓ Already-counted album (idempotent Discogs call) → called once anyway

Covers update_last_played integration:
  ✓ last_played_field_name configured → update_last_played called on album completion
  ✓ last_played_field_name not configured → update_last_played NOT called
  ✓ update_last_played returns False → logs warning, no crash

No audio hardware, display, or Discogs account required. DiscogsClient is mocked.
"""
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import (
    MetadataSource, TracklistEntry, TrackMetadata, PlaySession
)
from src.tracking.listen_tracker import ListenTracker


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_writer_mock(
    increment_play_count_return=True,
    last_played_field_name=None,
    update_last_played_return=True,
):
    """Mock DiscogsCollectionWriter with controlled return values (A-4).

    last_played_field_name defaults to None (not configured). Set it to a
    non-empty string to simulate a user who has the Last Played field enabled.
    """
    writer = MagicMock()
    writer.increment_play_count.return_value = increment_play_count_return
    writer.last_played_field_name = last_played_field_name
    writer.update_last_played.return_value = update_last_played_return
    # #61: the tracker now dispatches the Play Count / Last Played writes through
    # writer.run(fn, …) (the dedicated-executor delegate) instead of
    # loop.run_in_executor(None, …). The mock's run awaits and calls the target,
    # so increment_play_count / update_last_played return values + call-assertions
    # are unchanged.
    writer.run = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    return writer


def make_tracklist():
    return [
        TracklistEntry("A1", "Catholic Block"),
        TracklistEntry("A2", "Pipeline/Kill Time"),
        TracklistEntry("A3", "Stereo Sanctity"),
        TracklistEntry("B1", "Tuff Gnarl"),
        TracklistEntry("B2", "Cotton Crown"),
        TracklistEntry("B3", "White Cross"),
        TracklistEntry("B4", "Master-Dik"),
    ]


def make_track(
    title,
    release_id=12345,
    instance_id=67890,
    source=MetadataSource.DISCOGS_COLLECTION,
    tracklist=None,
):
    return TrackMetadata(
        title=title,
        artist="Sonic Youth",
        album="Sister",
        source=source,
        discogs_release_id=release_id,
        discogs_instance_id=instance_id,
        tracklist=tracklist if tracklist is not None else make_tracklist(),
    )


def make_tracker(
    increment_play_count_return=True,
    last_played_field_name=None,
    update_last_played_return=True,
):
    writer = make_writer_mock(
        increment_play_count_return=increment_play_count_return,
        last_played_field_name=last_played_field_name,
        update_last_played_return=update_last_played_return,
    )
    # A-4: ListenTracker takes a DiscogsCollectionWriter directly.
    tracker = ListenTracker(writer)
    return tracker, writer


# ---------------------------------------------------------------------------
# Session lifecycle via on_silence_event
# ---------------------------------------------------------------------------

def test_tracker_uses_the_injected_collection_writer():
    """A-4: the tracker depends on a DiscogsCollectionWriter injected directly,
    not the whole client dug out of a resolver's internals."""
    writer = MagicMock()
    tracker = ListenTracker(writer)
    assert tracker.writer is writer


def test_session_is_none_at_start():
    tracker, _ = make_tracker()
    assert tracker._session is None


def test_session_starts_on_music_started():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    assert tracker._session is not None
    assert isinstance(tracker._session, PlaySession)


def test_second_music_started_does_not_replace_existing_session():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    first_session = tracker._session
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)  # Should be a no-op
    assert tracker._session is first_session


# ---------------------------------------------------------------------------
# Happy path: full album → increment Play Count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_album_calls_increment_play_count():
    """Playing through the last track + SESSION_ENDED → Discogs Play Count incremented."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))
    await tracker.on_track_identified(make_track("Master-Dik"))  # Last track

    assert tracker._session.potential_last_track is True

    await tracker._end_session()  # Direct await for reliable test execution

    writer.increment_play_count.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_session_cleared_after_end():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()
    assert tracker._session is None


@pytest.mark.asyncio
async def test_increment_play_count_uses_correct_release_and_instance_ids():
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik", release_id=99, instance_id=77))
    await tracker._end_session()
    writer.increment_play_count.assert_called_once_with(99, 77)


# ---------------------------------------------------------------------------
# Edge case: only Side A played
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_side_a_played_does_not_increment():
    """Session ends before last track identified → no Discogs update."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))
    await tracker.on_track_identified(make_track("Stereo Sanctity"))
    # Side B tracks never identified

    assert tracker._session.potential_last_track is False
    await tracker._end_session()
    writer.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: last track never recognized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_but_last_track_identified_does_not_increment():
    """Recognizer missed the last track (e.g. needle skip) → no update."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    for title in ["Catholic Block", "Pipeline/Kill Time", "Stereo Sanctity",
                  "Tuff Gnarl", "Cotton Crown", "White Cross"]:
        await tracker.on_track_identified(make_track(title))
    # Master-Dik (B4, last) never identified

    assert tracker._session.potential_last_track is False
    await tracker._end_session()
    writer.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: album not in Discogs collection (fallback metadata)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_track_reached_but_fallback_source_does_not_increment():
    """Last track identified but metadata is FALLBACK (no release_id) → skip."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    fallback_last = TrackMetadata(
        title="Master-Dik",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(fallback_last)

    # potential_last_track IS True (we did identify the last track)
    assert tracker._session.potential_last_track is True
    # But there's no release_id to update
    assert tracker._session.album_release_id is None

    await tracker._end_session()
    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_database_source_without_instance_id_does_not_call_increment():
    """DISCOGS_DATABASE result has no instance_id → log_track refuses to latch
    the release_id (since we can't build a valid field-update URL without an
    instance_id), so _end_session sees album_release_id is None and skips the
    Discogs update entirely.
    """
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    db_last = TrackMetadata(
        title="Master-Dik",
        artist="Sonic Youth",
        album="Sister",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=12345,
        discogs_instance_id=None,  # DB results don't have instance IDs
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(db_last)
    # potential_last_track IS True (we DID identify the last track)
    assert tracker._session.potential_last_track is True
    # But the release_id was NOT latched, because there's no instance_id to go with it
    assert tracker._session.album_release_id is None
    assert tracker._session.album_instance_id is None

    await tracker._end_session()
    # No POST attempted with instance_id=None
    writer.increment_play_count.assert_not_called()
    writer.update_last_played.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: no tracks identified at all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_with_no_identified_tracks_does_not_increment():
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    # Music was heard but recognition never succeeded
    await tracker._end_session()
    writer.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# Edge case: SESSION_ENDED with no active session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_session_with_no_session_does_not_crash():
    """Spurious SESSION_ENDED (no active session) should be a safe no-op."""
    tracker, writer = make_tracker()
    assert tracker._session is None
    # Should not raise
    await tracker._end_session()
    writer.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# increment_play_count failure handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_increment_play_count_returning_false_does_not_raise():
    """Discogs API returning failure should log a warning but not crash. Since
    #163 the failed write is bounded-retried, so it is attempted (not once but)
    _FINALIZE_WRITE_ATTEMPTS times — still without raising."""
    from src.tracking.listen_tracker import _FINALIZE_WRITE_ATTEMPTS
    tracker, writer = make_tracker(increment_play_count_return=False)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    # Should complete without raising
    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()):
        await tracker._end_session()
    assert writer.increment_play_count.call_count == _FINALIZE_WRITE_ATTEMPTS


# ---------------------------------------------------------------------------
# #163 — album-split finalize write failure must not lose the credit. The old
# code latched `credited = True` BEFORE the increment await and detached the
# session before finalize, so a transient write failure (increment returns False)
# left the completed play marked credited, detached, and never retried. The fix
# splits in-flight (`crediting`) from committed (`credited`, set only on success)
# and bounded-retries the write.
# ---------------------------------------------------------------------------
from src.tracking.listen_tracker import _FINALIZE_WRITE_ATTEMPTS


@pytest.mark.asyncio
async def test_failed_credit_is_not_committed_and_is_bounded_retried(caplog):
    """A transient increment failure (returns False on every attempt) must NOT
    latch `credited`, and the write must be retried _FINALIZE_WRITE_ATTEMPTS times
    — reproduces the #163 loss (old code: credited=True after 1 attempt)."""
    import logging
    tracker, writer = make_tracker(increment_play_count_return=False)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()), \
         caplog.at_level(logging.ERROR):
        await tracker._end_session()
    assert session.credited is False                    # not falsely committed
    assert session.crediting is True                    # in-flight latch was set (B-8)
    assert writer.increment_play_count.call_count == _FINALIZE_WRITE_ATTEMPTS
    assert "LOST" in caplog.text                         # the loss is logged loudly


@pytest.mark.asyncio
async def test_credit_is_committed_when_a_retry_eventually_succeeds():
    """Increment fails twice then succeeds → committed, and it stopped retrying
    the moment it landed (no wasted attempt after success)."""
    tracker, writer = make_tracker()
    writer.increment_play_count.side_effect = [False, False, True]
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()):
        await tracker._end_session()
    assert session.credited is True
    assert writer.increment_play_count.call_count == 3


@pytest.mark.asyncio
async def test_successful_credit_does_not_retry():
    """The happy path is unchanged: a first-attempt success commits and never
    issues a second increment."""
    tracker, writer = make_tracker(increment_play_count_return=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()):
        await tracker._end_session()
    assert session.credited is True
    writer.increment_play_count.assert_called_once()


@pytest.mark.asyncio
async def test_reentrant_finalize_while_crediting_does_not_double_increment():
    """B-8 preserved: a finalize of a session whose credit is already in flight
    (crediting latched) must NOT issue a second increment."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    session.crediting = True   # a concurrent finalize already owns the credit
    await tracker._finalize_session(session)
    writer.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# on_track_identified wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_track_identified_starts_session_if_not_running():
    """on_track_identified can create a session if called before MUSIC_STARTED."""
    tracker, _ = make_tracker()
    assert tracker._session is None
    await tracker.on_track_identified(make_track("Catholic Block"))
    assert tracker._session is not None


@pytest.mark.asyncio
async def test_on_track_identified_appends_to_session():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))
    assert len(tracker._session.identified_tracks) == 2


@pytest.mark.asyncio
async def test_on_track_identified_sets_potential_last_track():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    assert tracker._session.potential_last_track is False
    await tracker.on_track_identified(make_track("Master-Dik"))
    assert tracker._session.potential_last_track is True


# ---------------------------------------------------------------------------
# Already-counted (idempotent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_counted_album_still_calls_increment_once():
    """increment_play_count handles existing counts — we just call it once per session."""
    tracker, writer = make_tracker(increment_play_count_return=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()
    # We called it; Discogs handles the read-before-write
    writer.increment_play_count.assert_called_once()


# ---------------------------------------------------------------------------
# update_last_played integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_album_calls_update_last_played_when_configured():
    """When last_played_field_name is configured, update_last_played is called on completion."""
    tracker, writer = make_tracker(last_played_field_name="Last Played")
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(12345, 67890)
    writer.update_last_played.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_discogs_writes_dispatch_through_the_dedicated_executor():
    """#61: the Play Count and Last Played writes go through writer.run (the
    dedicated executor delegate), not the shared default run_in_executor(None, …)
    pool. Reverting either write site would stop calling writer.run and fail this.
    With no Last.fm client the love path is skipped, so writer.run is called for
    exactly the two Discogs writes — nothing else is routed through it."""
    tracker, writer = make_tracker(last_played_field_name="Last Played")
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    dispatched = [c.args[0] for c in writer.run.call_args_list]
    assert writer.increment_play_count in dispatched
    assert writer.update_last_played in dispatched
    assert len(dispatched) == 2


@pytest.mark.asyncio
async def test_full_album_does_not_call_update_last_played_when_not_configured():
    """When last_played_field_name is None, update_last_played is never called."""
    tracker, writer = make_tracker(last_played_field_name=None)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once()
    writer.update_last_played.assert_not_called()


@pytest.mark.asyncio
async def test_update_last_played_returning_false_does_not_raise():
    """update_last_played failure should log a warning but not crash the session."""
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        update_last_played_return=False,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    # Should complete without raising
    await tracker._end_session()
    writer.update_last_played.assert_called_once()


@pytest.mark.asyncio
async def test_clock_skip_is_not_logged_as_a_failure(caplog):
    """STAB-2: when update_last_played skips the write because the clock is
    untrustworthy (pre-NTP), the finalize path must NOT ALSO report it as a
    failure — a deliberate skip is not a failure (the writer already WARNed)."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        update_last_played_return=False,   # the writer's gate skipped → returns False
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=False):
        await tracker._end_session()
    assert not any(
        "Failed to update Discogs Last Played" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_real_last_played_failure_is_still_logged(caplog):
    """The STAB-2 skip suppression must NOT mask a genuine failure: update_last_played
    returning False with a TRUSTWORTHY clock is a real error and is still logged."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        update_last_played_return=False,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=True):
        await tracker._end_session()
    assert any(
        "Failed to update Discogs Last Played" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# CONC-6 — a track whose session ended while awaiting the lifecycle lock is
# dropped, not resurrected as a phantom session.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_track_is_dropped_not_started_as_a_phantom_session():
    """CONC-6: if is_stale() is True when on_track_identified acquires the lock
    (the audio's session ended while it waited), drop the track — start no
    session and log nothing."""
    tracker, writer = make_tracker()
    assert tracker._session is None
    await tracker.on_track_identified(make_track("StaleTrack"), is_stale=lambda: True)
    assert tracker._session is None   # no phantom session created for dead audio


@pytest.mark.asyncio
async def test_live_track_is_logged_when_not_stale():
    """is_stale() False → normal behavior: a session starts and the track logs."""
    tracker, writer = make_tracker()
    await tracker.on_track_identified(make_track("Catholic Block"), is_stale=lambda: False)
    assert tracker._session is not None
    assert tracker._session.identified_tracks[-1].title == "Catholic Block"


@pytest.mark.asyncio
async def test_on_track_identified_without_is_stale_behaves_normally():
    """Backward compat: no is_stale predicate → no staleness check (the recognition
    loop's other callers and the existing suite pass none)."""
    tracker, writer = make_tracker()
    await tracker.on_track_identified(make_track("Catholic Block"))
    assert tracker._session is not None
    assert tracker._session.identified_tracks[-1].title == "Catholic Block"


# ---------------------------------------------------------------------------
# META-7 — the two Discogs collection writes (Play Count + Last Played) are
# independent POSTs and the session is destroyed right after.  If EXACTLY ONE
# lands, the collection item is left inconsistent with nothing to retry it.
# Surface ONE explicit divergence warning naming the item when the two writes
# disagree — but NOT for a deliberate STAB-2 clock-skip (intentional, self-
# correcting), which is excluded by the same clock gate.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_divergence_warning_when_playcount_lands_but_last_played_fails(caplog):
    """META-7: Play Count incremented but Last Played genuinely failed (trustworthy
    clock) → the item is inconsistent, so ONE DIVERGED warning naming the release
    is logged in addition to the per-write failure line."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        increment_play_count_return=True,
        update_last_played_return=False,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=True):
        await tracker._end_session()
    diverged = [r for r in caplog.records if "DIVERGED" in r.getMessage()]
    assert len(diverged) == 1, "exactly one divergence line expected"
    msg = diverged[0].getMessage()
    assert "12345" in msg and "67890" in msg   # names the release / instance
    assert "was incremented" in msg            # Play Count side landed
    assert "did NOT update" in msg             # Last Played side did not


@pytest.mark.asyncio
async def test_divergence_warning_when_last_played_lands_but_playcount_fails(caplog):
    """META-7 (reverse direction): the increment POST fails (e.g. a 429 or a
    read-before-write abort) but the subsequent Last Played POST succeeds → the
    item is inconsistent the OTHER way (stale count, fresh date).  This pins the
    two message operands the forward-direction test never reaches — 'did NOT
    increment' and 'was updated' — so a mutation of either branch string is caught."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        increment_play_count_return=False,
        update_last_played_return=True,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=True):
        await tracker._end_session()
    diverged = [r for r in caplog.records if "DIVERGED" in r.getMessage()]
    assert len(diverged) == 1, "exactly one divergence line expected"
    msg = diverged[0].getMessage()
    assert "12345" in msg and "67890" in msg   # names the release / instance
    assert "did NOT increment" in msg          # Play Count side did NOT land
    assert "was updated" in msg                # Last Played side did


@pytest.mark.asyncio
async def test_no_divergence_warning_when_both_writes_succeed(caplog):
    """META-7: both writes land → the item is consistent → no DIVERGED line."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        increment_play_count_return=True,
        update_last_played_return=True,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=True):
        await tracker._end_session()
    assert not any("DIVERGED" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_divergence_warning_when_both_writes_fail(caplog):
    """META-7: NEITHER write lands → the item is (still) consistent with itself,
    nothing partially applied → no DIVERGED line (the per-write failures log
    on their own)."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        increment_play_count_return=False,
        update_last_played_return=False,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=True):
        await tracker._end_session()
    assert not any("DIVERGED" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_divergence_warning_for_a_deliberate_clock_skip(caplog):
    """META-7 × STAB-2: Play Count landed but Last Played returned False because the
    clock is untrustworthy (a deliberate skip, not a failure).  The XOR is true, so
    the clock gate is what MUST suppress the DIVERGED line — a skipped-on-purpose
    Last Played is self-correcting on the next trustworthy play, not a divergence."""
    import logging
    tracker, writer = make_tracker(
        last_played_field_name="Last Played",
        increment_play_count_return=True,
        update_last_played_return=False,   # writer's own gate short-circuited the POST
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    with caplog.at_level(logging.WARNING), \
         patch("src.tracking.listen_tracker.clock_is_trustworthy", return_value=False):
        await tracker._end_session()
    assert not any("DIVERGED" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Background task management (v1.3.3)
#
# SESSION_ENDED schedules _end_session() as an asyncio task. asyncio holds
# only weak references to tasks, so ListenTracker must keep a strong
# reference until the task — which performs the Discogs play-count write —
# completes.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_ended_task_is_referenced_until_done():
    tracker = ListenTracker(make_writer_mock())
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    assert len(tracker._bg_tasks) == 1  # Strong reference held while running

    # Let the scheduled _end_session task run to completion
    for _ in range(5):
        await asyncio.sleep(0)

    assert tracker._session is None      # Session was ended
    assert len(tracker._bg_tasks) == 0   # Reference released on completion


@pytest.mark.asyncio
async def test_full_album_increments_via_public_session_ended_path():
    """T-5: drive the production wiring — on_silence_event(SESSION_ENDED) →
    create_task → _end_session — end to end, so the path that actually fires the
    Discogs write (and where B-2's race lives) is covered, not just a direct
    _end_session() await."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))  # the album closer

    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    for _ in range(5):                  # let the scheduled task run to completion
        await asyncio.sleep(0)

    writer.increment_play_count.assert_called_once_with(12345, 67890)
    assert tracker._session is None
    assert len(tracker._bg_tasks) == 0


@pytest.mark.asyncio
async def test_partial_album_via_public_session_ended_path_does_not_increment():
    """T-5: only Side A played → the public SESSION_ENDED path ends the session
    without a Discogs write."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))  # not the closer

    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    for _ in range(5):
        await asyncio.sleep(0)

    writer.increment_play_count.assert_not_called()
    assert tracker._session is None


# ---------------------------------------------------------------------------
# CONC-3 — the fire-and-forget SESSION_ENDED task's failure is LOGGED by its
# done-callback, not swallowed into asyncio's GC "never retrieved" warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conc3_raising_end_session_task_logs_the_failure(caplog):
    """If the fire-and-forget SESSION_ENDED task raises, its done-callback must
    LOG the failure (CONC-3).  Reproduced via an unwrapped write: the Play Count
    increment lands (credited) but ``update_last_played`` RAISES rather than
    returning False, so the exception propagates out of ``_end_session`` — where,
    before the fix, the bare ``_bg_tasks.discard`` callback dropped it and only
    asyncio's detached GC warning ever mentioned it, at an arbitrary later time.
    """
    tracker, writer = make_tracker(last_played_field_name="Last Played")
    writer.update_last_played.side_effect = RuntimeError("discogs 500 on last-played")
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))   # the album closer

    with caplog.at_level(logging.ERROR):
        tracker.on_silence_event(AudioEvent.SESSION_ENDED)
        task = next(iter(tracker._bg_tasks))        # capture before the callback discards it
        for _ in range(5):                          # let the task + its done-callback run
            await asyncio.sleep(0)

    assert task.done()
    assert any(
        "End-of-session credit task failed" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    # Retrieve so today's bare-discard path can't leak an unretrieved-exception
    # warning into later tests (the fix already retrieves it in the callback).
    if not task.cancelled():
        task.exception()


@pytest.mark.asyncio
async def test_conc3_cancelled_end_session_task_is_not_logged_as_error(caplog):
    """A task cancelled by shutdown / loop teardown must NOT be reported as a
    credit failure — and the callback must not call ``.exception()`` on it, which
    would raise ``CancelledError`` inside the callback (surfacing as an
    'Exception in callback' ERROR).  Pins the ``task.cancelled()`` guard."""
    tracker = ListenTracker(make_writer_mock())
    started = asyncio.Event()

    async def slow(expected=None):
        started.set()
        await asyncio.sleep(3600)

    tracker._end_session = slow

    with caplog.at_level(logging.ERROR):
        tracker.on_silence_event(AudioEvent.SESSION_ENDED)
        task = next(iter(tracker._bg_tasks))
        await started.wait()
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)

    assert task.cancelled()
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Album-change auto-split (v1.3.4)
#
# Swapping records faster than session_end_silence_seconds used to merge two
# albums into one session, letting record 2's closer credit record 1 with a
# play. on_track_identified now splits the session when a confirmed track's
# release_id differs from the latched one.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_album_change_splits_session():
    """A track from a different release ends the old session and starts fresh."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    first_session = tracker._session

    await tracker.on_track_identified(make_track("So What", release_id=999, instance_id=888))

    assert tracker._session is not first_session
    assert tracker._session.album_release_id == 999
    assert len(tracker._session.identified_tracks) == 1


@pytest.mark.asyncio
async def test_album_change_credits_first_record_if_its_closer_played():
    """Record 1 finished (closer identified), record 2 dropped within 45s:
    the split must still increment record 1's play count."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))
    assert tracker._session.potential_last_track is True

    await tracker.on_track_identified(make_track("So What", release_id=999, instance_id=888))

    writer.increment_play_count.assert_called_once_with(111, 222)


@pytest.mark.asyncio
async def test_album_change_does_not_credit_unfinished_first_record():
    """Record 1 abandoned mid-side: the split ends its session WITHOUT updates."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    await tracker.on_track_identified(make_track("So What", release_id=999, instance_id=888))

    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_same_release_does_not_split_session():
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block"))
    first_session = tracker._session
    await tracker.on_track_identified(make_track("Pipeline/Kill Time"))

    assert tracker._session is first_session
    assert len(tracker._session.identified_tracks) == 2


@pytest.mark.asyncio
async def test_fallback_track_without_release_id_does_not_split():
    """FALLBACK metadata (no release_id) can't be distinguished — no split."""
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Catholic Block", release_id=111, instance_id=222))
    first_session = tracker._session

    fallback = TrackMetadata(
        title="Mystery Tune",
        artist="Unknown",
        album="Bootleg",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
        tracklist=[],
    )
    await tracker.on_track_identified(fallback)

    assert tracker._session is first_session


@pytest.mark.asyncio
async def test_no_split_when_nothing_latched_yet():
    """First identified track of a session never triggers a split, whatever
    its release_id — there's nothing latched to differ from."""
    tracker, _ = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    first_session = tracker._session

    await tracker.on_track_identified(make_track("Catholic Block", release_id=777, instance_id=555))

    assert tracker._session is first_session
    assert tracker._session.album_release_id == 777


# ---------------------------------------------------------------------------
# Auto-split via last_release_id (v1.3.5)
#
# The v1.3.4 split compared against the LATCHED album_release_id, which only
# collection-owned tracks set. A DB-resolved record 1 (never latches) +
# closer played + quick swap to a collection-owned record 2 evaded detection,
# and record 2 inherited — and was phantom-credited for — record 1's
# completed play. Detection now compares against last_release_id, which
# updates from any source carrying a release ID.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_record_then_collection_record_splits():
    """Regression: a DB-resolved record 1 must not let record 2 inherit its
    session. Pre-v1.3.5 this merged sessions and phantom-credited record 2."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    # Record 1: DB-resolved (release_id but NO instance_id → never latches),
    # and its closer plays.
    db_closer = TrackMetadata(
        title="Master-Dik", artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=111, discogs_instance_id=None,
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(db_closer)
    assert tracker._session.potential_last_track is True
    assert tracker._session.album_release_id is None     # No latch (DB-only)
    assert tracker._session.last_release_id == 111       # But it WAS seen

    # Record 2 (collection-owned) dropped within 45s → must split.
    await tracker.on_track_identified(
        make_track("Catholic Block", release_id=999, instance_id=888)
    )

    # The split ended record 1's session; with no latch there was nothing to
    # credit (correct — we can't update a pressing the user doesn't own)...
    writer.increment_play_count.assert_not_called()
    # ...and record 2 starts CLEAN: no inherited potential_last_track that
    # could phantom-credit it at session end.
    assert tracker._session.potential_last_track is False
    assert tracker._session.album_release_id == 999
    assert tracker._session.last_release_id == 999


@pytest.mark.asyncio
async def test_love_not_repeated_on_double_finalize_fallback_album():
    """B-23: a fallback album (no release_id) never latches `credited`, so the
    B-8 credited-guard doesn't cover the love path.  The separate `loved` flag
    must still prevent a double-love on a re-entrant finalize."""
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.love_on_completion = True
    lastfm.love = MagicMock(return_value=True)
    tracker = ListenTracker(writer, lastfm)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    fallback_last = TrackMetadata(
        title="Master-Dik", artist="Sonic Youth", album="Sister",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None, discogs_instance_id=None,
        tracklist=make_tracklist(),
    )
    await tracker.on_track_identified(fallback_last)
    session = tracker._session
    assert session.potential_last_track is True
    assert session.album_release_id is None   # so `credited` will never latch

    await tracker._finalize_session(session)
    await tracker._finalize_session(session)   # re-entrant / double finalize

    lastfm.love.assert_called_once()           # the `loved` flag held the line
    assert session.loved is True


# ---------------------------------------------------------------------------
# #163 (love side) — the Last.fm love gets the same in-flight/committed +
# bounded-retry treatment as the credit: `loving` is latched before the await
# (B-23 re-entrancy), `loved` is committed only after the love lands, and a
# transient failure is retried instead of silently latched.
# ---------------------------------------------------------------------------

def _make_love_tracker(love_return=True, love_side_effect=None):
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.love_on_completion = True
    if love_side_effect is not None:
        lastfm.love = MagicMock(side_effect=love_side_effect)
    else:
        lastfm.love = MagicMock(return_value=love_return)
    return ListenTracker(writer, lastfm), lastfm


@pytest.mark.asyncio
async def test_failed_love_is_not_committed_and_is_bounded_retried():
    """A transient Last.fm love failure must NOT latch `loved`, and must be
    retried _FINALIZE_WRITE_ATTEMPTS times."""
    from src.tracking.listen_tracker import _FINALIZE_WRITE_ATTEMPTS
    tracker, lastfm = _make_love_tracker(love_return=False)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()):
        await tracker._end_session()
    assert session.loved is False
    assert session.loving is True
    assert lastfm.love.call_count == _FINALIZE_WRITE_ATTEMPTS


@pytest.mark.asyncio
async def test_love_is_committed_when_a_retry_succeeds():
    """Love fails once then succeeds → committed, and it stopped retrying on the
    success."""
    tracker, lastfm = _make_love_tracker(love_side_effect=[False, True])
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()):
        await tracker._end_session()
    assert session.loved is True
    assert lastfm.love.call_count == 2


@pytest.mark.asyncio
async def test_reentrant_finalize_while_loving_does_not_double_love():
    """B-23 preserved: a finalize of a session whose love is already in flight
    (loving latched) must NOT issue a second love."""
    tracker, lastfm = _make_love_tracker(love_return=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    session.loving = True   # a concurrent finalize already owns the love
    await tracker._finalize_session(session)
    lastfm.love.assert_not_called()


@pytest.mark.asyncio
async def test_collection_then_db_record_still_splits():
    """The original v1.3.4 direction (collection → DB) keeps working under
    last_release_id comparison."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    await tracker.on_track_identified(make_track("Master-Dik", release_id=111, instance_id=222))
    db_track = TrackMetadata(
        title="So What", artist="Miles Davis", album="Kind of Blue",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=555, discogs_instance_id=None,
        tracklist=[],
    )
    await tracker.on_track_identified(db_track)

    # Record 1 was collection-owned and finished → credited by the split.
    writer.increment_play_count.assert_called_once_with(111, 222)
    assert tracker._session.last_release_id == 555


# ---------------------------------------------------------------------------
# CONC-1 — drain(): shutdown must wait for an in-flight end-of-session credit
# so it is not torn in half (Play Count incremented, Last Played never written,
# credited latched, no retry). These pin drain()'s three behaviours plus a
# reproduction of the mid-write tear window.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_with_no_in_flight_tasks_is_a_noop():
    tracker = ListenTracker(make_writer_mock(), None)
    # No bg tasks; must return promptly without error.
    await asyncio.wait_for(tracker.drain(timeout=5), timeout=1.0)


@pytest.mark.asyncio
async def test_drain_waits_for_an_in_flight_credit_to_finish():
    tracker = ListenTracker(make_writer_mock(), None)
    finished = []
    gate = asyncio.Event()

    async def credit():
        await gate.wait()
        finished.append("done")

    t = asyncio.create_task(credit())
    tracker._bg_tasks.add(t)
    t.add_done_callback(tracker._bg_tasks.discard)

    drainer = asyncio.create_task(tracker.drain(timeout=5))
    await asyncio.sleep(0)
    assert not drainer.done()          # drain is still waiting on the credit
    assert finished == []

    gate.set()
    await drainer
    assert finished == ["done"]        # the credit completed before drain returned


@pytest.mark.asyncio
async def test_drain_lets_a_two_phase_credit_complete_not_torn():
    """The exact CONC-1 shape: a credit that has done its FIRST write and is
    awaiting its SECOND. Without draining, shutdown cancels it here and the
    second write is lost. drain() must let it finish both."""
    tracker = ListenTracker(make_writer_mock(), None)
    writes = []
    between_writes = asyncio.Event()

    async def credit():
        writes.append("play_count")        # first Discogs write lands
        await between_writes.wait()         # <-- the tear window
        writes.append("last_played")        # second write

    t = asyncio.create_task(credit())
    tracker._bg_tasks.add(t)
    t.add_done_callback(tracker._bg_tasks.discard)

    await asyncio.sleep(0)
    assert writes == ["play_count"]         # mid-write: this is where the old code tore it

    drainer = asyncio.create_task(tracker.drain(timeout=5))
    await asyncio.sleep(0)
    assert not drainer.done()               # drain holds shutdown open for the credit
    between_writes.set()
    await drainer
    assert writes == ["play_count", "last_played"]   # both writes landed


@pytest.mark.asyncio
async def test_drain_is_bounded_by_timeout_and_does_not_hang():
    """A stuck credit must not hang shutdown forever — drain returns after the
    timeout (the task is left for loop teardown to cancel)."""
    tracker = ListenTracker(make_writer_mock(), None)
    never = asyncio.Event()

    async def stuck():
        await never.wait()

    t = asyncio.create_task(stuck())
    tracker._bg_tasks.add(t)
    t.add_done_callback(tracker._bg_tasks.discard)

    # If drain ignored its timeout this would hang; wait_for bounds the test.
    await asyncio.wait_for(tracker.drain(timeout=0.05), timeout=2.0)

    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
