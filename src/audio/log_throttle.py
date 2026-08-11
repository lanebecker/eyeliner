"""A same-message / minimum-interval log throttle — the #178 pattern, generalised.

Extracted so failure paths that fire on every ~10s recognition hop can share one
implementation instead of re-deriving the counter / interval / last-message
bookkeeping that ``capture.py``'s ``_log_capture_error`` (#178) and its
drop-oldest warning (PCONC-4) established independently. On an SD-card Pi that
runs 24/7 and unattended, a fast-failing dependency must never write one journal
line per attempt: a week-long network outage makes ShazamIO connect-errors fire
every chunk (~8,640 identical lines/day, #197), drowning the handful of lines
that matter and amplifying writes to the SD card the project's #178 / PCONC-4 /
STAB-6 work exists to protect. This collapses a repeating failure into its first
line plus a periodic summary, and flushes a final tally when it recovers.

Design notes shared with the #178 original:
  * The FIRST occurrence, and any occurrence whose message CHANGED, log at once —
    a changed condition is worth surfacing immediately, not after the window.
  * The interval seed is ``-inf`` (NOT ``0.0``) so the first call always
    reports independent of the monotonic epoch: on the Pi CLOCK_MONOTONIC is
    uptime-based and resets to ~0 on reboot, so a ``0.0`` seed would swallow the
    first line during the first ``interval_seconds`` of uptime — the early-boot
    window the signal matters most. (Since R5-14 this seeding lives in the
    delegated :class:`~src.util.logthrottle.LogThrottle` as ``_last_emit``, not
    a local field here.)
  * ``time_source`` is injectable so tests drive the interval deterministically
    instead of sleeping.

Not thread-safe: intended for a single asyncio task's failure path, exactly as
``capture.py``'s #178 throttle is.
"""

import logging
import time
from typing import Callable

from src.util.logthrottle import LogThrottle

__all__ = ["ThrottledLogger"]


class ThrottledLogger:
    """Rate-limit a repeating log message to its first line + a periodic summary.

    ``error(message)`` records one failure. The first message, and any message
    that differs from the last one logged, emit immediately; identical repeats
    are counted and summarised at most once per ``interval_seconds``.
    ``reset()`` clears the streak after a success and flushes any pending count,
    so the journal records how many failures preceded the recovery and the next
    failure reports at once.

    R5-14: the throttle DECISION (first/changed-message emit, interval re-log,
    the -inf seed, the suppressed counter) is no longer re-implemented here — it
    is delegated to the shared :class:`~src.util.logthrottle.LogThrottle` (keyed
    on the message).  This class owns only the logging + the recovery-flush
    ``reset()`` surface the recognizer depends on, so a fix to the throttle
    policy (R5-13/R5-24) lands ONCE in LogThrottle and both call sites inherit it.
    """

    def __init__(
        self,
        log: logging.Logger,
        interval_seconds: float,
        level: int = logging.WARNING,
        time_source: Callable[[], float] = time.monotonic,
    ):
        self._log = log
        self._level = level
        self._now = time_source
        # R5-13/R5-24: per_message so alternating error strings can't defeat
        # the throttle and each message reports its own suppressed tally.
        self._throttle = LogThrottle(interval=interval_seconds, per_message=True)

    def error(self, message: str) -> None:
        """Record a failure ``message``, logging or suppressing it per policy.

        ``message`` is pre-rendered by the caller and emitted via ``%s`` (never
        as a format string) so a literal ``%`` in exception text can't blow up
        logging — safer than the f-string call sites this replaces.
        """
        emit, suppressed = self._throttle.should_log(self._now(), key=message)
        if not emit:
            return
        if suppressed > 0:
            self._log.log(
                self._level,
                "%s (%d further occurrence(s) suppressed since the last report)",
                message,
                suppressed,
            )
        else:
            self._log.log(self._level, "%s", message)

    def reset(self) -> None:
        """Clear the streak after a success; flush any suppressed tally first.

        A no-op when nothing has been suppressed (the common case: on vinyl a
        genuine *miss* returns ``None`` and never reaches a failure path, so a
        healthy loop never touches this). After a real outage it emits one
        summary line recording how many failures were swallowed before recovery,
        instead of dropping that count silently.
        """
        # R5-24: flush a recovery line for EVERY message with a held-back tally,
        # not just the last one emitted — otherwise the counts for other messages
        # suppressed during the outage are silently dropped (cold-review LOW).
        for message, count in self._throttle.pending_items():
            self._log.log(
                self._level,
                "%s (recovered after %d further suppressed occurrence(s))",
                message,
                count,
            )
        # R6-03: the per-message key map is LRU-bounded, so a long outage that
        # cycles through more than the cap's worth of distinct error strings can
        # evict a key that still held suppressed occurrences. Their per-message
        # attribution is gone, but their count is not — surface the aggregate so
        # the recovery record doesn't understate how many lines were swallowed.
        evicted = self._throttle.evicted_suppressed()
        if evicted > 0:
            self._log.log(
                self._level,
                "%d further suppressed occurrence(s) of other message(s) were "
                "dropped when the throttle key cap was reached",
                evicted,
            )
        self._throttle.reset()
