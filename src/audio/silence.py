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

# #195: a throttled diagnostic for a miscalibrated input gain. When audio is
# persistently PRESENT but below the music threshold — audible yet never crossing
# `threshold`, so it is treated as silence and never recognized (the recognition
# gate in AudioCapture, #193) — the operator otherwise gets no cards and no error,
# an invisible failure. This surfaces it: audio sustained in the band
# [threshold * _LOW_GAIN_FLOOR_RATIO, threshold) for _LOW_GAIN_WARN_SECONDS emits
# one WARNING, re-logged at most every _LOW_GAIN_WARN_INTERVAL_SECONDS (the #178
# throttle pattern). Below the floor is genuine silence and warns nothing.
_LOW_GAIN_FLOOR_RATIO = 0.25
_LOW_GAIN_WARN_SECONDS = 60
_LOW_GAIN_WARN_INTERVAL_SECONDS = 300

# #195 wall-clock safety net: the normal end of a session is a music→silence
# transition, but a LOCKED GROOVE or a stuck input can hold the RMS above the
# exit threshold indefinitely — that transition then never fires, SESSION_ENDED
# never comes, and the side is never credited (the same immortal-session class the
# recognition gate closes from the other side). After _MAX_MUSIC_SECONDS of
# CONTINUOUS music the detector forces the session to end so the side credits.
# 60min is far longer than any real LP side (~30min max), so it never clips real
# playback — it only catches a genuinely stuck session.
_MAX_MUSIC_SECONDS = 60 * 60


class AudioEvent(Enum):
    MUSIC_STARTED = auto()
    MUSIC_STOPPED = auto()   # Whole window went quiet → end-of-session timer armed (SIL-3)
    SESSION_ENDED = auto()   # Long silence — side/album finished
    # R8-16 (#350): the #195 forced end, distinguished from a genuine-silence
    # SESSION_ENDED.  The tracker treats it as a session end for crediting, but
    # it is NOT a physical spin boundary — music never actually stopped — so the
    # tracker must NOT clear its per-spin credit/scrobble memory on it (a locked
    # groove the recognizer still identifies would otherwise re-credit hourly).
    SESSION_ENDED_FORCED = auto()


