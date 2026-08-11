"""Regression tests for T-1 — main.py wiring + shutdown had zero coverage.

Covers the pieces extracted from main() for testability:
  - apply_state_silence_effect: the IDLE/ERROR → LISTENING transition and
    SESSION_ENDED → clear() (the exact paths the B-1 epoch guard relies on).
  - wire_silence_listeners: the two-listener split (CRIT-5) — state and tracker
    are separate Signal listeners (log-and-continue between them), state first.
  - run_pipeline: FIRST_COMPLETED shutdown — pending legs cancelled, a faulted
    leg's exception re-raised, capture/display stopped in the finally.
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

# main.py imports AudioCapture, which imports sounddevice (needs PortAudio at
# import time).  The stub is installed in the root conftest.py before any test
# module is imported (TQ-6), so `from main import ...` succeeds without the
# audio stack; conftest leaves a real sounddevice untouched where it exists.
from main import (
    apply_state_silence_effect, wire_silence_listeners, run_pipeline,
    install_io_executor, _IO_EXECUTOR_MAX_WORKERS,
    build_components, start_display, main,
)
import main as main_module
from src.audio.silence import AudioEvent
from src.state.player_state import PlayerState, PlayerStatus
from src.util.signal import Signal
from tests.factories import (
    make_audio_config, make_discogs_config, make_display_config,
    make_recognition_config, make_lastfm_config,
)


def _app_config(**display_overrides):
    """Assemble a full AppConfig from the section factories (there is no
    make_app_config).  display_overrides let a test point cover_art_cache_dir at
    a writable temp dir (happy path) or an uncreatable one (failure path)."""
    from src.config import AppConfig
    return AppConfig(
        audio=make_audio_config(),
        discogs=make_discogs_config(),
        display=make_display_config(**display_overrides),
        recognition=make_recognition_config(),
        lastfm=make_lastfm_config(),
    )


class _SignalSilence:
    """Stand-in for SilenceDetector exposing the same on_event/emit over a REAL
    Signal, so the two-listener wiring (CRIT-5) is exercised through the actual
    log-and-continue delivery path rather than a mock."""

    def __init__(self):
        self._sig = Signal("test-silence")

    def on_event(self, cb):
        self._sig.connect(cb)

    def emit(self, event):
        self._sig.emit(event)


# ---------------------------------------------------------------------------
# apply_state_silence_effect — the player-state half (CRIT-5)
# ---------------------------------------------------------------------------

def test_music_started_from_idle_enters_listening():
    state = PlayerState()
    apply_state_silence_effect(AudioEvent.MUSIC_STARTED, state)
    assert state.status == PlayerStatus.LISTENING


def test_music_started_from_error_enters_listening():
    state = PlayerState()
    state.set_status(PlayerStatus.ERROR)
    apply_state_silence_effect(AudioEvent.MUSIC_STARTED, state)
    assert state.status == PlayerStatus.LISTENING


def test_music_started_during_playing_keeps_now_playing_card():
    state = PlayerState()
    state.set_status(PlayerStatus.PLAYING)
    apply_state_silence_effect(AudioEvent.MUSIC_STARTED, state)
    assert state.status == PlayerStatus.PLAYING  # not dropped to LISTENING


def test_session_ended_clears_and_bumps_epoch():
    state = PlayerState()
    state.set_status(PlayerStatus.PLAYING)
    epoch0 = state.session_epoch
    apply_state_silence_effect(AudioEvent.SESSION_ENDED, state)
    assert state.status == PlayerStatus.IDLE
    assert state.session_epoch == epoch0 + 1  # B-1 epoch advances on clear()


# ---------------------------------------------------------------------------
# wire_silence_listeners — the two-listener split (CRIT-5)
# ---------------------------------------------------------------------------

def test_wire_silence_listeners_registers_two_separate_listeners():
    """The state and tracker effects are TWO separately-registered Signal
    listeners, so log-and-continue applies between them."""
    sig = Signal("s")
    silence = MagicMock()
    silence.on_event.side_effect = sig.connect
    wire_silence_listeners(silence, PlayerState(), MagicMock())
    assert len(sig) == 2


def test_state_cleared_before_tracker_end_is_scheduled_on_session_ended():
    """CRIT-5: on SESSION_ENDED the player state is cleared (epoch bump) BEFORE
    the tracker's end runs, so the epoch bump precedes the session detach."""
    order = []
    state = MagicMock()
    state.status = PlayerStatus.PLAYING
    state.clear.side_effect = lambda: order.append("state.clear")
    tracker = MagicMock()
    tracker.on_silence_event.side_effect = lambda e: order.append("tracker")

    silence = _SignalSilence()
    wire_silence_listeners(silence, state, tracker)
    silence.emit(AudioEvent.SESSION_ENDED)

    assert order == ["state.clear", "tracker"]        # state first, then tracker
    tracker.on_silence_event.assert_called_once_with(AudioEvent.SESSION_ENDED)


