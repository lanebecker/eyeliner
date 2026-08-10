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

    def epoch_guard(self, audio_epoch: int) -> "EpochGuard":
        """Bind an :class:`EpochGuard` to this audio's session epoch (arch-1/#217).

        Call once at commit entry with the AUDIO's OWN epoch — bound at capture
        time by the recognition loop (PCONC-1), never re-sampled here — and thread
        the returned guard through every commit-path side effect so the
        "re-validate after each await" invariant lives in one place.
        """
        return EpochGuard(self, audio_epoch)


class EpochGuard:
    """The commit-path session-epoch invariant, in ONE named home (arch-1/#217).

    The rule five separate bugs each violated — B-1 (#1), PCONC-1 (#80),
    B-19 (#68), LB-1 (#84), CONC-6 (#87) — is: *after any await in the commit
    path, the session the audio came from may have ENDED* (a needle lift bumps
    ``session_epoch`` via :meth:`PlayerState.clear`), so re-validate before the
    next side effect. Each of those bugs was one await missing one re-check.

    A guard binds the audio's own epoch once (via :meth:`PlayerState.epoch_guard`)
    and every commit-path step checks it through this object instead of
    re-deriving ``state.session_epoch != audio_epoch`` inline. New side effects
    compose with :meth:`run` rather than each needing a hand-remembered check —
    so the invariant is greppable (one type) and an unguarded ``await`` in the
    commit path reads as a pattern violation.

    Event-loop-thread-only, like the ``PlayerState`` it reads (A-12): it holds no
    snapshot, only a reference, so :meth:`still_current` / :meth:`is_stale` always
    read the LIVE epoch — which is why they are passed as bound methods to
    collaborators that re-check after an await (never a precomputed bool).
    """

    __slots__ = ("_state", "_audio_epoch")

    def __init__(self, state: "PlayerState", audio_epoch: int):
        self._state = state
        self._audio_epoch = audio_epoch

    def still_current(self) -> bool:
        """True while the audio's session is still the live one."""
        return self._state.session_epoch == self._audio_epoch

    def is_stale(self) -> bool:
        """True once the audio's session has ended (the needle lifted).

        Passed as a BOUND METHOD to collaborators that re-evaluate it AFTER
        acquiring a lock — the tracker's CONC-6 post-lock drop — so they read the
        live epoch, not a snapshot taken before the wait.
        """
        return self._state.session_epoch != self._audio_epoch

    async def run(self, step):
        """Await ``step()`` ONLY while the session is still current; else skip.

        THE sanctioned way to add a new commit-path side effect that follows an
        await (a v1.6 play-history append, say): routing it through ``run`` means
        its staleness re-check cannot be forgotten — the recurrence class #217
        closes. ``step`` is a ZERO-ARG CALLABLE returning an awaitable (a factory,
        so a skipped step's coroutine is never created — no "coroutine was never
        awaited" warning). Returns the step's result, or ``None`` when skipped.
        """
        if not self.still_current():
            return None
        return await step()
