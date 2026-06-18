"""Unit tests for SilenceDetector.

Fabricates music (scaled noise) and silence (zeros) as numpy arrays to drive
the detector without any microphone, USB audio interface, or Raspberry Pi.

time.monotonic is patched to control the SESSION_ENDED timer precisely.
"""
from unittest.mock import patch
import numpy as np

from src.audio.silence import SilenceDetector, AudioEvent
from tests.factories import make_audio_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100


def make_config(threshold=0.01, session_end_seconds=45):
    return make_audio_config(
        silence_threshold_rms=threshold,
        session_end_silence_seconds=session_end_seconds,
    )


def music_chunk(rms=0.05, samples=4096):
    """Return a numpy array whose RMS equals `rms` (simulates music above threshold)."""
    noise = np.random.randn(samples).astype(np.float32)
    actual_rms = float(np.sqrt(np.mean(noise ** 2)))
    return noise * (rms / actual_rms)


def silence_chunk(samples=4096):
    """Return a numpy array of all zeros (RMS = 0, silence below any threshold)."""
    return np.zeros(samples, dtype=np.float32)


def collect(detector):
    """Return a fresh events list and register it as a listener."""
    events = []
    detector.on_event(events.append)
    return events


# ---------------------------------------------------------------------------
# RMS sanity checks
# ---------------------------------------------------------------------------

def test_music_chunk_rms_exceeds_threshold():
    chunk = music_chunk(rms=0.05)
    assert float(np.sqrt(np.mean(chunk ** 2))) >= 0.01


def test_silence_chunk_rms_is_zero():
    chunk = silence_chunk()
    assert float(np.sqrt(np.mean(chunk ** 2))) == 0.0


# ---------------------------------------------------------------------------
# MUSIC_STARTED
# ---------------------------------------------------------------------------

def test_music_started_fires_on_first_music_chunk():
    detector = SilenceDetector(make_config())
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)
    assert AudioEvent.MUSIC_STARTED in events


def test_music_started_fires_only_once_for_continuous_music():
    detector = SilenceDetector(make_config())
    events = collect(detector)
    for _ in range(5):
        detector.process(music_chunk(), SAMPLE_RATE)
    assert events.count(AudioEvent.MUSIC_STARTED) == 1


def test_no_event_on_silence_at_startup():
    """Pure silence from the start emits nothing."""
    detector = SilenceDetector(make_config())
    events = collect(detector)
    for _ in range(10):
        detector.process(silence_chunk(), SAMPLE_RATE)
    assert events == []


# ---------------------------------------------------------------------------
# MUSIC_STOPPED
# ---------------------------------------------------------------------------

def test_music_stopped_fires_when_silence_follows_music():
    detector = SilenceDetector(make_config())
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)
    detector.process(silence_chunk(), SAMPLE_RATE)
    assert AudioEvent.MUSIC_STOPPED in events


def test_music_stopped_not_fired_without_prior_music():
    detector = SilenceDetector(make_config())
    events = collect(detector)
    detector.process(silence_chunk(), SAMPLE_RATE)
    assert AudioEvent.MUSIC_STOPPED not in events


def test_music_stopped_fires_only_once_per_gap():
    detector = SilenceDetector(make_config())
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)
    detector.process(silence_chunk(), SAMPLE_RATE)
    detector.process(silence_chunk(), SAMPLE_RATE)  # Second silent chunk
    assert events.count(AudioEvent.MUSIC_STOPPED) == 1


# ---------------------------------------------------------------------------
# Inter-track gap (MUSIC_STOPPED then MUSIC_STARTED again)
# ---------------------------------------------------------------------------

def test_music_started_fires_again_after_inter_track_gap():
    """A brief silence followed by more music should re-fire MUSIC_STARTED."""
    detector = SilenceDetector(make_config())
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)   # Track 1
    detector.process(silence_chunk(), SAMPLE_RATE) # Gap
    detector.process(music_chunk(), SAMPLE_RATE)   # Track 2
    assert events.count(AudioEvent.MUSIC_STARTED) == 2
    assert events.count(AudioEvent.MUSIC_STOPPED) == 1


def test_event_order_for_music_gap_music():
    detector = SilenceDetector(make_config())
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)
    detector.process(silence_chunk(), SAMPLE_RATE)
    detector.process(music_chunk(), SAMPLE_RATE)
    assert events == [
        AudioEvent.MUSIC_STARTED,
        AudioEvent.MUSIC_STOPPED,
        AudioEvent.MUSIC_STARTED,
    ]


# ---------------------------------------------------------------------------
# SESSION_ENDED timing (time.monotonic patched)
# ---------------------------------------------------------------------------

def test_session_ended_fires_after_sustained_silence():
    detector = SilenceDetector(make_config(session_end_seconds=45))
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)

    base = 1000.0
    with patch("src.audio.silence.time.monotonic") as t:
        t.return_value = base
        detector.process(silence_chunk(), SAMPLE_RATE)  # MUSIC_STOPPED; _silence_since = base

        t.return_value = base + 44.9
        detector.process(silence_chunk(), SAMPLE_RATE)
        assert AudioEvent.SESSION_ENDED not in events  # Not yet

        t.return_value = base + 45.0
        detector.process(silence_chunk(), SAMPLE_RATE)

    assert AudioEvent.SESSION_ENDED in events


def test_session_ended_not_fired_before_threshold():
    detector = SilenceDetector(make_config(session_end_seconds=45))
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)

    base = 1000.0
    with patch("src.audio.silence.time.monotonic") as t:
        t.return_value = base
        detector.process(silence_chunk(), SAMPLE_RATE)

        t.return_value = base + 40.0
        detector.process(silence_chunk(), SAMPLE_RATE)

    assert AudioEvent.SESSION_ENDED not in events


