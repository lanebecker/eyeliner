"""ListenTracker — session tracking and Discogs/Last.fm updater.

Logic:
  - Maintains a PlaySession from first track identification until SESSION_ENDED.
  - When the last track on the album is identified, sets potential_last_track = True.
  - On SESSION_ENDED (sustained silence), if potential_last_track is set:
      1. Calls DiscogsCollectionWriter.increment_play_count for the release.
      2. Calls DiscogsCollectionWriter.update_last_played if last_played_field_name is configured.
      3. Calls LastFmClient.love on the last track if love_on_completion is enabled.
  - Conservative by design: if the last track was never identified (e.g. only
    Side A was played), none of the above updates are triggered.

Album-change auto-split (v1.3.4)
--------------------------------
A session normally ends only after session_end_silence_seconds (default 45s)
of silence.  Swapping records faster than that used to merge two albums into
one session: the release ID stayed latched from record 1, so record 2's
closer could credit record 1 with a play.  on_track_identified now detects
the swap — a confirmed track whose discogs_release_id differs from the
last-seen one — and splits: the current session is ended (correctly
crediting record 1 if its closer played) and a fresh session begins for the
new record.  Detection compares against the session's last_release_id
(v1.3.5) rather than the latched album_release_id: the latch only sets from
collection-owned tracks, so a DB-resolved first record would otherwise leave
nothing to compare against and let record 2 inherit (and be phantom-credited
for) record 1's completed play.  The signal is reliable because
MetadataResolver's album cache (v1.3.3) guarantees every track of an album
resolves to identical release IDs within a session.  Tracks without a
release_id (FALLBACK source) can't be distinguished and never trigger a
split.
"""

import asyncio
import logging
from typing import Callable, Optional, TYPE_CHECKING

from src.metadata.models import PlaySession, TrackMetadata
from src.audio.silence import AudioEvent
from src.util.clock import clock_is_trustworthy

if TYPE_CHECKING:
    from src.metadata.discogs.writer import DiscogsCollectionWriter
    from src.tracking.lastfm_client import LastFmClient

log = logging.getLogger(__name__)

# Sentinel for _end_session(expected=...): "end whatever session is current"
# as opposed to "end only if the current session is this specific one".
_CURRENT_SESSION = object()

# Bounded window (seconds) that shutdown waits for an in-flight end-of-session
# Discogs credit to finish before the event loop tears it down (CONC-1). Long
# enough for the normal write pair to complete many times over, short enough to
# stay well within systemd's default 90s stop timeout even on a stuck network.
_SHUTDOWN_DRAIN_SECONDS = 10.0


