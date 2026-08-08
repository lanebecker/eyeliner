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
for) record 1's completed play.  The album cache (v1.3.3) makes the signal
MOSTLY reliable — every cached track of an album resolves to the same
release id — but B-4's carve-out deliberately leaves a transiently-degraded
DATABASE-tier resolve UNCACHED so the next track retries the collection
tier, and that retry routinely lands on a DIFFERENT (owned) pressing id for
the same album.  #184 therefore suppresses the split for exactly that tier
upgrade: last id DATABASE-sourced, incoming track COLLECTION-sourced with
the same resolver cache key (threaded as TrackMetadata.resolve_key).  A
genuine swap changes the key and still splits.  Residual, accepted: a
DB-sourced closer's is_last_track was computed against the DB pressing's
tracklist, which can differ from the owned pressing's — erring toward the
conservative miss.

Replay boundary (#185): equal release ids can also hide a REAL boundary —
the same record re-dropped inside the silence window used to merge two
complete playthroughs into one credit.  The album's OPENER (resolved row 0)
arriving after potential_last_track armed (and not a consecutive
re-identification) now splits exactly like a record change: the finished
playthrough credits at the boundary and the replay earns its own session.
Opener-only on purpose — a looser trigger let stale mid-album
re-identifications mint a double credit for one playthrough.  Accepted
conservative residuals: a re-drop straight into a later track still merges
(the old undercount, for that slice); a replay of a WHOLLY DB-degraded
playthrough is absorbed by the #184 suppression (costing at most one
Last.fm love — the degraded playthrough was uncreditable anyway); and after
a #184 tier upgrade the degraded DB row is not #182 support, so a SHORT
album needs two collection-resolved rows after the blip to credit.  One
KNOWN phantom residual survives opener-only (#227, LOW, accepted with Lane
2026-08-08): on a reprise/bookend album whose closer musically echoes the
opener, a late chunk of the still-playing closer can Shazam-resolve to the
opener's row (global_index 0), tripping this boundary with no real re-drop
and minting a second credit — distinguishing it from a genuine re-drop is
not cheaply possible (both yield a 2-row remainder), so it is documented
rather than fixed.

