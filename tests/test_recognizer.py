"""Unit tests for RecognitionLoop confirmation logic.

Tests the 2-of-N consecutive match requirement that prevents flickering
when Shazam returns a noisy or wrong result for a single chunk.

After A-9 the loop no longer commits anything itself: on confirmation it awaits
its injected ``on_confirmed`` callback (wired to TrackCommitService.commit in
production) with the confirmed RawRecognitionResult.  These tests therefore
assert on that emission; the resolve→state→track→scrobble side effects are
covered by tests/test_track_commit_service.py.

No audio hardware, network, or actual Shazam API calls needed.
The backend is replaced with a MagicMock; we drive _handle_result() directly.
"""
from unittest.mock import MagicMock, AsyncMock, patch

import asyncio

import numpy as np
import pytest

from src.audio.recognizer import RawRecognitionResult, RecognitionLoop, ShazamIOBackend
from tests.factories import make_recognition_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_raw(title="So What", artist="Miles Davis", album="Kind of Blue"):
    return RawRecognitionResult(title=title, artist=artist, album=album)


def make_loop(confirmation_required=2):
    """Build a RecognitionLoop with a mock state and an AsyncMock on_confirmed.

    Returns (loop, state, on_confirmed).  on_confirmed stands in for
    TrackCommitService.commit; assert on it to check whether a confirmed track
    was emitted.
    """
    config = make_recognition_config(confirmation_required=confirmation_required)
    state = MagicMock()
    state.current_raw = None
    state.current_track = None

    on_confirmed = AsyncMock()

    # Bypass _init_backend so we don't need ShazamIO installed during tests
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, on_confirmed)

    return loop, state, on_confirmed


def test_init_backend_rejects_an_unimplemented_backend():
    """CRIT-2 backstop: even if config validation is bypassed (direct
    construction), _init_backend rejects a backend not in the shared
    IMPLEMENTED_BACKENDS set — it never silently constructs a missing backend,
    and it validates against the SAME set config does (no drift)."""
    from src.config import IMPLEMENTED_BACKENDS
    assert "acrcloud" not in IMPLEMENTED_BACKENDS   # sanity: advertised, not built
    config = make_recognition_config(backend="acrcloud")
    with pytest.raises(ValueError, match="Unknown recognition backend"):
        RecognitionLoop(config, MagicMock(), MagicMock())


# ---------------------------------------------------------------------------
# Single result never commits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_result_does_not_commit():
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw())

    on_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_result_increments_pending_count_to_one():
    loop, state, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw())

    assert loop._pending_count == 1
    assert loop._pending_result is not None


