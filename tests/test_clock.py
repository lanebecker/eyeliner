"""Unit tests for the STAB-2 wall-clock trustworthiness gate."""
from datetime import datetime, timezone

from src.util.clock import clock_is_trustworthy, _CLOCK_SANITY_FLOOR_EPOCH


def test_floor_is_2026_01_01_utc():
    """The floor is the documented 2026-01-01T00:00:00Z (the software's era)."""
    assert _CLOCK_SANITY_FLOOR_EPOCH == int(
        datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    )


def test_epoch_and_stale_clocks_are_untrustworthy():
    assert clock_is_trustworthy(0.0) is False            # 1970 — fresh SD card
    assert clock_is_trustworthy(1_600_000_000) is False  # 2020 — stale fake-hwclock


def test_boundary_at_the_floor():
    """Pins the exact comparison: below the floor is untrusted, at/above is trusted."""
    assert clock_is_trustworthy(_CLOCK_SANITY_FLOOR_EPOCH - 1) is False
    assert clock_is_trustworthy(_CLOCK_SANITY_FLOOR_EPOCH) is True
    assert clock_is_trustworthy(_CLOCK_SANITY_FLOOR_EPOCH + 1) is True


def test_present_and_future_clocks_are_trustworthy():
    assert clock_is_trustworthy(2_000_000_000) is True   # 2033
    assert clock_is_trustworthy() is True                # the real (present-day) clock
