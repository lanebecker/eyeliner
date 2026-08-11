"""Unit tests for AudioCapture's hardware-free logic (new in v1.3.5).

capture.py imports sounddevice at module level, and sounddevice requires the
PortAudio system library at import time — which is why this file had zero
tests through v1.3.4.  The stub that makes the import succeed on machines
without PortAudio now lives in the root conftest.py (installed before any test
module is imported, and torn down at session end — TQ-6), replacing a
never-restored `sys.modules.setdefault(...)` that used to sit at this module's
scope.  On a dev Mac with the real sounddevice installed, conftest leaves it
untouched — so every test patches `src.audio.capture.sd` explicitly and never
touches real audio hardware either way.

What this covers (pure logic):
  ✓ _find_device_index: exact/substring/case-insensitive matching,
    input-channel filtering, multi-match first-wins + warning, not-found
    ValueError listing available devices
  ✓ The overlap >= chunk startup guard (warns, disables overlap)
  ✓ Constructor config plumbing and defaults
  ✓ _enqueue_block drop-oldest overflow policy (T-3)
  ✓ _make_callback marshaling: channel-0 copy scheduled on the loop (T-3)
  ✓ stop(): not-running flag + the None wake sentinel, edge cases (T-3)

What this deliberately does NOT cover (genuinely hardware-bound):
  - The live sd.InputStream integration (callback timing, PortAudio
    behavior) — that still needs the Pi + UCA222; the windowing logic it
    drives is covered hardware-free by tests/test_chunking.py.
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import numpy as np
import pytest

# The sounddevice stub is installed in the root conftest.py before this module
# is imported (TQ-6), so capture imports cleanly without PortAudio.
from src.audio import capture as capture_module  # noqa: E402
from src.audio.capture import AudioCapture  # noqa: E402
from tests.factories import make_audio_config  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(device_name="USB Audio Codec", chunk_seconds=15, overlap_seconds=5):
    return make_audio_config(
        device_name=device_name,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
    )


def make_capture(**config_kwargs):
    return AudioCapture(make_config(**config_kwargs), MagicMock(), MagicMock())


def device(name, inputs):
    return {"name": name, "max_input_channels": inputs}


# ---------------------------------------------------------------------------
# Constructor / config plumbing
# ---------------------------------------------------------------------------

def test_constructor_reads_audio_config():
    cap = make_capture()
    assert cap.sample_rate == 44100
    assert cap.chunk_seconds == 15
    assert cap.overlap_seconds == 5
    assert cap.device_name == "USB Audio Codec"


def test_overlap_defaults_to_five_seconds():
    # overlap_seconds omitted → AudioConfig's own default (5) applies, and
    # AudioCapture reads it straight through.
    config = make_audio_config()
    cap = AudioCapture(config, MagicMock(), MagicMock())
    assert cap.overlap_seconds == 5


def test_overlap_equal_to_chunk_is_disabled_at_startup():
    """overlap >= chunk would mean a zero/negative hop — an infinite
    re-recognition of the same audio. The guard warns and disables overlap."""
    cap = make_capture(chunk_seconds=10, overlap_seconds=10)
    assert cap.overlap_seconds == 0


def test_overlap_greater_than_chunk_is_disabled_at_startup():
    cap = make_capture(chunk_seconds=10, overlap_seconds=15)
    assert cap.overlap_seconds == 0


def test_valid_overlap_is_preserved():
    cap = make_capture(chunk_seconds=15, overlap_seconds=5)
    assert cap.overlap_seconds == 5


# ---------------------------------------------------------------------------
# _find_device_index
# ---------------------------------------------------------------------------

def test_find_device_returns_matching_input_device_index():
    cap = make_capture(device_name="USB Audio Codec")
    devices = [
        device("Built-in Microphone", 2),
        device("USB Audio CODEC", 2),
        device("HDMI Output", 0),
    ]
    with patch.object(capture_module.sd, "query_devices", return_value=devices):
        assert cap._find_device_index() == 1


def test_find_device_match_is_case_insensitive_substring():
    cap = make_capture(device_name="usb audio")
    devices = [device("Behringer USB AUDIO CODEC: - (hw:1,0)", 2)]
    with patch.object(capture_module.sd, "query_devices", return_value=devices):
        assert cap._find_device_index() == 0


def test_find_device_skips_output_only_devices():
    """A name match with zero input channels must not be selected."""
    cap = make_capture(device_name="USB Audio")
    devices = [
        device("USB Audio Playback", 0),   # output-only — skip despite name match
        device("USB Audio Codec", 2),
    ]
    with patch.object(capture_module.sd, "query_devices", return_value=devices):
        assert cap._find_device_index() == 1


def test_find_device_multiple_matches_uses_first_and_warns(caplog):
    """Multi-USB-audio setups: first match wins, all candidates are logged."""
    import logging
    cap = make_capture(device_name="USB")
    devices = [
        device("USB Audio Codec", 2),
        device("USB Microphone", 1),
    ]
    with patch.object(capture_module.sd, "query_devices", return_value=devices):
        with caplog.at_level(logging.WARNING, logger="src.audio.capture"):
            assert cap._find_device_index() == 0
    assert any("Multiple input devices match" in r.message for r in caplog.records)
    assert any("USB Microphone" in r.message for r in caplog.records)


def test_device_index_logs_once_per_index_not_every_lookup(caplog):
    """#164 follow-up: the lookup now runs on every stream rebuild, so its
    success INFO must fire only when the resolved index CHANGES — repeated
    resolution of the same device across a rebuild loop stays quiet (the PCONC-4
    anti-flood invariant this file already enforces for drop warnings)."""
    import logging
    cap = make_capture()
    devices = [device("USB Audio Codec", 2)]
    with patch.object(capture_module.sd, "query_devices", return_value=devices):
        with caplog.at_level(logging.INFO, logger="src.audio.capture"):
            for _ in range(5):                     # five rebuilds resolving index 0
                assert cap._find_device_index() == 0
    using = [r for r in caplog.records if "Using audio device" in r.message]
    assert len(using) == 1, f"expected 1 'Using audio device' log across 5 lookups, got {len(using)}"


def test_device_index_relogs_when_the_index_changes(caplog):
    """A re-plug that lands the device on a DIFFERENT index must re-log — that
    index change is exactly the signal #164 wants surfaced."""
    import logging
    cap = make_capture()
    with caplog.at_level(logging.INFO, logger="src.audio.capture"):
        with patch.object(capture_module.sd, "query_devices",
                          return_value=[device("USB Audio Codec", 2)]):
            assert cap._find_device_index() == 0          # index 0 → log
        with patch.object(capture_module.sd, "query_devices",
                          return_value=[device("Silent Sink", 0), device("USB Audio Codec", 2)]):
            assert cap._find_device_index() == 1          # re-plugged to index 1 → re-log
    using = [r for r in caplog.records if "Using audio device" in r.message]
    assert len(using) == 2


