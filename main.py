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

    try:
        # FIRST_COMPLETED (v1.3.5): if ANY leg exits, shut everything down.
        # Before this, asyncio.gather waited for ALL legs — so quitting the
        # display with ESC left capture and recognition running invisibly,
        # still scrobbling and writing play counts with no screen attached.
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            if not t.cancelled():
                t.result()  # Re-raises if the leg died with an exception
        log.info("Pipeline stopped.")
    finally:
        capture.stop()
        display.stop()


if __name__ == "__main__":
    asyncio.run(main())