# ---------------------------------------------------------------------------
# Two matching results commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_titleless_shazam_responses_never_confirm():
    """REC-3 end-to-end: repeated Shazam responses that carry a track object but
    no usable title must never confirm a track. _parse_shazam rejects them as a
    no-match, so the loop counts misses and never emits a titleless result to the
    commit service (which, pre-fix, would resolve it and — before SEC-1 — write to
    an arbitrary owned record)."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None
    state.status = None  # not LISTENING; we only care that nothing confirms

    # track exists (not the falsy-track None path) but has no title.
    titleless = {"track": {"subtitle": "Miles Davis", "sections": []}}
    for _ in range(3):
        parsed = ShazamIOBackend._parse_shazam(titleless)
        await loop._handle_result(parsed)

    on_confirmed.assert_not_awaited()
    assert loop._pending_result is None
    assert loop._pending_count == 0


# ---------------------------------------------------------------------------
# REC-2 — a Shazam JSON null in a string field must not crash the parser or the
#         dedup comparison (which stalled the loop with no miss counted)
# ---------------------------------------------------------------------------

def test_rec2_null_subtitle_is_coerced_to_empty_artist():
    """A JSON-null subtitle must coerce to "" (was None) — else _same_track's
    a.artist.strip() raises AttributeError inside _handle_result (REC-2)."""
    r = ShazamIOBackend._parse_shazam({"track": {"title": "So What", "subtitle": None}})
    assert r is not None
    assert r.artist == ""


def test_rec2_null_metadata_title_does_not_crash_parse():
    """A JSON-null metadata `title` must not crash _parse_shazam (was
    None.lower()), which would discard an otherwise-valid response as a miss."""
    r = ShazamIOBackend._parse_shazam({"track": {
        "title": "So What", "subtitle": "Miles Davis",
        "sections": [{"metadata": [{"title": None, "text": "Kind of Blue"}]}],
    }})
    assert r is not None and r.title == "So What"


def test_rec2_null_metadata_text_is_coerced_to_empty_album():
    """A JSON-null album `text` must coerce to "" (was None)."""
    r = ShazamIOBackend._parse_shazam({"track": {
        "title": "So What", "subtitle": "Miles Davis",
        "sections": [{"metadata": [{"title": "Album", "text": None}]}],
    }})
    assert r.album == ""


def test_rec2_same_track_is_null_safe():
    """_same_track must not raise on a None title OR artist on EITHER side (REC-2
    defense-in-depth); a None compares as empty, never crashes the dedup. A None
    title short-circuits the `and`; a None artist is reached only when titles
    MATCH — so cover both fields on both sides with a matching title."""
    ok = make_raw(title="So What", artist="Miles Davis")

    def with_none(field):
        r = make_raw(title="So What", artist="Miles Davis")
        setattr(r, field, None)
        return r

    # None of these may raise (was a.title/a.artist/b.title/b.artist .strip() on None).
    assert RecognitionLoop._same_track(with_none("title"), ok) is False    # a.title
    assert RecognitionLoop._same_track(with_none("artist"), ok) is False   # a.artist (titles match)
    assert RecognitionLoop._same_track(ok, with_none("title")) is False    # b.title
    assert RecognitionLoop._same_track(ok, with_none("artist")) is False   # b.artist (titles match)


@pytest.mark.asyncio
async def test_rec2_null_subtitle_does_not_stall_handle_result():
    """End-to-end: a null-subtitle response reaching _handle_result while a
    same-titled track is current must NOT raise — the pre-fix AttributeError
    escaped to run()'s handler, counting no miss and leaving the display stuck on
    IDENTIFYING forever while the journal filled once per chunk (REC-2)."""
    loop, state, on_confirmed = make_loop()
    state.current_raw = make_raw(title="So What", artist="Miles Davis")   # a current track
    result = ShazamIOBackend._parse_shazam({"track": {"title": "So What", "subtitle": None}})
    await loop._handle_result(result, epoch=0)   # must not raise AttributeError


@pytest.mark.asyncio
async def test_two_matching_results_emit_confirmed_track():
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    # epoch defaults to 0 when _handle_result is driven directly (PCONC-1).
    on_confirmed.assert_awaited_once_with(raw, 0)


@pytest.mark.asyncio
async def test_commit_clears_pending_state():
    """After a commit, pending_count and pending_result should reset."""
    loop, state, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    assert loop._pending_count == 0
    assert loop._pending_result is None


# ---------------------------------------------------------------------------
# Mismatch resets counter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_title_resets_pending_count():
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What", "Miles Davis"))
    await loop._handle_result(make_raw("Blue in Green", "Miles Davis"))  # Different

    assert loop._pending_count == 1
    assert loop._pending_result.title == "Blue in Green"
    on_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_artist_resets_pending_count():
    """Title match alone is not enough — artist must also match."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What", "Miles Davis"))
    await loop._handle_result(make_raw("So What", "Cover Band"))  # Same title, different artist

    assert loop._pending_count == 1
    on_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatch_then_two_matching_commits():
    """A mismatch resets, but the next run of matching results still commits."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What"))     # count = 1
    await loop._handle_result(make_raw("Blue in Green"))  # reset, count = 1
    await loop._handle_result(make_raw("Blue in Green"))  # count = 2, commit

    on_confirmed.assert_awaited_once()
    assert on_confirmed.await_args[0][0].title == "Blue in Green"


# ---------------------------------------------------------------------------
# None result (unrecognized audio)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_none_result_does_not_commit():
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(None)

    on_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_result_preserves_the_pending_candidate():
    """REC-1: a None miss carries NO recognition information, so it must NOT
    discard the pending candidate. On vinyl a hit/miss/hit pattern is the normal
    failure mode (surface noise, a worn side); zeroing the pending on every miss
    meant a repeatedly-identified track could never accumulate to confirmation."""
    loop, state, _ = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw())  # pending = A, count = 1
    await loop._handle_result(None)        # miss — pending must be KEPT

    assert loop._pending_count == 1
    assert loop._pending_result is not None


@pytest.mark.asyncio
async def test_none_between_matching_results_still_confirms():
    """REC-1: hit / miss / hit — the normal vinyl failure mode — must still reach
    confirmation. The intervening None no longer resets the streak, so the second
    matching result takes the pending count to confirmation_required and commits."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What"))  # count = 1
    await loop._handle_result(None)                  # miss — pending kept
    await loop._handle_result(make_raw("So What"))  # count = 2 → confirm

    on_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_different_track_still_resets_the_pending_across_a_miss():
    """The miss no longer resets the pending, but a DIFFERENT non-None result
    still does — genuine churn (two records bleeding together) must not confirm."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    await loop._handle_result(make_raw("So What"))     # pending = A, count = 1
    await loop._handle_result(None)                     # miss — pending kept (A)
    await loop._handle_result(make_raw("Blue in Green"))  # different → pending = B, count = 1
    await loop._handle_result(make_raw("So What"))     # A again → pending = A, count = 1

    assert loop._pending_count == 1
    on_confirmed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Same track as currently playing — skip silently
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_as_current_raw_does_not_re_commit():
    """If the recognized track matches what's already playing, do nothing."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = make_raw("So What", "Miles Davis")

    # Both results match the current track
    await loop._handle_result(make_raw("So What", "Miles Davis"))
    await loop._handle_result(make_raw("So What", "Miles Davis"))

    on_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_track_after_current_triggers_confirmation():
    """A different track from the current one should start the confirmation cycle."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = make_raw("So What", "Miles Davis")  # Already playing

    new_raw = make_raw("All Blues", "Miles Davis")
    await loop._handle_result(new_raw)
    await loop._handle_result(new_raw)

    on_confirmed.assert_awaited_once_with(new_raw, 0)


# ---------------------------------------------------------------------------
# Higher confirmation_required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_required_does_not_commit_on_two():
    loop, state, on_confirmed = make_loop(confirmation_required=3)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)  # count = 2

    on_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_three_required_commits_on_three():
    loop, state, on_confirmed = make_loop(confirmation_required=3)
    state.current_raw = None

    raw = make_raw()
    for _ in range(3):
        await loop._handle_result(raw)

    on_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_required_commits_immediately():
    loop, state, on_confirmed = make_loop(confirmation_required=1)
    state.current_raw = None

    await loop._handle_result(make_raw())

    on_confirmed.assert_awaited_once()


# ---------------------------------------------------------------------------
# No double-commit after a successful commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_double_commit_after_success():
    """After committing, subsequent identical results should not re-commit
    (because current_raw now matches)."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw)
    await loop._handle_result(raw)  # Commits; emits the confirmed raw

    # Simulate state.current_raw being updated (as the commit service would do).
    state.current_raw = raw

    # More results for the same track — should be skipped
    await loop._handle_result(raw)
    await loop._handle_result(raw)

    assert on_confirmed.await_count == 1  # Still only emitted once


