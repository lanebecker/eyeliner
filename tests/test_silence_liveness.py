"""Regression tests for B-6 — SESSION_ENDED liveness.

(1) The end-of-session timer must fire on wall-clock time via tick(), even when
    no audio chunks arrive (a stalled/restarting stream) — otherwise a completed
    album's Play Count is never credited.
(2) reset_music_state() must clear a stuck _is_music flag on stream restart so
    recovered audio re-emits MUSIC_STARTED — WITHOUT discarding an in-progress
    silence timer (which would re-create the very bug in (1)).
"""
import numpy as np
import pytest

from src.audio.silence import SilenceDetector, AudioEvent


def make_detector(threshold=0.1, session_end=45):
    cfg = {"audio": {
        "silence_threshold_rms": threshold,
        "session_end_silence_seconds": session_end,
    }}
    d = SilenceDetector(cfg)
    events = []
    d.on_event(events.append)
    return d, events


def loud(n=64):
    return np.ones(n, dtype=np.float32)   # rms 1.0 ≥ threshold → music


def quiet(n=64):
    return np.zeros(n, dtype=np.float32)  # rms 0 → silence


def patch_clock(monkeypatch, holder):
    monkeypatch.setattr("src.audio.silence.time.monotonic", lambda: holder[0])


# ---------------------------------------------------------------------------
# tick() — timer fires without chunks
# ---------------------------------------------------------------------------

def test_tick_fires_session_ended_without_chunks(monkeypatch):
    t = [1000.0]
    d, events = make_detector(session_end=45)
    patch_clock(monkeypatch, t)

    d.process(loud(), 44100)          # MUSIC_STARTED
    t[0] = 1001.0
    d.process(quiet(), 44100)         # MUSIC_STOPPED, silence starts at 1001
    events.clear()

    # No more chunks arrive; wall clock advances past the threshold.
    t[0] = 1001.0 + 45
    d.tick()
    assert AudioEvent.SESSION_ENDED in events


def test_tick_does_not_fire_before_threshold(monkeypatch):
    t = [0.0]
    d, events = make_detector(session_end=45)
    patch_clock(monkeypatch, t)

    d.process(loud(), 1)
    t[0] = 1.0
    d.process(quiet(), 1)
    events.clear()

    t[0] = 10.0  # only 9s of silence
    d.tick()
    assert AudioEvent.SESSION_ENDED not in events


def test_tick_fires_session_ended_only_once(monkeypatch):
    t = [0.0]
    d, events = make_detector(session_end=10)
    patch_clock(monkeypatch, t)

    d.process(loud(), 1)
    t[0] = 1.0
    d.process(quiet(), 1)
    events.clear()

    t[0] = 100.0
    d.tick()
    d.tick()  # subsequent ticks must not re-fire
    assert events.count(AudioEvent.SESSION_ENDED) == 1


# ---------------------------------------------------------------------------
# reset_music_state() — clears stuck music flag, preserves silence timer
# ---------------------------------------------------------------------------

def test_reset_music_state_reemits_music_started():
    d, events = make_detector()
    d.process(loud(), 1)              # MUSIC_STARTED, _is_music True
    events.clear()

    d.reset_music_state()
    assert d.is_music_playing is False

    d.process(loud(), 1)             # music again → MUSIC_STARTED re-emitted
    assert AudioEvent.MUSIC_STARTED in events


def test_reset_during_music_then_silence_still_fires_session_ended(monkeypatch):
    """The bug a naive reset introduces: stream errors mid-music, the album ends
    during the outage, and the stream recovers straight into silence.  Forcing
    _is_music False means process() never sees the music→silence edge that arms
    the timer — so reset_music_state() must arm it, or the completed album's
    SESSION_ENDED is lost forever."""
    t = [1000.0]
    d, events = make_detector(session_end=45)
    patch_clock(monkeypatch, t)

    d.process(loud(), 1)             # music playing, _is_music True
    d.reset_music_state()            # stream error mid-music → restart
    events.clear()

    t[0] = 1001.0
    d.process(quiet(), 1)            # recovers into silence (album already ended)
    t[0] = 1001.0 + 45
    d.tick()
    assert AudioEvent.SESSION_ENDED in events


def test_reset_at_startup_does_not_arm_timer(monkeypatch):
    """The first reset (startup, no music ever played) must NOT arm the timer —
    otherwise SESSION_ENDED would fire spuriously after 45s of idle."""
    t = [0.0]
    d, events = make_detector(session_end=45)
    patch_clock(monkeypatch, t)

    d.reset_music_state()            # startup: was_music False
    t[0] = 100.0
    d.tick()
    assert AudioEvent.SESSION_ENDED not in events


def test_reset_music_state_preserves_silence_timer(monkeypatch):
    """The critical correctness property: resetting on a stream restart that
    happens DURING silence must not strand the end-of-session timer."""
    t = [0.0]
    d, events = make_detector(session_end=45)
    patch_clock(monkeypatch, t)

    d.process(loud(), 1)             # music
    t[0] = 1.0
    d.process(quiet(), 1)            # silence starts at t=1
    events.clear()

    d.reset_music_state()            # stream restarts mid-silence
    t[0] = 1.0 + 45
    d.tick()                         # timer must STILL fire
    assert AudioEvent.SESSION_ENDED in events
