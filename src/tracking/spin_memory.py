"""Per-physical-spin credit + scrobble memory (R9-26/#384 — the Wave-1 vehicle).

One PHYSICAL SPIN = the stretch between genuine-silence boundaries (a terminal
SESSION_ENDED, or the R8-17 shutdown finalize).  ListenTracker swaps the live
``SpinMemory`` for a fresh one SYNCHRONOUSLY at each boundary
(``_begin_new_spin``) and hands the OUTGOING object to that boundary's own
finalize — so a finalize completing minutes late (honoured Retry-After) judges
and records against its own spin while the next spin starts clean (the R8-W1
F3 design, now owned by this object instead of hand-threaded dicts).

The memory answers exactly four questions, and owning them in one place is the
point — R9-01 and the R8-F3 bug class both lived in the threading between four
call sites:

* ``is_duplicate_credit(rid)`` — was this release already credited THIS spin?
  (Membership, not a wall-clock window: R8-02.  The #185 replay-boundary
  exemption stays the CALLER's check — it is a property of the session, not of
  the memory.)
* ``record_credit(rid, now)`` — a credit LANDED.  R9-01 (#378, LOCKED Lane
  2026-08-13): landing a genuine credit for one release DROPS every OTHER
  release's credit entries AND scrobble tallies — "the spin moved on to
  another record" — so a fast-swap evening (every gap under the silence
  threshold) no longer suppresses a record's genuine second play or its
  scrobbles.  The crediting release's OWN entries are kept (the ping-pong
  guard: a swing back to it is still a duplicate).  Ping-pong noise cannot
  trigger the drop: a foreign 1-track swing session never passes the
  completion gate, so it never lands a credit.  FALLBACK (release-None)
  scrobble tallies are dropped too — the spin moved on, and this incidentally
  lets a FALLBACK record replayed after an intervening credited record
  scrobble again (softening the documented R8-F4 residual).
* ``scrobble_count(key)`` / ``record_scrobble(key)`` — R9-03 (#380, REWORKED
  from the originally-locked row-aware key, Lane 2026-08-13): recognition
  cannot distinguish a duplicate-titled album's SECOND row from a re-commit of
  the first — ``SideIndex.from_tracklist`` resolves a repeated title to its
  FIRST occurrence by design (B-5), so a ``global_index`` key component would
  have been identical for both and the locked mechanism was inert.  Instead
  the memory stores a COUNT per key and the tracker allows up to N scrobbles
  per key per spin, where N = the number of tracklist rows sharing the folded
  title (known at commit time).  The album's second "Interlude" scrobbles; an
  N+1th commit is a swing-back and is suppressed.  Bounded worst case: a
  same-title swing-back during a ping-pong can consume a slot (over-scrobble
  ≤ N−1, only while a ping-pong is active) — strictly between the pre-R8
  unbounded re-scrobbles and the R8-09 always-lose-the-second regime.
* ``clear_release_scrobbles(rid)`` — a #185 replay boundary (genuine re-drop)
  legitimately replays the record; its tallies reset so it scrobbles again.

Bounded: entries accumulate only within one spin (a handful of releases /
track keys) and the whole object is discarded at every genuine-silence
boundary.
"""

from typing import Optional


class SpinMemory:
    """Credit + scrobble memory for ONE physical spin.  Not thread-safe by
    design — every reader/writer runs on the event loop (the tracker) or is
    the single finalize holding ``_finalize_lock``."""

    def __init__(self):
        # release_id -> monotonic time its credit LANDED (the timestamp is for
        # the suppression log line only; the guard is membership).
        self._credited: dict = {}
        # scrobble key -> count of scrobbles dispatched this spin.  Key shape:
        # (folded_title, folded_artist, release_id_or_None) — element [2] is
        # load-bearing (the replay-boundary and drop filters select on it).
        self._scrobbled: dict = {}

    # -- credits ------------------------------------------------------------

    def is_duplicate_credit(self, release_id: int) -> bool:
        return release_id in self._credited

    def credited_at(self, release_id: int) -> Optional[float]:
        return self._credited.get(release_id)

    def record_credit(self, release_id: int, now: float) -> None:
        """Record a LANDED credit — and apply the R9-01 drop-on-genuine-credit
        rule: every OTHER release's credit entries and scrobble tallies are
        dropped ("the spin moved on"); this release's own survive so a
        ping-pong swing back to it is still suppressed.

        Accepted tradeoff (Lane-locked; cold-review-2 disclosure): the scrobble
        drop is unconditional on the OTHER releases, so a foreign single F that
        was MISATTRIBUTED-and-scrobbled earlier this spin has its tally cleared
        by this credit — and if F is re-misattributed later in the same spin it
        scrobbles AGAIN.  The over-scrobble is bounded by the number of genuine
        other-release credits between F re-commits (a few records per evening),
        and R8-09's core case (a swing-back with NO intervening genuine credit)
        stays fully closed.  This is the missed-over-phantom cost of letting a
        genuine second play through: a phantom single re-scrobbles a handful of
        times over an evening, versus a genuine 20-minute play losing all its
        scrobbles forever."""
        self._credited = {
            rid: t for rid, t in self._credited.items() if rid == release_id
        }
        self._scrobbled = {
            k: c for k, c in self._scrobbled.items() if k[2] == release_id
        }
        self._credited[release_id] = now

    # -- scrobbles ----------------------------------------------------------

    def scrobble_count(self, key: tuple) -> int:
        return self._scrobbled.get(key, 0)

    def record_scrobble(self, key: tuple) -> None:
        self._scrobbled[key] = self._scrobbled.get(key, 0) + 1

    def clear_release_scrobbles(self, release_id: int) -> None:
        """#185 replay boundary: the re-dropped release's tallies reset (a
        genuine replay scrobbles its tracks again)."""
        self._scrobbled = {
            k: c for k, c in self._scrobbled.items() if k[2] != release_id
        }

    # -- introspection (logging / tests) ------------------------------------

    @property
    def credited_count(self) -> int:
        return len(self._credited)

    @property
    def scrobble_key_count(self) -> int:
        return len(self._scrobbled)