Tracks without a release_id (FALLBACK source) can't be distinguished and
never trigger a split.
"""

import asyncio
import logging
from typing import Callable, Optional, TYPE_CHECKING

from src.metadata.models import MetadataSource, PlaySession, TrackMetadata
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

# #163: an end-of-session Discogs credit (or Last.fm love) that fails transiently
# is retried a bounded number of times before the session is discarded. An
# album-split finalize detaches the session, so if the FIRST attempt fails there
# is no later chance to credit the completed play — the old code latched
# `credited` BEFORE the write and lost the credit on any failure.
#
# Bounded and SHORT for two reasons. First, the attempts must fit within the
# _SHUTDOWN_DRAIN_SECONDS window on shutdown (1s + 2s = 3s of backoff across 3
# attempts) and never spin. Second, on the album-split path the split-off
# session's finalize is awaited inline by on_track_identified (and so by the
# recognition pipeline). Since CONC-2 (#96) that finalize runs OUTSIDE the
# lifecycle lock, so it no longer stalls the next record's session start; a short
# bound still keeps that inline credit from lengthening the splitting commit.
# CONC-6's is_stale predicate prevents any phantom credit and the event loop is
# never blocked (sleep + writer.run yield).
_FINALIZE_WRITE_ATTEMPTS = 3
_FINALIZE_RETRY_BACKOFF_SECONDS = 1.0


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
        # session (B-2).  CONC-2: this lock now guards ONLY the fast, synchronous
        # session-pointer transitions — it is never held across an await — so a
        # slow end-of-session write can't stall the recognition pipeline behind it.
        self._lifecycle_lock = asyncio.Lock()
        # CONC-2: serializes the end-of-session crediting work (the Discogs /
        # Last.fm writes), which now runs OUTSIDE the lifecycle lock.  It preserves
        # the pre-CONC-2 guarantee that at most one crediting run — i.e. one
        # Discogs *writer* call over the shared requests.Session / max_workers=2
        # pool — is in flight at a time, without coupling that serialization to the
        # lifecycle lock the recognition pipeline needs.
        self._finalize_lock = asyncio.Lock()

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
            task.add_done_callback(self._on_end_session_done)

    def _on_end_session_done(self, task: "asyncio.Task"):
        """Done-callback for the fire-and-forget SESSION_ENDED task (CONC-3).

        Drops the strong reference AND retrieves the task's exception, so a raise
        inside ``_end_session`` is LOGGED here — at completion, attached to the
        SESSION_ENDED that caused it — instead of surfacing as asyncio's detached
        ``Task exception was never retrieved`` warning from the GC at an arbitrary
        later time (or, if the task object is collected quietly, nothing at all).

        Most write failures inside the credit path are already handled gracefully
        (``_finalize_write_with_retry`` catches and bounded-retries the Play Count
        increment and the Last.fm love, #163; and #171 now contains a raising
        ``update_last_played`` inside ``_finalize_session`` so it can't skip the
        love). This callback remains the backstop for any OTHER unexpected error
        anywhere in the SESSION_ENDED task, so the operator sees the failure in the
        same log that records every other write outcome, rather than a silent
        half-credited session.

        ``task.cancelled()`` is checked first: shutdown / loop teardown can cancel
        an in-flight credit (``drain`` already warns about that), and calling
        ``.exception()`` on a cancelled task raises ``CancelledError`` — so a
        cancelled task is discarded without being reported as a credit failure.
        """
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "End-of-session credit task failed and could not complete: %r — "
                "this session's Discogs Play Count / Last Played and any Last.fm "
                "love may be incomplete, and nothing will retry it until the record "
                "is played again.",
                exc,
            )

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
             ``_detach_session_locked``) is synchronous and holds no ``await``, so
             a synchronous ``_start_session`` cannot slip in between the lock
             acquisition and the null and have its new session immediately nulled.
          4. Since CONC-2, NO lifecycle critical section holds the lock across an
             ``await`` at all — the album split detaches *synchronously* and defers
             the (awaited) crediting to AFTER the lock is released — so the locked
             body runs to completion with nothing interleaving inside it, and there
             is no window for a concurrent MUSIC_STARTED to have its new session
             corrupted.

        Routing this through the lock would require scheduling (``on_silence_event``
        is sync), which would break the synchronous "session exists immediately
        after MUSIC_STARTED" contract and add a SESSION_ENDED ordering hazard for
        no real safety gain.  See test_listen_tracker_split_race.py.
        """
        if self._session is None:
            self._session = PlaySession()
            log.info("Play session started.")

    async def _end_session(self, expected=_CURRENT_SESSION):
        """End the active session: detach it under the lifecycle lock, then credit
        it OUTSIDE the lock (CONC-2).

        `expected` lets a scheduled SESSION_ENDED bind to the session that was
        active when the silence fired; if an album split has since swapped in a
        new session, ending is skipped.  The default sentinel means "end
        whatever session is current" (used by the direct-await callers and the
        existing test suite).

        The crediting work (up to three executor-dispatched HTTP round trips with
        bounded retry) must NOT run under `_lifecycle_lock`: `on_track_identified`
        takes that lock first and is awaited inline by the recognition pipeline,
        so holding it across the writes stalled recognition of the next record for
        the whole retry window (CONC-2).  The detach is synchronous and atomic
        under the lock (B-2); the finalize is serialized separately by
        `_finalize_detached`.
        """
        async with self._lifecycle_lock:
            detached = self._detach_session_locked(expected=expected)
        if detached is not None:
            await self._finalize_detached(detached)

    def _is_consecutive_reidentification(self, track) -> bool:
        """#185: is *track* the same physical identification as the last logged
        one?  Mirrors log_track's consecutive-dedup identity (title, artist,
        release id) — the replay boundary must not fire on a closer merely
        re-confirmed across overlapping chunks, and this check runs BEFORE
        log_track's own dedup gets the chance to swallow the duplicate."""
        if not self._session or not self._session.identified_tracks:
            return False
        last = self._session.identified_tracks[-1]
        return (
            last.title == track.title
            and last.artist == track.artist
            and last.discogs_release_id == track.discogs_release_id
        )

    def _detach_session_locked(self, expected=_CURRENT_SESSION):
        """Detach and return the active session (or None).  SYNCHRONOUS — no
        await — so the null-and-check runs atomically under the lifecycle lock,
        which the caller MUST hold.  The caller finalizes the returned session
        AFTER releasing the lock (CONC-2).

        Kept a separate method (was `_end_session_locked`) so the album-split path
        can detach-then-restart under a single lock acquisition; being await-free,
        it also strengthens B-2 (nothing can interleave inside it).
        """
        if self._session is None:
            return None
        if expected is not _CURRENT_SESSION and self._session is not expected:
            # A stale SESSION_ENDED whose session was already ended by an album
            # split (and possibly replaced by a new one) — do nothing.
            log.debug("Ignoring stale SESSION_ENDED for an already-replaced session.")
            return None

        session = self._session
        self._session = None
        return session

    async def _finalize_detached(self, session: PlaySession):
        """Credit an already-detached session, serialized against every other
        finalize (CONC-2).

        `_finalize_lock` preserves the pre-CONC-2 guarantee that at most one
        crediting run — one Discogs *writer* call over the shared requests.Session
        / max_workers=2 pool — is in flight at a time.  Before CONC-2 the
        lifecycle lock provided that serialization as a side effect of being held
        across the writes; now that the writes run outside it, this dedicated lock
        provides it directly, so a SESSION_ENDED credit and an album-split credit
        for two different detached sessions can never hit the writer concurrently.
        The two locks are never held at the same time, so there is no ordering
        deadlock.
        """
        async with self._finalize_lock:
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

        if (
            session.potential_last_track
            and session.album_release_id
            and not session.completion_supported
        ):
            # #182: the completion gate.  The closer armed the session, but no
            # earlier track of the latched release was identified — the classic
            # shape of a Shazam attribution swing that minted a one-track
            # split-off session for an owned compilation.  Suppress the credit
            # AND the love (below, same gate) rather than phantom-credit a
            # record that never left its sleeve.  Loud on purpose: a deliberate
            # closer-only needle drop lands here too (approved behaviour
            # change), and the operator deserves a diagnosable line.
            log.info(
                "Completion gate (#182): closer '%s' armed release %s, but the "
                "session's %d identification(s) [%s] resolve to only %d distinct "
                "tracklist row(s) of it — suppressing Play Count / Last Played / "
                "love (mis-attributed single, re-identified closer, or "
                "closer-only needle drop; missed count preferred over phantom).",
                session.closing_track.title if session.closing_track else "?",
                session.album_release_id,
                len(session.identified_tracks),
                ", ".join(repr(t.title) for t in session.identified_tracks[:6])
                + ("…" if len(session.identified_tracks) > 6 else ""),
                session.supporting_row_count,
            )
        elif session.potential_last_track and session.album_release_id:
            if session.crediting:
                # A concurrent/re-entrant finalize already owns this session's
                # credit — in flight, or having already exhausted its bounded
                # retries. Never issue a second increment for the same release
                # (B-8). The old code enforced this by latching `credited` before
                # the await, which is exactly what lost the credit on failure
                # (#163); the in-flight `crediting` latch preserves the guarantee
                # without prematurely recording success.
                log.debug("Credit already in flight for this session — skipping (B-8).")
            else:
                # #163: latch IN-FLIGHT before any await (a re-entrant finalize
                # bails on the guard above) — but commit `credited` only AFTER the
                # write lands, inside the helper, so a transient failure stays
                # uncommitted and is bounded-retried instead of silently lost.
                session.crediting = True
                try:
                    await self._credit_completed_album(session)
                except Exception as e:
                    # #171: the Last.fm love below "runs independently of Discogs"
                    # — but a Discogs write that RAISES (concretely the SINGLE,
                    # unretried update_last_played; the increment is already
                    # exception-caught by _finalize_write_with_retry) would
                    # otherwise propagate out of _finalize_session and skip the
                    # love too. Contain a crediting raise here so the love still
                    # runs. `credited` stays uncommitted (set only on the
                    # increment landing, inside the helper), so a lost credit is
                    # still not falsely latched; this only stops the raise from
                    # also costing the love.
                    log.error(
                        "⚠ Discogs crediting raised (%r); Play Count / Last Played "
                        "may be incomplete for release %s / instance %s — continuing "
                        "to the Last.fm love, which is independent.",
                        e, session.album_release_id, session.album_instance_id,
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

        # Last.fm: love the album's CLOSER if the full side completed and love
        # is enabled.  #181: the target is session.closing_track — the track
        # whose is_last_track armed potential_last_track — NOT the last track
        # identified: a side re-dropped inside the silence window or a
        # FALLBACK-resolved swap appends tracks AFTER the closer, and the love
        # must not land on those (identified_tracks[-1] remains only as a
        # fallback for sessions armed without a recorded closer).
        # Runs independently of Discogs — a Discogs failure doesn't prevent this.
        # Gated on session.loved (committed) AND session.loving (in-flight) so a
        # re-entrant/double finalize can't love the same track twice (B-23) — the
        # love-side analogue of the B-8 credited/crediting guard above.
        if (
            session.potential_last_track
            and session.completion_supported   # #182: same gate as the credit
            and not session.loved
            and not session.loving
            and self.lastfm
            and self.lastfm.love_on_completion
        ):
            last_track = session.closing_track or (
                session.identified_tracks[-1] if session.identified_tracks else None
            )
            if last_track:
                # #163: latch IN-FLIGHT before the await (a re-entrant finalize
                # bails on the guard above), but commit `loved` only AFTER the love
                # lands. The love is bounded-retried like the credit, so a
                # transient Last.fm failure doesn't silently latch loved=True and
                # lose the love with no retry.
                session.loving = True
                love_success = await self._finalize_write_with_retry(
                    "Last.fm love",
                    lambda: asyncio.get_running_loop().run_in_executor(
                        None, self.lastfm.love, last_track
                    ),
                )
                if love_success:
                    session.loved = True
                    log.info(f"✅ Last.fm loved: {last_track.artist} — {last_track.title}")
                else:
                    log.warning(
                        "⚠ Failed to love track on Last.fm after %d attempts.",
                        _FINALIZE_WRITE_ATTEMPTS,
                    )

    async def _finalize_write_with_retry(self, label: str, attempt) -> bool:
        """Run one end-of-session write with a BOUNDED retry (#163).

        ``attempt`` is a zero-arg callable returning a fresh awaitable that
        performs ONE write and resolves truthy on success. The write is retried
        up to ``_FINALIZE_WRITE_ATTEMPTS`` times — treating BOTH a falsy result
        and a raised exception as a failure — with a short linear backoff between
        attempts. Returns True the instant one attempt lands, False if all fail.

        This exists because an album-split finalize DETACHES the session before
        crediting it: a first-attempt failure would otherwise be the ONLY attempt,
        losing the completed play's Play Count credit (or its Last.fm love) with
        nothing to retry it (#163). The caller commits its latch (``credited`` /
        ``loved``) only when this returns True. The bound keeps the total backoff
        (1s + 2s = 3s) inside the shutdown drain window (_SHUTDOWN_DRAIN_SECONDS)
        and — on the album-split path, where this runs under the lifecycle lock —
        keeps that lengthened lock window small (see the constants above).
        """
        for n in range(1, _FINALIZE_WRITE_ATTEMPTS + 1):
            try:
                if await attempt():
                    return True
                log.warning("%s attempt %d/%d failed.", label, n, _FINALIZE_WRITE_ATTEMPTS)
            except Exception as e:
                log.warning(
                    "%s attempt %d/%d raised: %s", label, n, _FINALIZE_WRITE_ATTEMPTS, e
                )
            if n < _FINALIZE_WRITE_ATTEMPTS:
                await asyncio.sleep(_FINALIZE_RETRY_BACKOFF_SECONDS * n)
        return False

    async def _credit_completed_album(self, session: PlaySession):
        """Credit a completed album: increment Play Count (bounded retry, #163)
        and update Last Played. The caller has already set ``session.crediting``
        (in-flight) after confirming it was not already set, so this runs exactly
        once per session. Commits ``session.credited`` ONLY when the increment
        actually lands, leaving a transient failure uncommitted (and logged loud).
        """
        log.info(
            f"Last track confirmed for release {session.album_release_id} — "
            f"incrementing Play Count and updating Last Played in Discogs."
        )
        success = await self._finalize_write_with_retry(
            "Discogs Play Count increment",
            lambda: self.writer.run(
                self.writer.increment_play_count,
                session.album_release_id,
                session.album_instance_id,
            ),
        )
        if success:
            session.credited = True  # committed ONLY after the write landed (#163)
            log.info("✅ Discogs Play Count incremented successfully.")
        else:
            log.error(
                "⚠ Discogs Play Count credit LOST for release %s / instance %s after "
                "%d attempts — this completed play was NOT credited and nothing will "
                "retry it. `credited` left False (not falsely latched).",
                session.album_release_id, session.album_instance_id,
                _FINALIZE_WRITE_ATTEMPTS,
            )

        if self.writer.last_played_field_name:
            # update_last_played is a SINGLE attempt (NOT bounded-retried): a stale
            # Last Played is small and self-correcting on the next play (META-7),
            # and retrying it would tangle with the STAB-2 deliberate clock-skip (a
            # pre-NTP boot returns False on purpose and must not be retried). It
            # still inherits the crediting/committed split — it runs inside the
            # in-flight-guarded credit path.
            last_played_success = await self.writer.run(
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
            # don't say they belong together, so surface ONE explicit divergence
            # line naming the item.
            #
            # The trustworthy-clock gate excludes the STAB-2 case: on a pre-NTP
            # boot the writer DELIBERATELY skips Last Played (it defers rather
            # than fails, and has already WARNed to that effect), so counting it
            # as a divergence would cry wolf on every album finished before NTP
            # sync. This is a SECOND, independent clock read — not literally the
            # writer's decision — so only an NTP step across the 2026 sanity floor
            # in the gap between that write and this check could desync the two;
            # that does not happen in steady state. Genuine post-sync failures (a
            # 429 on the second POST, a dropped connection) run on a trustworthy
            # clock and still fire. Both disagreement directions are reported:
            # Play-Count-only (Last Played failed) AND Last-Played-only (the
            # increment failed but the date write landed).
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
        since ended.  It is re-evaluated *after* the lifecycle lock is acquired:
        between this track's audio being captured and this commit taking the lock,
        a SESSION_ENDED can detach the session and bump the epoch.  Without the
        check, this method would then see ``_session is None`` and start a PHANTOM
        session for audio that already stopped — which a later album split could
        phantom-credit.  A stale track is dropped instead.  Defaults to None (no
        check) for callers that don't thread it.  (Before CONC-2 that window could
        be seconds long, because the lock was held across the previous session's
        Discogs write; the finalize now runs outside the lock so the window is
        brief, but the race still exists, so the guard stays.)
        """
        detached = None
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
                # #195 tripwire: since the recognition gate (AudioCapture only
                # enqueues while the detector is in music-state), MUSIC_STARTED
                # always precedes recognition, so a session already exists here in
                # normal operation. Having to CREATE it means a track was
                # recognized without a music transition — the exact immortal-session
                # signature — so surface it loudly (defense in depth; the session
                # is still started so no play is lost).
                log.warning(
                    "Track '%s' was recognized without a preceding music "
                    "transition (no active session) — starting one, but this "
                    "should not happen with the recognition gate; check the "
                    "silence detector / capture wiring (#195).",
                    track.title,
                )
                self._start_session()

            split_reason = None
            if (
                self._session.last_release_id is not None
                and track.discogs_release_id is not None
                and track.discogs_release_id != self._session.last_release_id
            ):
                # #184: suppress the split for a B-4 tier upgrade — the last id
                # came from a DATABASE-sourced degraded resolve (transient blip
                # left the album uncached), and this track re-resolved the SAME
                # album (same resolver cache key) via the collection tier under
                # the owned pressing's id.  Asymmetric on purpose: a genuine
                # swap changes the key, and collection→database (unreachable —
                # collection results are cached) stays a conservative split.
                if (
                    self._session.last_release_source is MetadataSource.DISCOGS_DATABASE
                    and track.source is MetadataSource.DISCOGS_COLLECTION
                    and track.resolve_key is not None
                    and track.resolve_key == self._session.last_release_resolve_key
                ):
                    log.info(
                        f"Tier upgrade, not an album change (#184): release "
                        f"{self._session.last_release_id} (database-degraded) → "
                        f"{track.discogs_release_id} (owned pressing) for the same "
                        f"album {track.resolve_key!r} — continuing the session."
                    )
                else:
                    split_reason = (
                        f"Album change detected mid-session "
                        f"(release {self._session.last_release_id} → "
                        f"{track.discogs_release_id})"
                    )
            elif (
                # #185: the replay boundary.  The album's OPENER (resolved
                # tracklist row 0) arriving AFTER the closer armed the session
                # means the record was re-dropped inside the silence window.
                # Split exactly like a record change so the finished
                # playthrough credits and the replay earns its own session.
                #
                # Opener-only ON PURPOSE (#184/#185 cold review): a looser
                # "any same-release non-duplicate track" trigger double-
                # credited one physical playthrough — a stale mid-album
                # re-identification split + credited, then the still-playing
                # closer's own re-identification re-armed the remainder with
                # two genuine rows, passing the #182 gate for a second credit.
                # The opener is the one row a genuine re-drop identifies first
                # and chunk-overlap noise essentially never resurfaces late.
                # Accepted conservative residuals: a re-drop straight into a
                # LATER track (side-B replay, needle past the opener) still
                # merges (the pre-#185 undercount, for that slice only).
                # Release-less (FALLBACK) tracks keep never triggering splits.
                self._session.potential_last_track
                and track.discogs_release_id is not None
                and track.discogs_release_id == self._session.last_release_id
                and track.side_index.global_index == 0
                and not self._is_consecutive_reidentification(track)
            ):
                split_reason = (
                    f"Replay boundary (#185): '{track.title}' of release "
                    f"{track.discogs_release_id} identified after the closer "
                    f"armed the session — the record was re-dropped"
                )

            if split_reason is not None:
                log.info(f"{split_reason} — splitting session.")
                # Detach + restart atomically under the lock so a concurrently
                # scheduled SESSION_ENDED can't slip between them (B-2).  The
                # detach is synchronous; the OLD session's crediting is deferred to
                # AFTER the lock is released (CONC-2), so a slow write for the
                # previous record no longer holds the lock this commit — and the
                # next one — needs.
                detached = self._detach_session_locked()
                self._start_session()

            self._session.log_track(track)
            if track.is_last_track:
                log.info(f"Last track of album identified: '{track.title}' — watching for session end.")

        # CONC-2: credit the split-off session OUTSIDE the lifecycle lock, so the
        # NEXT record's session start (under the lock above) is never blocked by a
        # slow write.  This is still awaited inline by the recognition pipeline and
        # `_finalize_detached` takes `_finalize_lock`, so a creditable split can
        # briefly wait here behind an unrelated in-flight credit — bounded by the
        # retry window, far smaller and rarer than the pre-CONC-2 whole-pipeline
        # stall.
        #
        # #166: but the COMMON mid-album swap detaches a session that never reached
        # its last track — nothing to credit or love — and finalizing it would
        # still take the lock (and could stall the queue) to do only logging.
        # `potential_last_track` is a NECESSARY condition for BOTH the Play Count
        # credit and the Last.fm love in `_finalize_session`, so short-circuit the
        # non-creditable case here and never touch the lock. A creditable split
        # (its closer played right before the swap — rare) still finalizes below.
        if detached is not None and detached.potential_last_track:
            await self._finalize_detached(detached)
        elif detached is not None:
            log.debug(
                "Split-off session reached no last track — nothing to credit; "
                "skipping finalize and its lock (#166)."
            )