class ListenTracker:
    """Manages play sessions and triggers Discogs field updates on album completion."""

    def __init__(
        self,
        writer: "DiscogsCollectionWriter",
        lastfm: Optional["LastFmClient"] = None,
    ):
        # A-4: depend only on the Discogs WRITE half (Play Count / Last Played),
        # injected at the composition root (main.py).  A-3 had already moved this
        # off the resolver's internals; A-4 narrows it from the whole God client
        # to just the collection writer.
        self.writer = writer
        self.lastfm = lastfm
        self._session: Optional[PlaySession] = None
        # Strong references to in-flight _end_session tasks.  asyncio only
        # keeps weak references to tasks, so a fire-and-forget create_task()
        # could in principle be garbage-collected mid-flight — and this is the
        # task that performs the Discogs play-count write, so it must survive.
        self._bg_tasks: set = set()
        # Serializes every session-lifecycle transition (start / end / the
        # split's end-then-start).  Without it, the album-split path and a
        # fire-and-forget SESSION_ENDED task can interleave and end the wrong
        # session (B-2).
        self._lifecycle_lock = asyncio.Lock()

    def on_silence_event(self, event: AudioEvent):
        """Receive silence events from SilenceDetector (wired up in main.py)."""
        if event == AudioEvent.MUSIC_STARTED:
            self._start_session()
        elif event == AudioEvent.SESSION_ENDED:
            # Bind this end to the session that is active *now*.  If an album
            # split later replaces it, the task below sees the session changed
            # and becomes a no-op instead of ending the new session (B-2).
            target = self._session
            task = asyncio.create_task(self._end_session(expected=target))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def drain(self, timeout: float = _SHUTDOWN_DRAIN_SECONDS) -> None:
        """Wait (bounded) for any in-flight end-of-session credit tasks to finish.

        Called from the composition root's shutdown path (``run_pipeline``) so a
        SESSION_ENDED credit that is mid-write — ``increment_play_count`` done but
        ``update_last_played`` / the Last.fm love still pending — is not torn in
        half by the event loop closing (CONC-1). The credit runs as a
        fire-and-forget task in ``_bg_tasks``; it is NOT one of the pipeline
        legs, so without this nothing awaits it and ``asyncio.run`` cancels it
        mid-write, leaving the collection permanently half-updated (Play Count
        incremented, Last Played stale) with ``credited`` latched so nothing
        retries.

        Bounded by ``timeout`` so a stuck write cannot hang shutdown past
        systemd's stop timeout; a task still running afterwards is left for loop
        teardown to cancel, with a warning. Never raises — shutdown must proceed.
        """
        pending = [t for t in self._bg_tasks if not t.done()]
        if not pending:
            return
        log.info(
            "Draining %d in-flight session-credit task(s) before shutdown…",
            len(pending),
        )
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            log.warning(
                "%d session-credit task(s) still running after a %.0fs drain "
                "timeout; the Discogs update may be incomplete.",
                len(still_pending), timeout,
            )

    def _start_session(self):
        """Create a session if none is active.  Idempotent and create-only.

        Invariant (B-20): this is intentionally called WITHOUT the lifecycle
        lock from the synchronous ``on_silence_event(MUSIC_STARTED)`` path, and
        that is race-free on the single-threaded event loop because:

          1. It is *create-only and idempotent* — it never nulls or replaces an
             existing session, only assigns one when ``_session is None``.
          2. It has no ``await``, so it runs atomically; nothing interleaves
             inside it.
          3. The one place a session is *destroyed* (``_session = None`` in
             ``_end_session_locked``) has no ``await`` between acquiring the lock
             and the null, so a synchronous ``_start_session`` cannot slip in
             between them and have its new session immediately nulled.
          4. The only critical section that holds the lock across an ``await``
             (``on_track_identified``'s album split + ``_finalize_session``)
             operates on a *local* session reference after nulling
             ``self._session``, and re-checks ``self._session is None`` under the
             lock — so a session created by a concurrent MUSIC_STARTED is adopted,
             not corrupted.

        Routing this through the lock would require scheduling (``on_silence_event``
        is sync), which would break the synchronous "session exists immediately
        after MUSIC_STARTED" contract and add a SESSION_ENDED ordering hazard for
        no real safety gain.  See test_listen_tracker_split_race.py.
        """
        if self._session is None:
            self._session = PlaySession()
            log.info("Play session started.")

    async def _end_session(self, expected=_CURRENT_SESSION):
        """End the active session, holding the lifecycle lock.

        `expected` lets a scheduled SESSION_ENDED bind to the session that was
        active when the silence fired; if an album split has since swapped in a
        new session, ending is skipped.  The default sentinel means "end
        whatever session is current" (used by the direct-await callers and the
        existing test suite).
        """
        async with self._lifecycle_lock:
            await self._end_session_locked(expected=expected)

    async def _end_session_locked(self, expected=_CURRENT_SESSION):
        """End the active session.  The caller MUST hold self._lifecycle_lock.

        Split into this lock-free body so the album-split path can end and then
        restart a session under a single lock acquisition without deadlocking
        on a re-entrant lock.
        """
        if self._session is None:
            return
        if expected is not _CURRENT_SESSION and self._session is not expected:
            # A stale SESSION_ENDED whose session was already ended by an album
            # split (and possibly replaced by a new one) — do nothing.
            log.debug("Ignoring stale SESSION_ENDED for an already-replaced session.")
            return

        session = self._session
        self._session = None
        await self._finalize_session(session)

    async def _finalize_session(self, session: PlaySession):
        """Do the end-of-session crediting work for an already-detached session.

        Operates on a local `session` reference (self._session has already been
        cleared by the caller), so it is safe to await the Discogs/Last.fm
        executor calls here without another coroutine mutating it.
        """
        # Idempotency guard: never credit one session's Play Count twice, even
        # if a re-entrant end somehow finalizes the same session object again
        # (B-8).  Pairs with the B-2 lifecycle lock as defense-in-depth.
        if session.credited:
            log.debug("Session already credited — skipping to stay idempotent (B-8).")
            return

        track_count = len(session.identified_tracks)
        log.info(
            f"Play session ended. "
            f"Identified {track_count} track(s). "
            f"Last track reached: {session.potential_last_track}"
        )

        if session.potential_last_track and session.album_release_id:
            # Mark credited *before* the await so a re-entrant finalize that
            # slips in mid-write sees the flag and bails instead of issuing a
            # second increment for the same release (B-8).
            session.credited = True
            log.info(
                f"Last track confirmed for release {session.album_release_id} — "
                f"incrementing Play Count and updating Last Played in Discogs."
            )
            success = await asyncio.get_running_loop().run_in_executor(
                None,
                self.writer.increment_play_count,
                session.album_release_id,
                session.album_instance_id,
            )
            if success:
                log.info("✅ Discogs Play Count incremented successfully.")
            else:
                log.warning("⚠ Failed to increment Discogs Play Count.")

            if self.writer.last_played_field_name:
                last_played_success = await asyncio.get_running_loop().run_in_executor(
                    None,
                    self.writer.update_last_played,
                    session.album_release_id,
                    session.album_instance_id,
                )
                if last_played_success:
                    log.info("✅ Discogs Last Played updated successfully.")
                elif not clock_is_trustworthy():
                    # A pre-NTP clock made update_last_played skip the write (it
                    # already logged its own WARNING).  A deliberate skip is NOT a
                    # failure, so don't ALSO report it as one (STAB-2).  A False
                    # return with an untrustworthy clock is always the skip path —
                    # the writer's gate short-circuits before the POST — never a
                    # real write failure, so this can't mask a genuine error.
                    pass
                else:
                    log.warning("⚠ Failed to update Discogs Last Played.")

                # META-7: the two writes are independent POSTs and the session is
                # destroyed right after, so if EXACTLY ONE lands the record is left
                # inconsistent (e.g. Play Count incremented but Last Played stale)
                # with no retry until it plays again. The two per-write logs above
                # don't say they belong together, so surface ONE explicit
                # divergence line naming the item.
                #
                # The trustworthy-clock gate excludes the STAB-2 case: on a pre-NTP
                # boot the writer DELIBERATELY skips Last Played (it defers rather
                # than fails, and has already WARNed to that effect), so counting it
                # as a divergence would cry wolf on every album finished before NTP
                # sync. This is a SECOND, independent clock read — not literally the
                # writer's decision — so only an NTP step across the 2026 sanity
                # floor in the gap between that write and this check could desync the
                # two; that does not happen in steady state. Genuine post-sync
                # failures (a 429 on the second POST, a dropped connection) run on a
                # trustworthy clock and still fire. Both disagreement directions are
                # reported: Play-Count-only (Last Played failed) AND Last-Played-only
                # (the increment failed but the date write landed).
                if success != last_played_success and clock_is_trustworthy():
                    log.warning(
                        "⚠ Discogs writes DIVERGED for release %s / instance %s: "
                        "Play Count %s but Last Played %s — the collection item is "
                        "now inconsistent and nothing will retry it until this "
                        "record is played again.",
                        session.album_release_id, session.album_instance_id,
                        "was incremented" if success else "did NOT increment",
                        "was updated" if last_played_success else "did NOT update",
                    )

        elif session.potential_last_track and not session.album_release_id:
            log.info(
                "Last track reached but release not in Discogs collection — "
                "skipping Play Count and Last Played updates (fallback metadata)."
            )
        else:
            log.info(
                "Last track not reached — not incrementing Play Count "
                "or updating Last Played (likely only one side played)."
            )

        # Last.fm: love the last track if the full side completed and love is enabled.
        # Runs independently of Discogs — a Discogs failure doesn't prevent this.
        # Gated on (and latching) session.loved so a re-entrant/double finalize
        # can't love the same track twice (B-23) — the love-side analogue of the
        # B-8 `credited` guard above.
        if (
            session.potential_last_track
            and not session.loved
            and self.lastfm
            and self.lastfm.love_on_completion
        ):
            last_track = session.identified_tracks[-1] if session.identified_tracks else None
            if last_track:
                # Latch before the await so a re-entrant finalize bails instead of
                # issuing a second love for the same track.
                session.loved = True
                love_success = await asyncio.get_running_loop().run_in_executor(
                    None, self.lastfm.love, last_track
                )
                if love_success:
                    log.info(f"✅ Last.fm loved: {last_track.artist} — {last_track.title}")
                else:
                    log.warning("⚠ Failed to love track on Last.fm.")

    async def on_track_identified(
        self, track: TrackMetadata, is_stale: Optional[Callable[[], bool]] = None
    ):
        """Called by the commit service when a new track is confirmed.

        Detects mid-session album changes (v1.3.4): if this track resolved to
        a different Discogs release than the previous one in this session,
        the user swapped records faster than the silence threshold.  The
        current session is ended immediately — which correctly credits the
        previous record if its closer played — and a fresh session starts
        for the new record.  Comparison is against the session's
        last_release_id (v1.3.5), which updates from ANY source carrying a
        release ID — unlike the latch, which only collection-owned tracks
        set, and which previously let a DB-resolved first record evade
        detection.  Both IDs must be present for a split: nothing seen yet
        means nothing to compare, and a missing track ID (FALLBACK metadata)
        means the album can't be distinguished.

        ``is_stale`` (CONC-6): a predicate supplied by the caller that returns
        True if the audio this track came from belongs to a session that has
        since ended.  It is re-evaluated *after* the lifecycle lock is acquired,
        because that lock can be held for a while by a previous session's Discogs
        write (CONC-2) and a SESSION_ENDED landing in that window ends the session
        and bumps the epoch.  Without the check, this method would then see
        ``_session is None`` and start a PHANTOM session for audio that already
        stopped — which a later album split could phantom-credit.  A stale track
        is dropped instead.  Defaults to None (no check) for callers that don't
        thread it.
        """
        async with self._lifecycle_lock:
            # CONC-6: now that we hold the lock, drop this track if its session
            # ended while we were waiting for the lock — do not resurrect it as a
            # phantom session (see is_stale in the docstring).
            if is_stale is not None and is_stale():
                log.info(
                    "Dropping stale track '%s' — its session ended while this commit "
                    "waited for the lifecycle lock (CONC-6); not starting a phantom "
                    "session for audio that already stopped.",
                    track.title,
                )
                return

            if self._session is None:
                self._start_session()

            if (
                self._session.last_release_id is not None
                and track.discogs_release_id is not None
                and track.discogs_release_id != self._session.last_release_id
            ):
                log.info(
                    f"Album change detected mid-session "
                    f"(release {self._session.last_release_id} → "
                    f"{track.discogs_release_id}) — splitting session."
                )
                # End + restart atomically under the lock so a concurrently
                # scheduled SESSION_ENDED can't slip between them (B-2).
                await self._end_session_locked()
                self._start_session()

            self._session.log_track(track)
            if track.is_last_track:
                log.info(f"Last track of album identified: '{track.title}' — watching for session end.")
