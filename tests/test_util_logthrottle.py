"""LogThrottle — the shared always-on log throttle (arch-5 / #221).

capture.py's four log sites (drop-warn, error-log, and the two #164 device
dedups) are pinned end-to-end by tests/test_capture.py; these pin the class
directly at its new home, covering the three contracts it must express: a keyless
interval summarizer (PCONC-4), a change-key interval summarizer (#178), and a
pure dedup that never re-logs on time alone (#164).
"""
from src.util.logthrottle import LogThrottle


def test_first_observation_always_emits_even_at_low_monotonic():
    # -inf seed: the first signal reports regardless of the monotonic epoch
    # (Pi CLOCK_MONOTONIC restarts near 0 on reboot).
    t = LogThrottle(interval=5.0)
    emit, suppressed = t.should_log(0.001)
    assert emit is True and suppressed == 0


def test_keyless_interval_summarizer_counts_suppressed_between_emits():
    # PCONC-4 drop-warn shape: keyless, interval-gated, aggregate count.
    t = LogThrottle(interval=5.0)
    assert t.should_log(1000.0) == (True, 0)      # first emits
    for _ in range(9):                            # within the window → suppressed
        assert t.should_log(1000.0)[0] is False
    emit, suppressed = t.should_log(1006.0)       # past the window → emits summary
    assert emit is True and suppressed == 9       # caller reports suppressed(+1 for current)


def test_change_key_emits_immediately_on_a_new_condition():
    # #178: a changed error message surfaces at once, not behind the interval.
    t = LogThrottle(interval=30.0)
    assert t.should_log(1000.0, key="err A")[0] is True
    assert t.should_log(1000.0, key="err A")[0] is False   # identical → suppressed
    assert t.should_log(1000.0, key="err B")[0] is True    # changed → immediate
    # …and the suppressed count is per-emit (the one A held back is reported on B)
    assert t.should_log(1000.0, key="err B") == (False, 0)


def test_interval_none_is_pure_dedup_never_re_logs_on_time():
    # #164: with no interval, an unchanged key NEVER re-logs no matter how much
    # monotonic time passes — only a key change re-emits.
    t = LogThrottle()  # interval=None
    assert t.should_log(1000.0, key=3)[0] is True
    assert t.should_log(9_999_999.0, key=3)[0] is False    # eons later, same key → quiet
    assert t.should_log(9_999_999.0, key=7)[0] is True     # key changed → emit
    assert t.should_log(9_999_999.0, key=3)[0] is True     # back to 3 (now != last 7) → emit
