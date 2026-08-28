"""#472: EYELINER_DEBUG_RECOGNITION opt-in per-poll recognition diagnostics —
a permanent, env-toggled replacement for hand-patching debug log lines."""
import logging
from types import SimpleNamespace

import pytest

import src.audio.recognizer as rec
from src.audio.recognizer import RawRecognitionResult
from src.state.player_state import PlayerStatus
from tests.test_per_track_polling import _loop, _track


def test_idle_diagnostic_silent_when_flag_off(caplog):
    loop = _loop()
    loop.state.current_track = _track(0, ["4:00"])
    with caplog.at_level(logging.INFO):
        loop._go_idle_until_boundary(SimpleNamespace(match_offset=30.0), now=0.0)
    assert not any("recognition-debug" in r.getMessage() for r in caplog.records)


def test_idle_diagnostic_emitted_when_flag_on(caplog, monkeypatch):
    monkeypatch.setattr(rec, "_DEBUG_RECOGNITION", True)
    loop = _loop()
    loop.state.current_track = _track(0, ["4:00"])   # 240s
    with caplog.at_level(logging.INFO):
        loop._go_idle_until_boundary(SimpleNamespace(match_offset=30.0), now=0.0)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("recognition-debug idle:" in m and "wait=" in m for m in msgs)


@pytest.mark.asyncio
async def test_poll_diagnostic_emitted_when_flag_on(caplog, monkeypatch):
    monkeypatch.setattr(rec, "_DEBUG_RECOGNITION", True)
    loop = _loop()
    loop.state.status = PlayerStatus.LISTENING
    with caplog.at_level(logging.INFO):
        await loop._handle_result(RawRecognitionResult("Peddler", "Lunar Vacation", "X"))
    assert any("recognition-debug poll:" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_poll_diagnostic_silent_when_flag_off(caplog):
    loop = _loop()
    loop.state.status = PlayerStatus.LISTENING
    with caplog.at_level(logging.INFO):
        await loop._handle_result(RawRecognitionResult("Peddler", "Lunar Vacation", "X"))
    assert not any("recognition-debug" in r.getMessage() for r in caplog.records)


def test_flag_defaults_off():
    """The shipped default must be OFF (no diagnostic spam in normal operation)."""
    assert rec._DEBUG_RECOGNITION is False
