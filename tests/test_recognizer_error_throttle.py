"""#197: both recognition FAILURE legs must route through a throttle, and a
success must flush it — so a sustained network outage can't write one journal
line per ~10s chunk (~8,640/day) on the unattended SD-card Pi.

The throttling *arithmetic* is proven in tests/test_log_throttle.py. These tests
prove the WIRING: that ShazamIOBackend.recognize (the fast-fail leg) and
RecognitionLoop.run (the hung-outage leg — cancelled by recognize_timeout before
recognize's own except can log) each send failures to a throttle and reset it on
success. A spy stands in for the real ThrottledLogger so we assert routing
without re-testing intervals.
"""
import asyncio

import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.audio.recognizer import RecognitionLoop, ShazamIOBackend
from tests.factories import make_recognition_config


class _SpyThrottle:
    """Records error()/reset() calls in place of a real ThrottledLogger."""

    def __init__(self):
        self.errors = []
        self.resets = 0

    def error(self, message):
        self.errors.append(message)

    def reset(self):
        self.resets += 1


def _chunk():
    return np.zeros(16000, dtype=np.float32)


# ---------------------------------------------------------------------------
# Leg 1 — ShazamIOBackend.recognize (fast-fail: connection refused / DNS)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backend_failure_routes_through_throttle_and_returns_none():
    backend = ShazamIOBackend()
    spy = _SpyThrottle()
    backend._error_log = spy
    # Avoid the soundfile encode and the real Shazam transport.
    backend._encode_wav = lambda audio, sr: b"x"

    async def boom(_wav):
        raise RuntimeError("Cannot connect to host api.shazam.com")
    backend._call_shazam = boom

    out = await backend.recognize(_chunk(), 16000)

    assert out is None                       # a failure is still a miss
    assert spy.resets == 0
    assert len(spy.errors) == 1
    assert spy.errors[0] == (
        "ShazamIO recognition failed: Cannot connect to host api.shazam.com"
    )


@pytest.mark.asyncio
async def test_backend_parse_failure_is_throttled_not_reset():
    """#197 regression: transport OK but PARSE raises (a truthy-but-malformed
    response, e.g. a top-level JSON list) must NOT reset the throttle — otherwise
    a sustained parse-stage failure clears the streak every hop and re-floods.
    reset() belongs after a FULL success (transport AND parse), not after
    transport alone."""
    backend = ShazamIOBackend()
    spy = _SpyThrottle()
    backend._error_log = spy
    backend._encode_wav = lambda audio, sr: b"x"

    async def malformed(_wav):
        return ["not", "a", "dict"]   # (result or {}).get("track") → AttributeError
    backend._call_shazam = malformed

    out = await backend.recognize(_chunk(), 16000)

    assert out is None
    assert spy.resets == 0            # parse failed → NOT a success → no reset
    assert len(spy.errors) == 1
    assert spy.errors[0].startswith("ShazamIO recognition failed:")


@pytest.mark.asyncio
async def test_backend_success_resets_throttle():
    backend = ShazamIOBackend()
    spy = _SpyThrottle()
    backend._error_log = spy
    backend._encode_wav = lambda audio, sr: b"x"

    async def ok(_wav):
        return {"track": None}   # transport OK; parse → clean no-match (None)
    backend._call_shazam = ok

    out = await backend.recognize(_chunk(), 16000)

    assert out is None
    assert spy.errors == []
    assert spy.resets == 1       # transport succeeded → streak flushed


# ---------------------------------------------------------------------------
# Leg 2 — RecognitionLoop.run (hung outage: cancelled by recognize_timeout)
# ---------------------------------------------------------------------------

def _make_loop_with_backend(recognize):
    config = make_recognition_config(confirmation_required=2)
    state = MagicMock()
    state.current_raw = None
    state.current_track = None
    state.session_epoch = 0
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, AsyncMock())
    loop.backend = MagicMock()
    loop.backend.recognize = recognize
    loop.poll_interval = 0.01
    loop.recognize_timeout = 0.05
    return loop


async def _run_until(loop, predicate, timeout=2.0):
    task = asyncio.create_task(loop.run())
    try:
        loops = int(timeout / 0.01)
        for _ in range(loops):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("predicate never became true")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_loop_error_routes_through_throttle():
    spy = _SpyThrottle()
    loop = _make_loop_with_backend(AsyncMock(side_effect=RuntimeError("recognize blew up")))
    loop._loop_error_log = spy

    await loop.enqueue(_chunk(), 16000)
    await _run_until(loop, lambda: len(spy.errors) >= 1)

    assert spy.errors[0] == "Recognition loop error: RuntimeError('recognize blew up')"


@pytest.mark.asyncio
async def test_loop_success_resets_throttle():
    spy = _SpyThrottle()
    # recognize returns None → _handle_result registers a miss and returns cleanly,
    # so run() reaches the reset.
    loop = _make_loop_with_backend(AsyncMock(return_value=None))
    loop._loop_error_log = spy

    await loop.enqueue(_chunk(), 16000)
    await _run_until(loop, lambda: loop._loop_error_log.resets >= 1)

    assert spy.errors == []
    assert spy.resets >= 1