def test_session_ended_fires_only_once():
    """SESSION_ENDED must not re-fire on every subsequent silence chunk."""
    detector = SilenceDetector(make_config(session_end_seconds=45))
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)

    base = 1000.0
    with patch("src.audio.silence.time.monotonic") as t:
        t.return_value = base
        detector.process(silence_chunk(), SAMPLE_RATE)

        t.return_value = base + 50.0
        detector.process(silence_chunk(), SAMPLE_RATE)  # SESSION_ENDED fires

        t.return_value = base + 60.0
        detector.process(silence_chunk(), SAMPLE_RATE)  # Should NOT fire again

        t.return_value = base + 120.0
        detector.process(silence_chunk(), SAMPLE_RATE)  # Still should not fire

    assert events.count(AudioEvent.SESSION_ENDED) == 1


def test_session_ended_not_fired_from_startup_silence():
    """Silence at startup (needle never dropped) must never trigger SESSION_ENDED."""
    detector = SilenceDetector(make_config(session_end_seconds=45))
    events = collect(detector)

    # _silence_since is never set because MUSIC_STOPPED never fired
    base = 1000.0
    with patch("src.audio.silence.time.monotonic") as t:
        for i in range(200):
            t.return_value = base + i
            detector.process(silence_chunk(), SAMPLE_RATE)

    assert AudioEvent.SESSION_ENDED not in events


def test_new_music_after_session_ended_fires_fresh_music_started():
    """After SESSION_ENDED, dropping the needle again should start a new session."""
    detector = SilenceDetector(make_config(session_end_seconds=45))
    events = collect(detector)
    detector.process(music_chunk(), SAMPLE_RATE)  # Side A

    base = 1000.0
    with patch("src.audio.silence.time.monotonic") as t:
        t.return_value = base
        detector.process(silence_chunk(), SAMPLE_RATE)
        t.return_value = base + 50.0
        detector.process(silence_chunk(), SAMPLE_RATE)  # SESSION_ENDED

    detector.process(music_chunk(), SAMPLE_RATE)  # Side B starts
    assert events.count(AudioEvent.MUSIC_STARTED) == 2
    assert events.count(AudioEvent.SESSION_ENDED) == 1


def test_session_ended_resets_flag_for_next_session():
    """After new music resets the state, SESSION_ENDED can fire again for the next session."""
    detector = SilenceDetector(make_config(session_end_seconds=45))
    events = collect(detector)

    # First session
    detector.process(music_chunk(), SAMPLE_RATE)
    base = 1000.0
    with patch("src.audio.silence.time.monotonic") as t:
        t.return_value = base
        detector.process(silence_chunk(), SAMPLE_RATE)
        t.return_value = base + 50.0
        detector.process(silence_chunk(), SAMPLE_RATE)  # SESSION_ENDED #1

    # Second session
    detector.process(music_chunk(), SAMPLE_RATE)
    with patch("src.audio.silence.time.monotonic") as t:
        t.return_value = base + 200.0
        detector.process(silence_chunk(), SAMPLE_RATE)
        t.return_value = base + 250.0
        detector.process(silence_chunk(), SAMPLE_RATE)  # SESSION_ENDED #2

    assert events.count(AudioEvent.SESSION_ENDED) == 2


# ---------------------------------------------------------------------------
# is_music_playing property
# ---------------------------------------------------------------------------

def test_is_music_playing_false_initially():
    assert SilenceDetector(make_config()).is_music_playing is False


def test_is_music_playing_true_during_music():
    detector = SilenceDetector(make_config())
    detector.process(music_chunk(), SAMPLE_RATE)
    assert detector.is_music_playing is True


def test_is_music_playing_false_after_silence():
    detector = SilenceDetector(make_config())
    detector.process(music_chunk(), SAMPLE_RATE)
    detector.process(silence_chunk(), SAMPLE_RATE)
    assert detector.is_music_playing is False


# ---------------------------------------------------------------------------
# Multiple listeners
# ---------------------------------------------------------------------------

def test_all_registered_listeners_receive_events():
    detector = SilenceDetector(make_config())
    events_a, events_b, events_c = [], [], []
    detector.on_event(events_a.append)
    detector.on_event(events_b.append)
    detector.on_event(events_c.append)

    detector.process(music_chunk(), SAMPLE_RATE)

    for events in (events_a, events_b, events_c):
        assert AudioEvent.MUSIC_STARTED in events


# ---------------------------------------------------------------------------
# Custom threshold
# ---------------------------------------------------------------------------

def test_custom_threshold_respected():
    """A very high threshold should treat normal music as silence."""
    detector = SilenceDetector(make_config(threshold=0.99))
    events = collect(detector)
    detector.process(music_chunk(rms=0.05), SAMPLE_RATE)  # RMS 0.05 < threshold 0.99
    assert AudioEvent.MUSIC_STARTED not in events


def test_signal_just_at_threshold_is_music():
    """RMS exactly at threshold should be treated as music (>= comparison).

    Uses a constant float64 array so RMS is exactly equal to the threshold.
    float32 cannot represent 0.01 exactly (nearest value is ~0.009999999776),
    which would land just below the float64 threshold and cause a spurious miss.
    With float64, every sample stores the same bit pattern as self.threshold,
    so mean(x**2) == threshold**2 and sqrt → threshold exactly.
    """
    threshold = 0.01
    detector = SilenceDetector(make_config(threshold=threshold))
    events = collect(detector)
    # float64 (numpy default) — 0.01 has the same representation in the array
    # and in self.threshold, so the >= comparison resolves to exactly True.
    exact_chunk = np.full(4096, threshold)  # dtype=float64 by default
    detector.process(exact_chunk, SAMPLE_RATE)
    assert AudioEvent.MUSIC_STARTED in events