# ---------------------------------------------------------------------------
# enqueue drop-oldest policy (v1.3.5)
#
# When the recognition backend lags and the queue fills, the OLDEST chunk is
# evicted and the incoming one admitted — the freshest audio matters most
# for detecting a track change. (Previously the incoming chunk was discarded
# and stale audio kept being processed first.)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enqueue_drops_oldest_when_full():
    loop_obj, _, _ = make_loop()
    maxsize = loop_obj._audio_queue.maxsize

    for i in range(maxsize):
        await loop_obj.enqueue(np.full(4, float(i), dtype=np.float32), 44100)
    assert loop_obj._audio_queue.full()

    # One more: the oldest (marker 0.0) must yield to the newest.
    await loop_obj.enqueue(np.full(4, 99.0, dtype=np.float32), 44100)

    assert loop_obj._audio_queue.qsize() == maxsize
    first_audio, *_ = loop_obj._audio_queue.get_nowait()  # (audio, sr, epoch)
    assert first_audio[0] == 1.0   # marker 0.0 was evicted
    remaining = []
    while not loop_obj._audio_queue.empty():
        audio, *_ = loop_obj._audio_queue.get_nowait()
        remaining.append(audio[0])
    assert remaining[-1] == 99.0   # the newest chunk was admitted


@pytest.mark.asyncio
async def test_enqueue_below_capacity_keeps_everything():
    loop_obj, _, _ = make_loop()
    await loop_obj.enqueue(np.full(4, 1.0, dtype=np.float32), 44100)
    await loop_obj.enqueue(np.full(4, 2.0, dtype=np.float32), 44100)
    assert loop_obj._audio_queue.qsize() == 2


