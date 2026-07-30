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

  * **B-1 / PCONC-1 (epoch guard).** Every chunk carries the session epoch it
    was captured under (bound at enqueue in the recognition loop, and passed to
    :meth:`commit` as ``audio_epoch``).  A SESSION_ENDED (needle lift) bumps the
    session epoch via ``state.clear()`` — whether it lands *during* the resolve
    await OR *before* this confirmed commit ran at all, while the chunk lagged in
    the recognition queue.  The commit compares the live epoch against the
    AUDIO's epoch; a commit for audio whose session already ended is discarded
    rather than resurrecting a dead track onto the screen or corrupting a fresh
    session.  Sampling the epoch at commit *entry* (the pre-PCONC-1 design) could
    not see the queue-lag case: the entry sample already read the post-lift epoch
    and then found it "stable" across the resolve.
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

    async def commit(self, raw: "RawRecognitionResult", audio_epoch: int) -> bool:
        """Resolve full metadata for *raw* and commit it everywhere.

        ``audio_epoch`` is the session epoch this audio was captured under, bound
        at enqueue time by the recognition loop (PCONC-1) and threaded through
        confirmation to here.  It is **required** — the guard is only sound if
        the caller supplies the audio's own epoch; there is deliberately no
        default that would silently fall back to re-sampling at commit time (the
        exact defect PCONC-1 fixes).

        Returns ``True`` when the track was committed, ``False`` when the commit
        was discarded because the session that produced this audio has ended —
        whether the needle lifted mid-resolve (B-1) or before the queue-lagged
        commit ran at all (PCONC-1).  Resolver exceptions are NOT swallowed —
        they propagate to the recognition loop's ``run()`` handler, exactly as
        the old ``_commit_track`` did, so a transient resolve failure leaves
        ``current_raw`` un-advanced (B-11) and the track is re-attempted on the
        next chunk.
        """
        timestamp = int(time.time())
        # The epoch is bound to the AUDIO at capture/enqueue time (PCONC-1) and
        # passed in — NOT re-sampled here.  A commit-entry sample missed the
        # queue-lag race: a chunk captured before the needle lifted could be
        # dequeued and confirmed AFTER a new session began, and the entry sample
        # would read the new (stable-across-resolve) epoch and commit the dead
        # track into the fresh session.  Validating against the audio's own epoch
        # closes that window as well as the mid-resolve one (B-1).
        metadata = await self.resolver.resolve(raw)
        if self.state.session_epoch != audio_epoch:
            log.info(
                "Discarding stale commit for %s — %s: the session ended while "
                "metadata was resolving.",
                raw.artist, raw.title,
            )
            return False

        self.state.set_track(metadata)
        await self.tracker.on_track_identified(metadata)
        # Advance the dedup key (current_raw) only AFTER the tracker has accepted
        # the track, and only while the session is still the one this audio came
        # from (LB-1 + B-19).  Two failures are handled here that the old order
        # (set_raw *before* this await) silently mishandled:
        #   • on_track_identified RAISES — its album-split path awaits a Discogs
        #     write.  The exception propagates before this line, so current_raw is
        #     left un-advanced and the recognition loop re-attempts the track,
        #     instead of the old behavior where an already-advanced current_raw
        #     made the dedup treat a never-recorded track as "already playing" —
        #     displayed but never tracked, never scrobbled, never retried (LB-1).
        #   • the needle lifts DURING on_track_identified (SESSION_ENDED → clear()
        #     bumps the epoch and nulls current_raw).  Skipping the advance leaves
        #     current_raw null, so a re-drop of the same record can commit again
        #     rather than being suppressed by a resurrected dead-session dedup key.
        # Still satisfies B-11: set_track ran first, so current_raw never leads
        # current_track.
        if self.state.session_epoch == audio_epoch:
            self.state.set_raw(raw)
        log.info(
            f"Now playing: {metadata.artist} / {metadata.album} / "
            f"{metadata.title} [{metadata.source.name}]"
        )

        # Re-check the epoch before scrobbling (B-19).  set_track ran with no
        # intervening await since the post-resolve epoch check, so the display
        # commit is consistent with it — but on_track_identified CAN yield (its
        # album-split path awaits a Discogs write), and a SESSION_ENDED during that
        # window means the needle lifted.  The set_raw above already shares this
        # guard; apply it to the scrobble too so a track whose session has already
        # ended is neither re-committed nor scrobbled.
        if self.lastfm and self.state.session_epoch == audio_epoch:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.lastfm.scrobble, metadata, timestamp
                )
            except Exception as e:
                log.warning(f"Last.fm scrobble error: {e}")

        return True
