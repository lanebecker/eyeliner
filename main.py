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
import signal
import sys
from pathlib import Path

from src.config import load_config, ConfigError
from src.audio.capture import AudioCapture
from src.audio.silence import SilenceDetector, AudioEvent
from src.audio.recognizer import RecognitionLoop
from src.metadata.resolver import MetadataResolver
from src.display.renderer import DisplayRenderer
from src.state.player_state import PlayerState, PlayerStatus
from src.tracking.lastfm_client import LastFmClient
from src.tracking.listen_tracker import ListenTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


def read_version() -> str:
    """Read the version string from the VERSION file at the repo root."""
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip()
    except Exception:
        return "unknown"


def handle_silence_event(event: AudioEvent, state: PlayerState, tracker: ListenTracker):
    """Route a silence event to the tracker and the player state.

    Extracted from main() (T-1) so the wiring is unit-testable — in particular
    the IDLE/ERROR → LISTENING transition and SESSION_ENDED → clear(), the exact
    paths the B-1 epoch guard depends on.

      - MUSIC_STARTED: only enter LISTENING from IDLE or ERROR.  During an
        active session (e.g. a side flip) keep the now-playing card on screen
        instead of dropping to the IDENTIFYING spinner; from ERROR,
        "REPOSITION NEEDLE TO RETRY" recovers when music returns.
      - SESSION_ENDED: clear() → IDLE (and bumps the session epoch, B-1).
    """
    tracker.on_silence_event(event)
    if event == AudioEvent.MUSIC_STARTED:
        if state.status in (PlayerStatus.IDLE, PlayerStatus.ERROR):
            state.set_status(PlayerStatus.LISTENING)
    elif event == AudioEvent.SESSION_ENDED:
        state.clear()


async def run_pipeline(tasks, capture, display):
    """Run the pipeline legs until the first one finishes, then shut down.

    Extracted from main() (T-1) so the shutdown semantics are testable without
    real audio/display:
      - the moment ANY leg exits, cancel the rest (FIRST_COMPLETED) — this is
        the v1.3.5 "ESC zombie" fix;
      - re-raise a faulted leg's exception after cleanup;
      - always stop capture and display in the finally.
    """
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
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
        capture.stop()
        display.stop()


async def main():
    # A-2: parse + validate the YAML once into a typed AppConfig; every
    # component below receives its own typed section object (config.audio,
    # config.discogs, …) instead of reaching into a raw dict.  A bad config is
    # one friendly startup failure here, not a KeyError deep in a constructor.
    try:
        config = load_config()
    except ConfigError as e:
        log.error(f"Configuration error:\n{e}")
        sys.exit(1)

    state = PlayerState()
    resolver = MetadataResolver(config.discogs)
    lastfm = LastFmClient(config.lastfm)
    # A-3: the resolver and tracker share one DiscogsClient by explicit
    # composition — the tracker is injected with it directly.
    tracker = ListenTracker(resolver.discogs, lastfm)
    display = DisplayRenderer(config.display, state)
    silence = SilenceDetector(config.audio)
    recognizer = RecognitionLoop(config.recognition, state, resolver, tracker, lastfm)
    capture = AudioCapture(config.audio, silence, recognizer)

    # Wire silence events into state and tracker (logic in handle_silence_event,
    # extracted for testability — T-1).
    silence.on_event(lambda event: handle_silence_event(event, state, tracker))

    log.info(f"vinyl-now-playing v{read_version()} starting up 🎵")
    display.start()

    # The three long-running pipeline coroutines as named tasks.
    tasks = [
        asyncio.create_task(capture.run(), name="capture"),
        asyncio.create_task(recognizer.run(), name="recognizer"),
        asyncio.create_task(display.run(), name="display"),
    ]

    # Graceful shutdown on Ctrl+C or SIGTERM: cancel every leg.  Task.cancel
    # is a plain synchronous call, so it's safe to invoke directly from a
    # signal handler — no fire-and-forget task required.
    def _cancel_all():
        log.info("Shutdown signal received — stopping cleanly.")
        for t in tasks:
            t.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _cancel_all)

    # FIRST_COMPLETED shutdown + cleanup live in run_pipeline (extracted for
    # testability — T-1).
    await run_pipeline(tasks, capture, display)


if __name__ == "__main__":
    asyncio.run(main())