# ---------------------------------------------------------------------------
# run() — the actual polling loop (T-2: previously never driven; tests only
# ever called _handle_result directly)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_pulls_a_chunk_recognizes_it_and_emits(monkeypatch):
    """A queued chunk is pulled, handed to the backend, and the recognized
    result flows through _handle_result to on_confirmed — then a cancel unwinds
    the infinite loop cleanly."""
    loop, state, on_confirmed = make_loop(confirmation_required=1)

    raw = make_raw()
    loop.backend.recognize = AsyncMock(return_value=raw)

    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)

    task = asyncio.create_task(loop.run())
    # Yield enough times for the loop to drain the queue, recognize, and commit.
    for _ in range(10):
        await asyncio.sleep(0)
        if on_confirmed.await_count:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    loop.backend.recognize.assert_awaited_once()
    # run() threads the epoch bound at enqueue through to on_confirmed (PCONC-1).
    on_confirmed.assert_awaited_once_with(raw, state.session_epoch)  # confirmation_required=1 → immediate


@pytest.mark.asyncio
async def test_run_swallows_a_backend_error_and_keeps_looping(monkeypatch):
    """A backend exception is caught (logged + short sleep), not fatal: the loop
    survives to process the next chunk."""
    loop, state, on_confirmed = make_loop(confirmation_required=1)

    # First recognize raises, second succeeds — the loop must reach the second.
    raw = make_raw()
    loop.backend.recognize = AsyncMock(side_effect=[RuntimeError("shazam blip"), raw])

    # Collapse the error-path back-off (asyncio.sleep(2)) to a real zero-yield so
    # the test doesn't wait wall-clock seconds, while still ceding control to the
    # loop (a plain mock wouldn't yield, and the run() task would never advance).
    real_sleep = asyncio.sleep

    async def fast_sleep(_secs):
        await real_sleep(0)

    monkeypatch.setattr("src.audio.recognizer.asyncio.sleep", fast_sleep)

    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)
    await loop.enqueue(np.ones(4, dtype=np.float32), 44100)

    task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if on_confirmed.await_count:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.backend.recognize.await_count == 2   # survived the first error
    on_confirmed.assert_awaited_once_with(raw, state.session_epoch)


