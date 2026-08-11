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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import LastFmConfig
    from src.metadata.models import TrackMetadata

log = logging.getLogger(__name__)

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

    def scrobble(self, track: "TrackMetadata", timestamp: int) -> bool:
        """Submit a scrobble to Last.fm.

        Args:
            track: The confirmed TrackMetadata to scrobble.
            timestamp: Unix timestamp (int) of when the track started playing.

        Returns:
            True on success or when the client is disabled (no-op).
            False on any API or network error.
        """
        if not self.enabled:
            return True  # Graceful no-op

        try:
            with self._lock:  # CRIT-10: serialize access to the shared Network
                self._network.scrobble(
                    artist=track.artist,
                    title=track.title,
                    timestamp=timestamp,
                    album=track.album or None,
                )
            log.info(f"Last.fm scrobbled: {track.artist} — {track.title}")
            return True
        except Exception as e:
            log.warning(f"Last.fm scrobble failed ({track.artist} — {track.title}): {e}")
            return False

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
