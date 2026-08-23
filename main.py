"""vinyl-now-playing — entry point.

Wires all components together and starts the main event loop.

Shutdown design (v1.3.5)
------------------------
The three pipeline coroutines run as named tasks awaited with
asyncio.wait(return_when=FIRST_COMPLETED): the moment ANY leg exits — the
display closing on ESC/window-close, an unexpected coroutine death, or
SIGINT/SIGTERM cancelling everything — the remaining legs are cancelled and
main() unwinds through a finally block that stops capture and display.

History: v1.3.2 and earlier cancelled ALL tasks (including main() itself)
and called loop.stop() inside asyncio.run(), guaranteeing a RuntimeError
traceback on every Ctrl+C; v1.3.3 fixed that with a cancellable gather, but
gather waits for ALL legs — so closing the display via ESC left capture and
recognition running headless forever (the "ESC zombie", fixed in v1.3.5).
"""

import asyncio
import logging
import re
import signal
import sys
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.config import load_config, ConfigError
from src.app.track_commit_service import TrackCommitService
from src.metadata.discogs import DiscogsHttp, DiscogsReader, DiscogsCollectionWriter
# R9-13 (#396): AudioCapture is imported LAZILY where it is constructed, NOT at
# module level.  It pulls in `sounddevice` → the C library `libportaudio2`, which
# a fresh Raspberry Pi OS install lacks (a documented first-boot state).  A
# module-level import dies before main()'s try, so the ConfigError→exit-78 park
# never applies and systemd crash-loops to StartLimitBurst instead.  The startup
# probe `verify_audio_backend_importable` surfaces the failure as the friendly park.
from src.audio.silence import SilenceDetector, AudioEvent
from src.audio.recognizer import RecognitionLoop
from src.metadata.resolver import MetadataResolver
from src.display.renderer import DisplayRenderer
from src.state.player_state import PlayerState, PlayerStatus
from src.tracking.lastfm_client import LastFmClient
from src.tracking.listen_tracker import ListenTracker
from src.tracking.scrobble_dispatcher import ScrobbleDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

# EX_CONFIG (sysexits.h): a configuration error that will NOT self-heal on a
# restart. main() exits with this so the systemd unit's RestartPreventExitStatus=78
# can park the service instead of crash-looping a permanently-bad config (R6-27).
_EXIT_CONFIG_ERROR = 78

# R6-26: the installed secret-redacting filter, exposed at module scope so the
# __main__ crash handler can scrub an uncaught exception's traceback (which
# bypasses the logging filter). Set by install_secret_redaction once the config
# is loaded. It is None only during load_config + verify_recognition_backend_
# importable; a crash in that window IS written unscrubbed, but it carries no
# secret to leak — those steps make no token-bearing call, and a rendered
# traceback prints source + the exception message, never frame locals, so the
# token held in `config` is not emitted.
_REDACTOR = None

# CRIT-3: workers for the owned shared I/O pool (the loop's default executor).
# It serves every run_in_executor(None, …) blocking call that is NOT a Discogs
# write (which keeps its own dedicated 2-worker pool, #61): Last.fm scrobble/love,
# cover download, palette extraction, MusicBrainz cover-art lookup, and the
# recognition-hot-path WAV encode. Sized to match the interpreter default's width
# on the target hardware (min(32, cpu+4) ≈ 8 on a 4-core Pi) so OWNING the pool
# doesn't NARROW I/O concurrency versus the default it replaces — in particular so
# a burst of slow network calls (cover download + scrobble + love + MusicBrainz)
# can't queue the CPU-bound WAV encode behind them and delay recognition. It is
# still an explicit bound (never grows past this), so shutdown waits on at most
# this many RUNNING calls; cancel_futures drops the rest and TimeoutStopSec
# backstops a stuck one.
_IO_EXECUTOR_MAX_WORKERS = 8


def read_version() -> str:
    """Read the version string from the VERSION file at the repo root."""
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip()
    except Exception:
        return "unknown"