def test_tracker_fault_on_session_ended_still_clears_state():
    """CRIT-5: a raise in the tracker's SESSION_ENDED handler must NOT prevent the
    player state from clearing — otherwise the B-1 epoch never bumps and the
    now-playing card is stranded. The split + Signal log-and-continue guarantees
    it. (On the old single combined listener the raise skipped state.clear().)"""
    state = PlayerState()
    state.set_status(PlayerStatus.PLAYING)             # a now-playing card is on screen
    epoch0 = state.session_epoch
    tracker = MagicMock()
    tracker.on_silence_event.side_effect = RuntimeError("tracker blew up")

    silence = _SignalSilence()
    wire_silence_listeners(silence, state, tracker)
    silence.emit(AudioEvent.SESSION_ENDED)             # log-and-continue swallows the raise

    assert state.status == PlayerStatus.IDLE           # cleared DESPITE the tracker fault
    assert state.session_epoch == epoch0 + 1           # B-1 epoch advanced
    tracker.on_silence_event.assert_called_once_with(AudioEvent.SESSION_ENDED)


def test_state_fault_does_not_skip_the_tracker():
    """The converse: a fault in the state half must not skip the tracker half —
    Signal log-and-continue protects each listener from the other."""
    state = MagicMock()
    state.status = PlayerStatus.PLAYING
    state.clear.side_effect = RuntimeError("state blew up")
    tracker = MagicMock()

    silence = _SignalSilence()
    wire_silence_listeners(silence, state, tracker)
    silence.emit(AudioEvent.SESSION_ENDED)

    tracker.on_silence_event.assert_called_once_with(AudioEvent.SESSION_ENDED)


# ---------------------------------------------------------------------------
# run_pipeline
# ---------------------------------------------------------------------------

def _drainable_tracker():
    """A tracker stub whose drain() is an awaitable no-op — run_pipeline awaits
    it on shutdown so an in-flight end-of-session credit isn't torn (CONC-1)."""
    t = MagicMock()
    t.drain = AsyncMock()
    return t


