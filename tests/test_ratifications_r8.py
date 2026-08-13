"""R8 Wave 5 (#369) — hardening-residue pin.

R8-20/#369: the #194 hotplug recovery rests on sounddevice PRIVATE APIs
(`sd._terminate`/`sd._initialize`, pinned working at the 0.5.5 floor).  A
future `pip install -U sounddevice` could remove them and silently revert the
recovery — the degradation used to be a debug-level whisper.  It now WARNs
exactly once (with the installed version) so a bring-up journal shows it;
repeats stay debug.

(The rest of Wave 5 — #367/#368 ratified docstrings, #370 CHANGELOG note,
#371 floor documentation, #372 accepted cache churn — is prose by design.)
"""
import logging

from src.audio.capture import AudioCapture


def _capture_skeleton():
    c = AudioCapture.__new__(AudioCapture)
    c._device_refresh_degraded_warned = False
    return c


def test_r8_20_private_api_degradation_warns_once(monkeypatch, caplog):
    import src.audio.capture as cap_mod

    c = _capture_skeleton()

    class BrokenSD:
        __version__ = "9.9.9-hypothetical"

        def _terminate(self):
            raise AttributeError("private API removed upstream")

        def _initialize(self):
            raise AttributeError("unreachable")

    monkeypatch.setattr(cap_mod, "sd", BrokenSD())

    with caplog.at_level(logging.DEBUG, logger="src.audio.capture"):
        c._refresh_audio_devices()
        c._refresh_audio_devices()
        c._refresh_audio_devices()

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "hotplug recovery is degraded" in r.getMessage()]
    assert len(warnings) == 1, "the degradation must WARN exactly once (R8-20)"
    assert "9.9.9-hypothetical" in warnings[0].getMessage(), (
        "the warning must name the installed sounddevice version"
    )
    debugs = [r for r in caplog.records
              if r.levelno == logging.DEBUG and "refresh unavailable" in r.getMessage()]
    assert len(debugs) == 2, "repeats stay at debug"


def test_r8_20_working_private_api_stays_silent(monkeypatch, caplog):
    import src.audio.capture as cap_mod

    c = _capture_skeleton()

    class WorkingSD:
        __version__ = "0.5.5"

        def _terminate(self):
            pass

        def _initialize(self):
            pass

    monkeypatch.setattr(cap_mod, "sd", WorkingSD())

    with caplog.at_level(logging.DEBUG, logger="src.audio.capture"):
        c._refresh_audio_devices()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert c._device_refresh_degraded_warned is False