def verify_recognition_backend_importable(config, _import=None) -> None:
    """#198 (ops-1): fail LOUD at startup if the selected recognition backend's
    heavy dependencies can't be imported, instead of silently once per chunk.

    shazamio is imported LAZILY inside ShazamIOBackend (the A-13/#17 testability
    seam the suite relies on — the module must import without the audio stack), so
    a broken install surfaces only as a WARNING every ~10s chunk with the display
    latched to NO MATCH FOUND, while systemd sees a healthy service. The dominant
    cause is Python 3.13+ (the current default Raspberry Pi OS "trixie" image),
    where PEP 594 removed the stdlib `audioop` module that shazamio's `pydub`
    dependency imports. Probe the import once here so that failure becomes a
    ConfigError main() reports and exits non-zero on — which systemd surfaces —
    while the lazy import stays in place for the recognizer.

    ``_import`` is an injection seam for tests (defaults to importlib.import_module)
    so both the success and ImportError branches are exercisable without touching
    the real install.
    """
    if config.recognition.backend != "shazamio":
        return
    import importlib
    _import = _import or importlib.import_module
    try:
        _import("shazamio")
    except Exception as e:
        # R7-18: catch ANY import-time failure, not just ImportError. A broken
        # native dependency raises OSError ("libFLAC.so: cannot open shared object
        # file"), a mis-built wheel can raise RuntimeError/ValueError at import — all
        # the same reinstall-to-fix class this probe exists to surface. Without the
        # broadened catch they escape as a bare traceback → exit 1 → systemd
        # crash-loop, never the friendly exit-78 park. (Exception, not BaseException,
        # so KeyboardInterrupt / SystemExit still propagate.)
        raise ConfigError(
            f"The 'shazamio' recognition backend failed to import: {e}.\n"
            "  This is almost always Python 3.13+ (the current default Raspberry "
            "Pi OS 'trixie' image) missing the 'audioop' module that PEP 594 "
            "removed and shazamio's 'pydub' dependency imports at import time.\n"
            "  Fix: reinstall dependencies with 'pip install -r requirements.txt' "
            "(it now pulls in the 'audioop-lts' backport on Python 3.13+), or "
            "flash the 'Raspberry Pi OS (Legacy, 64-bit)' image (Python 3.11) — "
            "see docs/pi-setup-guide.md section 1."
        ) from e


def verify_audio_backend_importable(_import=None) -> None:
    """R9-13 (#396): probe the audio-capture C-extension dependency at startup.

    ``sounddevice`` imports the system library ``libportaudio2`` at import time,
    and a fresh Raspberry Pi OS install lacks it — a CAN-NEVER-SELF-HEAL config
    class exactly like the shazamio probe above.  ``AudioCapture`` is imported
    lazily (not at module level) so this failure surfaces HERE, inside main()'s
    try, as a ConfigError → the friendly exit-78 park — instead of a bare
    import-time traceback → exit 1 → systemd crash-loop to StartLimitBurst.

    ``_import`` is an injection seam for tests (defaults to
    ``importlib.import_module``), independent of the recognition-backend probe's
    seam so each is exercisable in isolation.
    """
    import importlib
    _import = _import or importlib.import_module
    try:
        _import("sounddevice")
    except Exception as e:
        # Exception, not BaseException, so KeyboardInterrupt/SystemExit propagate;
        # a missing libportaudio2 raises OSError, a mis-built wheel RuntimeError —
        # all the same apt-install-to-fix class this probe exists to surface.
        raise ConfigError(
            f"The 'sounddevice' audio-capture backend failed to import: {e}.\n"
            "  This almost always means the system 'libportaudio2' library is "
            "missing — a fresh Raspberry Pi OS install does not include it.\n"
            "  Fix: 'sudo apt-get install -y libportaudio2' — see "
            "docs/pi-setup-guide.md."
        ) from e


