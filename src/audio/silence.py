"""Silence detection and session event emission.

Calculates RMS energy of each audio chunk to distinguish music from silence.
Emits AudioEvents when music starts, stops, or a full session ends.
"""

import logging
import time
from enum import Enum, auto
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)


class AudioEvent(Enum):
    MUSIC_STARTED = auto()
    MUSIC_STOPPED = auto()   # Short silence — inter-track gap
    SESSION_ENDED = auto()   # Long silence — side/album finished


class SilenceDetector:
    """Detects silence vs. music and fires lifecycle events.

    Events:
        MUSIC_STARTED  — first music chunk after silence
        MUSIC_STOPPED  — RMS drops below threshold
        SESSION_ENDED  — silence persists beyond session_end_silence_seconds
    """

    def __init__(self, config: dict):
        cfg = config["audio"]
        self.threshold: float = cfg["silence_threshold_rms"]
        self.session_end_seconds: int = cfg["session_end_silence_seconds"]
        self._is_music = False
        self._silence_since: Optional[float] = None
        self._session_ended = False
        self._listeners: list[Callable[[AudioEvent], None]] = []

    def on_event(self, callback: Callable[[AudioEvent], None]):
        """Register a callback to receive AudioEvents."""
        self._listeners.append(callback)

    def _emit(self, event: AudioEvent):
        log.debug(f"SilenceDetector → {event.name}")
        for cb in self._listeners:
            cb(event)

    def process(self, audio: np.ndarray, sample_rate: int):
        """Process one audio chunk. Called synchronously from AudioCapture."""
        rms = float(np.sqrt(np.mean(audio ** 2)))
        now = time.monotonic()

        if rms >= self.threshold:
            # Music is playing
            if not self._is_music:
                self._is_music = True
                self._silence_since = None
                self._session_ended = False
                self._emit(AudioEvent.MUSIC_STARTED)
        else:
            # Silence
            if self._is_music:
                self._is_music = False
                self._silence_since = now
                self._emit(AudioEvent.MUSIC_STOPPED)
            elif self._silence_since is not None and not self._session_ended:
                elapsed = now - self._silence_since
                if elapsed >= self.session_end_seconds:
                    self._session_ended = True
                    self._emit(AudioEvent.SESSION_ENDED)

    @property
    def is_music_playing(self) -> bool:
        return self._is_music
