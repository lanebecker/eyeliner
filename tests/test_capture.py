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

What this deliberately does NOT cover (genuinely hardware-bound):
  - The live sd.InputStream integration (callback timing, PortAudio
    behavior) — that still needs the Pi + UCA222; the windowing logic it
    drives is covered hardware-free by tests/test_chunking.py.
"""
import sys
from unittest.mock import MagicMock, patch

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
