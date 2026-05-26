"""vinyl-now-playing — entry point.

Wires all components together and starts the main event loop.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

from src.audio.capture import AudioCapture
from src.audio.silence import SilenceDetector
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
    tracker = ListenTracker(config, resolver, lastfm)
    display = DisplayRenderer(config, state)
    silence = SilenceDetector(config)
    recognizer = RecognitionLoop(config, state, resolver, tracker, lastfm)
    capture = AudioCapture(config, silence, recognizer)

    # Wire silence events into state and tracker
    from src.audio.silence import AudioEvent
    def on_silence_event(event: AudioEvent):
        tracker.on_silence_event(event)
        if event == AudioEvent.MUSIC_STARTED:
            state.set_status(PlayerStatus.LISTENING)
        elif event == AudioEvent.SESSION_ENDED:
            state.clear()

    silence.on_event(on_silence_event)

    # Graceful shutdown on Ctrl+C or SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(shutdown(capture, display))
        )

    log.info(f"vinyl-now-playing v{read_version()} starting up 🎵")
    display.start()

    await asyncio.gather(
        capture.run(),
        recognizer.run(),
        display.run(),
    )


async def shutdown(capture: "AudioCapture", display: "DisplayRenderer"):
    log.info("Shutting down...")
    capture.stop()
    display.stop()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    asyncio.run(main())