class _SecretRedactingFilter(logging.Filter):
    """#202 (sec-1): scrub known credentials from every log record's message.

    python3-discogs-client authenticates by putting the user token in the URL
    QUERY, not a header, so a requests-level failure (flaky wifi) stringifies as
    '... url: /database/search?...&token=<RAW TOKEN>' and is logged VERBATIM by
    resolver.py / reader.py — bypassing transport._redact_url, which only guards
    the app's own header-auth transport. Rather than redact per-site, scrub at the
    boundary: render each record, replace the exact secret strings (and any
    `token=` query value as belt-and-suspenders for any future/unknown secret)
    with '<redacted>', then rewrite record.msg/.args so %-formatting downstream
    cannot reintroduce the secret.
    """

    _TOKEN_QUERY_RE = re.compile(r"(token=)[^&\s]+")

    def __init__(self, secrets):
        super().__init__()
        # Non-empty STRING secrets only (config values should be str, but guard so
        # a mistyped/None credential can't crash the log path), longest-first so a
        # secret that is a substring of another is masked before the shorter one.
        self._secrets = sorted(
            {s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True
        )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # A filter runs in Handler.handle() BEFORE emit(), outside logging's
            # emit()/handleError fault isolation. getMessage() does msg % args, so
            # a malformed %-format log call would otherwise raise straight out to
            # the caller — on this 24/7 pipeline that could kill a coroutine leg
            # and trip the FIRST_COMPLETED shutdown. DROP such a record (return
            # False): it can never render (emit()'s own getMessage() would raise
            # identically), so we lose no real log line — but a `return True` would
            # hand it to handleError(), which dumps the RAW record.msg/.args to
            # stderr, and a secret sitting in record.args would leak to exactly the
            # journal sink this filter exists to protect. Dropping is safe on both
            # axes: nothing renderable is lost, and nothing unredacted is emitted.
            return False
        scrubbed = self.scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        # R6-38: this record IS renderable, so keep it (sanitised). A record that
        # could NOT render was already dropped above (the except → return False) —
        # so "never drop a record" was wrong; the drop path is deliberate and the
        # only safe handling of a malformed %-format call on this security filter.
        return True

    def scrub(self, text: str) -> str:
        """Apply the same redaction to an arbitrary string — used both by
        :meth:`filter` (log records) and by the R6-26 crash handler (a rendered
        traceback, which bypasses logging entirely). Returns the scrubbed text."""
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "<redacted>")
        return self._TOKEN_QUERY_RE.sub(r"\1<redacted>", text)


def install_secret_redaction(config) -> _SecretRedactingFilter:
    """#202: attach the secret-redacting filter to the root HANDLER(s).

    Attached to the handler, NOT the root logger: per Python logging semantics a
    logger-level filter does not apply to records propagating up from child
    loggers (src.metadata.resolver, ...), so a root-LOGGER filter would scrub
    nothing from the sites that actually leak. basicConfig (module top) installs
    one root handler; adding the filter there covers every propagated record.
    """
    secrets = [
        config.discogs.user_token,
        config.lastfm.api_key,
        config.lastfm.api_secret,
        config.lastfm.session_key,
    ]
    log_filter = _SecretRedactingFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_filter)
    # R6-26: also make it reachable from the __main__ crash handler, which scrubs
    # an uncaught traceback (a sink the log filter does not cover).
    global _REDACTOR
    _REDACTOR = log_filter
    return log_filter


def install_io_executor(loop) -> ThreadPoolExecutor:
    """Create the owned shared I/O pool and make it ``loop``'s DEFAULT executor.

    Extracted from main() (like run_pipeline, T-1) so the routing is unit-testable:
    after this, every ``run_in_executor(None, …)`` blocking call lands on a pool
    WE own and shut down with cancel_futures at exit (CRIT-3) — not the
    interpreter's default pool, whose in-flight work gates process exit. Returns
    the executor so run_pipeline's finally can close it. Discogs keeps its own
    dedicated pool (#61); its explicit-executor calls are unaffected.
    """
    io_executor = ThreadPoolExecutor(
        max_workers=_IO_EXECUTOR_MAX_WORKERS, thread_name_prefix="vnp-io"
    )
    loop.set_default_executor(io_executor)
    return io_executor


# ARCH-10: the runtime components main() needs AFTER construction.  Bundling them
# lets build_components own the construction (and its failure message) while main()
# stays a thin wiring/lifecycle body.
Components = namedtuple(
    "Components",
    "discogs_http display silence recognizer capture tracker scrobble_dispatcher",
)


