"""LastFmClient — Last.fm scrobbling and track-loving via pylast.

All public methods are synchronous (pylast is synchronous). Callers in async
contexts should wrap them in run_in_executor, matching the pattern used by the
Discogs reader/writer throughout the codebase.

The client is a graceful no-op when:
  - the ``lastfm`` section is absent from config
  - ``scrobble_enabled`` is False (or absent)
  - any required credential (api_key, api_secret, session_key) is empty

No exceptions ever propagate out of this module — every failure is logged as
a warning and the method returns False.
"""

import logging
import threading
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import LastFmConfig
    from src.metadata.models import TrackMetadata

log = logging.getLogger(__name__)


class ScrobbleResult(Enum):
    """The three-way outcome of one scrobble attempt (R10-10 / #423).

    ``scrobble()`` collapses every failure into a single ``False``, which cannot
    express the distinction the owner decision requires: retry a *definite*
    failure (the scrobble provably did not apply), but NEVER retry an *ambiguous*
    one (the server may already have applied it) — otherwise a retry could
    double-credit.  :class:`~src.tracking.scrobble_dispatcher.ScrobbleDispatcher`
    acts on this richer result; ``scrobble()`` remains a back-compat shim
    (``result is DELIVERED``).

      * ``DELIVERED`` — accepted by Last.fm, or a graceful no-op (client disabled).
      * ``RETRYABLE`` — a definite failure that provably did not apply: the
        request never reached the service (pylast ``NetworkError``) or the
        service received and *rejected* it (pylast ``WSError``).  Retrying with
        the SAME confirmation timestamp is safe — Last.fm collapses duplicate
        scrobbles that share a (track, timestamp) — so a bounded in-memory retry
        recovers a transient outage without risking a double credit.
      * ``AMBIGUOUS`` — the outcome is unknown: a response arrived but could not
        be parsed (pylast ``MalformedResponseError``), or an unclassified error
        was raised.  The scrobble MAY have applied, so it is treated as
        delivered / no-retry.
    """

    DELIVERED = auto()
    RETRYABLE = auto()
    AMBIGUOUS = auto()


def _classify_scrobble_exception(exc: BaseException) -> "ScrobbleResult":
    """Map an exception raised by the pylast scrobble call to a retry policy.

    Classified by walking the raised type's MRO *names* rather than
    ``isinstance`` against ``pylast.*`` so it is correct whether pylast is the
    real module or a test's ``MagicMock`` stand-in (``isinstance`` against a
    mocked class raises ``TypeError``).  An unrecognised exception is
    conservatively ``AMBIGUOUS`` — never automatically retried — because we
    cannot prove the scrobble did not already apply.
    """
    names = {klass.__name__ for klass in type(exc).__mro__}
    if "MalformedResponseError" in names:
        return ScrobbleResult.AMBIGUOUS
    if "NetworkError" in names or "WSError" in names:
        return ScrobbleResult.RETRYABLE
    return ScrobbleResult.AMBIGUOUS

# R6-25: the literal credential placeholders shipped in config.example.yaml. They
# pass the non-empty credential check but are not real values, so pylast would
# "initialise" and then 401 on every scrobble/love — the likeliest half-done-setup
# state. Detect them and disable with a clear warning instead of a false success.
_LASTFM_PLACEHOLDERS = frozenset({
    "YOUR_LASTFM_API_KEY",
    "YOUR_LASTFM_API_SECRET",
    "YOUR_LASTFM_SESSION_KEY",
})


