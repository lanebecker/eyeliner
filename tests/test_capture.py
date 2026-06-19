"""Unit tests for AudioCapture's hardware-free logic (new in v1.3.5).

capture.py imports sounddevice at module level, and sounddevice requires the
PortAudio system library at import time — which is why this file had zero
tests through v1.3.4.  The trick: plant a stub module into
sys.modules["sounddevice"] BEFORE importing capture, so the import succeeds
on machines without PortAudio.  On machines where the real sounddevice IS
installed (e.g. a dev Mac with `brew install portaudio`), the real module
loads instead — so every test patches `src.audio.capture.sd` explicitly and
never touches real audio hardware either way.

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
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Make capture importable without PortAudio: only installs the stub when the
# real sounddevice is absent (setdefault), so dev machines with PortAudio use
# the genuine module and CI-like environments use the fake.
sys.modules.setdefault("sounddevice", MagicMock())

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