def test_multimatch_warning_fires_when_the_match_set_changes_not_just_the_winner(caplog):
    """#164 follow-up: the ambiguity WARNING is keyed on the match SET, not the
    winning index. A config that becomes newly ambiguous (a second matching
    device appears) must still warn even when index 0 keeps winning — and an
    unchanged set across a rebuild loop must NOT re-warn (anti-flood)."""
    import logging
    cap = make_capture(device_name="USB")
    with caplog.at_level(logging.WARNING, logger="src.audio.capture"):
        # Unambiguous first: one match at index 0 → no multi-match warning.
        with patch.object(capture_module.sd, "query_devices",
                          return_value=[device("USB Audio Codec", 2)]):
            assert cap._find_device_index() == 0
        assert not [r for r in caplog.records if "Multiple input devices" in r.message]

        # A second matching device appears; index 0 still wins → must warn ONCE.
        two = [device("USB Audio Codec", 2), device("USB Microphone", 1)]
        with patch.object(capture_module.sd, "query_devices", return_value=two):
            assert cap._find_device_index() == 0
            assert cap._find_device_index() == 0   # same set again → no re-warn
    warnings = [r for r in caplog.records if "Multiple input devices" in r.message]
    assert len(warnings) == 1, f"expected exactly one multi-match warning, got {len(warnings)}"


def test_find_device_not_found_raises_with_available_list():
    cap = make_capture(device_name="Nonexistent Interface")
    devices = [
        device("Built-in Microphone", 2),
        device("HDMI Output", 0),
    ]
    with patch.object(capture_module.sd, "query_devices", return_value=devices):
        with pytest.raises(ValueError) as exc_info:
            cap._find_device_index()
    # The error must name the missing device AND list available inputs
    # (but not output-only devices) so the user can fix config.yaml.
    msg = str(exc_info.value)
    assert "Nonexistent Interface" in msg
    assert "Built-in Microphone" in msg
    assert "HDMI Output" not in msg


# ---------------------------------------------------------------------------
# _enqueue_block — drop-oldest overflow policy (T-3)
#
# Mirrors test_enqueue_drops_oldest_when_full for the recognizer: when the
# block queue fills (the event loop stalled), the OLDEST block is evicted and
# the newest admitted, so recognition always sees the freshest audio.
# ---------------------------------------------------------------------------

def test_enqueue_block_appends_when_not_full():
    cap = make_capture()
    q = asyncio.Queue(maxsize=4)
    cap._enqueue_block(q, np.full(4, 1.0, dtype=np.float32))
    cap._enqueue_block(q, np.full(4, 2.0, dtype=np.float32))
    assert q.qsize() == 2


