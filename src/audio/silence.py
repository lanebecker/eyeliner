"""Silence detection and session event emission.

Calculates RMS energy of each audio chunk to distinguish music from silence.
Emits AudioEvents when music starts, stops, or a full session ends.
"""

import logging
import time
from enum import Enum, auto
from typing import Callable, Optional

import numpy as np

from src.util.signal import Signal

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
        # Shared Signal: log-and-continue delivery, so a throwing listener no
        # longer kills delivery to the rest mid-process() (A-11).
        self._on_event: "Signal[AudioEvent]" = Signal("SilenceDetector")

    def on_event(self, callback: Callable[[AudioEvent], None]):
        """Register a callback to receive AudioEvents."""
        self._on_event.connect(callback)

    def _emit(self, event: AudioEvent):
        log.debug(f"SilenceDetector → {event.name}")
        self._on_event.emit(event)

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
            else:
                self._check_session_end(now)

    def _check_session_end(self, now: float):
        """Fire SESSION_ENDED if sustained silence has elapsed.  Idempotent.

        Factored out so both process() (chunk-driven) and tick() (time-driven)
        evaluate the end-of-session timer identically.
        """
        if (
            not self._is_music
            and self._silence_since is not None
            and not self._session_ended
            and now - self._silence_since >= self.session_end_seconds
        ):
            self._session_ended = True
            self._emit(AudioEvent.SESSION_ENDED)

    def tick(self):
        """Re-evaluate the end-of-session timer WITHOUT a new audio chunk (B-6).

        process() only runs when a chunk arrives, so if capture stalls during
        silence (an InputStream error parks the loop in its retry sleep, or the
        block queue drains) the 45s timer is never sampled and a completed
        album is never credited.  A periodic caller (AudioCapture) invokes this
        so the timer fires on wall-clock time regardless of chunk flow.
        """
        self._check_session_end(time.monotonic())

    def reset_music_state(self):
        """Reconcile detection state on audio-stream (re)start (B-6).

        Two failure modes are handled:

        1. A >45s mid-music stall leaves _is_music=True, so when audio returns
           process() sees no False→True transition and never emits
           MUSIC_STARTED.  Forcing _is_music False fixes that.

        2. BUT forcing _is_music False also means the normal music→silence
           transition — the ONLY place process() arms _silence_since — won't be
           observed if the album ended *during* the outage and the stream
           recovers straight into silence.  Without arming the timer here, that
           completed album's SESSION_ENDED would never fire and its Play Count
           would be lost (the exact bug B-6 fixes, via a different door).

        So: if music was interrupted, arm the end-of-session timer now (unless
        it's already armed, or a session already ended).  If music actually
        resumes instead, the next loud chunk's MUSIC_STARTED clears it.  A reset
        during already-tracked silence leaves the existing timer untouched.
        """
        was_music = self._is_music
        self._is_music = False
        if was_music and self._silence_since is None and not self._session_ended:
            self._silence_since = time.monotonic()

    @property
    def is_music_playing(self) -> bool:
        return self._is_music