@pytest.mark.asyncio
async def test_run_timeout_from_recognize_is_logged_and_backed_off_not_swallowed(monkeypatch):
    """CONC-4: on Python 3.11 asyncio.TimeoutError IS builtins.TimeoutError. A
    socket timeout (or aiohttp ServerTimeoutError) escaping recognize()/commit()
    must be logged + backed off like any other error — NOT silently classified as
    'no audio queued' by the wait_for handler, which would hot-spin on a failing
    network with nothing in the journal."""
    loop, state, on_confirmed = make_loop(confirmation_required=1)

    raw = make_raw()
    # First recognize raises a bare TimeoutError (== asyncio.TimeoutError on 3.11);
    # the second succeeds, proving the loop logged, backed off, and recovered.
    loop.backend.recognize = AsyncMock(
        side_effect=[TimeoutError("socket read timed out"), raw]
    )

    errors = []
    monkeypatch.setattr(
        "src.audio.recognizer.log.error", lambda msg, *a, **k: errors.append(msg)
    )
    slept = []
    real_sleep = asyncio.sleep

    async def fast_sleep(secs):
        slept.append(secs)
        await real_sleep(0)

    monkeypatch.setattr("src.audio.recognizer.asyncio.sleep", fast_sleep)

    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)
    await loop.enqueue(np.ones(4, dtype=np.float32), 44100)

    task = asyncio.create_task(loop.run())
    for _ in range(30):
        await asyncio.sleep(0)
        if on_confirmed.await_count:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The timeout was LOGGED, not swallowed as an idle poll…
    assert errors, "TimeoutError from recognize was swallowed silently (no log)"
    # …the loop BACKED OFF instead of hot-spinning…
    assert slept, "no back-off after the timeout — the loop would hot-spin"
    # …and it recovered to process the next chunk.
    assert loop.backend.recognize.await_count == 2
    on_confirmed.assert_awaited_once_with(raw, state.session_epoch)


@pytest.mark.asyncio
async def test_run_idle_timeout_polls_again_without_error(monkeypatch):
    """CONC-4: a genuine idle poll (no audio for poll_interval) must simply loop
    again — no error logged, recognize() never reached — and must NOT fall through
    to the recognize path with unbound audio (which the two-try split guards via
    `continue`)."""
    config = make_recognition_config(confirmation_required=1, poll_interval_seconds=0.01)
    state = MagicMock()
    state.current_raw = None
    state.current_track = None
    on_confirmed = AsyncMock()
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        loop = RecognitionLoop(config, state, on_confirmed)
    loop.backend.recognize = AsyncMock(return_value=make_raw())

    errors = []
    monkeypatch.setattr(
        "src.audio.recognizer.log.error", lambda msg, *a, **k: errors.append(msg)
    )

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)   # let a few idle polls (0.01s each) elapse on the EMPTY queue
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert errors == [], f"idle poll logged an error (fell through to recognize?): {errors}"
    loop.backend.recognize.assert_not_awaited()   # never reached recognize on an idle poll


@pytest.mark.asyncio
async def test_run_hung_recognize_is_bounded_and_the_loop_recovers(monkeypatch):
    """PCONC-2: a recognize() that hangs (flaky wifi + shazamio's 20×60s retry
    default) must be abandoned by a wait_for timeout — not left to occupy the loop
    for minutes and saturate the audio queue (the lag PCONC-1 needs). The timeout
    surfaces as a TimeoutError to CONC-4's handler (logged + backed off) and the
    loop recovers on the next chunk."""
    loop, state, on_confirmed = make_loop(confirmation_required=1)
    loop.recognize_timeout = 0.05      # tiny recognize bound so the hung call is abandoned fast

    raw = make_raw()
    calls = {"n": 0}
    hang = asyncio.Event()               # never set → the first recognize hangs on it

    async def recognize(audio, sample_rate):
        calls["n"] += 1
        if calls["n"] == 1:
            await hang.wait()            # hangs forever (independent of asyncio.sleep)
        return raw                        # a later call succeeds

    loop.backend.recognize = recognize

    errors = []
    monkeypatch.setattr(
        "src.audio.recognizer.log.error", lambda msg, *a, **k: errors.append(msg)
    )
    real_sleep = asyncio.sleep

    async def fast_sleep(secs):          # collapse only the recognizer's error-path backoff
        await real_sleep(0)

    monkeypatch.setattr("src.audio.recognizer.asyncio.sleep", fast_sleep)

    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)
    await loop.enqueue(np.ones(4, dtype=np.float32), 44100)

    task = asyncio.create_task(loop.run())
    for _ in range(100):
        await real_sleep(0.01)           # REAL time so the 0.05s wait_for can fire
        if on_confirmed.await_count:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert on_confirmed.await_count == 1   # abandoned the hung call, recovered on the next
    assert errors                           # the timeout was logged (CONC-4), not swallowed
    # …with a non-empty message: a bare TimeoutError stringifies to "" so it is
    # logged via repr(), naming the failure this bound exists to surface.
    assert any("TimeoutError" in m for m in errors), errors


