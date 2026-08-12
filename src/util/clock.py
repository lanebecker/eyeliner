"""Wall-clock trustworthiness gate (STAB-2).

The Raspberry Pi 4 has no battery-backed real-time clock; at boot the clock is
whatever ``fake-hwclock`` last saved — or the Unix epoch on a fresh SD card —
until an NTP sync settles. Writing a **Last Played** date or a scrobble
timestamp from such a clock stamps a wrong value over real, unrecoverable
collection / listening-history data. Both date-dependent writes are gated on
:func:`clock_is_trustworthy`; a pre-NTP boot skips them (with a WARNING) rather
than corrupting the record.

This is the CODE-level defense-in-depth complement to the DEPLOYMENT-level fix
(CRIT-4 / #83): the systemd unit is ordered after ``time-sync.target`` with
``systemd-time-wait-sync`` enabled, so a correctly-deployed appliance never
starts before the clock syncs. This gate protects the manual-run and
mis-deployed cases, and catches the catastrophic epoch/stale reading directly.

Scope: this is a BOUNDED SANITY RANGE, not a sync check. It catches the two
catastrophic clock readings a date write must never trust:
  * BELOW the floor — an unset / epoch / grossly-stale clock (the finding's
    ``{'value': '1970-01-01'}``); and
  * ABOVE a far-future CEILING (R7-19) — a glitched RTC or garbage reading years
    in the future, which would otherwise stamp an equally unrecoverable
    future-dated Last Played / scrobble.

It does NOT catch a clock that is merely stale-but-post-floor, nor one set
MODESTLY into the future (below the ceiling): a ``fake-hwclock`` date anywhere
between the floor and the true present passes (a Pi last used months ago and
booted offline can still write a wrong-but-post-floor date), and a clock nudged a
few days/months ahead passes too. Closing those residual gaps needs the actual
NTP-sync signal, which the deployment unit owns (CRIT-4/#83 orders after
``time-sync.target`` with ``systemd-time-wait-sync``, so a correctly-deployed
appliance never runs before sync); the on-device ``/run/systemd/timesync/
synchronized`` check is a possible future tightening for the manual-run case. The
floor/ceiling are deliberately far apart (a full century) so they never clip a
genuine clock — they bound only the provably-garbage extremes, matching the
floor's original "catch the catastrophic, not the subtle" posture.
Play Count is intentionally NOT gated: it writes a count, not a date, so a wrong
clock cannot corrupt it.
"""
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# A live install's wall clock can never legitimately read earlier than this.
# Chosen as the software's own era: this v1.x line was first released in 2026, so
# a correctly NTP-synced clock is always well past this floor (it only moves
# forward), meaning the gate can never reject a genuine clock — yet the floor sits
# far above the Unix epoch and the manufacture-era timestamps a pre-NTP Pi
# restores via fake-hwclock. A reading below it is provably an unset clock.
# 2026-01-01T00:00:00Z.
_CLOCK_SANITY_FLOOR_EPOCH = 1_767_225_600

# R7-19: a symmetric FAR-FUTURE ceiling — floor + 100×365 days (≈ 2125-12-08Z; the
# ~24 leap days in the span are immaterial at this scale). The
# appliance will never legitimately run a century out, so this never clips a real
# NTP-synced clock, yet it rejects a glitched RTC / garbage reading years ahead
# that would otherwise stamp a future, equally-unrecoverable date over collection
# / listening-history data. A MODEST future mis-set (below the ceiling) still
# passes — the deployment NTP ordering owns that residual, exactly as it owns the
# stale-but-post-floor one (see the module docstring).
_CLOCK_SANITY_CEILING_EPOCH = _CLOCK_SANITY_FLOOR_EPOCH + 100 * 365 * 24 * 60 * 60


def clock_is_trustworthy(now: Optional[float] = None) -> bool:
    """Return True iff the wall clock is recent enough to trust for date writes.

    ``now`` is a Unix timestamp (seconds), injectable for testing and for
    validating a *specific* already-captured timestamp (e.g. the scrobble time);
    it defaults to :func:`time.time`. Returns False for an unset / epoch /
    grossly-stale clock (below the floor) OR a garbage far-future reading (at or
    above the century ceiling, R7-19) — the caller should then skip the date write
    with a WARNING rather than corrupt the record.
    """
    t = time.time() if now is None else now
    return _CLOCK_SANITY_FLOOR_EPOCH <= t < _CLOCK_SANITY_CEILING_EPOCH
