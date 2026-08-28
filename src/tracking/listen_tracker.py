"""ListenTracker — session tracking and Discogs/Last.fm updater.

Logic:
  - Maintains a PlaySession from first track identification until SESSION_ENDED.
  - When the last track on the album is identified, sets potential_last_track = True.
  - On SESSION_ENDED (sustained silence), if potential_last_track is set:
      1. Credits the release's Play Count via the #186 read-once / idempotent-set
         split (read_play_count then a retried set_play_count, in
         _credit_completed_album) — NOT increment_play_count (R6-12): retrying the
         whole read-modify-write double-credits an ambiguous-but-applied POST, so
         increment_play_count has no production caller.
      2. Calls DiscogsCollectionWriter.update_last_played if last_played_field_name is configured.
      3. Loves the closing_track on Last.fm if love_on_completion is enabled and
         the completion is supported (love_supported, R6-06).
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
Anchored on the release's FIRST VINYL row (R6-08), not tracklist row 0, so a
hybrid whose vinyl opener trails a leading CD/file row still splits a genuine
re-drop (the anchor falls back to row 0 for a plain numbered tracklist).  A
single-(vinyl-)row release is EXEMPT (R6-05): there the opener IS the closer,
so "replay" and "still playing" cannot be told apart, and a foreign
mis-attribution mid-spin — which breaks the consecutive-dedup chain the guard
relies on — would otherwise let the single's own re-id split, credit via the
carve-out, then re-arm into a SECOND credit for one play.  Anchored to the
opener (not any same-release track) on purpose — a looser trigger let stale
mid-album re-identifications mint a double credit for one playthrough.
Accepted conservative residuals: a re-drop straight into a later track still
merges (the old undercount, for that slice); a replay of a WHOLLY DB-degraded
playthrough is absorbed by the #184 suppression (costing at most one Last.fm
love — the degraded playthrough was uncreditable anyway); after a #184 tier
upgrade the degraded DB row is not #182 support, so a SHORT album needs two
collection-resolved rows after the blip to credit; and two spins of a single /
one-vinyl-row release in one sitting credit once (the singles analogue of #227,
now that the exemption merges them).  One KNOWN phantom residual survives on
MULTI-ROW albums (#227, LOW, accepted with Lane 2026-08-08): on a
reprise/bookend album whose closer musically echoes the opener, a late chunk of
the still-playing closer can Shazam-resolve to the opener's (first-vinyl) row,
tripping this boundary with no real re-drop and minting a second credit —
distinguishing it from a genuine re-drop is not cheaply possible (both yield a
2-row remainder), so it is documented rather than fixed.  The SINGLES shape of
that same mechanism is now handled by the single-row exemption above (R6-05).

Tracks without a release_id (FALLBACK source) can't be distinguished and
never trigger a split.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from src.metadata.models import MetadataSource, PlaySession, TrackMetadata
from src.metadata.normalize import fold_text
from src.audio.silence import AudioEvent
from src.metadata.discogs.transport import DiscogsRateLimited
from src.metadata.discogs.outcomes import (
    CollectionIdentity, PlayCountReadResult, PlayCountReadState,
)
from src.tracking.spin_memory import SpinMemory
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
# session's finalize runs OUTSIDE the lifecycle lock (CONC-2/#96), so it never
# stalls the next record's session START. R7-06 correction: it does NOT follow
# that the finalize is free — on_track_identified used to `await` the split
# credit inline, so the recognition LEG (the chunk-processing pipeline) blocked
# for the whole finalize, honoured Retry-After included (up to ~180s). That inline
# wait is now BOUNDED (see _SPLIT_CREDIT_INLINE_WAIT_SECONDS); the short retry
# bound here still keeps an ordinary (non-throttled) credit from lengthening the
# splitting commit noticeably. The event loop itself is never blocked within an
# attempt (sleep + writer.run yield); CONC-6's is_stale predicate prevents any
# phantom credit.
_FINALIZE_WRITE_ATTEMPTS = 3
_FINALIZE_RETRY_BACKOFF_SECONDS = 1.0

# #229: when a Play Count credit is rejected with a 429 whose Retry-After is
# longer than the transport's in-thread cap, the finalize layer honours the wait
# in the EVENT LOOP (asyncio.sleep — cancellable at shutdown, parks no thread)
# instead of burning all three attempts inside the same throttle window. This
# caps how long a single honoured wait may be: a Retry-After above it is waited
# only this long (the next 429 then reports the shrinking remainder), so a
# hostile/huge header can't turn one wait into an unbounded park. 90s comfortably
# covers Discogs' 60/min-window backoff, so the typical case is a SINGLE honoured
# wait and the credit lands on the next attempt. The bound is per-wait, not total:
# the retry loop can honour a wait after attempts 1 and 2 (never after the last),
# so the worst case is (_FINALIZE_WRITE_ATTEMPTS - 1) waits ≈ 180s for one
# finalize. This runs OUTSIDE the lifecycle lock (CONC-2/#96) and every wait is a
# cancellable event-loop sleep that shutdown drain() abandons after
# _SHUTDOWN_DRAIN_SECONDS. R7-06 correction: this ~180s does NOT "never stall the
# next record's session" — a SESSION_ENDED credit runs as a background task and
# never touched the leg, but an album-SPLIT credit used to be awaited inline by
# on_track_identified, so a honoured wait stalled the recognition pipeline for its
# full duration. That inline wait is now bounded (see the split-finalize site and
# _SPLIT_CREDIT_INLINE_WAIT_SECONDS): the honoured tail completes in the
# background while the leg goes back to processing chunks.
_HONORED_RETRY_AFTER_CAP_SECONDS = 90.0

# R7-06: how long on_track_identified (the recognition leg) will wait INLINE for a
# creditable album-split finalize before letting it finish in the background. The
# task is already tracked in _bg_tasks (drained at shutdown) with a failure-logging
# done-callback, so the leg need not block on it. The cap sits between the short
# linear-retry total (~3s) and the honoured-Retry-After floor: a 429 is only
# HONOURED (event-loop sleep, up to 90s) when its Retry-After exceeds the transport's
# _RATE_LIMIT_MAX_WAIT (10s), so a honoured wait ALWAYS exceeds this 5s cap and is
# backgrounded — capping the R7-06 pipeline stall at ~5s instead of ~180s. At 5s a
# normal credit and an ordinary linear-backoff retry (~3s) still complete inline, so
# common-case timing is unchanged. (A 429 whose Retry-After is in (5s, 10s] is retried
# IN-THREAD by the transport rather than honoured; such a credit also exceeds the cap
# and is backgrounded too — harmless, the credit still lands via _bg_tasks.)
_SPLIT_CREDIT_INLINE_WAIT_SECONDS = 5.0


class _DefinitiveMissingInstance(Exception):
    """Terminal signal for an old collection instance proven stale."""

    def __init__(self, result: PlayCountReadResult):
        super().__init__("collection instance is definitively missing")
        self.result = result


class _ReplacementReadAborted(Exception):
    """A safely recovered identity whose one replacement read was inconclusive."""

# R7-03 (flip-resume): how far back a newly-armed session may reach to inherit an
# immediately-prior UNARMED session's closing-side rows (Lane, 2026-08-11: fixed
# 5-minute wall-clock, same closing side only).  R8-01 (#345, Lane 2026-08-12):
# the window is the GAP between the sessions — `new_session.started_at -
# prev.ended_at` — NOT the elapsed time since the prior session STARTED.  The
# started_at anchor made the feature inert: fragment play + gap + tail play +
# trailing silence always exceeded 300s for real music, so the exact credit the
# feature shipped to save (a full play split by a sleeve-cleaning pause) was
# still lost every time.  A full play split by a mid-side gap resumes within
# this window; a genuinely separate later listening does not.
_FLIP_RESUME_WINDOW_SECONDS = 300.0


class ListenTracker:
    """Manages play sessions and triggers Discogs field updates on album completion."""

    def __init__(
        self,
        writer: "DiscogsCollectionWriter",
        lastfm: Optional["LastFmClient"] = None,
        recover_collection_instance: Optional[
            Callable[[tuple[str, str], int, int, tuple[int, ...]],
                     Awaitable[Optional[CollectionIdentity]]]
        ] = None,
    ):
        # A-4: depend only on the Discogs WRITE half (Play Count / Last Played),
        # injected at the composition root (main.py).  A-3 had already moved this
        # off the resolver's internals; A-4 narrows it from the whole God client
        # to just the collection writer.
        self.writer = writer
        self.lastfm = lastfm
        # #421: the tracker gets one narrow, async recovery port rather than a
        # metadata reader/resolver dependency.  It is used only before an
        # absolute Play Count target exists, after the writer has conclusively
        # proven the latched collection instance missing.
        self._recover_collection_instance = recover_collection_instance
        # R5-22: love-on-completion needs a WORKING Last.fm client. If the user
        # asked for it but the client is disabled (scrobble_enabled off, missing
        # creds, pylast absent, or init failure), warn ONCE at startup — otherwise
        # the feature silently does nothing while every album still logs a ✅
        # (the false-success this guard removes).
        if lastfm is not None and getattr(lastfm, "love_on_completion", False) \
                and not getattr(lastfm, "enabled", False):
            log.warning(
                "Last.fm 'love on completion' is enabled but the Last.fm client "
                "is not active (check scrobble_enabled and credentials) — no track "
                "will actually be loved."
            )
        self._session: Optional[PlaySession] = None
        # R8-02 (#346) / R9-26 (#384): the SILENCE-BOUNDARY per-spin credit +
        # scrobble memory, owned by :class:`SpinMemory` (see that module's
        # docstring for the full model).  The R9-08 (#382) corrected semantics:
        # the live object is SWAPPED for a fresh one SYNCHRONOUSLY at each
        # genuine-silence boundary event (``_begin_new_spin`` — at the boundary,
        # NOT when that boundary's finalize completes; a finalize legally
        # completes minutes late on the honoured-Retry-After path, and the swap
        # is precisely what keeps the next spin's fresh entries safe from it).
        # The OUTGOING object is handed to the boundary's own finalize, which
        # judges and records against its own spin.  NOT swapped by the #195
        # forced end (SESSION_ENDED_FORCED, R8-16: music never stopped) nor by
        # split-path finalizes (same spin continues).  A #185 replay-boundary
        # session (a real re-drop) stays EXEMPT from the duplicate-credit
        # check.  One spin whose attribution ping-pongs between releases splits
        # repeatedly and re-arms the same release — the memory suppresses the
        # repeat credits; R9-01's drop-on-genuine-credit rule (in SpinMemory)
        # lets a DIFFERENT record's genuine play advance the spin instead of
        # suppressing everything until silence.
        self._spin = SpinMemory()
        # R7-03 (flip-resume): the immediately-prior session that ended WITHOUT
        # arming a completion (a played side that never reached its closer, or
        # the first part of a closing side interrupted by a mid-side silence
        # gap).  The next armed session may inherit its closing-side rows if it
        # cannot cover the side alone — see :meth:`_apply_flip_resume`.  Holds at
        # most one session.  R8-15 (#349): overwritten on each unarmed TERMINAL
        # end, cleared on any armed end (completion, suppression, or unlatched
        # close) AND on an unarmed SPLIT-detach (the #166 short-circuit): a split
        # is attribution noise inside continuous music, not a flip, so it ends
        # the chain conservatively rather than leaving a stale session behind.
        self._prev_unarmed: Optional[PlaySession] = None
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
        elif event in (AudioEvent.SESSION_ENDED, AudioEvent.SESSION_ENDED_FORCED):
            # Bind this end to the session that is active *now*.  If an album
            # split later replaces it, the task below sees the session changed
            # and becomes a no-op instead of ending the new session (B-2).
            #
            # R8-16 (#350): only a GENUINE-silence SESSION_ENDED is a physical
            # spin boundary.  The #195 forced end (SESSION_ENDED_FORCED — locked
            # groove / stuck input, music never stopped) still ends and credits
            # the session identically, but must NOT clear the per-spin
            # credit/scrobble memory: the groove's still-identified closer would
            # otherwise re-arm a fresh session and mint one phantom credit per
            # hour until the needle lifts.
            target = self._session
            boundary = event is AudioEvent.SESSION_ENDED
            task = asyncio.create_task(
                self._end_session(expected=target, boundary=boundary)
            )
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

        R8-17 (#351, Lane 2026-08-12): a live ARMED session — closer played,
        coverage complete, merely waiting out the 45s silence window — used to be
        silently discarded here (drain only awaited already-created tasks), so a
        ``systemctl stop`` right after a record finished ate the credit with no
        log stronger than "Pipeline stopped".  Drain now detaches such a session
        and finalizes it behind the SAME gates as any end (completion gate,
        duplicate-credit memory, idempotent latches — ``_finalize_session`` is
        unchanged), as a tracked ``_bg_tasks`` task inside the same bounded wait.
        Treated as a spin boundary (the process is ending).  An UNARMED live
        session still has nothing to credit and is discarded (debug-logged).
        """
        armed = None
        async with self._lifecycle_lock:
            if self._session is not None and self._session.potential_last_track:
                armed = self._detach_session_locked()
            elif self._session is not None:
                log.debug(
                    "drain(): discarding a live UNARMED session (nothing to credit)."
                )
        if armed is not None:
            log.info(
                "R8-17: finalizing a live armed session at shutdown (release %s) — "
                "the closer played; the credit is not discarded.",
                armed.album_release_id,
            )
            # A boundary (the process is ending): swap the spin memory NOW and
            # judge the shutdown finalize against the outgoing SpinMemory
            # (R8-02/F3).
            spin_memory = self._begin_new_spin()
            task = asyncio.create_task(
                self._finalize_detached(armed, spin=spin_memory)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._on_end_session_done)
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

    async def _end_session(self, expected=_CURRENT_SESSION, boundary: bool = True):
        """End the active session: detach it under the lifecycle lock, then credit
        it OUTSIDE the lock (CONC-2).

        `expected` lets a scheduled SESSION_ENDED bind to the session that was
        active when the silence fired; if an album split has since swapped in a
        new session, ending is skipped.  The default sentinel means "end
        whatever session is current" (used by the direct-await callers and the
        existing test suite).

        ``boundary`` (R8-02/R8-16): True for a genuine-silence SESSION_ENDED —
        a physical spin boundary, after whose finalize the per-spin
        credit/scrobble memory is cleared.  False for the #195 forced end
        (SESSION_ENDED_FORCED): the session ends and credits identically, but
        the spin memory survives (music never stopped).  Defaults True — the
        direct-await test callers simulate genuine silence.

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
        # R8-02/F3: the spin-memory swap is SYNCHRONOUS with the boundary (no
        # await since the detach), never deferred to the finalize's completion —
        # a slow terminal finalize (honoured Retry-After) must not wipe keys the
        # NEXT spin records in the meantime.  Swap when a session was actually
        # detached, or when genuine silence arrived with NO session at all (the
        # F2 shape: a forced end whose input then decayed without ever
        # re-entering music — the re-armed detector delivers this boundary and
        # the memory must clear even though there is nothing to finalize).  A
        # STALE end (a split already replaced the session — music continuing)
        # swaps nothing: it is not a boundary of the CURRENT spin.
        boundary_effective = boundary and (detached is not None or self._session is None)
        spin_memory = self._begin_new_spin() if boundary_effective else self._spin
        if detached is not None:
            await self._finalize_detached(detached, spin=spin_memory)

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
        # R8-01 (#345): stamp the detach moment — the far edge of the flip-resume
        # gap.  For a terminal end this is when SESSION_ENDED fired (i.e. the
        # gap's start as #316 defined it); for a split it marks the swap.
        session.ended_at = time.monotonic()
        return session

    def _is_duplicate_credit(
        self, session: PlaySession, spin: Optional[SpinMemory] = None
    ) -> bool:
        """R8-02 (#346): True when *session*'s Play Count credit must be
        suppressed as a duplicate of one already made for the SAME release
        during the SAME physical spin.

        ``spin`` is the :class:`SpinMemory` to judge against — a boundary
        finalize's OUTGOING object, or (default) the live one (R8-F3; see
        :meth:`_begin_new_spin`).

        Checked in :meth:`_finalize_session`, so it guards BOTH the split-path
        credit (an attribution ping-pong re-arming an already-credited release)
        AND the terminal SESSION_ENDED credit (the spin ends on the re-armed
        release).  A session opened by a genuine #185 replay boundary is a real
        second spin and is NEVER suppressed (``opened_by_replay_boundary``) —
        the exemption is a property of the SESSION, so it lives here, not in
        SpinMemory.

        Membership, not a wall-clock window (R8-02): a release is a duplicate
        iff it was credited since the last physical spin boundary — where the
        boundary is the SWAP at the boundary EVENT (R9-08 corrected wording:
        not "when the finalize completes"; a boundary finalize legally
        completes minutes late and judges against its own outgoing spin).
        Read-only — the memory is written only when a credit actually lands,
        in :meth:`_credit_completed_album`, whose R9-01 drop-on-genuine-credit
        rule also advances the spin past OTHER releases.
        """
        if session.opened_by_replay_boundary:
            return False
        rid = session.album_release_id
        if rid is None:
            return False
        memory = spin if spin is not None else self._spin
        return memory.is_duplicate_credit(rid)

    @staticmethod
    def _scrobble_key(track: "TrackMetadata") -> tuple:
        """R8-09: per-spin scrobble identity — folded title/artist plus the
        release id (element [2] is load-bearing: SpinMemory's replay-boundary
        and drop-on-credit filters select on it).  R9-03: folded with the same
        ``fold_text`` the tracklist matcher uses, so the occurrence count below
        compares like with like."""
        return (fold_text(track.title), fold_text(track.artist),
                track.discogs_release_id)

    @staticmethod
    def _same_title_occurrences(track: "TrackMetadata") -> int:
        """R9-03 (#380, REWORKED — Lane 2026-08-13): the scrobble cap for this
        track's key = the number of tracklist rows sharing its FOLDED title.

        Why a COUNT and not row identity: recognition cannot distinguish a
        duplicate-titled album's second row from a re-commit of the first —
        ``SideIndex.from_tracklist`` resolves a repeated title to its FIRST
        occurrence by design (B-5), so the originally-locked ``global_index``
        key component was identical for both plays (mechanically inert).
        Tier-1 folded equality is the right comparison: the matcher's tier-2
        decoration strip REQUIRES a unique folded title, so duplicate rows can
        only ever arise as tier-1 equals.  No/unknown tracklist → cap 1 (the
        R8-09 behavior).
        """
        if not track.tracklist:
            return 1
        folded = fold_text(track.title)
        n = sum(1 for row in track.tracklist if fold_text(row.title) == folded)
        return max(1, n)

    def should_scrobble(self, track: "TrackMetadata") -> bool:
        """R8-09 (#348) / R9-03 (#380): False when this track's per-spin
        scrobble tally has reached its cap — the number of tracklist rows
        sharing its title (1 for a unique title: the plain swing-back dedup;
        N for a duplicated title, so the album's second "Interlude" scrobbles
        while an N+1th commit is a swing-back and is suppressed).  Bounded
        worst case: a same-title swing-back during an active ping-pong can
        consume a slot (over-scrobble ≤ N−1) — strictly between the pre-R8
        unbounded re-scrobbles and the always-lose-the-second regime.

        Same boundary as the credit memory (the live SpinMemory, swapped at
        genuine-silence boundaries; #185 replays reset the release's tallies;
        the R9-01 drop advances past other releases).  Called by
        TrackCommitService before its scrobble step; pair with
        :meth:`record_scrobble`.
        """
        key = self._scrobble_key(track)
        return self._spin.scrobble_count(key) < self._same_title_occurrences(track)

    def record_scrobble(self, track: "TrackMetadata") -> None:
        """R8-09: record *track* as scrobbled this spin (tally += 1).  Recorded
        at DISPATCH (before the scrobble awaits), the #163 in-flight-latch
        pattern: a re-commit racing the in-flight scrobble must be suppressed,
        and a failed scrobble has no retry path for the record to distort
        (per-track scrobbles are one-shot by design)."""
        self._spin.record_scrobble(self._scrobble_key(track))

    def _apply_flip_resume(self, session: PlaySession) -> None:
        """R7-03: when *session* armed a completion its OWN identifications cannot
        cover, inherit the immediately-prior unarmed session's rows of the SAME
        release on the SAME closing side — so a full play of a MULTI-row closing
        side split across sessions by a mid-side silence gap (or a long flip)
        still credits, instead of being suppressed as a mis-attributed single.

        Bounded (Lane, 2026-08-11; window re-anchored Lane, 2026-08-12, R8-01):
        only the immediately-prior unarmed session, only if the GAP between the
        sessions — this session's ``started_at`` minus the prior session's
        ``ended_at`` — is within ``_FLIP_RESUME_WINDOW_SECONDS``, only rows on
        the closing side.  The prior anchor (elapsed since ``prev.started_at``,
        measured at THIS session's finalize) summed fragment play + gap + tail
        play + trailing silence and always exceeded 300s for real music, so the
        feature never fired (#345).  ``prev.started_at`` is kept only as a
        fallback for hand-built sessions that were never detached.  Cannot
        manufacture a phantom: it adds only rows the prior session GENUINELY
        identified for THIS release AND that lie on the closing side, so a
        compilation's foreign/off-side rows never join the set.  A one-row
        closing side never reaches here (the closer covers it, so
        ``completion_supported`` is already True).  Mutates only
        ``session.inherited_side_rows`` (the completion gate re-reads it below);
        the love target is untouched.
        """
        prev = self._prev_unarmed
        if prev is None:
            return
        if not (session.potential_last_track and session.album_release_id):
            return
        if session.completion_supported:
            return  # already covered on its own — nothing to inherit
        # R8-01 (#345): the window bounds the GAP, per #316's original text —
        # "when MUSIC_STARTED follows a SESSION_ENDED … bounded window".
        gap_anchor = prev.ended_at if prev.ended_at is not None else prev.started_at
        if session.started_at - gap_anchor > _FLIP_RESUME_WINDOW_SECONDS:
            return  # the gap was longer than one flip — a separate listening
        closer = session.closing_track
        if closer is None or closer.discogs_release_id != session.album_release_id:
            return
        side_rows = session._closing_side_row_indices(closer)
        if not side_rows:
            return  # sideless closer — no closing side to resume
        inherited = {
            t.side_index.global_index
            for t in prev.identified_tracks
            if t.discogs_release_id == session.album_release_id
            and t.side_index.global_index in side_rows
        }
        if inherited:
            session.inherited_side_rows |= inherited
            log.info(
                "R7-03 flip-resume: inherited %d closing-side row(s) %s from the "
                "prior unarmed session for release %s — a full play split by a "
                "mid-side silence gap still credits.",
                len(inherited), sorted(inherited), session.album_release_id,
            )

    def _begin_new_spin(self) -> SpinMemory:
        """R8-02 (cold-review F3): SYNCHRONOUSLY swap the live :class:`SpinMemory`
        for a fresh one at a physical spin boundary, returning the OUTGOING one.

        Called (no await between the detach and this) on every boundary path —
        terminal genuine-silence SESSION_ENDED, R8-17 drain — so entries
        recorded by the NEXT spin can never be wiped by the old spin's finalize
        completing late (a terminal finalize legally takes minutes on the
        honoured-Retry-After path; an earlier design cleared the LIVE memory in
        that finalize's ``finally``, wiping whatever a new spin had recorded in
        the meantime).  The boundary finalize still judges and records against
        the returned OUTGOING object, so a ping-ponged spin's terminal credit
        stays suppressed while its landed credit dies with the spin — exactly
        right, since the next spin must not be suppressed by it.
        """
        outgoing = self._spin
        if outgoing.credited_count or outgoing.scrobble_key_count:
            log.debug(
                "Spin boundary: retiring per-spin memory "
                "(%d credited release(s), %d scrobble key(s)).",
                outgoing.credited_count, outgoing.scrobble_key_count,
            )
        self._spin = SpinMemory()
        return outgoing

    async def _finalize_detached(
        self, session: PlaySession, spin: Optional[SpinMemory] = None
    ):
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

        ``spin`` (R8-02): the :class:`SpinMemory` THIS finalize judges and
        records against.  Boundary callers (terminal SESSION_ENDED, drain) pass
        the OUTGOING object returned by :meth:`_begin_new_spin` — the swap
        already happened synchronously at the boundary EVENT, so this finalize
        judges its own spin while the live memory starts fresh.  Split-path and
        forced-end callers pass (or default to) the LIVE object — their spin
        continues.  ``None`` → the live object.
        """
        async with self._finalize_lock:
            await self._finalize_session(session, spin)

    async def _finalize_session(
        self, session: PlaySession, spin: Optional[SpinMemory] = None
    ):
        """Do the end-of-session crediting work for an already-detached session.

        Operates on a local `session` reference (self._session has already been
        cleared by the caller), so it is safe to await the Discogs/Last.fm
        executor calls here without another coroutine mutating it.

        ``spin`` (R8-02/F3): the :class:`SpinMemory` this finalize judges
        against and records a landed credit into — the OUTGOING object on
        boundary paths, the live one otherwise (None → live).
        """
        if spin is None:
            spin = self._spin
        # Idempotency guard: never credit one session's Play Count twice, even
        # if a re-entrant end somehow finalizes the same session object again
        # (B-8).  Pairs with the B-2 lifecycle lock as defense-in-depth.
        if session.credited:
            log.debug("Session already credited — skipping to stay idempotent (B-8).")
            return

        # R7-03: before the completion gate, let an armed session that cannot
        # cover its closing side alone inherit a recent prior unarmed session's
        # closing-side rows (a full play split by a mid-side silence gap).
        self._apply_flip_resume(session)

        track_count = len(session.identified_tracks)
        log.info(
            f"Play session ended. "
            f"Identified {track_count} track(s). "
            f"Last track reached: {session.potential_last_track}"
        )

        # R7-03: this session's outcome decides whether it can seed a later
        # flip-resume.  Default to clearing the chain; the not-armed branch below
        # re-arms it.  A completion, a suppression, or an unlatched close all end
        # the chain (nothing downstream should inherit from them).
        #
        # R9-02 (#379): a ZERO-track session touches the chain NOT AT ALL —
        # neither this clear nor the not-armed re-seed.  A one-chunk noise blip
        # during the very flip gap this feature exists for (a door slam, a
        # cueing thump) mints an empty session whose unarmed end used to
        # overwrite — and, after the first cut of this fix guarded only the
        # re-seed, CLEAR — the fragment, killing the flip-resume credit either
        # way.  An empty session carries no information about the chain; the
        # 300s gap anchor on the KEPT fragment still bounds the resume.
        if session.identified_tracks:
            self._prev_unarmed = None

        # R7-02: is this a duplicate credit of the same release within the silence
        # window (an attribution ping-pong, not a genuine #185 re-drop)?  Computed
        # once and applied to BOTH the Play Count credit and the Last.fm love, so
        # neither double-fires for one physical spin regardless of whether that
        # spin ends via a split or a terminal SESSION_ENDED.
        duplicate_credit = self._is_duplicate_credit(session, spin)

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
        elif session.potential_last_track and session.album_release_id and duplicate_credit:
            # R8-02 (#346): this release was already credited during the SAME
            # physical spin (an attribution ping-pong re-armed an
            # already-credited release; no silence boundary has intervened), and
            # this session was NOT opened by a genuine #185 re-drop — suppress
            # the duplicate Play Count credit AND the love (the love gate below
            # also honours `duplicate_credit`).  Loud on purpose so a real edge
            # case is diagnosable.  Reaches here on ANY finalize path: a
            # split-detached re-arm, the terminal SESSION_ENDED credit of a spin
            # that ends on the re-armed release, or a #195 forced end whose
            # groove re-armed an hour later (R8-16 — the memory survives forced
            # ends precisely so this fires).
            rid = session.album_release_id
            log.info(
                "R8-02: suppressing a duplicate credit for release %s — it was "
                "credited %.1fs ago within this same physical spin (no silence "
                "boundary since) and this session was not opened by a #185 "
                "replay boundary; one physical play must not be double-credited.",
                rid, time.monotonic() - spin.credited_at(rid),
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
                    await self._credit_completed_album(session, spin)
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
            # R7-03: a played stretch that never reached its closer — the flip
            # source.  If the next armed session is the remainder of this same
            # closing side (a mid-side gap split the play), it can inherit these
            # rows.  Held whether or not a release is latched; _apply_flip_resume
            # filters by release + closing side, so an irrelevant one is ignored.
            #
            # R9-02 (#379): only a session that actually IDENTIFIED something
            # may seed (overwrite) the chain.  A ZERO-track session — a one-
            # chunk noise blip during the very flip gap this feature exists for
            # (a door slam, a cueing thump) — used to clobber the fragment and
            # kill the flip-resume credit (executed: control 1 credit, with
            # blip 0).  An empty session can seed no rows, so keeping the
            # prior fragment is strictly better; the 300s gap anchor on the
            # KEPT session still bounds the resume.
            if session.identified_tracks:
                self._prev_unarmed = session

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
            and session.love_supported   # #182 gate + R6-06: unlatched DB-tier
                                         # closers need ≥2 rows too (the credit
                                         # path skips them; the love must as well)
            and not duplicate_credit     # R7-02: a duplicate credit must not
                                         # double-love the closer either
            and not session.loved
            and not session.loving
            and self.lastfm
            # R5-22: require an ENABLED client. A disabled client's love() is a
            # graceful no-op returning True, so entering this branch would log a
            # false "✅ Last.fm loved" and latch loved=True while nothing was sent.
            and self.lastfm.enabled
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

    async def _run_writer_cancellation_safe(self, fn, *args):
        """Run one Discogs writer operation without releasing on cancellation.

        Cancelling a ``run_in_executor`` await does not stop an already-running
        worker.  Every caller is under ``_finalize_lock``; keep that caller alive
        until the submitted writer operation has actually finished, retrieve a
        late success or exception, then re-propagate the original cancellation.
        This prevents a second finalizer from entering the shared writer while
        the cancelled finalizer's worker still owns it.
        """
        writer_task = asyncio.ensure_future(self.writer.run(fn, *args))
        try:
            return await asyncio.shield(writer_task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            while not writer_task.done():
                try:
                    await asyncio.shield(writer_task)
                except asyncio.CancelledError:
                    if current_task is not None:
                        current_task.uncancel()
                    continue
                except BaseException:
                    break
            try:
                writer_task.result()
            except BaseException:
                pass
            raise asyncio.CancelledError

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
        ``loved``) only when this returns True. The default per-attempt backoff is
        short (1s + 2s = 3s total) so the attempts fit the shutdown drain window
        (_SHUTDOWN_DRAIN_SECONDS) and never spin. This whole helper runs OUTSIDE
        the lifecycle lock (CONC-2/#96) — it holds only the finalize lock — so its
        backoff never stalls the next record's session START. (R7-06: on the
        album-split path the recognition LEG no longer blocks on this either — the
        split credit's inline wait is bounded by _SPLIT_CREDIT_INLINE_WAIT_SECONDS
        and a honoured Retry-After finishes in the background.)

        #229: when an attempt raises :class:`DiscogsRateLimited` (a Play Count 429
        whose Retry-After exceeds the transport's in-thread cap), the backoff for
        that attempt becomes the honoured server wait (``asyncio.sleep``, capped at
        _HONORED_RETRY_AFTER_CAP_SECONDS) instead of the short linear one — so the
        credit lands after the throttle window clears rather than losing itself to
        three futile in-window retries. The sleep is a normal event-loop await, so
        shutdown's drain cancels it cleanly (no parked worker thread), and #186's
        idempotent absolute-set means the honoured re-POST cannot double-credit.
        """
        for n in range(1, _FINALIZE_WRITE_ATTEMPTS + 1):
            backoff = _FINALIZE_RETRY_BACKOFF_SECONDS * n
            try:
                if await attempt():
                    return True
                log.warning("%s attempt %d/%d failed.", label, n, _FINALIZE_WRITE_ATTEMPTS)
            except DiscogsRateLimited as e:
                # #229: honour the server's Retry-After in the event loop instead
                # of the short linear backoff, so the write lands once the throttle
                # window clears. Capped so a huge/hostile header can't wedge the
                # serialized finalize path.
                backoff = min(float(e.retry_after), _HONORED_RETRY_AFTER_CAP_SECONDS)
                log.warning(
                    "%s attempt %d/%d rate-limited (Retry-After=%ss); honouring the "
                    "wait in the event loop (sleeping %ss) before retrying (#229).",
                    label, n, _FINALIZE_WRITE_ATTEMPTS, e.retry_after, backoff,
                )
            except (_DefinitiveMissingInstance, _ReplacementReadAborted):
                raise
            except Exception as e:
                log.warning(
                    "%s attempt %d/%d raised: %s", label, n, _FINALIZE_WRITE_ATTEMPTS, e
                )
            if n < _FINALIZE_WRITE_ATTEMPTS:
                await asyncio.sleep(backoff)
        return False

    async def _credit_completed_album(
        self, session: PlaySession, spin: Optional[SpinMemory] = None
    ):
        """Credit a completed album: increment Play Count (bounded retry, #163)
        and update Last Played. The caller has already set ``session.crediting``
        (in-flight) after confirming it was not already set, so this runs exactly
        once per session. Commits ``session.credited`` ONLY when the increment
        actually lands, leaving a transient failure uncommitted (and logged loud).

        ``spin`` (R8-02/F3): the :class:`SpinMemory` a landed credit is recorded
        into — the caller's OUTGOING object on boundary paths (the record dies
        with the spin, correctly leaving the NEXT spin unsuppressed), the live
        one otherwise (None → live).
        """
        if spin is None:
            spin = self._spin
        log.info(
            f"Last track confirmed for release {session.album_release_id} — "
            f"incrementing Play Count and updating Last Played in Discogs."
        )
        # #186: read the current count ONCE and compute the absolute target ONCE,
        # then retry only the idempotent absolute POST. Retrying the whole
        # read-modify-write (the pre-#186 shape) re-read an ambiguous-but-applied
        # POST's new value and credited it AGAIN (+2 for one play). The read result
        # is memoised across attempts in `plan`; a transient READ failure (None)
        # leaves `plan` unset so the next attempt safely re-reads (the GET is
        # idempotent), while a landed-but-unobserved POST re-POSTs the SAME target.
        plan: dict = {}
        # #421: this budget belongs to the detached session, not the retry loop.
        # A replacement read that is rate-limited may retry under #229, but it
        # must never restart resolver recovery or select another identity.
        recovery_spent = False
        replacement_identity_established = False

        def _valid_resolve_key(key) -> bool:
            return (
                isinstance(key, tuple)
                and len(key) == 2
                and all(isinstance(part, str) and bool(part.strip()) for part in key)
            )

        async def _read_replacement() -> None:
            """Perform the replacement read without opening another recovery path."""
            result = await self._run_writer_cancellation_safe(
                self.writer.read_play_count,
                session.album_release_id,
                session.album_instance_id,
            )
            if result.state is PlayCountReadState.DEFINITIVE_INSTANCE_MISSING:
                # A second complete snapshot says the just-established target is
                # stale too.  Neither field may write to it and recovery is spent.
                raise _DefinitiveMissingInstance(result)
            if result.state is not PlayCountReadState.READY:
                # The identity is still safe for META-7 Last Played, but an
                # inconclusive replacement read must not get a second recovery or
                # fabricate an absolute Play Count target.
                raise _ReplacementReadAborted()
            plan["field_id"] = result.field_id
            plan["current"] = result.current_count
            plan["target"] = result.current_count + 1

        async def _credit_attempt() -> bool:
            nonlocal recovery_spent, replacement_identity_established
            if not plan:
                if replacement_identity_established:
                    # #229 can re-enter here after a rate-limited replacement
                    # GET.  It retries that GET only; the recovery budget remains
                    # spent and the established pair cannot change.
                    await _read_replacement()
                else:
                    result = await self._run_writer_cancellation_safe(
                        self.writer.read_play_count,
                        session.album_release_id,
                        session.album_instance_id,
                    )
                    if result.state is PlayCountReadState.DEFINITIVE_INSTANCE_MISSING:
                        if recovery_spent:
                            raise _DefinitiveMissingInstance(result)
                        recovery_spent = True  # spend BEFORE an await/cancellation point
                        stale_release_id = session.album_release_id
                        stale_instance_id = session.album_instance_id
                        resolve_key = session.album_resolve_key
                        if (
                            self._recover_collection_instance is None
                            or not _valid_resolve_key(resolve_key)
                            or type(stale_release_id) is not int or stale_release_id <= 0
                            or type(stale_instance_id) is not int or stale_instance_id <= 0
                        ):
                            raise _DefinitiveMissingInstance(result)
                        try:
                            identity = await self._recover_collection_instance(
                                resolve_key,
                                stale_release_id,
                                stale_instance_id,
                                result.observed_instance_ids,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.warning(
                                "Discogs collection recovery "
                                "stage=recovery-callback-failed "
                                "expected_release_id=%d expected_instance_id=%d; "
                                "suppressing both field writes.",
                                stale_release_id, stale_instance_id,
                            )
                            raise _DefinitiveMissingInstance(result) from None
                        if (
                            not isinstance(identity, CollectionIdentity)
                            or identity.release_id != stale_release_id
                            or identity.instance_id == stale_instance_id
                            or len(result.observed_instance_ids) != 1
                            or identity.instance_id != result.observed_instance_ids[0]
                        ):
                            log.info(
                                "Discogs collection recovery refused release %s / "
                                "instance %s; suppressing both field writes.",
                                stale_release_id, stale_instance_id,
                            )
                            raise _DefinitiveMissingInstance(result)
                        # Update the detached session only after the narrow port
                        # has established the same-release replacement safely.
                        session.album_release_id = identity.release_id
                        session.album_instance_id = identity.instance_id
                        replacement_identity_established = True
                        await _read_replacement()
                    elif result.state is not PlayCountReadState.READY:
                        # Field missing / unreadable / non-integer — abort WITHOUT
                        # writing (META-1/META-2), already logged loud by the reader.
                        # Treated as a failed attempt so a transient read is retried.
                        return False
                    else:
                        plan["field_id"] = result.field_id
                        plan["current"] = result.current_count
                        plan["target"] = result.current_count + 1
            return await self._run_writer_cancellation_safe(
                self.writer.set_play_count,
                session.album_release_id,
                session.album_instance_id,
                plan["field_id"],
                plan["current"],
                plan["target"],
            )

        try:
            success = await self._finalize_write_with_retry(
                "Discogs Play Count increment", _credit_attempt
            )
        except _DefinitiveMissingInstance:
            log.error(
                "⚠ Discogs Play Count credit FAILED for release %s / instance %s: "
                "the collection instance is definitively missing; suppressing Last Played.",
                session.album_release_id, session.album_instance_id,
            )
            return
        except _ReplacementReadAborted:
            # Recovery established a safe replacement, so META-7 retains its
            # independent one-shot Last Played attempt.  Only the Play Count
            # plan is absent; do not retry this ambiguous read or recover again.
            success = False
            log.error(
                "⚠ Discogs Play Count credit FAILED for recovered release %s / "
                "instance %s: replacement read was inconclusive; no count write "
                "was attempted.",
                session.album_release_id, session.album_instance_id,
            )
        if success:
            session.credited = True  # committed ONLY after the write landed (#163)
            # R8-02 (#346): remember that this release was credited THIS SPIN so
            # any subsequent credit for it before the next silence boundary (an
            # attribution ping-pong re-arm, or a forced-end locked-groove re-arm,
            # R8-16 — not a genuine #185 re-drop) is suppressed.  Recorded only
            # on a landed credit, keyed by release.  R9-01 (#378): record_credit
            # ALSO drops every OTHER release's entries and scrobble tallies —
            # a genuine credit for a different record means the spin moved on,
            # so a fast-swap evening no longer suppresses a record's second
            # genuine play (ping-pong noise can't trigger the drop: a foreign
            # 1-track swing never passes the completion gate to land a credit).
            # Bounding is the boundary swap in :meth:`_begin_new_spin`.
            if session.album_release_id is not None:
                spin.record_credit(session.album_release_id, time.monotonic())
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
            last_played_success = await self._run_writer_cancellation_safe(
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
                # R6-10: softened from a WARNING that always cried "check the
                # wiring". There is a KNOWN benign interleave that lands here with
                # nothing wrong: a SESSION_ENDED task detached the previous session
                # in the SAME event-loop turn that a MUSIC_STARTED had merged into
                # it, so recognition finds no session and starts a fresh one — no
                # play is lost. Name that case instead of alarming on every
                # occurrence; a genuine wiring gap shows up as this recurring OUTSIDE
                # a session end (plus other symptoms), not as one self-healing line.
                log.info(
                    "Track '%s' recognized with no active session — starting one "
                    "(no play lost). Expected benign case (R6-10): a SESSION_ENDED "
                    "detached the previous session in the same event-loop turn a "
                    "MUSIC_STARTED merged into it. If this RECURS and not around a "
                    "session end, it can indicate a silence-detector / capture "
                    "wiring gap (#195).",
                    track.title,
                )
                self._start_session()

            split_reason = None
            # R7-02: True only when the split below is a genuine #185 replay
            # boundary (the same record was re-dropped).  A replay-boundary split
            # detaches a genuinely-completed playthrough that earns its credit
            # even if the release was credited moments ago; an attribution-swing
            # split (album change) does not, so the credited-memory guard applies.
            replay_boundary_split = False
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
                    # #468: a DIFFERENT release_id that shares the album's MASTER is
                    # just another pressing (a collection/database tier flip from the
                    # AudD album-string variance that breaks the #184 resolve_key
                    # match, in EITHER direction) — the same album, not a swap.
                    track.discogs_master_id is not None
                    and track.discogs_master_id == self._session.last_master_id
                ):
                    log.info(
                        f"Same album master {track.discogs_master_id} — release "
                        f"{self._session.last_release_id} → {track.discogs_release_id} "
                        f"is a different pressing of the same album (#468), not an "
                        f"album change; continuing the session."
                    )
                elif (
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
                #
                # R6-08: anchor on the release's FIRST VINYL row, not tracklist
                # row 0.  R5-16(a) made only the CLOSER vinyl-aware; a hybrid
                # whose vinyl opener follows a leading CD/file row (global_index
                # 1+) never matched `== 0`, so a genuine re-drop merged (one
                # credit for two plays).  `first_playable_index` falls back to 0,
                # so a plain numbered/side-A-first tracklist is unchanged.
                #
                # R6-05: EXEMPT a single-playable-row release (`is_last_track` at
                # the opener anchor → opener IS the closer).  There "replay" and
                # "still playing" are indistinguishable, so a foreign
                # mis-attribution mid-spin (which breaks the consecutive-dedup
                # chain the guard below relies on) would otherwise let the
                # single's own re-id split → carve-out credit → re-arm → a SECOND
                # credit for ONE physical play (the #227 mechanism in singles
                # shape).  Merging is the conservative posture the codebase takes
                # for the analogous bookend-album residual (#227).
                #
                # Accepted conservative residuals: a re-drop straight into a
                # LATER track (side-B replay, needle past the opener) still
                # merges (the pre-#185 undercount, for that slice only); and two
                # spins of a single/one-vinyl-row release in one sitting credit
                # once (the singles analogue of #227).
                # Release-less (FALLBACK) tracks keep never triggering splits.
                self._session.potential_last_track
                and track.discogs_release_id is not None
                and track.discogs_release_id == self._session.last_release_id
                and track.side_index.global_index == track.side_index.first_playable_index
                and not track.is_last_track
                and not self._is_consecutive_reidentification(track)
            ):
                split_reason = (
                    f"Replay boundary (#185): '{track.title}' of release "
                    f"{track.discogs_release_id} identified after the closer "
                    f"armed the session — the record was re-dropped"
                )
                replay_boundary_split = True   # R7-02: a genuine re-drop, not a swing

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
                # R7-02: mark a session opened by a genuine #185 re-drop so its
                # eventual credit (via EITHER the split path below or a terminal
                # SESSION_ENDED) is exempt from the credited-memory guard — a real
                # second spin earns its own credit.  An attribution-swing (album
                # change) split leaves this False, so its credit is guarded.
                self._session.opened_by_replay_boundary = replay_boundary_split
                if replay_boundary_split:
                    # R8-09 (#348): a genuine re-drop REPLAYS the record, so its
                    # tracks legitimately scrobble again — drop the re-dropped
                    # release's keys from the per-spin scrobble memory (the
                    # credit side needs no analogue: the replay session carries
                    # the opened_by_replay_boundary exemption).
                    self._spin.clear_release_scrobbles(track.discogs_release_id)

            self._session.log_track(track)
            if track.is_last_track:
                log.info(f"Last track of album identified: '{track.title}' — watching for session end.")

        # CONC-2: credit the split-off session OUTSIDE the lifecycle lock, so the
        # NEXT record's session START (under the lock above) is never blocked by a
        # slow write.  R7-06: the recognition LEG, however, DID block — this credit
        # was awaited inline below, so a finalize honouring a Retry-After stalled
        # chunk processing for up to ~180s.  The leg now waits only a bounded
        # _SPLIT_CREDIT_INLINE_WAIT_SECONDS and lets a slow credit finish in the
        # background (it is in _bg_tasks and drained at shutdown).
        #
        # #166: but the COMMON mid-album swap detaches a session that never reached
        # its last track — nothing to credit or love — and finalizing it would
        # still take the lock (and could stall the queue) to do only logging.
        # `potential_last_track` is a NECESSARY condition for BOTH the Play Count
        # credit and the Last.fm love in `_finalize_session`, so short-circuit the
        # non-creditable case here and never touch the lock. A creditable split
        # (its closer played right before the swap — rare) still finalizes below.
        if detached is not None and detached.potential_last_track:
            # #187 + R7-06: run the creditable split finalize as a TRACKED task.
            # It is registered in _bg_tasks with the same done-callback as a
            # SESSION_ENDED credit (which logs any failure) and is drained at
            # shutdown, so it is complete-and-safe WITHOUT the recognition leg
            # blocking on it.  Because it is an independent task (not a child of
            # this coroutine), the leg's shutdown cancellation does NOT cancel it —
            # drain() awaits it via _bg_tasks.
            #
            # #187 was right that a bare inline `await task` loses the credit at
            # shutdown (cancelling the awaiter cancels the awaited), but its cure —
            # `await asyncio.shield(task)` UNBOUNDED — meant the leg still blocked
            # for the WHOLE finalize, honoured Retry-After included, stalling chunk
            # processing up to ~180s (R7-06).  Fix: wait_for a BOUNDED
            # _SPLIT_CREDIT_INLINE_WAIT_SECONDS; on timeout the shielded task keeps
            # running in the background and the leg returns to processing audio.
            # shield is still required so wait_for's timeout-cancel hits only the
            # wrapper, never the credit.  In NORMAL operation (no throttle) the
            # credit completes well inside the bound, so common-case timing is
            # unchanged; only a honoured-Retry-After tail is deferred to the
            # background.
            # R8-02/F3: bind the LIVE spin memory at task creation — the split's
            # spin is the CURRENT one, and if a boundary swap happened before
            # this task ran, defaulting at run time would judge against the
            # wrong (new) spin's memory.
            task = asyncio.create_task(
                self._finalize_detached(detached, spin=self._spin)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._on_end_session_done)
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), _SPLIT_CREDIT_INLINE_WAIT_SECONDS
                )
            except asyncio.TimeoutError:
                # R7-06: the credit is taking longer than the inline bound (almost
                # always a honoured Discogs Retry-After). Stop blocking the
                # recognition leg — the shielded task is still running, in
                # _bg_tasks, and its done-callback will log the outcome.
                log.info(
                    "R7-06: split credit for release %s still in flight after %.1fs "
                    "(likely honouring a Discogs Retry-After) — continuing to "
                    "process audio; it completes in the background and is drained "
                    "at shutdown.",
                    detached.album_release_id, _SPLIT_CREDIT_INLINE_WAIT_SECONDS,
                )
            except asyncio.CancelledError:
                # Shutdown cancelled the recognition leg mid-wait — propagate so
                # the leg tears down cleanly. drain() still awaits the shielded
                # task through _bg_tasks (that's what #187 registered it for).
                raise
            except Exception:
                # R6-11: the task's done-callback (_on_end_session_done) already
                # logs any exception from the detached finalize — at completion,
                # attached to the SESSION_ENDED that caused it. Don't ALSO let it
                # propagate through the recognition leg and get logged a second
                # time as a recognition error; the credit already failed and was
                # reported once. (A raise here is rare — _finalize_session contains
                # its own write failures — so this is a backstop, not the norm.)
                pass
        elif detached is not None:
            log.debug(
                "Split-off session reached no last track — nothing to credit; "
                "skipping finalize and its lock (#166)."
            )
            # R8-15 (#349): this unarmed split-detach bypasses _finalize_session,
            # which is where the _prev_unarmed chain is normally maintained — so
            # without this line a STALE prior-unarmed session survived here,
            # falsifying the documented invariant.  End the chain rather than
            # seed it: a split is attribution noise inside CONTINUOUS music, not
            # a flip (flip-resume's scenario is a real silence gap), so the
            # conservative, missed-over-phantom choice is to clear.
            self._prev_unarmed = None
