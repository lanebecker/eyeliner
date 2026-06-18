"""Tests for the shared Signal helper (A-11) and the observers it now backs.

Before A-11, PlayerState guarded each listener but SilenceDetector did not, so
one throwing listener killed delivery to the rest mid-process().  Both now use
Signal's single log-and-continue policy.
"""
import logging

import numpy as np

from src.util.signal import Signal
from src.audio.silence import SilenceDetector, AudioEvent
from src.state.player_state import PlayerState, PlayerStatus


def test_signal_delivers_to_all_listeners_in_order():
    sig = Signal("t")
    seen = []
    sig.connect(lambda v: seen.append(("a", v)))
    sig.connect(lambda v: seen.append(("b", v)))
    sig.emit(42)
    assert seen == [("a", 42), ("b", 42)]
    assert len(sig) == 2


def test_signal_logs_and_continues_on_listener_error(caplog):
    sig = Signal("unit")
    delivered = []

    def boom(_):
        raise RuntimeError("listener boom")

    sig.connect(boom)
    sig.connect(lambda v: delivered.append(v))

    with caplog.at_level(logging.ERROR):
        sig.emit("x")

    assert delivered == ["x"]                       # 2nd listener still ran
    assert any("listener boom" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# A-11: the observers both gain the log-and-continue guard
# ---------------------------------------------------------------------------

def test_silence_detector_survives_a_throwing_listener():
    cfg = {"audio": {"silence_threshold_rms": 0.1, "session_end_silence_seconds": 45}}
    d = SilenceDetector(cfg)
    got = []

    def boom(_):
        raise RuntimeError("boom")

    d.on_event(boom)            # registered first — used to kill the rest
    d.on_event(got.append)

    d.process(np.ones(64, dtype=np.float32), 44100)   # → MUSIC_STARTED

    assert AudioEvent.MUSIC_STARTED in got            # delivery survived the throw


def test_player_state_survives_a_throwing_listener():
    s = PlayerState()
    got = []

    def boom(_):
        raise RuntimeError("boom")

    s.on_change(boom)
    s.on_change(lambda state: got.append(state.status))

    s.set_status(PlayerStatus.LISTENING)

    assert got == [PlayerStatus.LISTENING]
