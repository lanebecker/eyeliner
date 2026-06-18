"""Central player state — the single source of truth all components read from.

Intentionally simple: in-memory only, no persistence between reboots.
"""

import logging
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from src.util.signal import Signal

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
    ERROR = auto()          # Music detected but recognition repeatedly failed
                            # (v1.4.1 — "NO MATCH FOUND"; recovered by
                            # repositioning the needle or a successful commit)


class PlayerState:
    """Holds the current status, raw recognition result, and resolved track metadata.

    Threading contract (A-12): PlayerState is **event-loop-thread-only**.  There
    is no locking; correctness relies entirely on cooperative single-threaded
    asyncio — every mutation (set_status/set_track/set_raw/clear) and every
    on_change listener runs on the one event-loop thread.  `_notify` invokes
    listeners **synchronously inside the setter**, so a state mutation has
    re-entrant side effects (e.g. the display's on_change prefetches cover art
    and queues palette work).  Listeners must not block.  This synchronous-hub
    design is the structural soil B-1's stale-commit race grew in, which is why
    set_track is epoch-guarded at its one caller (see session_epoch / B-1).
    """

    def __init__(self):
        self.status: PlayerStatus = PlayerStatus.IDLE
        self.current_track: Optional["TrackMetadata"] = None
        self.current_raw: Optional["RawRecognitionResult"] = None
        self._on_change: "Signal[PlayerState]" = Signal("PlayerState")
        # Monotonic session token, bumped every time a session ends (clear()).
        # A coroutine that yields the loop across an await (e.g. metadata
        # resolution) can capture this before and compare after: if it changed,
        # the needle lifted and the session ended mid-flight, so whatever the
        # coroutine was about to commit is stale and must be dropped (B-1).
        self.session_epoch: int = 0

    def on_change(self, callback):
        """Register a callback to be called whenever state changes."""
        self._on_change.connect(callback)

    def _notify(self):
        self._on_change.emit(self)

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
        """Reset to idle state (call on SESSION_ENDED).

        Bumps session_epoch so any in-flight commit that began before the
        needle lifted can detect that its session ended and discard itself
        instead of resurrecting a stale track onto the screen (B-1).
        """
        self.current_track = None
        self.current_raw = None
        self.session_epoch += 1
        self.set_status(PlayerStatus.IDLE)