class LastFmClient:
    """Wraps pylast to scrobble tracks and optionally mark them as Loved.

    Construct once at startup (via main.py) and inject into TrackCommitService
    (scrobble) and ListenTracker (love). pylast is imported lazily so that the module can be
    imported even when pylast is not installed — the client simply disables
    itself in that case.
    """

    def __init__(self, config: "LastFmConfig"):
        self._love_on_completion: bool = config.love_on_completion
        self._network = None  # pylast.LastFMNetwork, or None when disabled

        # CRIT-10: this one client is injected into BOTH TrackCommitService
        # (scrobble) and ListenTracker (love), and each caller dispatches its
        # sync method via run_in_executor — so two DIFFERENT executor threads can
        # touch the single pylast Network object at the same time (a session-end
        # love overlapping a fresh scrobble), and pylast documents no thread-
        # safety guarantee. This lock serializes every Network access below so
        # only one call is ever in flight against it. A threading.Lock (not an
        # asyncio.Lock) is required: the guarded calls run OFF the event loop, on
        # executor threads. Contention is rare and brief (one scrobble/track, one
        # love/album), so the serialization is effectively free.
        self._lock = threading.Lock()

        if not config.scrobble_enabled:
            # R6-24: INFO (was DEBUG) so the disabled state is visible at the
            # shipped log level — otherwise a typo'd `scrobble_enable:` silently
            # defaults scrobbling OFF with zero journal evidence.
            log.info("Last.fm scrobbling is disabled (scrobble_enabled: false).")
            return

        api_key    = config.api_key.strip()
        api_secret = config.api_secret.strip()
        session_key = config.session_key.strip()

        if not all([api_key, api_secret, session_key]):
            log.warning(
                "Last.fm scrobbling is enabled but credentials are incomplete. "
                "Set api_key, api_secret, and session_key in config.yaml. "
                "Run get_lastfm_session_key.py to generate a session key."
            )
            return

        # R6-25: reject the config.example.yaml placeholders — they are non-empty
        # (so they passed the check above) but would 401 at runtime while this
        # __init__ logged a misleading "scrobbling initialised" success.
        if _LASTFM_PLACEHOLDERS & {api_key, api_secret, session_key}:
            log.warning(
                "Last.fm scrobbling is enabled but the credentials are still the "
                "config.example.yaml placeholders — disabling scrobbling. Replace "
                "api_key, api_secret, and session_key in config.yaml with real "
                "values (run get_lastfm_session_key.py for the session key)."
            )
            return

        try:
            import pylast
            self._network = pylast.LastFMNetwork(
                api_key=api_key,
                api_secret=api_secret,
                session_key=session_key,
            )
            log.info("Last.fm scrobbling initialised.")
        except ImportError:
            log.warning(
                "pylast is not installed — Last.fm scrobbling disabled. "
                "Run: pip install pylast"
            )
        except Exception as e:
            log.warning(f"Failed to initialise Last.fm network: {e}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the client is configured and ready to make API calls."""
        return self._network is not None

    @property
    def love_on_completion(self) -> bool:
        """True when the user has opted in to loving tracks on album completion."""
        return self._love_on_completion

    def scrobble_result(self, track: "TrackMetadata", timestamp: int) -> "ScrobbleResult":
        """Submit a scrobble and report the three-way outcome (R10-10 / #423).

        This is the primary entry point used by
        :class:`~src.tracking.scrobble_dispatcher.ScrobbleDispatcher`.  It runs
        synchronously (pylast is synchronous) under the CRIT-10 lock so it is
        safe to dispatch from an executor thread concurrently with :meth:`love`.

        Args:
            track: The confirmed TrackMetadata to scrobble.
            timestamp: Unix timestamp (int) — the confirmation-time stamp bound
                once at commit (#383) and reused unchanged on every retry so
                Last.fm's (track, timestamp) de-duplication prevents a double
                credit.

        Returns:
            :class:`ScrobbleResult` — ``DELIVERED`` on success or when the client
            is disabled (no-op); ``RETRYABLE`` for a definite failure that did
            not apply; ``AMBIGUOUS`` when the outcome is unknown.  Never raises.
        """
        if not self.enabled:
            return ScrobbleResult.DELIVERED  # Graceful no-op

        try:
            with self._lock:  # CRIT-10: serialize access to the shared Network
                self._network.scrobble(
                    artist=track.artist,
                    title=track.title,
                    timestamp=timestamp,
                    album=track.album or None,
                )
            log.info(f"Last.fm scrobbled: {track.artist} — {track.title}")
            return ScrobbleResult.DELIVERED
        except Exception as e:
            outcome = _classify_scrobble_exception(e)
            log.warning(
                "Last.fm scrobble failed (%s — %s): %s [%s]",
                track.artist, track.title, e, outcome.name,
            )
            return outcome

    def scrobble(self, track: "TrackMetadata", timestamp: int) -> bool:
        """Back-compat boolean shim over :meth:`scrobble_result`.

        Returns True only when the scrobble was DELIVERED (success or disabled
        no-op); False for any failure outcome.  Retained so existing callers and
        tests that expect a bool keep working; new code should call
        :meth:`scrobble_result` to honour the retry/no-retry distinction.
        """
        return self.scrobble_result(track, timestamp) is ScrobbleResult.DELIVERED

    def love(self, track: "TrackMetadata") -> bool:
        """Mark a track as Loved on Last.fm.

        Only does anything when ``love_on_completion`` is True *and* the client
        is enabled. This is called by ListenTracker after a full album side
        completes.

        Returns:
            True on success or when the client is disabled / love is off (no-op).
            False on any API or network error.
        """
        if not self.enabled or not self.love_on_completion:
            return True  # Graceful no-op

        try:
            with self._lock:  # CRIT-10: serialize access to the shared Network
                pylast_track = self._network.get_track(track.artist, track.title)
                pylast_track.love()
            log.info(f"Last.fm loved: {track.artist} — {track.title}")
            return True
        except Exception as e:
            log.warning(f"Last.fm love failed ({track.artist} — {track.title}): {e}")
            return False
