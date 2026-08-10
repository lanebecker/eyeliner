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

from typing import Optional

# Distinct from None so a keyless throttle (key always None) still treats its
# very first observation as "changed" and logs it.
_UNSET = object()


class LogThrottle:
    """One repeating log line's throttle state (see module docstring)."""

    def __init__(self, interval: Optional[float] = None):
        self.interval = interval
        self._last_key = _UNSET
        self._last_emit = float("-inf")
        self._suppressed = 0

    def should_log(self, now: float, key=None):
        """Decide whether to emit for this observation, updating throttle state.

        ``now`` is a monotonic timestamp. ``key`` is the change-key (see module
        docstring). Returns ``(emit, suppressed)``.
        """
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