def test_enqueue_block_drops_oldest_when_full():
    cap = make_capture()
    q = asyncio.Queue(maxsize=2)
    cap._enqueue_block(q, np.full(4, 1.0, dtype=np.float32))   # oldest
    cap._enqueue_block(q, np.full(4, 2.0, dtype=np.float32))   # queue now full
    cap._enqueue_block(q, np.full(4, 3.0, dtype=np.float32))   # evict 1.0, admit 3.0

    assert q.qsize() == 2
    first = q.get_nowait()
    second = q.get_nowait()
    assert first[0] == 2.0    # the oldest (1.0) was dropped
    assert second[0] == 3.0   # the newest was admitted


# ---------------------------------------------------------------------------
# _make_callback — marshals each block onto the event loop (T-3)
# ---------------------------------------------------------------------------

def test_callback_schedules_channel0_copy_on_the_loop():
    cap = make_capture()
    loop = MagicMock()
    q = asyncio.Queue(maxsize=4)
    callback = cap._make_callback(loop, q)

    indata = np.array([[1.0], [2.0], [3.0]], dtype=np.float32)  # (frames, 1 channel)
    callback(indata, 3, None, None)

    loop.call_soon_threadsafe.assert_called_once()
    fn, blocks_arg, block_arg = loop.call_soon_threadsafe.call_args[0]
    assert fn == cap._enqueue_block          # marshalled to the enqueue, not run inline
    assert blocks_arg is q
    np.testing.assert_array_equal(block_arg, np.array([1.0, 2.0, 3.0], dtype=np.float32))


def test_callback_copies_block_so_portaudio_buffer_reuse_is_safe():
    """PortAudio reuses the indata buffer after the callback returns, so the
    scheduled block must be an independent copy."""
    cap = make_capture()
    loop = MagicMock()
    callback = cap._make_callback(loop, asyncio.Queue(maxsize=4))

    indata = np.array([[1.0], [2.0]], dtype=np.float32)
    callback(indata, 2, None, None)
    block_arg = loop.call_soon_threadsafe.call_args[0][2]

    indata[0, 0] = 99.0  # simulate PortAudio overwriting its buffer
    assert block_arg[0] == 1.0  # the scheduled block is unaffected


# ---------------------------------------------------------------------------
# stop() — flips _running and wakes a parked run() with the None sentinel (T-3)
# ---------------------------------------------------------------------------

def test_stop_clears_running_and_enqueues_wake_sentinel():
    cap = make_capture()
    cap._blocks = asyncio.Queue(maxsize=4)
    cap._running = True

    cap.stop()

    assert cap._running is False
    assert cap._blocks.get_nowait() is None  # the sentinel that wakes blocks.get()


def test_stop_is_safe_before_run_creates_a_queue():
    cap = make_capture()
    cap._blocks = None  # run() never started

    cap.stop()  # must not raise

    assert cap._running is False


def test_stop_tolerates_a_full_block_queue():
    cap = make_capture()
    q = asyncio.Queue(maxsize=1)
    q.put_nowait(np.zeros(4, dtype=np.float32))  # already full
    cap._blocks = q
    cap._running = True

    cap.stop()  # QueueFull is swallowed — run() already has something to wake for

    assert cap._running is False


# ---------------------------------------------------------------------------
# CONC-5 — a stream that goes quiet after starting (device brown-out/unplug, or
# a callback that aborts from CFFI) delivers NO exception to run(); the consumer
# used to park on blocks.get() forever. run() now times out the get() and
# rebuilds the stream, and the callback body is guarded so it can't abort the
# stream silently.
# ---------------------------------------------------------------------------

def _cm_stream_mock():
    """A mock sd.InputStream that is a context manager and does NOT suppress
    exceptions raised in the `with stream:` body (default MagicMock __exit__
    returns a truthy mock, which WOULD suppress — that must be False here)."""
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    return s


