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
  * ``_last_log`` seeds to ``-inf`` (NOT ``0.0``) so the first call always
    reports independent of the monotonic epoch: on the Pi CLOCK_MONOTONIC is
    uptime-based and resets to ~0 on reboot, so a ``0.0`` seed would swallow the
    first line during the first ``interval_seconds`` of uptime — the early-boot
    window the signal matters most.
  * ``time_source`` is injectable so tests drive the interval deterministically
    instead of sleeping.

Not thread-safe: intended for a single asyncio task's failure path, exactly as
``capture.py``'s #178 throttle is.
"""

import logging
import time
from typing import Callable, Optional

__all__ = ["ThrottledLogger"]


class ThrottledLogger:
    """Rate-limit a repeating log message to its first line + a periodic summary.

    ``error(message)`` records one failure. The first message, and any message
    that differs from the last one logged, emit immediately; identical repeats
    are counted and summarised at most once per ``interval_seconds``.
    ``reset()`` clears the streak after a success and flushes any pending count,
    so the journal records how many failures preceded the recovery and the next
    failure reports at once.
    """

    def __init__(
        self,
        log: logging.Logger,
        interval_seconds: float,
        level: int = logging.WARNING,
        time_source: Callable[[], float] = time.monotonic,
    ):
        self._log = log
        self._interval = interval_seconds
        self._level = level
        self._now = time_source
        # Count of identical messages held back since the last line was logged.
        self._suppressed = 0
        # Monotonic timestamp of the last line logged; -inf so the first always
        # reports (see module docstring on the epoch-independence rationale).
        self._last_log = float("-inf")
        # The last message actually logged, so a CHANGED message surfaces at once
        # instead of waiting out the throttle window.
        self._last_msg: Optional[str] = None

    def error(self, message: str) -> None:
        """Record a failure ``message``, logging or suppressing it per policy.

        ``message`` is pre-rendered by the caller and emitted via ``%s`` (never
        as a format string) so a literal ``%`` in exception text can't blow up
        logging — safer than the f-string call sites this replaces.
        """
        now = self._now()
        if message != self._last_msg or now - self._last_log >= self._interval:
            if self._suppressed > 0:
                self._log.log(
                    self._level,
                    "%s (%d further occurrence(s) suppressed since the last report)",
                    message,
                    self._suppressed,
                )
            else:
                self._log.log(self._level, "%s", message)
            self._suppressed = 0
            self._last_log = now
            self._last_msg = message
        else:
            self._suppressed += 1

    def reset(self) -> None:
        """Clear the streak after a success; flush any suppressed tally first.

        A no-op when nothing has been suppressed (the common case: on vinyl a
        genuine *miss* returns ``None`` and never reaches a failure path, so a
        healthy loop never touches this). After a real outage it emits one
        summary line recording how many failures were swallowed before recovery,
        instead of dropping that count silently.
        """
        if self._suppressed > 0:
            self._log.log(
                self._level,
                "%s (recovered after %d further suppressed occurrence(s))",
                self._last_msg,
                self._suppressed,
            )
        self._suppressed = 0
        self._last_msg = None
        self._last_log = float("-inf")