def build_components(config, state: PlayerState) -> "Components":
    """Construct and return every runtime component (ARCH-10, T-1 style).

    Extracted from main() so the construction path is GUARDED and unit-testable.
    Component construction is not pure — most concretely, ``DisplayRenderer`` builds
    a ``CoverArtCache`` whose ``__init__`` does ``cache_dir.mkdir(...)``, which
    raises ``OSError`` when ``display.cover_art_cache_dir`` is not writable (a
    read-only location, or a file where a directory must go — the directory itself
    is created automatically).  Before
    this, such a failure reached the operator as a bare traceback naming no remedy
    (the finding). Now it is one actionable log line pointing at the checklist,
    then a re-raise — the process still exits non-zero, so systemd handles it
    (bounded by STAB-4's StartLimitBurst).
    """
    try:
        # A-4: one shared Discogs transport; the read half goes to the resolver,
        # the write half to the tracker — each depends only on the slice it uses.
        discogs_http = DiscogsHttp(config.discogs.user_token)
        resolver = MetadataResolver(DiscogsReader(discogs_http, config.discogs))
        lastfm = LastFmClient(config.lastfm)
        # R8-02/R9-08: the credited-memory is silence-BOUNDARY keyed — the live
        # SpinMemory is SWAPPED at the boundary EVENT itself (not when that
        # boundary's finalize completes; a finalize legally completes minutes
        # late) — not wall-clock windowed, so the tracker no longer needs the
        # silence timeout injected.
        tracker = ListenTracker(
            DiscogsCollectionWriter(discogs_http, config.discogs),
            lastfm,
            recover_collection_instance=resolver.recover_collection_instance,
        )
        # A-9: the application-layer commit service owns resolve → state → track →
        # scrobble; the recognition loop just confirms a result and hands it off.
        # R10-09 (#422): the confirmed-track scrobble is dispatched off the sole
        # recognition consumer through this lifecycle-owned, single-consumer
        # queue (started in main(), drained in run_pipeline).  It wraps the SAME
        # LastFmClient the tracker uses for love(); the client's own lock keeps
        # scrobble and love serialized against the shared pylast Network (CRIT-10).
        scrobble_dispatcher = ScrobbleDispatcher(lastfm)
        commit_service = TrackCommitService(
            state, resolver, tracker, scrobble_dispatcher
        )
        display = DisplayRenderer(config.display, state)
        silence = SilenceDetector(config.audio)
        # R10-11 (#424): the recognizer flags a queued chunk as stale once it has
        # waited longer than ONE CAPTURE HOP — the cadence fresh audio arrives at.
        # The hop lives in the audio config. AudioCapture DISABLES overlap when
        # overlap_seconds >= chunk_seconds (a benign, config-ALLOWED degradation —
        # config validation enforces only overlap >= 0, not overlap < chunk), so
        # mirror that here: the effective hop is chunk − overlap while overlap is
        # smaller than the chunk, otherwise the full (non-overlapping) chunk. Since
        # chunk_seconds > 0 is enforced, the result is always positive.
        chunk_s = config.audio.chunk_seconds
        overlap_s = config.audio.overlap_seconds
        hop_seconds = chunk_s - overlap_s if overlap_s < chunk_s else chunk_s
        recognizer = RecognitionLoop(
            config.recognition, state, commit_service.commit, hop_seconds=hop_seconds
        )
        from src.audio.capture import AudioCapture   # R9-13: lazy (see import note)
        capture = AudioCapture(config.audio, silence, recognizer)
    except Exception:
        log.error(
            "Failed to construct the application components. The most common "
            "first-boot cause is that display.cover_art_cache_dir (%r) is not "
            "writable — a read-only location, or a file sitting where a directory "
            "must go (the directory itself is created automatically). See "
            "docs/first-boot-checklist.md ('Display / startup won't initialize'). "
            "Re-raising the underlying error below.",
            config.display.cover_art_cache_dir,
        )
        raise
    return Components(
        discogs_http=discogs_http,
        display=display,
        silence=silence,
        recognizer=recognizer,
        capture=capture,
        tracker=tracker,
        scrobble_dispatcher=scrobble_dispatcher,
    )


