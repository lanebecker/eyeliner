"""Unit tests for PlayerState — the central observable state object.

This module previously had NO tests, which is exactly how the v1.3.3
set_track() notification bug survived: set_track() routed its notification
through set_status(), which only notifies when the status actually changes.
Every track after the first (status already PLAYING) was therefore silently
swallowed, so the renderer never prefetched new cover art or queued palette
transitions mid-session.

Verifies:
  ✓ Initial state is IDLE with no track/raw
  ✓ set_status notifies on change, stays quiet when unchanged
  ✓ set_track transitions to PLAYING and notifies
  ✓ set_track notifies on EVERY call — including track changes while
    already PLAYING (the v1.3.3 regression test)
  ✓ set_raw does not notify (it's a pre-resolution precursor, not a
    display-relevant change)
  ✓ clear() resets track + raw and notifies via the IDLE transition
  ✓ A listener that raises does not break other listeners

No hardware, network, or pygame required.
"""
from unittest.mock import MagicMock

from src.audio.recognizer import RawRecognitionResult
from src.metadata.models import MetadataSource, TrackMetadata
from src.state.player_state import PlayerState, PlayerStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(title="So What", album="Kind of Blue"):
    return TrackMetadata(
        title=title,
        artist="Miles Davis",
        album=album,
        source=MetadataSource.DISCOGS_COLLECTION,
    )


def make_state_with_listener():
    state = PlayerState()
    listener = MagicMock()
    state.on_change(listener)
    return state, listener


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_initial_state_is_idle():
    state = PlayerState()
    assert state.status == PlayerStatus.IDLE
    assert state.current_track is None
    assert state.current_raw is None


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------

def test_set_status_notifies_on_change():
    state, listener = make_state_with_listener()
    state.set_status(PlayerStatus.LISTENING)
    listener.assert_called_once_with(state)
    assert state.status == PlayerStatus.LISTENING


def test_set_status_does_not_notify_when_unchanged():
    state, listener = make_state_with_listener()
    state.set_status(PlayerStatus.IDLE)  # Already IDLE
    listener.assert_not_called()


# ---------------------------------------------------------------------------
# set_track — including the v1.3.3 regression
# ---------------------------------------------------------------------------

def test_set_track_transitions_to_playing_and_notifies():
    state, listener = make_state_with_listener()
    track = make_track()
    state.set_track(track)
    assert state.status == PlayerStatus.PLAYING
    assert state.current_track is track
    listener.assert_called_once_with(state)


def test_set_track_notifies_on_track_change_while_already_playing():
    """Regression test for the v1.3.3 notification bug.

    Track 2 of an album arrives while status is already PLAYING.  The status
    doesn't change, but listeners (the renderer) must still be told — cover
    prefetch and palette transitions hang off this callback.
    """
    state, listener = make_state_with_listener()
    state.set_track(make_track(title="So What"))
    state.set_track(make_track(title="Freddie Freeloader"))
    assert listener.call_count == 2
    assert state.current_track.title == "Freddie Freeloader"
    assert state.status == PlayerStatus.PLAYING


def test_set_track_notifies_exactly_once_per_call():
    """The first set_track must not double-notify (status change + track change)."""
    state, listener = make_state_with_listener()
    state.set_status(PlayerStatus.LISTENING)
    listener.reset_mock()
    state.set_track(make_track())
    listener.assert_called_once_with(state)


# ---------------------------------------------------------------------------
# set_raw
# ---------------------------------------------------------------------------

def test_set_raw_stores_result_without_notifying():
    state, listener = make_state_with_listener()
    raw = RawRecognitionResult(title="So What", artist="Miles Davis", album="Kind of Blue")
    state.set_raw(raw)
    assert state.current_raw is raw
    listener.assert_not_called()


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

def test_clear_resets_everything_and_notifies():
    state, listener = make_state_with_listener()
    state.set_raw(RawRecognitionResult(title="t", artist="a", album="b"))
    state.set_track(make_track())
    listener.reset_mock()

    state.clear()

    assert state.status == PlayerStatus.IDLE
    assert state.current_track is None
    assert state.current_raw is None
    listener.assert_called_once_with(state)


# ---------------------------------------------------------------------------
# Listener robustness
# ---------------------------------------------------------------------------

def test_raising_listener_does_not_break_other_listeners():
    state = PlayerState()
    bad = MagicMock(side_effect=RuntimeError("listener exploded"))
    good = MagicMock()
    state.on_change(bad)
    state.on_change(good)

    state.set_track(make_track())  # Must not raise

    bad.assert_called_once()
    good.assert_called_once_with(state)
