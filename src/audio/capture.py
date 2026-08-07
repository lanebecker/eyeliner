"""Audio capture from USB audio interface.

Records continuously from the configured sounddevice input and feeds
genuinely overlapping chunks into the silence detector and recognition loop.

Capture design (v1.3.3)
-----------------------
Earlier versions used blocking sd.rec() calls separated by a sleep, which
left a dead gap between chunks (see src/audio/chunking.py for the full
story).  Capture now works in three stages:

  1. sd.InputStream records continuously; its PortAudio callback (which runs
     on a non-asyncio audio thread) hands each ~0.25s block to the event loop
     via loop.call_soon_threadsafe.
  2. run() drains those blocks from an asyncio.Queue and feeds them to a
     ChunkAssembler, which emits a chunk_seconds-long window every
     (chunk_seconds - overlap_seconds).
  3. Each emitted chunk goes synchronously to SilenceDetector.process() and
     asynchronously to RecognitionLoop.enqueue() — same consumers, same
     chunk shape as before; only the windowing changed.

If the block queue ever fills (the event loop stalls for >16s), the OLDEST
block is dropped and a warning logged — recent audio wins.
"""

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

import numpy as np
import sounddevice as sd

from src.audio.chunking import ChunkAssembler

if TYPE_CHECKING:
    from src.audio.silence import SilenceDetector
    from src.audio.recognizer import RecognitionLoop
    from src.config import AudioConfig

log = logging.getLogger(__name__)

# Size of each InputStream callback block, in seconds.  Small enough to keep
# silence-detection latency low, large enough that call_soon_threadsafe runs
# only ~4×/second.
_BLOCK_SECONDS = 0.25

# Max blocks buffered between the audio callback and run().  64 × 0.25s = 16s
# of slack before the drop-oldest policy kicks in.
_BLOCK_QUEUE_MAX = 64

# How often the silence ticker re-evaluates the end-of-session timer when no
# audio chunks are arriving (B-6).  The session-end threshold is tens of
# seconds, so 1s granularity is plenty and the cost (one comparison) is trivial.
_SILENCE_TICK_SECONDS = 1.0

# PCONC-4: minimum wall-clock gap between drop-oldest warnings. The overflow
# fires once per dropped block (4×/s per stalled second), and one stalled loop
# turn was measured emitting 53 WARNING records — an SD-card log flood. So the
# drops are COUNTED and surfaced as at most one summarizing warning per this
# interval: a sustained-backlog health signal, not a per-event log.
_DROP_WARN_INTERVAL_SECONDS = 5.0

# CONC-5: how long run() waits for a block before deciding the stream is DEAD.
# Blocks arrive ~every _BLOCK_SECONDS (4×/s) on a healthy stream, so a gap of
# 16 blocks' worth (4s) means the PortAudio callback has stopped firing — the
# device browned out / was unplugged, or the callback aborted from CFFI — and
# no exception surfaced in the consumer. Generous enough that a transient
# event-loop hiccup can't false-trip it, short enough to recover promptly.
_BLOCK_STALL_TIMEOUT_SECONDS = 16 * _BLOCK_SECONDS

# Backoff before rebuilding the stream after a capture error (a construction
# failure OR a detected stall). Named so tests can drive the retry fast.
_STREAM_RETRY_BACKOFF_SECONDS = 1.0