def start_display(display) -> None:
    """Initialize the display, turning a first-boot failure into ACTIONABLE log
    output before re-raising (ARCH-10).

    ``display.start()`` calls ``pygame.display.set_mode(...)``, which raises
    ``pygame.error`` ("No available video device") when there is no HDMI/console
    or no X server on the target ``DISPLAY``.  Unguarded, that reached the operator
    as a bare traceback naming no remedy — the single most probable first-power-on
    failure per the brief.  Log the concrete checks first, then re-raise so the
    process still exits non-zero (systemd Restart handles it, bounded by STAB-4).
    """
    try:
        display.start()
    except Exception:
        log.error(
            "Display initialization failed — the screen will stay black. Check, "
            "in order: (1) the HDMI cable is seated and the monitor/panel was "
            "powered on BEFORE the Pi booted; (2) a desktop / X server is running "
            "on the target DISPLAY (default :0 — see the Environment=DISPLAY and "
            "XAUTHORITY lines in the systemd unit); (3) docs/first-boot-checklist.md "
            "('Display / startup won't initialize'). Re-raising the underlying "
            "error below."
        )
        raise


def apply_state_silence_effect(event: AudioEvent, state: PlayerState):
    """The player-state half of a silence event (CRIT-5).

    Registered as its OWN Signal listener, separate from the tracker's (see
    wire_silence_listeners), so the Signal's log-and-continue (A-11) applies
    BETWEEN them: a fault in ``tracker.on_silence_event`` can no longer skip this
    half. In the old single-listener handler a raise in the tracker call left
    ``state.clear()`` unrun — the B-1 epoch never bumped and the now-playing card
    was stranded on screen with only a log line. That split is the fix.

      - MUSIC_STARTED: only enter LISTENING from IDLE or ERROR.  During an
        active session (e.g. a side flip) keep the now-playing card on screen
        instead of dropping to the IDENTIFYING spinner; from ERROR,
        "REPOSITION NEEDLE TO RETRY" recovers when music returns.
      - SESSION_ENDED / SESSION_ENDED_FORCED: clear() → IDLE (and bumps the
        session epoch, B-1).  R8-16 cold-review F1: the forced end MUST take
        this branch too — the forced/genuine distinction matters only to the
        tracker's per-spin credit memory; the player-state half is identical
        for both (card cleared, epoch bumped so an in-flight commit for the
        force-ended session is discarded, IDLE shown).  Missing it stranded
        the card on screen indefinitely and let a stale commit pass every
        epoch check.
    """
    if event == AudioEvent.MUSIC_STARTED:
        if state.status in (PlayerStatus.IDLE, PlayerStatus.ERROR):
            state.set_status(PlayerStatus.LISTENING)
    elif event in (AudioEvent.SESSION_ENDED, AudioEvent.SESSION_ENDED_FORCED):
        state.clear()


def wire_silence_listeners(silence, state: PlayerState, tracker: ListenTracker):
    """Register the silence-event handlers as TWO separate Signal listeners
    (CRIT-5), the player-state effect FIRST and the tracker effect SECOND.

    Extracted from main() (like run_pipeline / install_io_executor, T-1) so the
    wiring is unit-testable. The load-bearing fix is the SPLIT: two separate
    listeners mean the Signal's log-and-continue (A-11) applies between them — a
    fault in the tracker half no longer skips the state half (a stranded card),
    and vice versa. State is registered first to honor the finding's "clear the
    player state before scheduling the tracker end", but that ordering is
    otherwise a no-op: the tracker's actual session detach happens in the
    ``_end_session`` task it schedules (which runs only after this synchronous
    Signal.emit unwinds), so the epoch bump is synchronous and lands before the
    detach in EITHER listener order.
    """
    silence.on_event(lambda event: apply_state_silence_effect(event, state))
    silence.on_event(tracker.on_silence_event)


