"""vinyl-now-playing — entry point.

Wires all components together and starts the main event loop.

Shutdown design (v1.3.3)
------------------------
SIGINT/SIGTERM cancel the gathered pipeline tasks and let main() unwind
naturally.  The previous approach (cancel ALL tasks — including main()
itself — then call loop.stop() from inside asyncio.run()) "worked" but
guaranteed a RuntimeError("Event loop stopped before Future completed")
traceback on every Ctrl+C.  Cancellation now flows top-down:

    signal → run_task.cancel() → gather propagates CancelledError to
    capture/recognizer/display coroutines → main()'s finally block calls
    capture.stop() and display.stop() → asyncio.run() exits cleanly.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

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


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        log.error(
            "config.yaml not found. "
            "Copy config.example.yaml to config.yaml and fill in your values."
        )
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


async def main():
    config = load_config()

    state = PlayerState()
    resolver = MetadataResolver(config)
    lastfm = LastFmClient(config)
    tracker = ListenTracker(resolver, lastfm)
    display = DisplayRenderer(config, state)
    silence = SilenceDetector(config)
    recognizer = RecognitionLoop(config, state, resolver, tracker, lastfm)
    capture = AudioCapture(config, silence, recognizer)

    # Wire silence events into state and tracker
    def on_silence_event(event: AudioEvent):
        tracker.on_silence_event(event)
        if event == AudioEvent.MUSIC_STARTED:
            # v1.3.4: only enter LISTENING from IDLE.  During an active
            # session (e.g. a side flip), keep the now-playing card on
            # screen — it updates in place when the next track commits —
            # instead of dropping to the IDENTIFYING spinner for ~25s.
            if state.status == PlayerStatus.IDLE:
                state.set_status(PlayerStatus.LISTENING)
        elif event == AudioEvent.SESSION_ENDED:
            state.clear()

    silence.on_event(on_silence_event)

    log.info(f"vinyl-now-playing v{read_version()} starting up 🎵")
    display.start()

    # The three long-running pipeline coroutines, gathered into one awaitable.
    run_task = asyncio.gather(
        capture.run(),
        recognizer.run(),
        display.run(),
    )

    # Graceful shutdown on Ctrl+C or SIGTERM: cancelling the gather propagates
    # CancelledError into all three coroutines.  run_task.cancel is a plain
    # synchronous call, so it's safe to invoke directly from a signal handler —
    # no fire-and-forget task required.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, run_task.cancel)

    try:
        await run_task
    except asyncio.CancelledError:
        log.info("Shutdown signal received — stopping cleanly.")
    finally:
        capture.stop()
        display.stop()


if __name__ == "__main__":
    asyncio.run(main())
