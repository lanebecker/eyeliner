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


# ---------------------------------------------------------------------------
# R5-13 / R5-24 — per_message mode
# ---------------------------------------------------------------------------

def test_per_message_new_key_still_emits_immediately():
    """#178 preserved: a genuinely new key surfaces at once, even in per_message."""
    t = LogThrottle(interval=30.0, per_message=True)
    assert t.should_log(1000.0, key="err A")[0] is True
    assert t.should_log(1000.0, key="err B")[0] is True   # a different new key


def test_per_message_alternating_keys_do_not_defeat_the_throttle():
    """R5-13: two alternating conditions collapse to one emit each per interval,
    not one per observation (the single-key mode floods this — see
    test_interval_none_is_pure_dedup for that documented behavior)."""
    t = LogThrottle(interval=30.0, per_message=True)
    emits = 0
    for i in range(100):
        key = "A" if i % 2 == 0 else "B"
        if t.should_log(float(i), key=key)[0]:
            emits += 1
    assert emits <= 8            # 2 immediate + ~1 per interval per key


def test_per_message_each_key_reports_its_own_tally():
    """R5-24: an emit reports the suppressed count for THAT key, never the
    previous key's — the mis-attribution the single-key mode produces."""
    t = LogThrottle(interval=30.0, per_message=True)
    assert t.should_log(0.0, key="A") == (True, 0)     # A emits
    for i in range(1, 4):
        assert t.should_log(float(i), key="A")[0] is False   # A suppressed x3
    # B is a NEW key: it must emit with ITS OWN tally (0), not A's 3.
    assert t.should_log(4.0, key="B") == (True, 0)
    # A re-emits after the interval with A's own accumulated tally (3).
    assert t.should_log(31.0, key="A") == (True, 3)


def test_per_message_map_is_bounded_by_an_lru_cap():
    """The per-key map is bounded by _PER_MESSAGE_MAX_KEYS via LRU eviction, even
    for keys that were SUPPRESSED at least once and then never recur — the exact
    class the earlier idle-only prune left immortal (cold-review MEDIUM)."""
    from src.util.logthrottle import _PER_MESSAGE_MAX_KEYS
    t = LogThrottle(interval=10.0, per_message=True)
    for i in range(_PER_MESSAGE_MAX_KEYS * 4):
        # Each key emits then is suppressed once (sup==1) and never recurs.
        t.should_log(float(i) * 100.0, key=f"err-{i}")
        t.should_log(float(i) * 100.0 + 1.0, key=f"err-{i}")
    assert len(t._per) <= _PER_MESSAGE_MAX_KEYS


def test_per_message_reset_flushes_every_pending_key():
    """reset() must surface EACH suppressed message's count, not only the last
    (R5-24 — the discarded-total loss the cold review flagged)."""
    t = LogThrottle(interval=30.0, per_message=True)
    t.should_log(0.0, key="A"); t.should_log(1.0, key="A")   # A: 1 suppressed
    t.should_log(2.0, key="B"); t.should_log(3.0, key="B"); t.should_log(4.0, key="B")  # B: 2
    items = dict(t.pending_items())
    assert items == {"A": 1, "B": 2}


def test_per_message_reset_flushes_total_pending_and_rearms():
    t = LogThrottle(interval=30.0, per_message=True)
    t.should_log(0.0, key="A")
    t.should_log(1.0, key="A")    # A suppressed x1
    t.should_log(2.0, key="A")    # x2
    assert t.reset() == 2         # total pending flushed
    assert t.should_log(3.0, key="A")[0] is True   # re-armed: new again
