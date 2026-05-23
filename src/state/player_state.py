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
    IDLE = auto()           # Startup or after session ended
    LISTENING = auto()      # Music detected, awaiting first recognition
    PLAYING = auto()        # Track identified and displayed
    SESSION_ENDED = auto()  # Silence threshold crossed, transitioning to idle


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
        """Set the fully resolved track metadata and transition to PLAYING."""
        self.current_track = track
        self.set_status(PlayerStatus.PLAYING)

    def clear(self):
        """Reset to idle state (call on SESSION_ENDED)."""
        self.current_track = None
        self.current_raw = None
        self.set_status(PlayerStatus.IDLE)
