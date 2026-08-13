"""R9 Wave 3 (#395–#405) — ops & polish.

R9-07/#395  the capture-throttle key no longer FRAGMENTS on the 3-arg
  PortAudioError shape (quoted ALSA free text) — it keys on the stable condition.
R9-13/#396  a missing audio C-extension (libportaudio2) becomes the friendly
  ConfigError→exit-78 park, not an import-time crash-loop.
R9-15/#397  an errno-carrying OSError (EIO) during validate PROPAGATES (transient
  back-off) instead of condemning the URL to a permanent blacklist.
R9-25/#404  accept-with-comment — the np.dot RMS optimization was rejected (it
  breaks the exact-threshold boundary); no code change, so no test here.
"""
import errno

import pytest


# ---------------------------------------------------------------------------
# R9-07 (#395) — capture-error key stability on the 3-arg PortAudioError shape
# ---------------------------------------------------------------------------

class _PortAudioError(Exception):
    """Stand-in with the type NAME the key derives from — no sounddevice import."""


def _key(msg):
    from src.audio.capture import AudioCapture
    return AudioCapture._capture_error_key(_PortAudioError(msg))


def test_r9_07_three_arg_portaudio_error_keys_on_stable_condition():
    """RED before R9-07: the last colon segment is ARBITRARY quoted ALSA text, so
    two faults of the SAME condition minted different keys ('Resource vs 'No)."""
    k1 = _key("Error opening InputStream: Unanticipated host API error: "
              "'Resource busy' [ALSA error -16]")
    k2 = _key("Error opening InputStream: Unanticipated host API error: "
              "'No such device' [ALSA error -19]")
    assert k1 == k2 == "_PortAudioError:Unanticipated", (k1, k2)


def test_r9_07_two_arg_portaudio_error_shape_unchanged():
    """The 2-arg shape (no quoted free text) still keys on the condition word."""
    assert _key("Error opening InputStream: Device unavailable [PaErrorCode -9985]") \
        == "_PortAudioError:Device"


def test_r9_07_plain_oserror_leading_bracket_unchanged():
    assert _key("[Errno -9997] Invalid sample rate") == "_PortAudioError:Invalid"


# ---------------------------------------------------------------------------
# R9-13 (#396) — audio-backend import probe → friendly park
# ---------------------------------------------------------------------------

def test_r9_13_missing_libportaudio_raises_configerror():
    """RED before R9-13: the sounddevice import happened at MODULE level, so this
    failure crashed before main()'s try. Now a startup probe converts it to the
    ConfigError main() turns into exit-78."""
    from main import verify_audio_backend_importable
    from src.config import ConfigError

    def boom(_name):
        raise OSError("libportaudio2.so.2: cannot open shared object file")

    with pytest.raises(ConfigError):
        verify_audio_backend_importable(_import=boom)


def test_r9_13_present_sounddevice_passes():
    from main import verify_audio_backend_importable
    verify_audio_backend_importable(_import=lambda _name: object())   # no raise


def test_r9_13_audiocapture_not_imported_at_module_level():
    """The whole point: importing main must not import sounddevice-backed
    AudioCapture (else a broken libportaudio2 crashes before the park)."""
    import main
    assert "AudioCapture" not in vars(main), (
        "AudioCapture is imported at module level again — a missing libportaudio2 "
        "would crash-loop instead of parking"
    )


# ---------------------------------------------------------------------------
# R9-15 (#397) — errno-carrying OSError propagates, not permanent-blacklist
# ---------------------------------------------------------------------------

def test_r9_15_eio_during_validate_propagates_transient(tmp_path, monkeypatch):
    """RED before R9-15: validate_image_file unconditionally raised
    PermanentCoverError, so a transient EIO mid-read blacklisted the URL forever."""
    from src.display.palette import validate_image_file, PermanentCoverError

    p = tmp_path / "x.jpg"
    p.write_bytes(b"whatever")

    def boom_open(*a, **k):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr("PIL.Image.open", boom_open)

    with pytest.raises(OSError) as ei:
        validate_image_file(str(p))
    assert ei.value.errno == errno.EIO
    assert not isinstance(ei.value, PermanentCoverError), (
        "a transient EIO must NOT become a permanent condemnation"
    )


def test_r9_15_errno_less_content_failure_still_condemns(tmp_path, monkeypatch):
    """The other half: an errno-LESS decode failure (corrupt bytes) still becomes
    PermanentCoverError — the taxonomy only spares real disk errors."""
    from src.display.palette import validate_image_file, PermanentCoverError

    p = tmp_path / "x.jpg"
    p.write_bytes(b"whatever")

    def boom_open(*a, **k):
        raise OSError("cannot identify image file")   # errno is None

    monkeypatch.setattr("PIL.Image.open", boom_open)

    with pytest.raises(PermanentCoverError):
        validate_image_file(str(p))

# R9-25 (#404): accept-with-comment — no code change (see silence.py's RMS
# comment for why np.dot was rejected), so no test lives here.  The existing
# test_silence.py::test_signal_just_at_threshold_is_music is exactly the guard
# that the np.dot optimization would have broken, and it stays green.
