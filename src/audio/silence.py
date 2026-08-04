"""Silence detection and session event emission.

Calculates RMS energy of each audio chunk to distinguish music from silence.
Emits AudioEvents when music starts, stops, or a full session ends.
"""

import logging
import math
import time
from enum import Enum, auto
from typing import Callable, Optional, TYPE_CHECKING

import numpy as np

from src.util.signal import Signal

if TYPE_CHECKING:
    from src.config import AudioConfig

log = logging.getLogger(__name__)

# SIL-4: hysteresis ratio for the RMS music/silence classifier.  Music is ENTERED
# at `silence_threshold_rms` (the configured threshold) but only LEFT once the RMS
# drops below `threshold * _MUSIC_EXIT_RATIO`.  The dead band between the two stops
# an RMS hovering at a single threshold from flapping MUSIC_STARTED/MUSIC_STOPPED
# every chunk — each flap re-armed the end-of-session timer, so a fade-out or a
# locked groove sitting at the boundary could hold SESSION_ENDED off indefinitely
# and never credit the finished side.  0.5 = the exit bar sits an octave below entry.
_MUSIC_EXIT_RATIO = 0.5


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

    def __init__(self, config: "AudioConfig"):
        self.threshold: float = config.silence_threshold_rms
        # SIL-4: leave music only below a LOWER exit threshold (hysteresis), so an
        # RMS hovering right at `threshold` can't flap MUSIC_STARTED/MUSIC_STOPPED.
        # Enter at `threshold`; exit below `threshold * _MUSIC_EXIT_RATIO`; the dead
        # band [exit_threshold, threshold) holds whichever state is current.
        self.exit_threshold: float = self.threshold * _MUSIC_EXIT_RATIO
        self.session_end_seconds: int = config.session_end_silence_seconds
        # SIL-1: a chunk is a `chunk_seconds`-long *trailing* window, so the
        # silence it reports began chunk_seconds ago, not "now".  Needed to
        # back-date _silence_since so the session-end timer measures real
        # wall-clock silence rather than silence + one window's latency.
        self.chunk_seconds: int = config.chunk_seconds
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

        # SIL-2: a NaN or inf anywhere in the window makes `rms` non-finite (one
        # bad sample out of ~661,500 poisons np.mean, so this must guard the
        # aggregate, not assume the input is finite).  `nan >= threshold` is False
        # in IEEE-754, so an unguarded NaN falls through to the silence branch and
        # FAKES a needle lift — arming the end-of-session timer and, via the
        # wall-clock ticker, possibly firing SESSION_ENDED early; an `inf` is the
        # mirror (`inf >= threshold` is True) and would fake MUSIC_STARTED.  A
        # corrupt chunk is evidence of neither, so skip it and leave detection
        # state untouched; the next clean chunk drives it.
        if not math.isfinite(rms):
            log.warning(
                "Non-finite RMS (%r) in an audio chunk — ignoring it as corrupt "
                "rather than treating it as silence (SIL-2).",
                rms,
            )
            return

        now = time.monotonic()

        # SIL-4: hysteresis — which threshold applies depends on the CURRENT
        # state.  Enter music at `threshold`; leave it only once the RMS falls
        # below the lower `exit_threshold`.  An RMS hovering in the dead band
        # [exit_threshold, threshold) holds the current state instead of flapping.
        if self._is_music:
            if rms < self.exit_threshold:
                # Music → silence: dropped below the lower exit threshold.
                self._is_music = False
                # SIL-1: back-date to the START of this trailing window.  The
                # chunk we just judged silent covers [now - chunk_seconds, now],
                # so the silence has in truth lasted a full window already; arming
                # at `now` would make SESSION_ENDED fire one chunk_seconds late.
                # This removes the chunk_seconds component only — because the
                # first fully-silent window still lands on the hop grid, an
                # up-to-one-hop (chunk_seconds - overlap_seconds) residual
                # remains, so a 45s threshold fires at ~45-55s, not exactly 45s
                # (documented in config.example.yaml).  Fully removing it would
                # require sub-chunk RMS sampling, out of scope here.
                self._silence_since = now - self.chunk_seconds
                self._emit(AudioEvent.MUSIC_STOPPED)
            # else: still music (at or above exit_threshold, incl. the dead band).
        else:
            if rms >= self.threshold:
                # Silence → music: rose to the enter threshold.
                self._is_music = True
                self._silence_since = None
                self._session_ended = False
                self._emit(AudioEvent.MUSIC_STARTED)
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