async def run_pipeline(
    tasks, capture, display, tracker, discogs_http, io_executor, scrobble_dispatcher
):
    """Run the pipeline legs until the first one finishes, then shut down.

    Extracted from main() (T-1) so the shutdown semantics are testable without
    real audio/display:
      - the moment ANY leg exits, cancel the rest (FIRST_COMPLETED) — this is
        the v1.3.5 "ESC zombie" fix;
      - re-raise a faulted leg's exception after cleanup;
      - drain the tracker's in-flight end-of-session credit tasks (CONC-1) —
        they are fire-and-forget, not pipeline legs, so nothing else awaits them
        and ``asyncio.run`` would otherwise cancel a mid-write credit — then
        always stop capture and display, and finally close BOTH thread pools —
        the dedicated Discogs executor (#61) and the owned shared I/O executor
        (CRIT-3) — in the finally.
    """
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        # R5-25: a pending leg can raise a NON-CancelledError while unwinding its
        # cancellation (e.g. a finally that awaits a ticker or closes a resource)
        # — exactly the path where cleanup bugs live. The results were discarded,
        # so such a fault vanished silently. Log any non-cancellation exception;
        # the CancelledError from the cancel we just issued is expected and skipped.
        for r in await asyncio.gather(*pending, return_exceptions=True):
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                log.error("Pipeline leg raised while unwinding shutdown: %r", r)
        # Log EVERY faulted leg before re-raising the first (B-14).  Previously
        # `t.result()` re-raised on the first done task and left the loop, so if
        # several legs died simultaneously only one exception was ever surfaced.
        first_exc = None
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                log.error(f"Pipeline leg '{t.get_name()}' failed: {exc!r}")
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            raise first_exc
        log.info("Pipeline stopped.")
    finally:
        # Let an in-flight end-of-session Discogs credit finish before the loop
        # tears it down (CONC-1); bounded so a stuck write can't hang shutdown.
        # Runs even when a leg faulted — a crash must not abandon a half-written
        # collection update.
        #
        # R9-24 (#403, HYPOTHESIS/unreachable): the first statement here awaits.
        # If run_pipeline ITSELF were cancelled during this drain, the
        # CancelledError would propagate out of the await and SKIP the stop/close
        # statements below (a resource leak). This does not happen under the
        # shipped wiring: the signal handler (install_signal_handlers) cancels
        # only the pipeline LEGS, never the run_pipeline coroutine, and main()
        # awaits run_pipeline directly (no outer cancel). Kept as a note, not a
        # shield: if a future change ever cancels run_pipeline directly, wrap the
        # body below in `asyncio.shield` or move the awaited drain last.
        await tracker.drain()
        # R10-09 (#422): flush the confirmed-track scrobble queue (bounded) before
        # the shared I/O pool closes — its worker runs the scrobble via
        # run_in_executor on that pool, exactly like the tracker's love() credit.
        # Bounded like tracker.drain() so a wedged Last.fm call can't hang
        # shutdown; anything still queued at the bound is dropped (best-effort).
        await scrobble_dispatcher.drain()
        capture.stop()
        display.stop()
        # Close BOTH thread pools LAST — after drain() has awaited the credit
        # writes that run on them (the Discogs pool via writer.run; the shared I/O
        # pool via lastfm.love's run_in_executor). These MUST be the final
        # statements with NO await after them: a credit task resuming post-close
        # would dispatch to a shut pool (RuntimeError). Both use wait=False so they
        # return immediately even if drain() timed out with a write still running.
        # (#61 gave Discogs its own pool; CRIT-3 does the same for everything else.)
        discogs_http.close()
        # CRIT-3: shut the owned I/O pool with cancel_futures so QUEUED blocking
        # work (Last.fm scrobble/love, cover download, palette extraction,
        # MusicBrainz lookup, WAV encode) is DROPPED rather than waited on. Without
        # an owned pool this work sat on the interpreter's default executor, whose
        # shutdown (asyncio.run → loop.shutdown_default_executor, a wait=True join
        # with no timeout) then gated process exit on it — a SIGTERM could hang
        # past systemd's 90s default and get SIGKILLed mid-write (CONC-1 becomes
        # permanent). A still-RUNNING call can't be interrupted (Python can't kill
        # a thread), so the unit's TimeoutStopSec is the backstop for that residue.
        io_executor.shutdown(wait=False, cancel_futures=True)


