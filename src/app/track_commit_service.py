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

from src.util.clock import clock_is_trustworthy

if TYPE_CHECKING:
    from src.audio.recognizer import RawRecognitionResult
    from src.metadata.models import TrackMetadata
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
        # This is the scrobble timestamp — one of the two DATE-DEPENDENT WRITES in
        # the app (the other is the Last Played `date.today()` in writer.py). Both
        # are gated by `clock_is_trustworthy` (STAB-2): this timestamp is validated
        # below (see the clock-sanity gate before the scrobble) so a pre-NTP boot
        # can't stamp and drop a scrobble. Noted here because META-5 cited only the
        # writer for "bogus scrobble timestamps"; the scrobble timestamp is taken
        # HERE, not there (CRIT-9).
        timestamp = int(time.time())
        # arch-1/#217: the "re-validate the session epoch after each await" rule
        # (B-1 / PCONC-1 / B-19 / LB-1 / CONC-6) lives in ONE object now. Bind it
        # once to the AUDIO's own epoch — passed in, bound at capture/enqueue
        # (PCONC-1), NOT re-sampled here (a commit-entry sample missed the
        # queue-lag race: a chunk captured before the needle lifted could be
        # dequeued and confirmed AFTER a new session began, and the entry sample
        # would read the new, stable-across-resolve epoch and commit the dead
        # track into the fresh session). Every side effect below checks `guard`.
        guard = self.state.epoch_guard(audio_epoch)

        metadata = await self.resolver.resolve(raw)
        if guard.is_stale():   # B-1: session ended while metadata was resolving
            log.info(
                "Discarding stale commit for %s — %s: the session ended while "
                "metadata was resolving.",
                raw.artist, raw.title,
            )
            return False

        self.state.set_track(metadata)
        # Hand the tracker a staleness predicate it re-checks AFTER it acquires the
        # lifecycle lock (CONC-6): on_track_identified can park on that lock while a
        # previous session's Discogs write holds it, and a SESSION_ENDED landing in
        # that window ends this audio's session. The predicate lets the tracker drop
        # the track then, instead of resurrecting it as a phantom session.
        # Hand the tracker the guard's is_stale BOUND METHOD (CONC-6): it re-reads
        # the live epoch AFTER acquiring the lifecycle lock, so a SESSION_ENDED
        # that lands while on_track_identified parks on that lock drops the track
        # instead of starting a phantom session. A precomputed bool would miss it.
        await self.tracker.on_track_identified(metadata, is_stale=guard.is_stale)
        # #196 (conc-4): the session can end WHILE on_track_identified is in
        # flight (it can await a Discogs write; a SESSION_ENDED in that window
        # bumps the epoch and the tracker drops the track via is_stale). Take the
        # same discard exit as the post-resolve path BEFORE the "Now playing" log
        # and return — otherwise commit() logs a now-playing line for a track
        # already cleared off the screen and returns True, contradicting its
        # documented "returns False when discarded" contract.
        if guard.is_stale():
            log.info(
                "Discarding stale commit for %s — %s: the session ended while the "
                "track was being handed to the tracker.",
                raw.artist, raw.title,
            )
            return False
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
        #
        # R6-13: this ``still_current()`` re-check is presently ALWAYS True — the
        # ``is_stale()`` gate above returns for a stale session and nothing awaits
        # between it and here, so the epoch cannot change in the gap (``is_stale``
        # and ``still_current`` are exact negations). It is kept as a defensive
        # belt-and-suspenders: if a future edit inserts an await between that gate
        # and this line, a session that ends in that window must NOT have its
        # dedup key advanced. It is NOT (as an earlier comment implied) guarding a
        # live race that exists today.
        if guard.still_current():
            self.state.set_raw(raw)
        log.info(
            f"Now playing: {metadata.artist} / {metadata.album} / "
            f"{metadata.title} [{metadata.source.name}]"
        )

        # Scrobble is the last commit-path side effect and the natural spot a
        # future await (v1.6 play-history) would be added, so it goes through
        # guard.run() — the sanctioned pattern (#217): run() re-checks the epoch
        # (B-19: on_track_identified's album-split path can yield on a Discogs
        # write, and a SESSION_ENDED there means the needle lifted) and skips the
        # step for a session that has already ended, neither re-committing nor
        # scrobbling.
        if self.lastfm:
            await guard.run(lambda: self._scrobble(metadata, timestamp))

        return True

    async def _scrobble(self, metadata: "TrackMetadata", timestamp: int) -> None:
        """Submit the Last.fm scrobble, off the event loop. Run ONLY via
        ``guard.run`` so it can't fire for an ended session (B-19).

        Clock-sanity gate (STAB-2): validates the EXACT timestamp captured at the
        top of commit(). A pre-NTP boot (the Pi has no RTC) stamps an epoch/stale
        time; Last.fm silently drops a scrobble that is too old or in the future —
        or lands it at the wrong point in listening history — while reporting
        success either way. Skip with one WARNING rather than submit a wrong time.
        """
        if not clock_is_trustworthy(timestamp):
            log.warning(
                "Skipping Last.fm scrobble for %s — %s: the system clock is not yet "
                "trustworthy (pre-NTP boot?); a wrong timestamp would be dropped or land "
                "at the wrong point in listening history.",
                metadata.artist, metadata.title,
            )
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self.lastfm.scrobble, metadata, timestamp
            )
        except Exception as e:
            log.warning(f"Last.fm scrobble error: {e}")