@pytest.mark.asyncio
async def test_stalled_stream_is_detected_and_rebuilt(monkeypatch):
    """A stream whose callback never delivers a block (a dead device) must be
    detected via the get() timeout and rebuilt — not parked on forever."""
    cap = make_capture()
    monkeypatch.setattr(capture_module.sd, "query_devices",
                        lambda *a, **k: [device("USB Audio Codec", 2)])
    built = asyncio.Event()
    streams = []

    def make_stream(**kwargs):
        s = _cm_stream_mock()
        streams.append(s)
        if len(streams) >= 2:      # a rebuild happened after the first stalled
            built.set()
        return s

    input_stream = MagicMock(side_effect=make_stream)
    monkeypatch.setattr(capture_module.sd, "InputStream", input_stream)
    # Fast stall + fast rebuild so the test doesn't wall-clock wait.
    monkeypatch.setattr(capture_module, "_BLOCK_STALL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)

    task = asyncio.create_task(cap.run())
    try:
        # Fixed code: stall fires in ~0.02s → rebuild → 2nd stream → event set.
        # Broken code: get() parks forever → event never set → wait_for raises.
        await asyncio.wait_for(built.wait(), timeout=2.0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert input_stream.call_count >= 2   # the stalled stream was torn down + rebuilt


def test_callback_error_is_logged_not_raised(caplog):
    """A raising callback body aborts the PortAudio stream from CFFI with no
    exception surfacing in run() (CONC-5) — capture would silently die. The
    callback must swallow + log instead of propagating."""
    import logging
    cap = make_capture()
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = RuntimeError("marshal boom")
    callback = cap._make_callback(loop, asyncio.Queue(maxsize=4))

    indata = np.array([[1.0], [2.0]], dtype=np.float32)
    with caplog.at_level(logging.ERROR, logger="src.audio.capture"):
        callback(indata, 2, None, None)   # must NOT raise

    rec = next(r for r in caplog.records if "callback" in r.message.lower())
    assert any("callback" in r.message.lower() for r in caplog.records)
    assert rec.exc_info is not None          # R5-26: traceback attached


# ---------------------------------------------------------------------------
# PCONC-4 — the drop-oldest warning must be throttled, not one-per-drop
# (53 WARNING records in a single stalled loop turn was measured — an SD-card
# log flood). The drops are counted and surfaced as one throttled health signal.
# ---------------------------------------------------------------------------

def test_drop_warning_is_throttled_not_one_per_drop(caplog):
    import logging
    cap = make_capture()
    q = asyncio.Queue(maxsize=2)
    cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # fill
    cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # full now
    with caplog.at_level(logging.WARNING, logger="src.audio.capture"):
        for _ in range(20):    # 20 rapid drops within one throttle window
            cap._enqueue_block(q, np.zeros(4, dtype=np.float32))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected 1 throttled warning, got {len(warnings)}"


def test_drop_warning_reports_aggregate_count_after_window(monkeypatch, caplog):
    import logging
    cap = make_capture()
    clock = {"t": 1000.0}
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: clock["t"])
    q = asyncio.Queue(maxsize=1)
    cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # full
    with caplog.at_level(logging.WARNING, logger="src.audio.capture"):
        cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # drop 1 -> logs "1"
        for _ in range(9):                                     # drops 2..10 silent
            cap._enqueue_block(q, np.zeros(4, dtype=np.float32))
        clock["t"] += 999.0                                    # jump past the window
        cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # drop 11 -> logs aggregate
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2                     # first drop + post-window summary
    assert "10" in warnings[1].message            # the 10 drops accrued since the first report


# ---------------------------------------------------------------------------
# #178 — the capture-loop retry-error log is throttled: a PERMANENT failure (a
# misconfigured device_name that never matches, a device absent forever) raises
# every retry, so at ~1 error per _STREAM_RETRY_BACKOFF_SECONDS it would flood
# the journal/SD card (the PCONC-4 class). First error + any CHANGED error logs
# immediately; identical repeats are counted and summarized once per interval.
# ---------------------------------------------------------------------------

def test_capture_error_first_occurrence_logs_immediately(monkeypatch, caplog):
    import logging
    cap = make_capture()
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: 1000.0)
    with caplog.at_level(logging.ERROR, logger="src.audio.capture"):
        cap._log_capture_error(ValueError("device 'X' not found"))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "device 'X' not found" in errors[0].message


def test_capture_error_identical_repeats_are_throttled(monkeypatch, caplog):
    """A permanent misconfig raising the SAME error every retry logs once, not
    once per retry."""
    import logging
    cap = make_capture()
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: 1000.0)  # frozen < interval
    err = ValueError("Audio device 'Nope' not found. Available input devices: []")
    with caplog.at_level(logging.ERROR, logger="src.audio.capture"):
        for _ in range(20):
            cap._log_capture_error(err)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, f"expected 1 throttled error, got {len(errors)}"


def test_capture_error_summarizes_after_the_interval(monkeypatch, caplog):
    import logging
    cap = make_capture()
    clock = {"t": 1000.0}
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: clock["t"])
    err = ValueError("device 'Nope' not found")
    with caplog.at_level(logging.ERROR, logger="src.audio.capture"):
        cap._log_capture_error(err)                # 1st -> logs
        for _ in range(9):                         # 2..10 suppressed
            cap._log_capture_error(err)
        clock["t"] += capture_module._CAPTURE_ERROR_WARN_INTERVAL_SECONDS + 1  # past window
        cap._log_capture_error(err)                # -> logs a summary
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2                        # first + post-window summary
    assert "9" in errors[1].message                # the 9 suppressed since the first report


def test_capture_error_changed_message_logs_immediately(monkeypatch, caplog):
    """A DIFFERENT error (a new condition) must surface at once, not wait out the
    throttle window behind an unrelated repeating error."""
    import logging
    cap = make_capture()
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: 1000.0)  # frozen < interval
    with caplog.at_level(logging.ERROR, logger="src.audio.capture"):
        cap._log_capture_error(ValueError("device absent"))
        cap._log_capture_error(ValueError("device absent"))   # identical -> suppressed
        cap._log_capture_error(RuntimeError("audio stream stalled"))  # CHANGED -> logs
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert "device absent" in errors[0].message
    assert "stream stalled" in errors[1].message