@pytest.mark.asyncio
async def test_shazam_client_pins_the_retry_policy(monkeypatch):
    """PCONC-2: the Shazam client is built with an EXPLICIT, SHORT ExponentialRetry
    — not shazamio's minutes-long attempts=20 / max_timeout=60 default that lets one
    degraded call occupy the recognition loop."""
    shazamio = pytest.importorskip("shazamio")
    aiohttp_retry = pytest.importorskip("aiohttp_retry")
    from src.audio.recognizer import (
        ShazamIOBackend, _SHAZAM_RETRY_ATTEMPTS, _SHAZAM_RETRY_MAX_TIMEOUT_SECONDS,
    )

    captured = {}

    class FakeRetry:
        def __init__(self, **kw):
            captured["retry"] = kw

    class FakeHTTPClient:
        def __init__(self, retry_options=None):
            captured["retry_options"] = retry_options

    class FakeShazam:
        def __init__(self, http_client=None):
            captured["http_client"] = http_client

        async def recognize(self, wav):
            return {"track": None}

    monkeypatch.setattr(shazamio, "Shazam", FakeShazam)
    monkeypatch.setattr(shazamio, "HTTPClient", FakeHTTPClient)
    monkeypatch.setattr(aiohttp_retry, "ExponentialRetry", FakeRetry)

    backend = ShazamIOBackend()
    await backend._call_shazam(b"fake-wav-bytes")

    # The retry policy was pinned to our small values, wired through HTTPClient
    # into the Shazam client — not left as the default (http_client=None → 20×60).
    assert captured["retry"]["attempts"] == _SHAZAM_RETRY_ATTEMPTS
    assert captured["retry"]["max_timeout"] == _SHAZAM_RETRY_MAX_TIMEOUT_SECONDS
    assert captured["retry"]["statuses"] == {500, 502, 503, 504, 429}  # retryable codes preserved
    assert _SHAZAM_RETRY_ATTEMPTS < 20                 # tighter than shazamio's default
    assert captured["http_client"] is not None         # an explicit client was passed
    assert isinstance(captured["retry_options"], FakeRetry)


# ---------------------------------------------------------------------------
# PCONC-1 (#80) — the session epoch is bound to the AUDIO at enqueue (capture)
# time and threaded through recognition → confirmation → commit, so a chunk that
# lagged in the queue past a needle-lift is committed against the session it came
# from, not whichever one is live when the delayed commit runs.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enqueue_binds_the_current_epoch_to_the_chunk():
    """The chunk carries the session epoch that was live when it was enqueued."""
    loop, state, _ = make_loop()
    state.session_epoch = 7
    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)

    _audio, _sr, epoch = loop._audio_queue.get_nowait()
    assert epoch == 7


@pytest.mark.asyncio
async def test_queued_epoch_is_frozen_at_enqueue_not_read_later():
    """A chunk that sat in the queue while the needle lifted must still carry its
    ORIGINAL (pre-lift) epoch — the epoch is sampled at enqueue, never re-read at
    dequeue (dequeue-time would already be the post-lift epoch and defeat the
    guard)."""
    loop, state, _ = make_loop()
    state.session_epoch = 3
    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)

    state.session_epoch = 4          # needle lifts AFTER the chunk is queued

    _audio, _sr, epoch = loop._audio_queue.get_nowait()
    assert epoch == 3                # frozen at enqueue, not re-read as 4


@pytest.mark.asyncio
async def test_handle_result_forwards_the_audio_epoch_to_on_confirmed():
    """On confirmation the loop hands the AUDIO's epoch to on_confirmed so the
    commit is validated against the session the audio came from (PCONC-1)."""
    loop, state, on_confirmed = make_loop(confirmation_required=2)
    state.current_raw = None

    raw = make_raw()
    await loop._handle_result(raw, epoch=5)
    await loop._handle_result(raw, epoch=5)

    on_confirmed.assert_awaited_once_with(raw, 5)


