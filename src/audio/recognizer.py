"""Recognition loop — polls for track identity while music plays.

Abstracts the recognition backend behind RecognizerBackend so ShazamIO,
ACRCloud, or AudD can be swapped via config without touching this file.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.metadata.resolver import MetadataResolver
    from src.state.player_state import PlayerState
    from src.tracking.listen_tracker import ListenTracker

log = logging.getLogger(__name__)


@dataclass
class RawRecognitionResult:
    """Minimal result from any recognition backend."""
    title: str
    artist: str
    album: str
    isrc: Optional[str] = None
    confidence: Optional[float] = None


class RecognizerBackend(ABC):
    """Interface all recognition backends must implement."""

    @abstractmethod
    async def recognize(
        self, audio: np.ndarray, sample_rate: int
    ) -> Optional[RawRecognitionResult]:
        """Identify a chunk of audio. Returns None if unrecognized."""
        ...


class ShazamIOBackend(RecognizerBackend):
    """Recognition via ShazamIO (unofficial Shazam API — free, personal use)."""

    async def recognize(
        self, audio: np.ndarray, sample_rate: int
    ) -> Optional[RawRecognitionResult]:
        try:
            import io
            import soundfile as sf
            from shazamio import Shazam

            # ShazamIO expects bytes; serialize audio to an in-memory WAV
            buf = io.BytesIO()
            sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
            buf.seek(0)

            shazam = Shazam()
            result = await shazam.recognize(buf.read())

            track = result.get("track")
            if not track:
                return None

            # Pull album from the metadata section if present
            album = ""
            for section in track.get("sections", []):
                for meta in section.get("metadata", []):
                    if meta.get("title", "").lower() == "album":
                        album = meta.get("text", "")
                        break

            return RawRecognitionResult(
                title=track.get("title", ""),
                artist=track.get("subtitle", ""),
                album=album,
                isrc=track.get("isrc"),
            )
        except Exception as e:
            log.warning(f"ShazamIO recognition failed: {e}")
            return None


class RecognitionLoop:
    """Manages the async recognition polling loop.

    Requires `confirmation_required` consecutive identical results before
    committing a track change, avoiding flickering on noisy matches.
    """

    def __init__(
        self,
        config: dict,
        state: "PlayerState",
        resolver: "MetadataResolver",
        tracker: "ListenTracker",
    ):
        self.config = config["recognition"]
        self.state = state
        self.resolver = resolver
        self.tracker = tracker
        self.poll_interval: int = self.config["poll_interval_seconds"]
        self.confirmation_required: int = self.config.get("confirmation_required", 2)
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._pending_result: Optional[RawRecognitionResult] = None
        self._pending_count: int = 0
        self.backend: RecognizerBackend = self._init_backend()

    def _init_backend(self) -> RecognizerBackend:
        backend_name = self.config.get("backend", "shazamio")
        if backend_name == "shazamio":
            return ShazamIOBackend()
        # TODO: add AcrcloudBackend, AuddBackend
        raise ValueError(f"Unknown recognition backend: '{backend_name}'")

    async def enqueue(self, audio: np.ndarray, sample_rate: int):
        """Called by AudioCapture to hand off a chunk for recognition."""
        if not self._audio_queue.full():
            await self._audio_queue.put((audio, sample_rate))

    async def run(self):
        """Main recognition loop."""
        log.info("Recognition loop started.")
        while True:
            try:
                audio, sample_rate = await asyncio.wait_for(
                    self._audio_queue.get(), timeout=self.poll_interval
                )
                result = await self.backend.recognize(audio, sample_rate)
                await self._handle_result(result)
            except asyncio.TimeoutError:
                pass  # No audio queued — fine
            except Exception as e:
                log.error(f"Recognition loop error: {e}")
                await asyncio.sleep(2)

    async def _handle_result(self, result: Optional[RawRecognitionResult]):
        """Apply confirmation logic, then resolve metadata and update state."""
        if result is None:
            self._pending_result = None
            self._pending_count = 0
            return

        current = self.state.current_raw
        if (
            current
            and result.title == current.title
            and result.artist == current.artist
        ):
            return  # Same track still playing

        if (
            self._pending_result
            and result.title == self._pending_result.title
            and result.artist == self._pending_result.artist
        ):
            self._pending_count += 1
        else:
            self._pending_result = result
            self._pending_count = 1

        if self._pending_count >= self.confirmation_required:
            log.info(f"Track confirmed: {result.artist} — {result.title}")
            await self._commit_track(result)
            self._pending_result = None
            self._pending_count = 0

    async def _commit_track(self, raw: RawRecognitionResult):
        """Resolve full metadata and update state + tracker."""
        self.state.set_raw(raw)
        metadata = await self.resolver.resolve(raw)
        self.state.set_track(metadata)
        await self.tracker.on_track_identified(metadata)
        log.info(
            f"Now playing: {metadata.artist} / {metadata.album} / "
            f"{metadata.title} [{metadata.source.name}]"
        )
