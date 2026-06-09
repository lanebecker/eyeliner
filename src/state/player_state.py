"""Central player state — the single source of truth all components read from.

Intentionally simple: in-memory only, no persistence between reboots.
"""

import logging
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.metadata.models import TrackMetadata
    from src.audio.recognizer import RawRecognitionResult

log = logging.getLogger(__name__)


class PlayerStatus(Enum):
    """Display-facing player status.

    Note: AudioEvent.SESSION_ENDED (src/audio/silence.py) is a separate
    concept — when it fires, main.py calls clear(), which transitions
    directly to IDLE.  A PlayerStatus.SESSION_ENDED value existed through
    v1.3.3 but was never set by any code path and was removed in v1.3.4.
    """
    IDLE = auto()           # Startup or after session ended
    LISTENING = auto()      # Music detected, awaiting first recognition
    PLAYING = auto()        # Track identified and displayed


class PlayerState:
    """Holds the current status, raw recognition result, and resolved track metadata."""

    def __init__(self):
        self.status: PlayerStatus = PlayerStatus.IDLE
        self.current_track: Optional["TrackMetadata"] = None
        self.current_raw: Optional["RawRecognitionResult"] = None
        self._listeners: list = []

    def on_change(self, callback):
        """Register a callback to be called whenever state changes."""
        self._listeners.append(callback)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb(self)
            except Exception as e:
                log.error(f"State change listener error: {e}")

    def set_status(self, status: PlayerStatus):
        if self.status != status:
            log.debug(f"PlayerStatus: {self.status.name} → {status.name}")
            self.status = status
            self._notify()

    def set_raw(self, raw: "RawRecognitionResult"):
        """Set the raw recognition result (pre-resolution)."""
        self.current_raw = raw

    def set_track(self, track: "TrackMetadata"):
        """Set the fully resolved track metadata and transition to PLAYING.

        Listeners are notified exactly once on EVERY call — including when the
        status is already PLAYING.  Track changes mid-session don't change the
        status, but consumers (e.g. DisplayRenderer, which prefetches cover art
        and queues palette transitions from its state-change callback) still
        need to hear about them.  Relying on set_status() alone would silently
        swallow every track change after the first (v1.3.3 bug fix).
        """
        self.current_track = track
        if self.status != PlayerStatus.PLAYING:
            self.set_status(PlayerStatus.PLAYING)  # status change → notifies
        else:
            log.debug(f"Track changed while PLAYING: {track.artist} — {track.title}")
            self._notify()  # status unchanged, but the track did change

    def clear(self):
        """Reset to idle state (call on SESSION_ENDED)."""
        self.current_track = None
        self.current_raw = None
        self.set_status(PlayerStatus.IDLE)