class SilenceDetector:
    """Detects silence vs. music and fires lifecycle events.

    Events:
        MUSIC_STARTED  — first music chunk after silence
        MUSIC_STOPPED  — the whole trailing chunk_seconds window dropped below
                         the exit threshold (music → silence); this arms the
                         end-of-session timer.  NOT an inter-track gap: the RMS
                         is computed over the ENTIRE window, so a short 2–6s gap
                         between tracks stays well above threshold and cannot
                         trip it (SIL-3).  Has no external consumer today — it is
                         the internal music→silence transition marker.
        SESSION_ENDED  — silence persists beyond session_end_silence_seconds
        SESSION_ENDED_FORCED — the #195 safety net force-ended a session after
                         _MAX_MUSIC_SECONDS of CONTINUOUS music (locked groove /
                         stuck input).  Both consumers treat it as a session
                         end — the player-state half clears the card and bumps
                         the epoch identically (main.apply_state_silence_effect),
                         and the tracker ends and credits the session — but it
                         is NOT a physical spin boundary, so the tracker's
                         per-spin credit/scrobble memory survives it (R8-16).
                         The detector re-arms its silence timer afterwards, so
                         continued non-music still yields a genuine
                         SESSION_ENDED one window later (cold-review F2).
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
        # #195 low-gain diagnostic: monotonic time since audio first sat in the
        # sub-threshold-but-audible band, and the last time we warned (-inf so the
        # first sustained stretch warns regardless of the monotonic epoch).
        self._low_gain_since: Optional[float] = None
        self._low_gain_last_warned: float = float("-inf")
        # #195 max-session safety: monotonic time the CURRENT continuous music run
        # began (set on silence→music, cleared on music→silence). None = not in a
        # music run.
        self._music_since: Optional[float] = None
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
                self._music_since = None   # #195: music run ended (natural)
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
                self._music_since = now   # #195: start the max-session clock
                self._emit(AudioEvent.MUSIC_STARTED)
            else:
                self._check_session_end(now)

        # #195: after the state is settled, enforce the wall-clock max-session
        # safety, then surface a persistent sub-threshold input level (low gain)
        # that would otherwise fail silently.
        self._check_max_session(now)
        self._check_low_gain(rms, now)

    def _check_low_gain(self, rms: float, now: float):
        """Warn (throttled) when audio is persistently present but below the music
        threshold — the miscalibrated-preamp case that yields no cards and no
        error (#195). Only fires when NOT in music-state and the RMS sits in the
        audible-but-sub-threshold band; genuine near-silence resets the timer."""
        floor = self.threshold * _LOW_GAIN_FLOOR_RATIO
        if not self._is_music and floor <= rms < self.threshold:
            if self._low_gain_since is None:
                self._low_gain_since = now
            elif (
                now - self._low_gain_since >= _LOW_GAIN_WARN_SECONDS
                and now - self._low_gain_last_warned >= _LOW_GAIN_WARN_INTERVAL_SECONDS
            ):
                self._low_gain_last_warned = now
                log.warning(
                    "Audio present but persistently below the silence threshold "
                    "(RMS ≈ %.4f vs threshold %.4f) for ≥%ds — possible low input "
                    "gain / miscalibrated preamp; music at this level is treated as "
                    "silence and will not be recognized. Raise the capture level "
                    "(alsamixer) or the preamp output.",
                    rms, self.threshold, _LOW_GAIN_WARN_SECONDS,
                )
        else:
            self._low_gain_since = None

    def _check_max_session(self, now: float):
        """#195 safety net: force a session end after _MAX_MUSIC_SECONDS of
        CONTINUOUS music.

        The normal end is a music→silence transition, but a locked groove or a
        stuck input can hold the RMS above the exit threshold forever, so that
        transition never fires and the side never credits. Emitting a session end
        directly (rather than arming the silence timer) is deliberate: for a
        locked groove the RMS never actually falls, so an armed silence timer
        would be cancelled by the next music chunk and never fire. Evaluated from
        both process() and tick() so it holds whether or not chunks keep flowing.

        R8-16 (#350): emits SESSION_ENDED_FORCED, not SESSION_ENDED — music never
        actually stopped, so this is NOT a physical spin boundary.  The tracker
        credits the side (unchanged) but keeps its per-spin credit memory, so a
        groove the recognizer still identifies as the closer cannot mint one
        phantom credit per hour until the needle lifts.
        """
        if (
            self._is_music
            and self._music_since is not None
            and now - self._music_since >= _MAX_MUSIC_SECONDS
        ):
            log.warning(
                "Music has played continuously for ≥%ds — forcing session end "
                "(possible locked groove or a stuck input that never returned to "
                "silence); crediting the side now.",
                _MAX_MUSIC_SECONDS,
            )
            self._is_music = False
            self._music_since = None
            # R8-16 cold-review F2: RE-ARM the silence timer instead of latching
            # the session closed (`_silence_since=None; _session_ended=True`, the
            # pre-R8 shape).  If the input never re-crosses the music ENTER
            # threshold after the forced end (a stuck input decaying into the
            # hysteresis dead band — the very miscalibration class #195 exists
            # for), the old latch meant NO event could ever fire again, so the
            # tracker's per-spin credit memory survived indefinitely and ate the
            # next genuine spin's credit.  Re-armed: a locked groove whose RMS
            # sits at or above the ENTER threshold trips MUSIC_STARTED on the
            # next chunk (timer cancelled, nothing spurious), while continued
            # non-music produces a GENUINE SESSION_ENDED one silence window
            # later — the spin boundary the tracker needs.  Documented residual
            # (2nd-pass finding): a groove whose RMS sits INSIDE the hysteresis
            # dead band [exit, enter) trips neither — the re-armed timer fires a
            # genuine SESSION_ENDED while audio continues, clearing the spin
            # memory; if the RMS later wanders above enter, the R8-16 hourly
            # re-credit can resume for that pathological band.  Accepted: the
            # discriminating fix (require an RMS < exit sample before honouring
            # the re-armed timer) is new design, and the alternative latch
            # (F2) silently ate GENUINE credits — worse in the common case.
            self._silence_since = now
            self._session_ended = False
            self._emit(AudioEvent.MUSIC_STOPPED)
            self._emit(AudioEvent.SESSION_ENDED_FORCED)   # R8-16: not a spin boundary

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
        now = time.monotonic()
        self._check_max_session(now)   # #195: force-end a stuck (never-silent) session
        self._check_session_end(now)

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
        self._music_since = None   # #195: the interrupted music run is over
        if was_music and self._silence_since is None and not self._session_ended:
            self._silence_since = time.monotonic()

    @property
    def is_music_playing(self) -> bool:
        return self._is_music