# ---------------------------------------------------------------------------
# TQ-7 — _silence_ticker and run()'s construction-retry path, headless.
#
# _silence_ticker is the SESSION_ENDED / Play-Count safety net during a stall,
# and run()'s except-branch rebuilds a fresh stream after a construction
# failure. Both were fully uncovered on hardware that has never been run. These
# exercise them with a pure-asyncio mock detector + a mocked InputStream.
# (The stall-timeout rebuild path is already covered by
# test_stalled_stream_is_detected_and_rebuilt above.)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_silence_ticker_ticks_repeatedly_and_survives_a_raising_listener(monkeypatch):
    """The ticker must keep calling silence.tick() on wall-clock time, and a
    listener raising inside tick() must NOT kill it — a dead ticker would
    permanently disable the session-end safety net it exists to provide."""
    cap = make_capture()
    ticks = {"n": 0}

    def raising_tick():
        ticks["n"] += 1
        raise RuntimeError("listener boom")   # every tick raises

    cap.silence.tick = raising_tick
    monkeypatch.setattr(capture_module, "_SILENCE_TICK_SECONDS", 0.001)

    cap._running = True
    task = asyncio.create_task(cap._silence_ticker())
    for _ in range(500):                       # bounded wait for ≥5 ticks
        if ticks["n"] >= 5:
            break
        await asyncio.sleep(0.001)
    cap._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ticks["n"] >= 5                      # survived the raising listener


