"""#229 — the end-of-session finalize honours a long Retry-After for the Play
Count credit in the EVENT LOOP instead of burning all three attempts inside the
same throttle window.

Mirrors #192's `/tmp/repro_f6.py` ("credit landed: False; POSTs issued: 3;
elapsed 3.0s vs a 60s window") at the finalize seam: with #229 the credit LANDS,
and the wait honoured is the server's Retry-After (capped), not the short linear
backoff.  `asyncio.sleep` is patched so the tests assert the honoured durations
without actually waiting.
"""
from unittest.mock import AsyncMock, patch

from src.tracking.listen_tracker import (
    ListenTracker,
    _FINALIZE_WRITE_ATTEMPTS,
    _FINALIZE_RETRY_BACKOFF_SECONDS,
    _HONORED_RETRY_AFTER_CAP_SECONDS,
)
from src.metadata.discogs.transport import DiscogsRateLimited
from src.metadata.discogs.outcomes import (
    CollectionIdentity, PlayCountReadResult, PlayCountReadState,
)
from src.metadata.models import PlaySession
from tests.test_listen_tracker import make_writer_mock


def _tracker():
    return ListenTracker(writer=make_writer_mock())


async def _run(attempt):
    """Drive _finalize_write_with_retry with asyncio.sleep patched out, returning
    (result, [honoured sleep durations])."""
    tracker = _tracker()
    with patch(
        "src.tracking.listen_tracker.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        result = await tracker._finalize_write_with_retry("Play Count", attempt)
    waits = [c.args[0] for c in sleep.call_args_list]
    return result, waits


async def test_finalize_honors_retry_after_then_credit_lands():
    """The repro-f6 fix: a first attempt rate-limited with Retry-After=60, the
    second succeeds → the credit LANDS (True), and the wait honoured is the
    server's 60s (not the 1s linear backoff)."""
    attempt = AsyncMock(side_effect=[DiscogsRateLimited(60), True])

    result, waits = await _run(attempt)

    assert result is True
    assert attempt.call_count == 2
    assert waits == [60.0]           # honoured server wait, NOT _FINALIZE_RETRY_BACKOFF (1.0)


async def test_finalize_caps_the_honored_wait():
    """A hostile/huge Retry-After is clamped to the honoured cap so the serialized
    finalize path can't be wedged for minutes."""
    attempt = AsyncMock(side_effect=[DiscogsRateLimited(9999), True])

    result, waits = await _run(attempt)

    assert result is True
    assert waits == [_HONORED_RETRY_AFTER_CAP_SECONDS]   # 90.0, not 9999


async def test_finalize_waits_out_of_window_across_all_attempts():
    """Every attempt rate-limited: the credit still fails after the bound, but the
    honoured waits (60s each) prove it waited for the throttle window to clear
    rather than firing three futile in-window retries with the 1s/2s backoff."""
    attempt = AsyncMock(
        side_effect=[DiscogsRateLimited(60)] * _FINALIZE_WRITE_ATTEMPTS
    )

    result, waits = await _run(attempt)

    assert result is False
    assert attempt.call_count == _FINALIZE_WRITE_ATTEMPTS
    # One wait fewer than attempts (no sleep after the last), all honoured.
    assert waits == [60.0] * (_FINALIZE_WRITE_ATTEMPTS - 1)


async def test_finalize_non_rate_limit_failure_keeps_linear_backoff():
    """Guard: a plain falsy failure (not a 429) still uses the short LINEAR
    backoff — #229 changes only the rate-limited path."""
    attempt = AsyncMock(return_value=False)

    result, waits = await _run(attempt)

    assert result is False
    assert waits == [
        _FINALIZE_RETRY_BACKOFF_SECONDS * n
        for n in range(1, _FINALIZE_WRITE_ATTEMPTS)
    ]   # [1.0, 2.0] — unchanged


async def test_finalize_generic_exception_still_linear_backoff():
    """A non-rate-limit exception is retried with the linear backoff too (the
    DiscogsRateLimited branch must not swallow other errors)."""
    attempt = AsyncMock(side_effect=RuntimeError("boom"))

    result, waits = await _run(attempt)

    assert result is False
    assert waits == [
        _FINALIZE_RETRY_BACKOFF_SECONDS * n
        for n in range(1, _FINALIZE_WRITE_ATTEMPTS)
    ]


async def test_rate_limited_replacement_read_does_not_replenish_recovery_budget():
    """#229 can retry a throttled replacement read, never the recovery itself."""
    writer = make_writer_mock()
    writer.read_play_count.side_effect = [
        PlayCountReadResult(
            PlayCountReadState.DEFINITIVE_INSTANCE_MISSING,
            observed_instance_ids=(88,),
        ),
        DiscogsRateLimited(60),
        PlayCountReadResult(PlayCountReadState.READY, 3, 4),
    ]
    recovery = AsyncMock(return_value=CollectionIdentity(999, 88))
    tracker = ListenTracker(writer=writer, recover_collection_instance=recovery)
    session = PlaySession(
        album_release_id=999,
        album_instance_id=77,
        album_resolve_key=("sonic youth", "sister"),
    )

    with patch(
        "src.tracking.listen_tracker.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        await tracker._credit_completed_album(session)

    recovery.assert_awaited_once_with(("sonic youth", "sister"), 999, 77, (88,))
    writer.set_play_count.assert_called_once_with(999, 88, 3, 4, 5)
    assert [call.args[0] for call in sleep.call_args_list] == [60.0]
