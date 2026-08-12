"""Regression tests for Wave 3 bundle 1 — recognition gate + session lifecycle.

#193 (stab-1, HIGH): AudioCapture enqueued EVERY assembled chunk for Shazam
  recognition unconditionally — silence included — so an idle turntable POSTs to
  Shazam's unofficial API ~8,640×/day forever. Fix: gate recognition dispatch on
  the already-computed music verdict (SilenceDetector.is_music_playing).

#195 (conc-3, MEDIUM): because recognition ran ungated, sub-threshold audio (low
  input gain) could confirm tracks and start a tracker session while the
  SilenceDetector never left silence-state — so _silence_since was never armed,
  SESSION_ENDED could NEVER fire, and the session was immortal (card stuck on
  screen forever; Play Count / Last Played / love silently never written). The
  same gate fixes this structurally: a session can only start when the detector
  is in music-state, so the music→silence transition that ends it is always
  reachable. A throttled low-gain diagnostic surfaces the miscalibration.

#196 (conc-4, LOW): on the stale-discard race (session ends while
  on_track_identified is in flight), commit() fell through to the "Now playing"
  log and returned True, contradicting its documented False-on-discard contract.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.audio.capture import AudioCapture
from src.audio.silence import SilenceDetector, AudioEvent
from tests.factories import make_audio_config


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, seconds):
        self.t += seconds


def _chunk(rms_value, n=2048):
    """A constant-valued chunk whose RMS equals |rms_value|."""
    return np.full(n, rms_value, dtype=np.float32)


# ---------------------------------------------------------------------------
# #193 / #195 — the recognition gate (via the _dispatch_chunk seam).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_silent_chunk_is_classified_but_not_recognized():
    """A chunk the detector judges NOT music is still classified (drives the
    session lifecycle) but is NOT enqueued for recognition (#193)."""
    silence = MagicMock()
    silence.is_music_playing = False
    cap = AudioCapture(make_audio_config(), silence, MagicMock())
    cap.recognizer.enqueue = AsyncMock()

    chunk = _chunk(0.0)
    await cap._dispatch_chunk(chunk, cap.sample_rate)

    silence.process.assert_called_once_with(chunk, cap.sample_rate)
    cap.recognizer.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_music_chunk_is_recognized():
    """A chunk the detector judges music IS enqueued for recognition (guards
    against over-gating that would break identification)."""
    silence = MagicMock()
    silence.is_music_playing = True
    cap = AudioCapture(make_audio_config(), silence, MagicMock())
    cap.recognizer.enqueue = AsyncMock()

    chunk = _chunk(0.5)
    await cap._dispatch_chunk(chunk, cap.sample_rate)

    cap.recognizer.enqueue.assert_awaited_once_with(chunk, cap.sample_rate)


@pytest.mark.asyncio
async def test_sub_threshold_audio_is_not_recognized_with_a_real_detector():
    """#195 root cause: with a REAL SilenceDetector, low-gain audio (RMS below
    silence_threshold_rms) is judged silence, so recognition is not dispatched —
    no phantom session can start, so none can become immortal."""
    cfg = make_audio_config(silence_threshold_rms=0.01)
    det = SilenceDetector(cfg)
    cap = AudioCapture(cfg, det, MagicMock())
    cap.recognizer.enqueue = AsyncMock()

    await cap._dispatch_chunk(_chunk(0.005), cfg.sample_rate)   # RMS 0.005 < 0.01

    assert det.is_music_playing is False
    cap.recognizer.enqueue.assert_not_called()


def test_session_end_is_unreachable_without_a_music_transition():
    """#195 mechanism: SESSION_ENDED can only fire after a music→silence
    transition arms _silence_since. If recognition were allowed to start a
    session while the detector never saw music (the bug the gate prevents), that
    session would be immortal. This pins the invariant the gate relies on."""
    cfg = make_audio_config(silence_threshold_rms=0.01, session_end_silence_seconds=1)
    det = SilenceDetector(cfg)
    events = []
    det.on_event(events.append)

    # Only ever sub-threshold audio: the detector never enters music-state.
    for _ in range(5):
        det.process(_chunk(0.005), cfg.sample_rate)
    det.tick()   # wall-clock evaluation

    assert AudioEvent.MUSIC_STARTED not in events
    assert AudioEvent.SESSION_ENDED not in events   # never armed → never fires


# ---------------------------------------------------------------------------
# #195 — the low-gain diagnostic.
# ---------------------------------------------------------------------------

def test_low_gain_diagnostic_warns_on_sustained_sub_threshold_audio(monkeypatch, caplog):
    """Audio persistently present but below the silence threshold (a miscalibrated
    preamp) emits a throttled WARNING so the operator sees WHY nothing is tracked
    instead of a silent, invisible failure."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.02)
    det = SilenceDetector(cfg)

    with caplog.at_level(logging.WARNING):
        # Sub-threshold but clearly audible: RMS 0.008 is above the low-gain floor
        # (0.02 * 0.25 = 0.005) and below threshold 0.02. First chunk only ARMS
        # the timer.
        det.process(_chunk(0.008), cfg.sample_rate)
        assert not any("gain" in r.message.lower() for r in caplog.records)

        # A second chunk still WITHIN the sustain window must NOT warn — the band
        # has to be held for _LOW_GAIN_WARN_SECONDS, not merely observed twice.
        clock.advance(silence_mod._LOW_GAIN_WARN_SECONDS - 1)
        det.process(_chunk(0.008), cfg.sample_rate)
        assert not any("gain" in r.message.lower() for r in caplog.records), \
            "must not warn until the band is held for the full sustain window"

        # Past the window → warn.
        clock.advance(2)
        det.process(_chunk(0.008), cfg.sample_rate)

    assert any("gain" in r.message.lower() for r in caplog.records), \
        "sustained sub-threshold audio must warn about possible low gain"


def test_low_gain_timer_resets_when_audio_leaves_the_band(monkeypatch, caplog):
    """Leaving the sub-threshold band (real silence, or music) RESETS the sustain
    timer, so a brief dip doesn't get credited toward a later stretch and warn
    prematurely."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.02)
    det = SilenceDetector(cfg)

    with caplog.at_level(logging.WARNING):
        det.process(_chunk(0.008), cfg.sample_rate)        # arm at t0
        clock.advance(silence_mod._LOW_GAIN_WARN_SECONDS - 1)
        det.process(_chunk(0.0001), cfg.sample_rate)       # true silence → RESET
        clock.advance(2)                                   # now > window since t0…
        det.process(_chunk(0.008), cfg.sample_rate)        # …but the timer restarted

    assert not any("gain" in r.message.lower() for r in caplog.records), \
        "a reset must restart the sustain clock, not warn on the old arming time"


def test_low_gain_warning_is_throttled(monkeypatch, caplog):
    """Once warned, a still-sub-threshold stream must NOT re-warn every chunk — it
    re-logs at most once per _LOW_GAIN_WARN_INTERVAL_SECONDS (the #178 flood-guard
    the code cites). Without the throttle this is ~6 identical WARNINGs/min."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.02)
    det = SilenceDetector(cfg)

    def gain_warnings():
        return [r for r in caplog.records if "gain" in r.message.lower()]

    with caplog.at_level(logging.WARNING):
        det.process(_chunk(0.008), cfg.sample_rate)                       # arm
        clock.advance(silence_mod._LOW_GAIN_WARN_SECONDS + 1)
        det.process(_chunk(0.008), cfg.sample_rate)                       # warn #1
        assert len(gain_warnings()) == 1

        # Another in-band chunk within the re-log interval: suppressed.
        clock.advance(silence_mod._LOW_GAIN_WARN_INTERVAL_SECONDS - 1)
        det.process(_chunk(0.008), cfg.sample_rate)
        assert len(gain_warnings()) == 1, "must not re-warn within the throttle interval"

        # Past the interval: one more warning, not a per-chunk flood.
        clock.advance(2)
        det.process(_chunk(0.008), cfg.sample_rate)
        assert len(gain_warnings()) == 2


def test_low_gain_diagnostic_silent_on_true_silence(monkeypatch, caplog):
    """Near-zero audio (real silence, below the low-gain floor) must NOT warn."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.02)
    det = SilenceDetector(cfg)

    with caplog.at_level(logging.WARNING):
        det.process(_chunk(0.0001), cfg.sample_rate)       # below the floor
        clock.advance(silence_mod._LOW_GAIN_WARN_SECONDS + 1)
        det.process(_chunk(0.0001), cfg.sample_rate)

    assert not any("gain" in r.message.lower() for r in caplog.records)


def test_low_gain_diagnostic_silent_when_music_plays(monkeypatch, caplog):
    """Normal music (RMS at/above threshold) must NOT warn about low gain."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.02)
    det = SilenceDetector(cfg)

    with caplog.at_level(logging.WARNING):
        det.process(_chunk(0.5), cfg.sample_rate)          # clearly music
        clock.advance(silence_mod._LOW_GAIN_WARN_SECONDS + 1)
        det.process(_chunk(0.5), cfg.sample_rate)

    assert not any("gain" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# #195 (bundle 1b) — wall-clock max-session safety net.
# ---------------------------------------------------------------------------

def test_max_session_forces_end_on_continuous_music(monkeypatch):
    """A locked groove / stuck input keeps RMS above the exit threshold forever,
    so the normal music→silence transition never fires and the side never
    credits. The wall-clock safety must force SESSION_ENDED after
    _MAX_MUSIC_SECONDS of continuous music."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.01)
    det = SilenceDetector(cfg)
    events = []
    det.on_event(events.append)

    det.process(_chunk(0.5), cfg.sample_rate)              # music starts
    assert AudioEvent.MUSIC_STARTED in events
    # Advance to EXACTLY the max: the bound is inclusive (`>=`), so this must fire
    # (pins the boundary — a `>` off-by-one would not).
    clock.advance(silence_mod._MAX_MUSIC_SECONDS)
    det.process(_chunk(0.5), cfg.sample_rate)              # still music (locked groove)

    # R8-16 (#350): the forced end is its OWN event — the tracker credits on it
    # but must not treat it as a physical spin boundary (a genuine-silence
    # SESSION_ENDED here would clear the per-spin credit memory and let a
    # still-identified groove re-credit hourly).
    assert AudioEvent.SESSION_ENDED_FORCED in events       # forced end → side credits
    assert AudioEvent.SESSION_ENDED not in events          # …but NOT a spin boundary


def test_max_session_also_fires_from_tick(monkeypatch):
    """The safety must fire on wall-clock time even if no new chunk arrives
    (capture stalled mid-session), via tick()."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.01)
    det = SilenceDetector(cfg)
    events = []
    det.on_event(events.append)

    det.process(_chunk(0.5), cfg.sample_rate)
    clock.advance(silence_mod._MAX_MUSIC_SECONDS + 1)
    det.tick()

    assert AudioEvent.SESSION_ENDED_FORCED in events       # R8-16: forced variant


def test_normal_music_under_max_is_not_force_ended(monkeypatch, caplog):
    """A legitimately long-but-under-max session must NOT be force-ended."""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.01)
    det = SilenceDetector(cfg)
    events = []
    det.on_event(events.append)

    with caplog.at_level(logging.WARNING):
        det.process(_chunk(0.5), cfg.sample_rate)
        clock.advance(silence_mod._MAX_MUSIC_SECONDS - 100)
        det.process(_chunk(0.5), cfg.sample_rate)
        det.tick()

    assert AudioEvent.SESSION_ENDED not in events
    assert not any("forc" in r.message.lower() for r in caplog.records)


def test_max_session_clock_resets_after_a_natural_silence(monkeypatch, caplog):
    """The max-session clock must restart on each music run — a natural
    music→silence→music cycle must not carry the first run's age into the second
    and force-end prematurely. (The restart is effected by the silence→music
    transition re-stamping _music_since; the music→silence reset is defensive and
    non-observable, so this pins the observable per-run restart, not that line.)"""
    import src.audio.silence as silence_mod
    clock = _Clock()
    monkeypatch.setattr(silence_mod.time, "monotonic", clock)

    cfg = make_audio_config(silence_threshold_rms=0.01)
    det = SilenceDetector(cfg)

    with caplog.at_level(logging.WARNING):
        det.process(_chunk(0.5), cfg.sample_rate)             # music run #1 (t0)
        clock.advance(silence_mod._MAX_MUSIC_SECONDS - 50)
        det.process(_chunk(0.0), cfg.sample_rate)             # natural music→silence
        clock.advance(100)
        det.process(_chunk(0.5), cfg.sample_rate)             # music run #2 (fresh clock)
        clock.advance(silence_mod._MAX_MUSIC_SECONDS - 50)    # <max since run #2
        det.tick()

    assert not any("forc" in r.message.lower() for r in caplog.records), \
        "the max-session clock must restart on each music run, not accumulate"


# ---------------------------------------------------------------------------
# #195 (bundle 1b) — recognized-in-silence tripwire.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recognition_without_a_music_transition_is_a_softened_tripwire(caplog):
    """Defense in depth: with the gate, MUSIC_STARTED always precedes recognition,
    so a session already exists when a track is confirmed. If on_track_identified
    has to CREATE the session itself, recognition happened without a music
    transition (the #195 signature) — still surfaced, but R6-10 softened it from a
    WARNING that cried 'check the wiring' every time to an INFO line naming the
    KNOWN benign SESSION_ENDED/MUSIC_STARTED same-turn interleave (which
    self-heals, no play lost)."""
    from tests.test_listen_tracker import make_tracker, make_track

    tracker, _ = make_tracker()
    # No on_silence_event(MUSIC_STARTED) → no session yet.
    with caplog.at_level(logging.INFO):
        await tracker.on_track_identified(make_track("Master-Dik"))

    assert tracker._session is not None                       # still starts (no data lost)
    trip = [r for r in caplog.records
            if "no active session" in r.message.lower() and "R6-10" in r.message]
    assert len(trip) == 1, "the #195 tripwire must still surface (naming R6-10's benign case)"
    assert trip[0].levelno == logging.INFO, "softened from WARNING to INFO (R6-10)"


# ---------------------------------------------------------------------------
# #196 — commit() honours its False-on-discard contract on the stale path.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_returns_false_and_no_now_playing_on_stale_drop(caplog):
    """When the session ends WHILE on_track_identified is in flight, commit() must
    return False and must NOT log 'Now playing' — the stale track was already
    cleared off the screen (#196)."""
    from src.app.track_commit_service import TrackCommitService
    from src.state.player_state import PlayerState, PlayerStatus
    from tests.test_track_commit_service import make_raw

    state = PlayerState()
    state.set_status(PlayerStatus.LISTENING)
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=MagicMock())
    tracker = MagicMock()

    async def end_during_tail(metadata, is_stale=None):
        state.clear()   # needle lifts mid-tracker → epoch bumps

    tracker.on_track_identified = AsyncMock(side_effect=end_during_tail)
    service = TrackCommitService(state, resolver, tracker, None)

    with caplog.at_level(logging.INFO):
        committed = await service.commit(make_raw(), state.session_epoch)

    assert committed is False                                   # honours the contract
    assert not any("Now playing" in r.message for r in caplog.records)