@pytest.mark.asyncio
async def test_stream_construction_failure_retries_with_a_fresh_stream(monkeypatch):
    """sd.InputStream() raising on CONSTRUCTION (distinct from the stall
    timeout) must be caught, backed off, and retried with a fresh stream."""
    cap = make_capture()
    monkeypatch.setattr(capture_module.sd, "query_devices",
                        lambda *a, **k: [device("USB Audio Codec", 2)])
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(capture_module, "_BLOCK_STALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(capture_module, "_SILENCE_TICK_SECONDS", 0.001)

    built = asyncio.Event()
    calls = {"n": 0}

    def make_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("PortAudio: device busy")   # construction fails once
        built.set()
        return _cm_stream_mock()                      # second construction succeeds

    monkeypatch.setattr(capture_module.sd, "InputStream", MagicMock(side_effect=make_stream))

    task = asyncio.create_task(cap.run())
    try:
        await asyncio.wait_for(built.wait(), timeout=2.0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert calls["n"] >= 2         # the failed construction was retried


@pytest.mark.asyncio
async def test_absent_device_at_startup_is_retried_not_crash_looped(monkeypatch):
    """#164 + #194: a device absent when the service starts (a mistyped
    audio.device_name, or the UCA222 not yet USB-enumerated) must be retried
    INSIDE the loop, NOT escape run() and crash-loop under systemd (#164) — AND
    must actually be RECOVERED once it appears, which needs a PortAudio table
    refresh between attempts (#194).

    #194 is why this test changed. PortAudio freezes its device table at import,
    so a fresh _find_device_index() alone sees the same stale snapshot every
    retry — a device absent at import can never appear no matter how long the loop
    spins. run()'s failure handler must call sd._terminate()+sd._initialize() to
    re-enumerate BEFORE the next lookup; this asserts that ordering. The old test
    let query_devices silently return a new list on call 2 and asserted nothing
    about a refresh, so it was green against code that could never recover on real
    hardware — the exact false pin #194 records. The mock returning a new list on
    the second lookup is kept, but is now a FAITHFUL model: only the rescan can
    make PortAudio report a device it didn't report before, and we assert the
    rescan is what happened."""
    cap = make_capture()
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(capture_module, "_BLOCK_STALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(capture_module, "_SILENCE_TICK_SECONDS", 0.001)

    # Ordered trace of every device-API interaction, so we can assert a refresh
    # sits BETWEEN the failed first lookup and the successful second one.
    trace = []

    def query_devices(*a, **k):
        trace.append("lookup")
        # ABSENT on the first lookup (no input-capable match → ValueError); the
        # device APPEARS only after a refresh has re-enumerated the table.
        if trace.count("lookup") == 1:
            return [device("Some Output-Only Sink", 0)]   # no input match → ValueError
        return [device("USB Audio Codec", 2)]             # now present
    monkeypatch.setattr(capture_module.sd, "query_devices", query_devices)
    monkeypatch.setattr(
        capture_module.sd, "_terminate",
        MagicMock(side_effect=lambda *a, **k: trace.append("terminate")),
    )
    monkeypatch.setattr(
        capture_module.sd, "_initialize",
        MagicMock(side_effect=lambda *a, **k: trace.append("initialize")),
    )

    built = asyncio.Event()

    def make_stream(**kwargs):
        built.set()
        return _cm_stream_mock()
    monkeypatch.setattr(capture_module.sd, "InputStream", MagicMock(side_effect=make_stream))

    task = asyncio.create_task(cap.run())
    try:
        # Fixed: lookup 1 raises INSIDE the loop → caught → refresh → backoff →
        # lookup 2 finds the device → stream built → event set.
        # Pre-#164: lookup raised ABOVE the loop → task faulted (event never set).
        # Pre-#194: refresh never happened → the frozen table never gains the
        # device → event never set. Either way wait_for times out (RED).
        await asyncio.wait_for(built.wait(), timeout=2.0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert trace.count("lookup") >= 2   # the absent-device lookup was retried, not fatal
    assert built.is_set()               # and a stream was built once the device appeared
    # The crux (#194): a _terminate()+_initialize() rescan ran BETWEEN the failed
    # first lookup and the successful later one — the real recovery mechanism, not
    # a mock silently re-enumerating on its own.
    first = trace.index("lookup")
    second = trace.index("lookup", first + 1)
    assert trace[first + 1:second] == ["terminate", "initialize"], (
        f"expected a PortAudio refresh between the failed and successful lookups, "
        f"got trace={trace!r}"
    )


@pytest.mark.asyncio
async def test_replug_to_new_index_is_recovered_via_refresh(monkeypatch):
    """#194: a device re-plugged to a DIFFERENT ALSA card index mid-run (e.g.
    after a CONC-5 stall tears the stream down) must be recovered. Without the
    refresh, _find_device_index() keeps returning the stale index off the frozen
    table and sd.InputStream(device=stale) raises every retry — capture never
    comes back. With the refresh, the rescan surfaces the new index and the
    rebuilt stream opens against it."""
    cap = make_capture()
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(capture_module, "_BLOCK_STALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(capture_module, "_SILENCE_TICK_SECONDS", 0.001)

    refreshed = {"n": 0}

    def query_devices(*a, **k):
        # Before any refresh: the device sits at index 0 (a stale entry that will
        # fail to open). After a refresh: it re-enumerates at index 1 (the new
        # card index), which opens successfully.
        if refreshed["n"] == 0:
            return [device("USB Audio Codec", 2)]                     # stale @ idx 0
        return [device("Some Other Sink", 0), device("USB Audio Codec", 2)]  # new @ idx 1
    monkeypatch.setattr(capture_module.sd, "query_devices", query_devices)
    monkeypatch.setattr(capture_module.sd, "_terminate", MagicMock())
    monkeypatch.setattr(
        capture_module.sd, "_initialize",
        MagicMock(side_effect=lambda *a, **k: refreshed.__setitem__("n", refreshed["n"] + 1)),
    )

    opened_indices = []
    built = asyncio.Event()

    def make_stream(**kwargs):
        idx = kwargs.get("device")
        opened_indices.append(idx)
        if idx == 0:
            # The stale index no longer addresses the interface — opening it fails,
            # driving the rebuild/refresh path.
            raise OSError("Invalid device (stale index after re-plug)")
        built.set()
        return _cm_stream_mock()
    monkeypatch.setattr(capture_module.sd, "InputStream", MagicMock(side_effect=make_stream))

    task = asyncio.create_task(cap.run())
    try:
        await asyncio.wait_for(built.wait(), timeout=2.0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert 0 in opened_indices and 1 in opened_indices  # stale tried, new index recovered
    assert built.is_set()
    assert refreshed["n"] >= 1                           # a refresh actually ran


@pytest.mark.asyncio
async def test_stream_start_failure_closes_stream_before_refresh(monkeypatch):
    """#194: sd.InputStream opens the stream at CONSTRUCTION (Pa_OpenStream), and
    `with stream:` starts it via __enter__. If start() raises — a device
    brown-out/unplug in the window between open and start, the exact hotplug class
    #194 targets — Python never runs __exit__, so the stream stays OPEN.
    _terminate() (inside _refresh_audio_devices) against a live stream is
    undefined behaviour, so run() MUST close the opened stream BEFORE refreshing
    the device table."""
    cap = make_capture()
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(capture_module, "_SILENCE_TICK_SECONDS", 0.001)
    monkeypatch.setattr(capture_module.sd, "query_devices",
                        lambda *a, **k: [device("USB Audio Codec", 2)])

    trace = []

    def make_stream(**kwargs):
        s = MagicMock()
        # Construction SUCCEEDED (this returned a stream), but __enter__/start()
        # raises → `with` does NOT call __exit__ → the stream is left open.
        s.__enter__ = MagicMock(side_effect=OSError("Pa_StartStream: device unavailable"))
        s.__exit__ = MagicMock(return_value=False)
        s.close = MagicMock(side_effect=lambda *a, **k: trace.append("close"))
        return s
    monkeypatch.setattr(capture_module.sd, "InputStream", MagicMock(side_effect=make_stream))
    monkeypatch.setattr(capture_module.sd, "_terminate",
                        MagicMock(side_effect=lambda *a, **k: trace.append("terminate")))
    monkeypatch.setattr(capture_module.sd, "_initialize",
                        MagicMock(side_effect=lambda *a, **k: trace.append("initialize")))

    task = asyncio.create_task(cap.run())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if "close" in trace and "terminate" in trace:
                break
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The open stream was closed, and BEFORE the _terminate() rescan touched
    # PortAudio — never _terminate() against a live stream.
    assert "close" in trace and "terminate" in trace, trace
    assert trace.index("close") < trace.index("terminate"), trace


@pytest.mark.asyncio
async def test_device_refresh_private_api_failure_degrades_not_crashes(monkeypatch):
    """#194: _terminate()/_initialize() are PRIVATE sounddevice APIs. If an
    upstream refactor removes or breaks them, the refresh must degrade to the
    pre-#194 behaviour (retry against the existing table), NOT turn a recoverable
    device-down loop into a crash. The loop must keep running and recover once the
    device appears on its own."""
    cap = make_capture()
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(capture_module, "_BLOCK_STALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(capture_module, "_SILENCE_TICK_SECONDS", 0.001)

    lookups = {"n": 0}

    def query_devices(*a, **k):
        lookups["n"] += 1
        if lookups["n"] == 1:
            return [device("Some Output-Only Sink", 0)]   # absent → ValueError
        return [device("USB Audio Codec", 2)]             # appears anyway
    monkeypatch.setattr(capture_module.sd, "query_devices", query_devices)
    # The refresh recipe is broken upstream — both raise.
    monkeypatch.setattr(
        capture_module.sd, "_terminate",
        MagicMock(side_effect=AttributeError("sounddevice refactored away _terminate")),
    )
    monkeypatch.setattr(
        capture_module.sd, "_initialize",
        MagicMock(side_effect=AttributeError("sounddevice refactored away _initialize")),
    )

    built = asyncio.Event()

    def make_stream(**kwargs):
        built.set()
        return _cm_stream_mock()
    monkeypatch.setattr(capture_module.sd, "InputStream", MagicMock(side_effect=make_stream))

    task = asyncio.create_task(cap.run())
    try:
        # The broken refresh is swallowed (logged at debug); the loop keeps
        # retrying and still builds a stream when the device appears.
        await asyncio.wait_for(built.wait(), timeout=2.0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert lookups["n"] >= 2   # retried despite the refresh raising — no crash
    assert built.is_set()


@pytest.mark.asyncio
async def test_run_cancels_and_awaits_the_ticker_on_exit(monkeypatch):
    """run()'s finally must tear the ticker down (cancel + await) so it never
    outlives the capture loop."""
    cap = make_capture()
    monkeypatch.setattr(capture_module.sd, "query_devices",
                        lambda *a, **k: [device("USB Audio Codec", 2)])
    monkeypatch.setattr(capture_module.sd, "InputStream",
                        MagicMock(side_effect=lambda **k: _cm_stream_mock()))
    monkeypatch.setattr(capture_module, "_BLOCK_STALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(capture_module, "_STREAM_RETRY_BACKOFF_SECONDS", 0.0)

    lifecycle = {"started": False, "cancelled": False}

    async def fake_ticker():
        lifecycle["started"] = True
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            lifecycle["cancelled"] = True
            raise

    monkeypatch.setattr(cap, "_silence_ticker", fake_ticker)

    task = asyncio.create_task(cap.run())
    for _ in range(200):
        if lifecycle["started"]:
            break
        await asyncio.sleep(0.005)
    assert lifecycle["started"]

    cap.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert lifecycle["cancelled"]     # run()'s finally cancelled + awaited the ticker


def test_first_drop_logs_even_at_low_monotonic(monkeypatch, caplog):
    """PCONC-4 (cold-review regression): CLOCK_MONOTONIC is uptime-based and
    resets to ~0 on a Pi reboot, so the very first overflow drop in the first
    few seconds of uptime must STILL warn — a 0.0 seed would suppress it."""
    import logging
    cap = make_capture()
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: 2.0)  # < 5s interval
    q = asyncio.Queue(maxsize=1)
    cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # full
    with caplog.at_level(logging.WARNING, logger="src.audio.capture"):
        cap._enqueue_block(q, np.zeros(4, dtype=np.float32))   # first-ever drop
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1     # reported despite monotonic() < interval


# ---------------------------------------------------------------------------
# #209 (R4:test-1) — run()'s block loop is the SINGLE integration point that
# feeds the whole pipeline (silence → session lifecycle; recognizer → display/
# scrobble). Coverage showed the happy-path dispatch never executed: a mutant
# replacing the loop body with `pass` (every chunk silently dropped) passed the
# full suite. These pin that run() actually feeds each assembled chunk to
# silence.process + (music-gated) recognizer.enqueue, plus the None stop-sentinel.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_feeds_assembled_chunks_to_silence_and_recognizer(monkeypatch):
    # chunk_seconds=1, overlap=0 → chunk_frames == sample_rate, so a single block
    # of sample_rate samples makes ChunkAssembler emit exactly one chunk.
    cap = make_capture(chunk_seconds=1, overlap_seconds=0)
    cap.silence = MagicMock()
    cap.silence.is_music_playing = True          # music verdict → recognition runs (#193)
    cap.recognizer = MagicMock()
    cap.recognizer.enqueue = AsyncMock()          # run() awaits enqueue

    monkeypatch.setattr(capture_module.sd, "query_devices",
                        lambda *a, **k: [device("USB Audio Codec", 2)])
    monkeypatch.setattr(capture_module.sd, "InputStream",
                        MagicMock(side_effect=lambda **k: _cm_stream_mock()))

    dispatched = asyncio.Event()
    real_dispatch = cap._dispatch_chunk
    async def spy(chunk, sr):
        await real_dispatch(chunk, sr)
        dispatched.set()
    cap._dispatch_chunk = spy

    task = asyncio.create_task(cap.run())
    try:
        for _ in range(200):                      # wait until run() creates the queue
            if cap._blocks is not None:
                break
            await asyncio.sleep(0)
        assert cap._blocks is not None
        cap._blocks.put_nowait(np.ones(cap.sample_rate, dtype=np.float32))
        await asyncio.wait_for(dispatched.wait(), timeout=2.0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # silence.process ran on the assembled chunk with the stream's sample_rate…
    assert cap.silence.process.call_count >= 1
    proc_args = cap.silence.process.call_args.args
    assert isinstance(proc_args[0], np.ndarray) and len(proc_args[0]) == cap.sample_rate
    assert proc_args[1] == cap.sample_rate
    # …and the same chunk was enqueued for recognition with the sample_rate.
    cap.recognizer.enqueue.assert_awaited()
    assert cap.recognizer.enqueue.await_args.args[1] == cap.sample_rate


@pytest.mark.asyncio
async def test_dispatch_chunk_gates_recognition_on_the_music_verdict():
    # #193/#195: silence.process runs on EVERY chunk (drives the lifecycle), but
    # recognizer.enqueue is skipped when the detector reports no music — an idle
    # turntable must not POST digital silence to Shazam every hop.
    cap = make_capture()
    cap.silence = MagicMock()
    cap.silence.is_music_playing = False
    cap.recognizer = MagicMock()
    cap.recognizer.enqueue = AsyncMock()
    chunk = np.zeros(8, dtype=np.float32)

    await cap._dispatch_chunk(chunk, 44100)

    cap.silence.process.assert_called_once_with(chunk, 44100)
    cap.recognizer.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_treats_a_none_block_as_a_stop_sentinel_not_a_chunk(monkeypatch):
    # #209: `if block is None: continue` — the stop() wake sentinel must be
    # skipped, never fed to assembler.feed() (which raises on None and would take
    # the CONC-5 error/rebuild path). Deterministic pin (no sleeps, no reliance on
    # queue.empty() — which flips at PUT time, before run() resumes): spy
    # ChunkAssembler.feed and assert every block it receives is a real array. A
    # trailing real block, once fed, proves run() consumed PAST the sentinel; on
    # the guard-removed mutant feed(None) records a None first and this goes RED.
    cap = make_capture(chunk_seconds=1, overlap_seconds=0)
    cap.silence = MagicMock()
    cap.silence.is_music_playing = True
    cap.recognizer = MagicMock()
    cap.recognizer.enqueue = AsyncMock()

    fed = []
    real_feed = capture_module.ChunkAssembler.feed
    def spy_feed(self, block):
        fed.append(block)
        return real_feed(self, block)
    monkeypatch.setattr(capture_module.ChunkAssembler, "feed", spy_feed)

    monkeypatch.setattr(capture_module.sd, "query_devices",
                        lambda *a, **k: [device("USB Audio Codec", 2)])
    monkeypatch.setattr(capture_module.sd, "InputStream",
                        MagicMock(side_effect=lambda **k: _cm_stream_mock()))

    task = asyncio.create_task(cap.run())
    try:
        for _ in range(200):
            if cap._blocks is not None:
                break
            await asyncio.sleep(0)
        assert cap._blocks is not None
        cap._blocks.put_nowait(None)                                        # the stop sentinel
        cap._blocks.put_nowait(np.ones(cap.sample_rate, dtype=np.float32))  # a real block behind it
        for _ in range(500):                    # wait until the assembler is fed at all
            if fed:
                break
            await asyncio.sleep(0)
    finally:
        cap.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The assembler WAS fed (the real block got through), and NOTHING it received
    # was the None sentinel — i.e. `if block is None: continue` held.
    assert fed, "run() never fed the assembler — the block did not flow through"
    assert all(isinstance(b, np.ndarray) for b in fed)