async def main():
    # A-2: parse + validate the YAML once into a typed AppConfig; every
    # component below receives its own typed section object (config.audio,
    # config.discogs, …) instead of reaching into a raw dict.  A bad config is
    # one friendly startup failure here, not a KeyError deep in a constructor.
    try:
        config = load_config()
        # #198: fail loud NOW if the configured recognition backend can't import
        # (e.g. Python 3.13 missing audioop) — reuses this same friendly exit
        # instead of a silent per-chunk miss at runtime.
        verify_recognition_backend_importable(config)
        # R9-13 (#396): probe the audio C-extension dep in the SAME startup try,
        # so a missing libportaudio2 parks (exit 78) instead of crash-looping.
        verify_audio_backend_importable()
    except ConfigError as e:
        log.error(f"Configuration error:\n{e}")
        # R6-27: exit EX_CONFIG (78), not 1 — a config error can never self-heal on
        # a restart, so the unit's RestartPreventExitStatus=78 parks the service
        # instead of churning Restart=on-failure through StartLimitBurst (each cold
        # start re-pages the whole Discogs collection index). A transient crash
        # still exits non-zero-but-not-78 and restarts as before.
        sys.exit(_EXIT_CONFIG_ERROR)

    # #202: install the credential-redaction filter on the root log handler as
    # early as possible — before any component can log a token-bearing exception
    # (the discogs-client library carries the token in the request URL).
    install_secret_redaction(config)

    # CRIT-3: own the shared I/O pool (see install_io_executor) so every
    # run_in_executor(None, …) blocking call runs on a pool we shut down with
    # cancel_futures at exit — instead of the interpreter's default pool, whose
    # in-flight work gates process exit (see run_pipeline's finally).
    loop = asyncio.get_running_loop()
    io_executor = install_io_executor(loop)

    state = PlayerState()

    # #170: everything from here to run_pipeline is the STARTUP body. If it aborts
    # (build_components' unwritable-cache-dir raise, start_display's no-HDMI raise,
    # or task/signal setup) run_pipeline is never entered, so its cleanup finally
    # never runs and the io_executor (created above) — plus the DiscogsHttp pool,
    # if components were built — would be left to concurrent.futures' atexit join.
    # Own that cleanup here for the pre-run_pipeline abort path, gated on
    # `started_pipeline` so run_pipeline (which closes BOTH pools on every path it
    # IS entered) is never double-closed.
    components = None
    started_pipeline = False
    try:
        # ARCH-10: construction is guarded + testable in build_components — a
        # failure (most concretely an unwritable cover_art_cache_dir) is now an
        # actionable log line pointing at the checklist, then a re-raise, not a
        # bare traceback.
        components = build_components(config, state)

        # Wire silence events into state and tracker as TWO separate Signal
        # listeners (CRIT-5, in wire_silence_listeners): splitting them lets the
        # Signal's log-and-continue isolate a fault in one from the other (the
        # stranded-card bug); state is registered first to match the finding's
        # clear-before-the-end ordering.
        wire_silence_listeners(components.silence, state, components.tracker)

        log.info(f"vinyl-now-playing v{read_version()} starting up 🎵")
        # ARCH-10: guard the display init too — the single most probable first-boot
        # failure (no HDMI / X down) is now an actionable message before the re-raise.
        start_display(components.display)

        # R10-09 (#422): start the scrobble dispatcher's single consumer task now
        # that the loop is running and the shared I/O executor is installed. It is
        # NOT a FIRST_COMPLETED pipeline leg (it never exits on its own, and its
        # exit must not trigger shutdown); it is a tracked background task that
        # run_pipeline's finally drains and cancels, like the tracker's credit tasks.
        components.scrobble_dispatcher.start()

        # The three long-running pipeline coroutines as named tasks.
        tasks = [
            asyncio.create_task(components.capture.run(), name="capture"),
            asyncio.create_task(components.recognizer.run(), name="recognizer"),
            asyncio.create_task(components.display.run(), name="display"),
        ]

        # Graceful shutdown on Ctrl+C or SIGTERM: cancel every leg.  Task.cancel
        # is a plain synchronous call, so it's safe to invoke directly from a
        # signal handler — no fire-and-forget task required.
        def _cancel_all():
            log.info("Shutdown signal received — stopping cleanly.")
            for t in tasks:
                t.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _cancel_all)

        # FIRST_COMPLETED shutdown + cleanup live in run_pipeline (extracted for
        # testability — T-1).  The tracker is passed so shutdown can drain its
        # in-flight end-of-session credit before the loop closes (CONC-1); the
        # shared DiscogsHttp and the owned I/O executor are passed so
        # run_pipeline's finally can close BOTH pools LAST, after that credit has
        # drained off them (#61 / CRIT-3).  From here run_pipeline OWNS pool
        # cleanup on every path it takes, so mark it before the await.
        started_pipeline = True
        await run_pipeline(
            tasks, components.capture, components.display, components.tracker,
            components.discogs_http, io_executor, components.scrobble_dispatcher,
        )
    finally:
        # #170: only the PRE-run_pipeline abort path reaches here with cleanup
        # still owed — run_pipeline's own finally already closed both pools once it
        # was entered (started_pipeline). Mirror that close for the abort path so a
        # startup failure doesn't lean on atexit. Closing an unused/lazy pool is a
        # cheap no-op, and both close idempotently.
        if not started_pipeline:
            if components is not None:
                # If the abort landed AFTER the dispatcher's consumer task was
                # started (between start() and started_pipeline), drain+cancel it
                # so it isn't left pending for loop teardown to reap. Bounded and
                # never-raises; a no-op if start() was never reached.
                await components.scrobble_dispatcher.drain()
                components.discogs_http.close()
            io_executor.shutdown(wait=False, cancel_futures=True)