@pytest.mark.asyncio
async def test_run_pipeline_cancels_pending_and_stops():
    capture, display = MagicMock(), MagicMock()

    async def quick():
        return

    async def forever():
        await asyncio.sleep(3600)

    done_leg = asyncio.create_task(quick())
    pending_leg = asyncio.create_task(forever())

    await run_pipeline([done_leg, pending_leg], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

    assert pending_leg.cancelled()
    capture.stop.assert_called_once()
    display.stop.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_drains_then_stops_subsystems_then_closes_pools():
    """CONC-1 + #61 + CRIT-3: shutdown must (1) await the tracker's in-flight
    credit tasks (drain) BEFORE tearing down capture/display, and (2) close BOTH
    thread pools LAST — after drain, because those credit writes run on them (the
    Discogs pool via writer.run, the shared I/O pool via lastfm.love), so closing
    earlier could reject an in-flight write. The owned I/O pool is shut with
    cancel_futures so queued work is dropped. Deleting either close (or reordering
    before drain) fails this test."""
    capture, display = MagicMock(), MagicMock()
    tracker = _drainable_tracker()
    discogs_http = MagicMock()
    io_executor = MagicMock()
    order = []
    tracker.drain.side_effect = lambda *a, **k: order.append("drain")
    capture.stop.side_effect = lambda: order.append("capture.stop")
    display.stop.side_effect = lambda: order.append("display.stop")
    discogs_http.close.side_effect = lambda: order.append("discogs.close")
    io_executor.shutdown.side_effect = lambda *a, **k: order.append("io.shutdown")

    async def quick():
        return

    await run_pipeline([asyncio.create_task(quick())], capture, display, tracker, discogs_http, io_executor)

    tracker.drain.assert_awaited_once()
    discogs_http.close.assert_called_once()
    io_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert order[0] == "drain"                 # credit finishes before anything else
    assert order == ["drain", "capture.stop", "display.stop", "discogs.close", "io.shutdown"]


@pytest.mark.asyncio
async def test_run_pipeline_still_drains_when_a_leg_faults():
    """A faulted leg must not abandon an in-flight credit — drain still runs in
    the finally before the exception propagates."""
    capture, display = MagicMock(), MagicMock()
    tracker = _drainable_tracker()

    async def boom():
        raise RuntimeError("leg died")

    discogs_http = MagicMock()
    with pytest.raises(RuntimeError, match="leg died"):
        await run_pipeline([asyncio.create_task(boom())], capture, display, tracker, discogs_http, MagicMock())

    tracker.drain.assert_awaited_once()
    capture.stop.assert_called_once()
    display.stop.assert_called_once()
    # #61: a faulted leg must not leak the dedicated executor — close() is in the
    # same finally, so it runs before the exception propagates.
    discogs_http.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_logs_a_pending_leg_that_raises_while_unwinding(caplog):
    """R5-25: a pending leg that raises a NON-CancelledError while unwinding its
    cancellation (e.g. a finally that fails during shutdown) was captured by the
    gather and silently discarded. It must be logged."""
    import logging as _logging
    capture, display = MagicMock(), MagicMock()

    async def finisher():
        return None                      # completes first → triggers shutdown

    async def bad_on_cancel():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup exploded")   # non-cancel error while unwinding

    done_leg = asyncio.create_task(finisher())
    pending_leg = asyncio.create_task(bad_on_cancel())
    with caplog.at_level(_logging.ERROR):
        await run_pipeline([done_leg, pending_leg], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

    assert any("unwinding shutdown" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_pipeline_reraises_faulted_leg_and_still_cleans_up():
    capture, display = MagicMock(), MagicMock()

    async def boom():
        raise RuntimeError("leg died")

    async def forever():
        await asyncio.sleep(3600)

    boom_leg = asyncio.create_task(boom())
    pending_leg = asyncio.create_task(forever())

    with pytest.raises(RuntimeError, match="leg died"):
        await run_pipeline([boom_leg, pending_leg], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

    # finally ran despite the re-raise…
    capture.stop.assert_called_once()
    display.stop.assert_called_once()
    # …and the other leg was cancelled.
    assert pending_leg.cancelled()


@pytest.mark.asyncio
async def test_run_pipeline_logs_every_faulted_leg(caplog):
    """B-14: when several legs die at once, ALL their exceptions are logged
    (not just the first), and one is still re-raised."""
    import logging

    capture, display = MagicMock(), MagicMock()

    async def boom_a():
        raise RuntimeError("leg A died")

    async def boom_b():
        raise ValueError("leg B died")

    leg_a = asyncio.create_task(boom_a(), name="legA")
    leg_b = asyncio.create_task(boom_b(), name="legB")
    # Let both finish so both land in `done` deterministically.
    await asyncio.gather(leg_a, leg_b, return_exceptions=True)

    with caplog.at_level(logging.ERROR):
        with pytest.raises((RuntimeError, ValueError)):
            await run_pipeline([leg_a, leg_b], capture, display, _drainable_tracker(), MagicMock(), MagicMock())

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "leg A died" in logged
    assert "leg B died" in logged
    capture.stop.assert_called_once()
    display.stop.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_shuts_down_the_io_executor_dropping_queued_work():
    """CRIT-3: run_pipeline owns the bounded I/O executor and shuts it down with
    cancel_futures at the end of teardown — so QUEUED blocking work is DROPPED at
    exit rather than waited on. On the interpreter's default pool that queued work
    gated process exit (asyncio.run awaits shutdown_default_executor, a wait=True
    join with no timeout)."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    io_executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    ran_queued = []

    def occupy():
        started.set()
        release.wait(timeout=2)

    def queued():
        ran_queued.append("ran")

    io_executor.submit(occupy)
    assert started.wait(timeout=1)       # the single worker is busy
    io_executor.submit(queued)           # this one sits in the queue

    capture, display = MagicMock(), MagicMock()

    async def quick():
        return

    await run_pipeline(
        [asyncio.create_task(quick())], capture, display,
        _drainable_tracker(), MagicMock(), io_executor,
    )

    # The executor was shut down — new submissions are rejected…
    with pytest.raises(RuntimeError):
        io_executor.submit(lambda: None)
    # …and the queued work was cancelled (cancel_futures), never run.
    release.set()
    time.sleep(0.1)
    assert ran_queued == [], "queued blocking work was NOT cancelled at shutdown"


# ---------------------------------------------------------------------------
# ARCH-10 — construction + display.start() are guarded with an actionable
# first-boot message instead of a bare traceback
# ---------------------------------------------------------------------------

def test_start_display_reraises_with_actionable_operator_message(caplog):
    """A pygame display-init failure (no HDMI / X down on :0) must be logged with
    a concrete remedy pointing at the checklist BEFORE it re-raises — not surface
    as a bare pygame.error traceback naming no fix (ARCH-10)."""
    import logging

    class BoomDisplay:
        def start(self):
            raise RuntimeError("No available video device")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="No available video device"):
            start_display(BoomDisplay())

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "HDMI" in msg
    assert "first-boot-checklist.md" in msg


def test_start_display_happy_path_calls_start():
    display = MagicMock()
    start_display(display)
    display.start.assert_called_once()


def test_build_components_reraises_with_actionable_message_on_unwritable_cache_dir(caplog, tmp_path):
    """The most concrete first-boot construction failure: cover_art_cache_dir is
    not creatable (read-only path, or — here — a file where a directory must go),
    so CoverArtCache.__init__ mkdir raises OSError. build_components must log an
    actionable message naming the setting + checklist, then re-raise (ARCH-10)."""
    import logging

    blocker = tmp_path / "afile"
    blocker.write_text("")                      # a FILE where a directory must go
    bad_dir = str(blocker / "cache")            # mkdir under a file -> OSError
    cfg = _app_config(cover_art_cache_dir=bad_dir)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(OSError):
            build_components(cfg, PlayerState())

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "cover_art_cache_dir" in msg
    assert "first-boot-checklist.md" in msg


def test_build_components_returns_wired_bundle(tmp_path):
    """Happy path: with a writable cache dir every component is constructed and
    returned in the bundle main() consumes.  Assert the TYPE of each field, not
    merely non-None, so a mis-wire to another constructed object (e.g.
    capture=display) can't slip through."""
    from src.metadata.discogs import DiscogsHttp
    from src.display.renderer import DisplayRenderer
    from src.audio.silence import SilenceDetector
    from src.audio.recognizer import RecognitionLoop
    from src.audio.capture import AudioCapture
    from src.tracking.listen_tracker import ListenTracker

    cfg = _app_config(cover_art_cache_dir=str(tmp_path / "cache"))
    components = build_components(cfg, PlayerState())
    assert isinstance(components.discogs_http, DiscogsHttp)
    assert isinstance(components.display, DisplayRenderer)
    assert isinstance(components.silence, SilenceDetector)
    assert isinstance(components.recognizer, RecognitionLoop)
    assert isinstance(components.capture, AudioCapture)
    assert isinstance(components.tracker, ListenTracker)


@pytest.mark.asyncio
async def test_install_io_executor_routes_default_run_in_executor():
    """CRIT-3: after install_io_executor, every run_in_executor(None, …) runs on
    the OWNED bounded pool (thread-name prefix 'vnp-io'), not the interpreter's
    default — so the cancel_futures shutdown actually governs those calls. Without
    the set_default_executor inside, this routing is lost and the fix is inert."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    ex = install_io_executor(loop)
    try:
        assert isinstance(ex, ThreadPoolExecutor)
        assert ex._max_workers == _IO_EXECUTOR_MAX_WORKERS   # bounded, owned pool

        seen = {}

        def work():
            seen["thread"] = threading.current_thread().name

        await loop.run_in_executor(None, work)   # None → loop default → owned pool
        assert seen["thread"].startswith("vnp-io"), seen
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# TQ-2 — main() itself: the config-error startup guard and the SIGINT/SIGTERM
# shutdown wiring were entirely uncovered (the extracted helpers above were
# tested, but not main()'s own body).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_exits_ex_config_78_on_config_error(monkeypatch):
    """R6-27: a ConfigError from load_config becomes sys.exit(78) (EX_CONFIG), not a
    traceback and not exit 1 — so the unit's RestartPreventExitStatus=78 parks a
    permanently-bad config instead of crash-looping it."""
    from src.config import ConfigError

    def boom():
        raise ConfigError("bad config.yaml")

    monkeypatch.setattr(main_module, "load_config", boom)
    with pytest.raises(SystemExit) as exc_info:
        await main()
    assert exc_info.value.code == 78
    assert exc_info.value.code == main_module._EXIT_CONFIG_ERROR


@pytest.mark.asyncio
async def test_main_registers_signal_handlers_and_cancel_all_cancels_tasks(monkeypatch):
    """main() must register a SIGINT and SIGTERM handler, and that handler
    (_cancel_all) must cancel every pipeline task."""
    import signal as signal_module

    monkeypatch.setattr(main_module, "load_config", lambda: MagicMock())
    # #198/#202: stub the new startup steps — their own behaviour is covered in
    # tests/test_main_startup_hardening.py; here they'd otherwise run against a
    # MagicMock config and leave a redaction filter on the global root handler.
    monkeypatch.setattr(main_module, "verify_recognition_backend_importable", lambda config: None)
    monkeypatch.setattr(main_module, "install_secret_redaction", lambda config: None)
    monkeypatch.setattr(main_module, "install_io_executor", lambda loop: MagicMock())
    monkeypatch.setattr(main_module, "wire_silence_listeners", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "start_display", lambda display: None)
    monkeypatch.setattr(main_module, "read_version", lambda: "test")

    async def _leg():
        await asyncio.sleep(3600)   # long-lived; only ends via cancel

    comps = MagicMock()
    comps.capture.run = _leg
    comps.recognizer.run = _leg
    comps.display.run = _leg
    monkeypatch.setattr(main_module, "build_components", lambda config, state: comps)

    recorded = {"handlers": {}, "tasks": None}

    async def fake_run_pipeline(tasks, *a, **k):
        recorded["tasks"] = list(tasks)   # capture without awaiting the legs

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)

    # Patch add_signal_handler on the REAL running loop (main() uses this loop
    # for create_task), so signal registration is captured without touching the
    # process's actual signal disposition.
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler",
        lambda sig, handler, *a: recorded["handlers"].__setitem__(sig, handler),
    )

    await main()

    # Both signals registered, to the same _cancel_all closure.
    assert set(recorded["handlers"]) == {signal_module.SIGINT, signal_module.SIGTERM}
    cancel_all = recorded["handlers"][signal_module.SIGINT]
    assert recorded["handlers"][signal_module.SIGTERM] is cancel_all

    # _cancel_all cancels every pipeline task.
    tasks = recorded["tasks"]
    assert len(tasks) == 3
    assert not any(t.cancelled() for t in tasks)
    cancel_all()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(t.cancelled() for t in tasks)


# ---------------------------------------------------------------------------
# #170 — a startup abort BEFORE run_pipeline must still close the pools main()
# owns (io_executor always; discogs_http if components were built), instead of
# leaking them to concurrent.futures' atexit join.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_shuts_io_executor_when_build_components_aborts(monkeypatch):
    """build_components raises (e.g. an unwritable cover_art_cache_dir) before
    run_pipeline is entered → its cleanup finally never runs, so main() must shut
    down the io_executor it created. components is None here, so discogs_http has
    nothing to close."""
    monkeypatch.setattr(main_module, "load_config", lambda: MagicMock())
    io_executor = MagicMock()
    monkeypatch.setattr(main_module, "install_io_executor", lambda loop: io_executor)
    monkeypatch.setattr(
        main_module, "build_components",
        MagicMock(side_effect=RuntimeError("unwritable cover_art_cache_dir")),
    )

    with pytest.raises(RuntimeError, match="unwritable cover_art_cache_dir"):
        await main()

    io_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_main_closes_both_pools_when_start_display_aborts(monkeypatch):
    """start_display raises (no HDMI / X down) AFTER components are built but
    before run_pipeline → main() must close BOTH the discogs_http pool and the
    io_executor on the abort path."""
    monkeypatch.setattr(main_module, "load_config", lambda: MagicMock())
    io_executor = MagicMock()
    monkeypatch.setattr(main_module, "install_io_executor", lambda loop: io_executor)
    components = MagicMock()
    monkeypatch.setattr(main_module, "build_components", lambda config, state: components)
    monkeypatch.setattr(main_module, "wire_silence_listeners", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "read_version", lambda: "test")
    monkeypatch.setattr(
        main_module, "start_display",
        MagicMock(side_effect=RuntimeError("no HDMI / X down")),
    )

    with pytest.raises(RuntimeError, match="no HDMI"):
        await main()

    components.discogs_http.close.assert_called_once()
    io_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_main_normal_path_does_not_double_close_pools(monkeypatch):
    """#170: when run_pipeline IS entered it owns pool cleanup (its own finally),
    so main()'s abort-path finally must NOT close them a second time. Pins the
    `started_pipeline` gate — a mutation that never sets the flag double-closes and
    this fails."""
    monkeypatch.setattr(main_module, "load_config", lambda: MagicMock())
    io_executor = MagicMock()
    monkeypatch.setattr(main_module, "install_io_executor", lambda loop: io_executor)
    components = MagicMock()

    async def _leg():
        await asyncio.sleep(3600)   # long-lived; captured + cancelled below
    components.capture.run = _leg
    components.recognizer.run = _leg
    components.display.run = _leg
    monkeypatch.setattr(main_module, "build_components", lambda config, state: components)
    monkeypatch.setattr(main_module, "wire_silence_listeners", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "start_display", lambda display: None)
    monkeypatch.setattr(main_module, "read_version", lambda: "test")

    recorded = {}

    async def fake_run_pipeline(tasks, *a, **k):
        recorded["tasks"] = list(tasks)   # entered → run_pipeline OWNS cleanup (mocked away)

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: None)

    await main()

    # run_pipeline was entered (started_pipeline=True) → main()'s finally is gated off.
    io_executor.shutdown.assert_not_called()
    components.discogs_http.close.assert_not_called()

    for t in recorded["tasks"]:          # tidy the still-pending legs
        t.cancel()
    await asyncio.gather(*recorded["tasks"], return_exceptions=True)


# ---------------------------------------------------------------------------
# R6-26 (#291) — an uncaught exception's traceback is scrubbed (last secret sink).
# ---------------------------------------------------------------------------

def test_r6_26_redactor_scrub_redacts_arbitrary_text():
    """R6-26: the redactor exposes scrub() for non-log text — a crash traceback,
    which bypasses the logging filter entirely."""
    f = main_module._SecretRedactingFilter(["SUPERSECRETTOKEN"])
    out = f.scrub("HTTPError url: /database/search?token=SUPERSECRETTOKEN&q=x SUPERSECRETTOKEN")
    assert "SUPERSECRETTOKEN" not in out
    assert "<redacted>" in out


def test_r6_26_run_scrubbed_redacts_an_uncaught_traceback(monkeypatch, capsys):
    """R6-26: an uncaught exception's traceback is rendered through the scrub before
    it reaches stderr/journald, and the process exits non-zero."""
    monkeypatch.setattr(
        main_module, "_REDACTOR", main_module._SecretRedactingFilter(["LEAKY_TOKEN_123"])
    )
    monkeypatch.setattr(main_module, "main", lambda: None)   # no un-awaited coroutine

    def _boom(_coro):
        raise RuntimeError("crash with token=LEAKY_TOKEN_123 in the url")
    monkeypatch.setattr(main_module.asyncio, "run", _boom)

    with pytest.raises(SystemExit) as ei:
        main_module._run_scrubbed()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "LEAKY_TOKEN_123" not in err       # scrubbed before stderr
    assert "<redacted>" in err


def test_r6_26_run_scrubbed_passes_system_exit_through(monkeypatch):
    """A clean SystemExit (the ConfigError EX_CONFIG path) must pass through
    untouched — its exit code and the parked-service behaviour are preserved."""
    monkeypatch.setattr(main_module, "main", lambda: None)

    def _cfg_exit(_coro):
        raise SystemExit(78)
    monkeypatch.setattr(main_module.asyncio, "run", _cfg_exit)

    with pytest.raises(SystemExit) as ei:
        main_module._run_scrubbed()
    assert ei.value.code == 78