class AudioCapture:
    """Wraps sounddevice to stream overlapping audio chunks from the USB interface."""

    def __init__(self, config: "AudioConfig", silence: "SilenceDetector", recognizer: "RecognitionLoop"):
        self.silence = silence
        self.recognizer = recognizer
        self._running = False
        self._blocks: Optional[asyncio.Queue] = None

        # PCONC-4: drop-oldest bookkeeping. `_drop_count` accrues drops since the
        # last warning; `_last_drop_warn` is the monotonic timestamp of that
        # warning, so bursts collapse into one throttled health signal.
        # Seeded to -inf (NOT 0.0) so the very first drop always reports,
        # independent of the monotonic epoch: on the Pi CLOCK_MONOTONIC is
        # uptime-based and resets to ~0 on reboot, so a 0.0 seed would swallow
        # the first overflow warning during the first _DROP_WARN_INTERVAL_SECONDS
        # of uptime — the exact early-boot window the signal matters most.
        self._drop_count = 0
        self._last_drop_warn = float("-inf")

        self.sample_rate: int = config.sample_rate
        self.chunk_seconds: int = config.chunk_seconds
        self.overlap_seconds: int = config.overlap_seconds
        self.device_name: str = config.device_name

        # Guard against a misconfigured overlap: hop must stay >= 1 frame.
        # overlap >= chunk would mean each chunk advances zero (or negative)
        # frames — an infinite re-recognition of the same audio.
        if self.overlap_seconds >= self.chunk_seconds:
            log.warning(
                f"audio.overlap_seconds ({self.overlap_seconds}) >= "
                f"chunk_seconds ({self.chunk_seconds}); disabling overlap. "
                f"Fix config.yaml — overlap must be smaller than the chunk."
            )
            self.overlap_seconds = 0

    def _find_device_index(self) -> int:
        """Look up the sounddevice index for the configured device name.

        Matching is case-insensitive substring against the device name.  If
        more than one input device matches, the first is used but ALL matches
        are logged — multi-USB-audio setups (e.g. UCA222 + a USB mic) can be
        diagnosed from the logs without having to guess which one got picked.
        """
        devices = sd.query_devices()
        matches = [
            (i, device) for i, device in enumerate(devices)
            if (
                self.device_name.lower() in device["name"].lower()
                and device["max_input_channels"] > 0
            )
        ]
        if matches:
            if len(matches) > 1:
                others = ", ".join(f"[{i}] {d['name']}" for i, d in matches[1:])
                log.warning(
                    f"Multiple input devices match '{self.device_name}'. "
                    f"Using the first; others were: {others}. "
                    f"Tighten audio.device_name in config.yaml if this is wrong."
                )
            i, device = matches[0]
            log.info(f"Using audio device [{i}]: {device['name']}")
            return i
        available = [d["name"] for d in devices if d["max_input_channels"] > 0]
        raise ValueError(
            f"Audio device '{self.device_name}' not found. "
            f"Available input devices: {available}"
        )

    def _enqueue_block(self, blocks: asyncio.Queue, block: np.ndarray):
        """Put an audio block on the queue, dropping the OLDEST first when it's
        full so recent audio wins — the drop-oldest overflow policy the module
        docstring sells as a correctness feature.

        Runs on the event-loop thread (scheduled by the callback via
        call_soon_threadsafe), never on the PortAudio audio thread.
        """
        if blocks.full():
            try:
                blocks.get_nowait()  # Drop the OLDEST block — recent audio wins
            except asyncio.QueueEmpty:  # pragma: no cover — full() just said otherwise
                pass
            else:
                # PCONC-4: count every drop, but WARN at most once per
                # _DROP_WARN_INTERVAL_SECONDS with the aggregate since the last
                # report — so a stalled loop dropping 4 blocks/s can't flood the
                # journal (53 records in one turn was measured). The first drop
                # after a quiet spell reports immediately (`_last_drop_warn`
                # starts at -inf); a sustained backlog then reports periodically.
                self._drop_count += 1
                now = time.monotonic()
                if now - self._last_drop_warn >= _DROP_WARN_INTERVAL_SECONDS:
                    log.warning(
                        "Audio block queue full; dropped %d block(s) since the "
                        "last report — the event loop is stalling (recent audio "
                        "wins).",
                        self._drop_count,
                    )
                    self._drop_count = 0
                    self._last_drop_warn = now
        blocks.put_nowait(block)

    def _make_callback(self, loop: asyncio.AbstractEventLoop, blocks: asyncio.Queue):
        """Build the InputStream callback (runs on the PortAudio audio thread).

        The callback must never touch asyncio objects directly — it marshals
        each block onto the event loop with call_soon_threadsafe, where
        _enqueue_block applies the drop-oldest overflow policy.
        """
        def callback(indata, frames, time_info, status):
            try:
                if status:
                    log.warning(f"Audio input status: {status}")
                # Copy: PortAudio reuses the indata buffer after the callback returns.
                block = indata[:, 0].copy()
                loop.call_soon_threadsafe(self._enqueue_block, blocks, block)
            except Exception as e:
                # CONC-5: a raising callback aborts the PortAudio stream from CFFI
                # with NO exception surfacing in run(), so capture would silently
                # die. Swallow + log so the stream keeps running; a genuinely dead
                # stream is then caught by the block-stall timeout in run().
                log.error(f"Audio callback error: {e}")

        return callback

    async def _silence_ticker(self):
        """Periodically poke the SilenceDetector so the end-of-session timer is
        evaluated even when no audio chunks are arriving (B-6).

        process() only runs on chunk arrival, so a stall during silence (an
        InputStream error parking run() in its retry sleep, or a drained block
        queue) would otherwise leave a completed album's SESSION_ENDED unfired
        and its Play Count never credited.  This task ticks on wall-clock time
        independently of chunk flow.
        """
        while self._running:
            await asyncio.sleep(_SILENCE_TICK_SECONDS)
            try:
                self.silence.tick()
            except Exception as e:
                # A listener raising must not kill the ticker — that would
                # permanently disable the session-end safety net this task
                # exists to provide.  (CancelledError is BaseException and is
                # intentionally NOT caught, so shutdown still propagates.)
                log.error(f"Silence ticker tick failed: {e}")

    async def run(self):
        """Main capture loop. Streams audio and dispatches overlapping chunks."""
        device_index = self._find_device_index()
        loop = asyncio.get_running_loop()

        # int() guards against fractional seconds in config.yaml (v1.3.5):
        # float frame counts previously sailed through to numpy slicing and
        # crashed mid-capture with a cryptic TypeError.  ChunkAssembler also
        # validates integrality as a second line of defence.
        chunk_frames = int(self.chunk_seconds * self.sample_rate)
        hop_frames = int((self.chunk_seconds - self.overlap_seconds) * self.sample_rate)
        self._running = True

        log.info(
            f"Starting audio capture: {self.chunk_seconds}s chunks "
            f"at {self.sample_rate}Hz, new chunk every "
            f"{self.chunk_seconds - self.overlap_seconds}s "
            f"({self.overlap_seconds}s overlap)"
        )

        # Independent timer tick so SESSION_ENDED fires on wall-clock time even
        # while the stream is down and no chunks flow (B-6).
        ticker = asyncio.create_task(self._silence_ticker())
        try:
            while self._running:
                # Clear any stuck "music playing" flag from a previous stream
                # so recovered audio re-emits MUSIC_STARTED (B-6).
                self.silence.reset_music_state()
                assembler = ChunkAssembler(chunk_frames, hop_frames)
                blocks: asyncio.Queue = asyncio.Queue(maxsize=_BLOCK_QUEUE_MAX)
                self._blocks = blocks
                try:
                    stream = sd.InputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                        device=device_index,
                        blocksize=int(_BLOCK_SECONDS * self.sample_rate),
                        callback=self._make_callback(loop, blocks),
                    )
                    with stream:
                        while self._running:
                            try:
                                block = await asyncio.wait_for(
                                    blocks.get(), timeout=_BLOCK_STALL_TIMEOUT_SECONDS
                                )
                            except asyncio.TimeoutError:
                                # CONC-5: no block for the stall window while the
                                # stream is open → the PortAudio callback stopped
                                # firing (device brown-out/unplug, or a callback
                                # abort from CFFI). Nothing raised on its own, so
                                # raise here to reuse the tear-down + backoff +
                                # rebuild path below — the same one a stream
                                # CONSTRUCTION failure already takes.
                                raise RuntimeError(
                                    f"audio stream stalled: no block for "
                                    f"{_BLOCK_STALL_TIMEOUT_SECONDS}s (device "
                                    f"brown-out/unplug or callback abort)"
                                )
                            if block is None:
                                continue  # stop() sentinel — re-check self._running
                            for chunk in assembler.feed(block):
                                # Silence detection is sync and fast (one RMS).
                                self.silence.process(chunk, self.sample_rate)
                                # Recognition enqueue never blocks (drops when full).
                                await self.recognizer.enqueue(chunk, self.sample_rate)
                except Exception as e:
                    # CancelledError is BaseException and intentionally NOT caught
                    # here — shutdown cancellation propagates to main() cleanly.
                    log.error(f"Audio capture error: {e}")
                    await asyncio.sleep(_STREAM_RETRY_BACKOFF_SECONDS)  # Then retry with a fresh stream
        finally:
            # Tear the ticker down with the capture loop (covers normal exit and
            # cancellation), and await it so it doesn't outlive run().
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass

    def stop(self):
        self._running = False
        # Wake run() if it's parked on blocks.get() so it can observe
        # self._running == False without needing to be cancelled.
        if self._blocks is not None:
            try:
                self._blocks.put_nowait(None)
            except asyncio.QueueFull:
                pass  # run() has plenty to wake up for already
        log.info("Audio capture stopped.")