def _run_scrubbed() -> None:
    """__main__ entry: run main(), and if it crashes with an UNHANDLED exception,
    render the traceback through the secret scrub before it reaches stderr/journald.

    R6-26: the #202 log filter scrubs LOG records, but an uncaught exception's
    traceback bypasses logging entirely — Python's default excepthook writes it raw
    to stderr → journald. discogs-client carries the token in request URLs, so a
    requests-level error that escaped every catch layer would leak it to exactly the
    journal sink #202 exists to protect. Scrub the rendered traceback at this last
    boundary, then exit non-zero. SystemExit (the ConfigError path's clean
    EX_CONFIG exit, and KeyboardInterrupt's default) is re-raised untouched so exit
    codes and Ctrl+C behaviour are preserved.
    """
    import traceback
    try:
        asyncio.run(main())
    except SystemExit:
        # Clean exit codes preserved (the ConfigError path's EX_CONFIG 78, etc.);
        # its __context__ is a friendly ConfigError already logged, not a secret.
        raise
    except KeyboardInterrupt:
        # R7-20 (sec): a BARE re-raise lets Python's default excepthook render the
        # whole __context__ chain raw to stderr → journald. If Ctrl+C lands WHILE a
        # token-bearing exception is in flight (a discogs-client request error
        # carries the token in its URL), that chained context leaks the secret to
        # exactly the sink R6-26 exists to protect. Scrub-and-print the full chain
        # here, then re-raise a CONTEXT-SEVERED KeyboardInterrupt (`from None`) so
        # the default excepthook has nothing secret left to print — SIGINT exit 130
        # preserved. A plain Ctrl+C with no in-flight error (no __context__) just
        # re-raises untouched.
        exc = sys.exc_info()[1]
        if exc is not None and exc.__context__ is not None:
            rendered = traceback.format_exc()
            if _REDACTOR is not None:
                rendered = _REDACTOR.scrub(rendered)
            sys.stderr.write(rendered)
            raise KeyboardInterrupt() from None
        raise
    except BaseException:
        rendered = traceback.format_exc()
        if _REDACTOR is not None:
            rendered = _REDACTOR.scrub(rendered)
        sys.stderr.write(rendered)
        raise SystemExit(1)


if __name__ == "__main__":
    _run_scrubbed()