@pytest.mark.asyncio
async def test_run_threads_the_enqueued_epoch_through_to_on_confirmed():
    """End-to-end: a chunk enqueued under epoch 9 confirms and commits with
    epoch 9 — even after the session moves on to 10 while the stale chunk waits
    in the queue.  This is the PCONC-1 queue-lag race the commit-time epoch
    sample could not see."""
    loop, state, on_confirmed = make_loop(confirmation_required=1)
    state.session_epoch = 9

    raw = make_raw()
    loop.backend.recognize = AsyncMock(return_value=raw)

    await loop.enqueue(np.zeros(4, dtype=np.float32), 44100)
    state.session_epoch = 10   # needle lifts / new session AFTER capture

    task = asyncio.create_task(loop.run())
    for _ in range(10):
        await asyncio.sleep(0)
        if on_confirmed.await_count:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    on_confirmed.assert_awaited_once_with(raw, 9)   # the PRE-lift epoch, not 10


# ---------------------------------------------------------------------------
# PCONC-3 — per-session health counters must reset on a session-epoch change.
# The pending candidate is already voided across a boundary (REC-1 review); the
# miss/churn counters were not, so a fresh side inherited the previous side's
# streak.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_miss_count_resets_on_session_epoch_change():
    from src.state.player_state import PlayerStatus
    loop, state, _ = make_loop()
    state.status = PlayerStatus.LISTENING
    for _ in range(3):
        await loop._handle_result(None, epoch=0)     # 3 misses in session 0
    assert loop._miss_count == 3
    await loop._handle_result(None, epoch=1)          # first miss of the NEW session
    assert loop._miss_count == 1                       # reset on the boundary, not 4


@pytest.mark.asyncio
async def test_churn_count_resets_on_session_epoch_change():
    from src.state.player_state import PlayerStatus
    loop, state, _ = make_loop()
    state.status = PlayerStatus.LISTENING
    for i in range(3):
        await loop._handle_result(make_raw(title=f"T{i}", artist=f"A{i}"), epoch=0)
    assert loop._churn_count == 3
    await loop._handle_result(make_raw(title="Z", artist="Z"), epoch=1)
    assert loop._churn_count == 1                       # reset on the boundary, not 4


@pytest.mark.asyncio
async def test_epoch_change_does_not_reset_within_a_session():
    """Guard: REC-1's accumulate-across-misses is WITHIN a session (constant
    epoch) and must be unaffected by the PCONC-3 boundary reset."""
    from src.state.player_state import PlayerStatus
    loop, state, _ = make_loop()
    state.status = PlayerStatus.LISTENING
    for _ in range(3):
        await loop._handle_result(None, epoch=0)
    assert loop._miss_count == 3                        # same epoch → keeps accumulating


# ---------------------------------------------------------------------------
# REC-4 — _same_track must be whitespace-insensitive as its docstring claims.
# ---------------------------------------------------------------------------

def test_same_track_collapses_internal_whitespace():
    a = make_raw(title="My  Song", artist="The  Band", album="")
    b = make_raw(title="My Song", artist="The Band", album="")
    assert RecognitionLoop._same_track(a, b) is True


def test_same_track_still_distinguishes_different_titles():
    a = make_raw(title="Song One", artist="X")
    b = make_raw(title="Song Two", artist="X")
    assert RecognitionLoop._same_track(a, b) is False


def test_backend_can_be_injected():
    """ARCH-8: an injected backend is used verbatim and _init_backend is not
    consulted (the caller owns the choice)."""
    from unittest.mock import MagicMock, patch
    config = make_recognition_config()
    state = MagicMock()
    fake_backend = MagicMock(name="fake-backend")
    with patch.object(RecognitionLoop, "_init_backend",
                      side_effect=AssertionError("_init_backend must not run when a backend is injected")):
        loop = RecognitionLoop(config, state, AsyncMock(), backend=fake_backend)
    assert loop.backend is fake_backend
