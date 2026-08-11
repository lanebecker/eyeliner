"""LogThrottle — collapse a repeating log line into one throttled health signal.

Rounds 2–3 fixed journal / SD-card log floods point by point, and each fix
hand-rolled its own throttle sharing the same non-obvious ``-inf`` monotonic seed
— documented twice because it was re-derived twice (arch-5 / #221). This is the
one importable home for that pattern, so a future always-on log site (v1.8's
web-request error logging, say) reuses it instead of becoming copy #6 and the
next flood finding.

Two knobs cover the shapes proven in capture.py:

  * ``interval`` — seconds between periodic re-logs of an UNCHANGED condition.
    ``None`` means **pure dedup**: never re-log while the change-key is unchanged
    (#164's device-lookup dedup, which must NOT turn into periodic re-warning).
  * change-key (passed per observation to :meth:`should_log`) — a NEW key logs
    immediately, because a changed condition is worth surfacing at once (#178's
    changed-error-message). Pass ``None`` for a keyless interval summarizer
    (PCONC-4's drop warning, whose message never changes — only its count does).

The FIRST observation always logs because its change-key differs from the
``_UNSET`` sentinel (true even for a keyless site, where the key is always None).
``_last_emit`` is seeded to ``-inf`` (not ``0.0``) as belt-and-suspenders for the
interval path: on the Pi ``CLOCK_MONOTONIC`` is uptime-based and resets to ~0 on
reboot, so a ``0.0`` seed could otherwise suppress an interval-gated re-log during
the first ``interval`` of uptime — the early-boot window it matters most.

:meth:`should_log` returns ``(emit, suppressed)``: when ``emit`` is True the
caller logs and may report ``suppressed`` — the number of observations held back
since the last emit, EXCLUDING this one (a drop-style caller that counts the
current event too just reports ``suppressed + 1``). When ``emit`` is False the
observation was counted and held back.
"""

from collections import OrderedDict
from typing import Optional

# Hard cap on the per_message key map so it stays bounded on a 24/7 appliance
# even when suppressed keys never recur (e.g. connect-error text embedding a
# rotating CDN IP). Least-recently-touched keys are evicted past this (R5-13
# cold-review MEDIUM).
_PER_MESSAGE_MAX_KEYS = 64

# Distinct from None so a keyless throttle (key always None) still treats its
# very first observation as "changed" and logs it.
_UNSET = object()


class LogThrottle:
    """One repeating log line's throttle state (see module docstring)."""

    def __init__(self, interval: Optional[float] = None, per_message: bool = False):
        self.interval = interval
        # R5-13/R5-24: per_message throttles EACH distinct key independently.
        #   * A never-before-seen key emits immediately (the #178 "a changed
        #     condition surfaces at once" property is preserved), but two
        #     ALTERNATING keys can no longer defeat the throttle — each is
        #     rate-limited on its OWN interval, so A/B/A/B collapses to one A +
        #     one B per interval instead of a line per observation (R5-13).
        #   * Each key carries its OWN suppressed tally, so an emit reports the
        #     count for THAT message, never the previous message's (R5-24).
        # Default OFF: the single-key mode below is the change-KEY throttle the
        # capture.py device/dedup sites (#164) depend on (a key change re-emits,
        # even back to a prior key), and must not shift under them.
        self.per_message = per_message
        self._last_key = _UNSET
        self._last_emit = float("-inf")
        self._suppressed = 0
        # per_message state: key -> [last_emit, suppressed], LRU-ordered so
        # the map can be bounded by evicting the least-recently-touched key.
        self._per: OrderedDict = OrderedDict()

    def should_log(self, now: float, key=None):
        """Decide whether to emit for this observation, updating throttle state.

        ``now`` is a monotonic timestamp. ``key`` is the change-key (see module
        docstring). Returns ``(emit, suppressed)``.
        """
        if self.per_message:
            return self._should_log_per_message(now, key)
        changed = key != self._last_key
        due = self.interval is not None and (now - self._last_emit) >= self.interval
        if changed or due:
            suppressed = self._suppressed
            self._last_key = key
            self._last_emit = now
            self._suppressed = 0
            return True, suppressed
        self._suppressed += 1
        return False, 0

    def _should_log_per_message(self, now, key):
        """Per-key throttle (see __init__): a NEW key emits at once; a repeated
        key is rate-limited on its own interval and carries its own tally.

        The key map is an LRU bounded at ``_PER_MESSAGE_MAX_KEYS``: a genuinely
        new key past the cap evicts the least-recently-touched entry, so the map
        stays bounded regardless of how many distinct messages (with or without a
        pending tally) appear over 24/7 uptime. An evicted key that recurs is
        treated as new and re-surfaces immediately — the correct signal.
        """
        st = self._per.get(key)
        if st is None:
            # Never-seen key — surface at once (#178).
            if len(self._per) >= _PER_MESSAGE_MAX_KEYS:
                self._per.popitem(last=False)   # evict least-recently-touched
            self._per[key] = [now, 0]
            return True, 0
        self._per.move_to_end(key)              # mark recently touched (LRU)
        last_emit, suppressed = st
        due = self.interval is not None and (now - last_emit) >= self.interval
        if due:
            st[0] = now
            st[1] = 0
            return True, suppressed
        st[1] += 1
        return False, 0

    @property
    def suppressed(self) -> int:
        """Observations held back since the last emit (single-key mode)."""
        return self._suppressed

    def pending(self, key) -> int:
        """Suppressed count currently held for *key* (per_message mode)."""
        st = self._per.get(key)
        return st[1] if st is not None else 0

    def pending_items(self):
        """[(key, suppressed), …] for every key with a held-back tally
        (per_message mode) — so a recovery flush can report EACH message's own
        count rather than only the last-emitted one (R5-24)."""
        return [(k, sup) for k, (_, sup) in self._per.items() if sup > 0]

    def reset(self) -> int:
        """Clear all throttle state and return the pending suppressed count.

        Used by a caller that flushes a recovery summary and re-arms after an
        outage (ThrottledLogger.reset). After this the NEXT observation (even the
        same key) emits immediately, and the interval seed is restored to ``-inf``
        (the early-boot epoch-independence guard). In per_message mode this
        returns the TOTAL pending across all keys and clears the per-key map.
        """
        if self.per_message:
            pending = sum(sup for _, sup in self._per.values())
            self._per = {}
            return pending
        pending = self._suppressed
        self._last_key = _UNSET
        self._last_emit = float("-inf")
        self._suppressed = 0
        return pending
