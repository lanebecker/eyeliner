"""Unit tests for ThrottledLogger — the shared #178/#197 same-message/interval
log throttle (src/audio/log_throttle.py).

A fake monotonic clock drives the interval deterministically (no sleeping); a
real named logger + caplog captures what actually gets emitted.
"""

import logging

import pytest

from src.audio.log_throttle import ThrottledLogger


class FakeClock:
    """A monotonic-style clock the test advances explicitly."""

    def __init__(self):
        self.t = 1000.0  # arbitrary non-zero start (not the -inf seed)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make(interval=60.0, level=logging.WARNING):
    clock = FakeClock()
    log = logging.getLogger(f"test_throttle_{id(clock)}")
    log.propagate = True
    throttle = ThrottledLogger(log, interval, level=level, time_source=clock)
    return throttle, clock, log


def test_first_message_logs_immediately(caplog):
    throttle, _clock, log = _make()
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("boom")
    assert [r.getMessage() for r in caplog.records] == ["boom"]


def test_identical_repeats_within_interval_are_suppressed(caplog):
    throttle, clock, log = _make(interval=60.0)
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("boom")          # logs
        for _ in range(500):
            clock.advance(0.1)          # 50s total — still < 60s
            throttle.error("boom")      # all suppressed
    assert len(caplog.records) == 1     # only the first line, despite 501 calls


def test_summary_line_after_interval_reports_suppressed_count(caplog):
    throttle, clock, log = _make(interval=60.0)
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("boom")          # line 1 (immediate)
        for _ in range(5):
            clock.advance(1.0)
            throttle.error("boom")      # 5 suppressed
        clock.advance(60.0)             # cross the interval
        throttle.error("boom")          # line 2 (summary)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 2
    assert msgs[0] == "boom"
    assert "boom" in msgs[1]
    assert "5 further occurrence(s) suppressed" in msgs[1]


def test_changed_message_logs_immediately_even_within_interval(caplog):
    throttle, clock, log = _make(interval=60.0)
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("error A")       # logs
        clock.advance(1.0)
        throttle.error("error B")       # different message → logs at once
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["error A", "error B"]


def test_reset_flushes_suppressed_tally_and_rearms(caplog):
    throttle, clock, log = _make(interval=60.0)
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("boom")          # line 1
        for _ in range(3):
            clock.advance(1.0)
            throttle.error("boom")      # 3 suppressed
        throttle.reset()                # flush → line 2 (recovery summary)
        throttle.error("boom")          # streak cleared → line 3 (immediate again)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 3
    assert "recovered after 3 further suppressed occurrence(s)" in msgs[1]
    assert msgs[2] == "boom"


def test_reset_with_nothing_suppressed_is_silent(caplog):
    throttle, _clock, log = _make()
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.reset()                # never logged anything → no-op
        throttle.error("boom")          # first real line
        throttle.reset()                # only the first, zero suppressed → no flush
    assert [r.getMessage() for r in caplog.records] == ["boom"]


def test_reset_surfaces_suppressed_tally_dropped_by_the_key_cap(caplog):
    """R6-03: if the per-message key cap evicts a message that still had
    suppressed occurrences held back, ``reset()``'s recovery flush must report
    that they were dropped, not silently omit them."""
    from src.util.logthrottle import _PER_MESSAGE_MAX_KEYS
    throttle, _clock, log = _make(interval=1000.0)
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("victim")          # emits
        throttle.error("victim")          # suppressed x1
        throttle.error("victim")          # suppressed x2
        for i in range(_PER_MESSAGE_MAX_KEYS + 5):
            throttle.error(f"flood-{i}")  # floods the map → evicts "victim" (LRU)
        caplog.clear()                    # isolate the reset() output
        throttle.reset()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("dropped when the throttle key cap was reached" in m for m in msgs)
    assert any("2 further" in m for m in msgs)


def test_respects_configured_level(caplog):
    throttle, _clock, log = _make(level=logging.ERROR)
    with caplog.at_level(logging.ERROR, logger=log.name):
        throttle.error("kaboom")
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR


def test_percent_in_message_is_not_a_format_string(caplog):
    """The message is emitted via %s, so a literal % in exception text (e.g. a
    URL with %20) must not raise or be mis-substituted."""
    throttle, _clock, log = _make()
    with caplog.at_level(logging.WARNING, logger=log.name):
        throttle.error("failed on http://x/a%20b and 100% packet loss")
    assert caplog.records[0].getMessage() == (
        "failed on http://x/a%20b and 100% packet loss"
    )
