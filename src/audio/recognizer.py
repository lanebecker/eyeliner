"""Recognition loop — polls for track identity while music plays.

Abstracts the recognition backend behind RecognizerBackend so ShazamIO,
ACRCloud, or AudD can be swapped via config without touching this file.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import numpy as np

from src.state.player_state import PlayerStatus

if TYPE_CHECKING:
    from src.metadata.resolver import MetadataResolver
    from src.state.player_state import PlayerState
    from src.tracking.lastfm_client import LastFmClient
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
    """Recognition via ShazamIO (unofficial Shazam API — free, personal use).

    The Shazam client is created once on first use and reused for every
    subsequent recognition (v1.3.3) — constructing a fresh client per chunk
    threw away its internal HTTP session several times a minute for no
    benefit.  recognize() is split into three isolated stages (A-13):
    `_encode_wav` (executor), `_call_shazam` (transport), and the pure
    `_parse_shazam` (response-shape parsing).  The shazamio import (_call_shazam)
    and soundfile import (_encode_wav) are kept lazy on purpose: they keep this
    module importable (and the rest of the suite testable) on machines without
    the audio stack installed.
    """

    def __init__(self):
        self._shazam = None  # Created lazily on first recognize()

    async def recognize(
        self, audio: np.ndarray, sample_rate: int
    ) -> Optional[RawRecognitionResult]:
        # Three isolated stages (A-13): encode (executor) → call Shazam
        # (transport) → parse (pure).  One broad except is the true boundary
        # back to the recognition loop, which treats any failure as a miss.
        try:
            # Serialize the chunk to an in-memory WAV in an executor — soundfile's
            # sf.write is a blocking C call (~1.3 MB encode from a ~2.6 MB float32
            # chunk) that would otherwise stall the event loop inline (P-6).
            loop = asyncio.get_running_loop()
            wav_bytes = await loop.run_in_executor(
                None, self._encode_wav, audio, sample_rate
            )
            result = await self._call_shazam(wav_bytes)
            return self._parse_shazam(result)
        except Exception as e:
            log.warning(f"ShazamIO recognition failed: {e}")
            return None

    async def _call_shazam(self, wav_bytes: bytes) -> dict:
        """Transport-only: lazily build the Shazam client and call it.

        The shazamio import is kept lazy here so the module stays importable
        (and the suite testable) on machines without the audio stack (A-13).
        """
        from shazamio import Shazam

        if self._shazam is None:
            self._shazam = Shazam()
        return await self._shazam.recognize(wav_bytes)

    @staticmethod
    def _parse_shazam(result: dict) -> Optional[RawRecognitionResult]:
        """Pure parse of a Shazam JSON response → RawRecognitionResult or None.

        No I/O, no imports — unit-testable against captured JSON, isolating the
        fragile Shazam response-shape knowledge from transport (A-13).
        """
        track = (result or {}).get("track")
        if not track:
            return None

        # Pull album from the metadata section if present.  Break BOTH loops as
        # soon as the album is found — without the outer break the inner break
        # only exits the metadata loop and a later section could overwrite it.
        album = ""
        for section in track.get("sections", []):
            for meta in section.get("metadata", []):
                if meta.get("title", "").lower() == "album":
                    album = meta.get("text", "")
                    break
            if album:
                break

        return RawRecognitionResult(
            title=track.get("title", ""),
            artist=track.get("subtitle", ""),
            album=album,
            isrc=track.get("isrc"),
        )

    @staticmethod
    def _encode_wav(audio: np.ndarray, sample_rate: int) -> bytes:
        """Serialize an audio chunk to in-memory WAV bytes (PCM_16).

        Pure CPU/IO with no event-loop interaction, so it runs in an executor
        (see recognize) — sf.write is a blocking C call (P-6).
        """
        import io
        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()


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
        lastfm: Optional["LastFmClient"] = None,
    ):
        self.config = config["recognition"]
        self.state = state
        self.resolver = resolver
        self.tracker = tracker
        self.lastfm = lastfm
        self.poll_interval: int = self.config["poll_interval_seconds"]
        self.confirmation_required: int = self.config.get("confirmation_required", 2)
        # Consecutive failed recognitions while LISTENING before the display
        # shows the error state (v1.4.1).  At ~10-12s per chunk, the default
        # of 6 puts "NO MATCH FOUND" on screen after roughly a minute of
        # music that ShazamIO can't identify.
        self.error_after_misses: int = self.config.get("error_after_misses", 6)
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._pending_result: Optional[RawRecognitionResult] = None
        self._pending_count: int = 0
        self._miss_count: int = 0
        self.backend: RecognizerBackend = self._init_backend()

    def _init_backend(self) -> RecognizerBackend:
        backend_name = self.config.get("backend", "shazamio")
        if backend_name == "shazamio":
            return ShazamIOBackend()
        # TODO: add AcrcloudBackend, AuddBackend
        raise ValueError(f"Unknown recognition backend: '{backend_name}'")

    async def enqueue(self, audio: np.ndarray, sample_rate: int):
        """Called by AudioCapture to hand off a chunk for recognition.

        If the recognition queue is full (Shazam is taking longer than capture
        is producing), drop the OLDEST queued chunk and admit this one
        (v1.3.5) — the freshest audio is the most relevant for detecting a
        track change, and this matches AudioCapture's block-queue policy.
        (Previously the incoming chunk was discarded, so a lagging backend
        kept grinding through stale audio and delayed track-change
        detection.)  Drops are logged at debug level so a "stopped
        identifying" complaint has a breadcrumb in the journal.
        """
        if self._audio_queue.full():
            try:
                self._audio_queue.get_nowait()  # Drop the OLDEST — recent audio wins
                log.debug(
                    "Recognition queue full (maxsize=%d); dropped the oldest chunk. "
                    "If this happens consistently, recognition is slower than capture.",
                    self._audio_queue.maxsize,
                )
            except asyncio.QueueEmpty:  # pragma: no cover — full() just said otherwise
                pass
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

    @staticmethod
    def _same_track(a: Optional[RawRecognitionResult], b: Optional[RawRecognitionResult]) -> bool:
        """Compare two recognition results case- and whitespace-insensitively.

        Shazam occasionally returns subtly different formatting for the same
        track between chunks (trailing whitespace, capitalization tweaks).
        Without normalization those count as a new track and trigger an
        unnecessary re-resolve / re-scrobble.
        """
        if a is None or b is None:
            return False
        return (
            a.title.strip().lower() == b.title.strip().lower()
            and a.artist.strip().lower() == b.artist.strip().lower()
        )

    async def _handle_result(self, result: Optional[RawRecognitionResult]):
        """Apply confirmation logic, then resolve metadata and update state."""
        if result is None:
            self._pending_result = None
            self._pending_count = 0
            self._register_miss()
            return

        if self._same_track(result, self.state.current_raw):
            self._miss_count = 0  # same track still playing — recognition works (B-7)
            return  # Same track still playing

        if self._same_track(result, self._pending_result):
            self._pending_count += 1
        else:
            self._pending_result = result
            self._pending_count = 1

        if self._pending_count >= self.confirmation_required:
            log.info(f"Track confirmed: {result.artist} — {result.title}")
            self._miss_count = 0  # a real commit — recognition works (B-7)
            await self._commit_track(result)
            self._pending_result = None
            self._pending_count = 0
        else:
            # A non-None result that neither matches the current track nor (yet)
            # confirms — unconfirmable churn (a noisy room, two records bleeding
            # together).  Count it toward ERROR so the display doesn't spin on
            # the boot/IDENTIFYING screen forever.  Previously _miss_count was
            # reset on EVERY non-None result, so neither churn nor interspersed
            # None-misses could ever accumulate to surface ERROR (B-7).
            self._register_miss()

    def _register_miss(self):
        """Count a failed recognition; surface ERROR after enough of them.

        Misses only matter while LISTENING — before the first successful
        identification.  During PLAYING, surface noise and quiet passages
        produce routine misses that mean nothing; in IDLE there's no needle
        down; in ERROR we're already showing the failure.  ERROR is recovered
        by repositioning the needle (silence → music re-enters LISTENING) or
        by a successful commit (set_track → PLAYING).
        """
        if self.state.status == PlayerStatus.LISTENING:
            self._miss_count += 1
            if self._miss_count >= self.error_after_misses:
                log.info(
                    "Recognition failed %d consecutive times while listening — "
                    "showing NO MATCH FOUND.", self._miss_count,
                )
                self._miss_count = 0
                self.state.set_status(PlayerStatus.ERROR)
        else:
            self._miss_count = 0

    async def _commit_track(self, raw: RawRecognitionResult):
        """Resolve full metadata and update state + tracker + Last.fm scrobble."""
        timestamp = int(time.time())
        # Capture the session token before the resolve await.  resolve() yields
        # the event loop, during which a SESSION_ENDED (needle lift) can run
        # state.clear() and bump the epoch.  If that happens, this commit is for
        # audio that has already stopped: displaying it would resurrect a dead
        # track, and logging/scrobbling it would corrupt a fresh session (B-1).
        commit_epoch = self.state.session_epoch
        metadata = await self.resolver.resolve(raw)
        if self.state.session_epoch != commit_epoch:
            log.info(
                "Discarding stale commit for %s — %s: the session ended while "
                "metadata was resolving.",
                raw.artist, raw.title,
            )
            return
        self.state.set_track(metadata)
        # Advance current_raw only AFTER the resolved track is displayed (B-11).
        # If set_raw ran first and resolve/set_track then failed, current_raw
        # would be ahead of current_track, so the dedup at the top of
        # _handle_result would treat the new track as "already playing" and the
        # loop would never re-attempt it — display stuck on the old track.
        self.state.set_raw(raw)
        await self.tracker.on_track_identified(metadata)
        log.info(
            f"Now playing: {metadata.artist} / {metadata.album} / "
            f"{metadata.title} [{metadata.source.name}]"
        )
        if self.lastfm:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.lastfm.scrobble, metadata, timestamp
                )
            except Exception as e:
                log.warning(f"Last.fm scrobble error: {e}")
