"""TrackCommitService — the application-layer coordinator for committing a
confirmed track (A-9).

This sequence used to live in ``RecognitionLoop._commit_track`` (the audio
layer), where a low-level recognition component injected and drove four
high-level collaborators — ``state``, ``resolver``, ``tracker``, ``lastfm`` —
and owned the cross-cutting commit.  That inverted the dependency direction and
made the loop untestable without the whole stack (the scrobble branch was never
exercised because tests never passed a ``lastfm`` — T-2).

The audio layer now only *confirms* a :class:`RawRecognitionResult` and hands it
off; this service owns resolve → state → track → scrobble.  The two correctness
invariants that lived in the old ``_commit_track`` are preserved exactly:

  * **B-1 (epoch guard).** ``resolve()`` yields the event loop; a SESSION_ENDED
    (needle lift) during it runs ``state.clear()`` and bumps the session epoch.
    The epoch captured *before* resolving is re-checked *after*; a commit for
    audio that already stopped is discarded rather than resurrecting a dead
    track onto the screen or corrupting a fresh session.
  * **B-11 (ordering).** ``set_raw`` is advanced only *after* ``set_track``
    succeeds — otherwise ``current_raw`` would lead ``current_track`` and the
    loop's dedup would treat the new track as "already playing" and never
    re-attempt it.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.audio.recognizer import RawRecognitionResult
    from src.metadata.resolver import MetadataResolver
    from src.state.player_state import PlayerState
    from src.tracking.lastfm_client import LastFmClient
    from src.tracking.listen_tracker import ListenTracker

log = logging.getLogger(__name__)


class TrackCommitService:
    """Owns the resolve → state → track → scrobble commit for a confirmed track.

    Constructed once at startup and handed the same ``PlayerState`` the
    recognition loop reads, plus the metadata resolver, the listen tracker, and
    (optionally) the Last.fm client.  :meth:`commit` is wired as the recognition
    loop's ``on_confirmed`` callback.
    """

    def __init__(
        self,
        state: "PlayerState",
        resolver: "MetadataResolver",
        tracker: "ListenTracker",
        lastfm: Optional["LastFmClient"] = None,
    ):
        self.state = state
        self.resolver = resolver
        self.tracker = tracker
        self.lastfm = lastfm

    async def commit(self, raw: "RawRecognitionResult") -> bool:
        """Resolve full metadata for *raw* and commit it everywhere.

        Returns ``True`` when the track was committed, ``False`` when the commit
        was discarded because the session ended mid-resolve (B-1).  Resolver
        exceptions are NOT swallowed — they propagate to the recognition loop's
        ``run()`` handler, exactly as the old ``_commit_track`` did, so a
        transient resolve failure leaves ``current_raw`` un-advanced (B-11) and
        the track is re-attempted on the next chunk.
        """
        timestamp = int(time.time())
        # Capture the session token before the resolve await (B-1).
        commit_epoch = self.state.session_epoch
        metadata = await self.resolver.resolve(raw)
        if self.state.session_epoch != commit_epoch:
            log.info(
                "Discarding stale commit for %s — %s: the session ended while "
                "metadata was resolving.",
                raw.artist, raw.title,
            )
            return False

        self.state.set_track(metadata)
        # Advance current_raw only AFTER the resolved track is displayed (B-11).
        self.state.set_raw(raw)
        await self.tracker.on_track_identified(metadata)
        log.info(
            f"Now playing: {metadata.artist} / {metadata.album} / "
            f"{metadata.title} [{metadata.source.name}]"
        )

        if self.lastfm:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.lastfm.scrobble, metadata, timestamp
                )
            except Exception as e:
                log.warning(f"Last.fm scrobble error: {e}")

        return True
