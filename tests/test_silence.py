"""Tests for SilenceDetector."""

import numpy as np
import pytest

from src.audio.silence import SilenceDetector, AudioEvent


SAMPLE_CONFIG = {
    "audio": {
        "silence_threshold_rms": 0.01,
        "session_end_silence_seconds": 45,
    }
}


def make_audio(rms: float, samples: int = 44100) -> np.ndarray:
    """Generate a sine wave with approximately the given RMS level."""
    if rms == 0:
        return np.zeros(samples, dtype=np.float32)
    t = np.linspace(0, 1, samples)
    amplitude = rms * np.sqrt(2)
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


class TestSilenceDetector:
    def setup_method(self):
        self.detector = SilenceDetector(SAMPLE_CONFIG)
        self.events: list[AudioEvent] = []
        self.detector.on_event(self.events.append)

    def test_music_started_on_first_loud_chunk(self):
        self.detector.process(make_audio(rms=0.5), 44100)
        assert AudioEvent.MUSIC_STARTED in self.events

    def test_music_stopped_after_loud_then_quiet(self):
        self.detector.process(make_audio(rms=0.5), 44100)
        self.detector.process(make_audio(rms=0.0), 44100)
        assert AudioEvent.MUSIC_STOPPED in self.events

    def test_no_events_on_continuous_silence(self):
        self.detector.process(make_audio(rms=0.0), 44100)
        assert len(self.events) == 0

    def test_no_double_music_started(self):
        self.detector.process(make_audio(rms=0.5), 44100)
        self.detector.process(make_audio(rms=0.5), 44100)
        started = [e for e in self.events if e == AudioEvent.MUSIC_STARTED]
        assert len(started) == 1

    def test_session_ended_after_sustained_silence(self):
        self.detector.process(make_audio(rms=0.5), 44100)
        self.detector.process(make_audio(rms=0.0), 44100)
        # Fast-forward the silence clock
        self.detector._silence_since -= 50
        self.detector.process(make_audio(rms=0.0), 44100)
        assert AudioEvent.SESSION_ENDED in self.events

    def test_session_ended_not_fired_during_inter_track_gap(self):
        """Short silence between tracks should NOT fire SESSION_ENDED."""
        self.detector.process(make_audio(rms=0.5), 44100)
        self.detector.process(make_audio(rms=0.0), 44100)
        self.detector._silence_since -= 3  # Only 3 seconds
        self.detector.process(make_audio(rms=0.0), 44100)
        assert AudioEvent.SESSION_ENDED not in self.events
