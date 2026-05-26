"""Audio capture from USB audio interface.

Continuously records overlapping chunks from the configured sounddevice input
and feeds them into the recognition loop queue.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd

if TYPE_CHECKING:
    from src.audio.silence import SilenceDetector
    from src.audio.recognizer import RecognitionLoop

log = logging.getLogger(__name__)


class AudioCapture:
    """Wraps sounddevice to record overlapping audio chunks from the USB interface."""

    def __init__(self, config: dict, silence: "SilenceDetector", recognizer: "RecognitionLoop"):
        self.config = config["audio"]
        self.silence = silence
        self.recognizer = recognizer
        self._running = False

        self.sample_rate: int = self.config["sample_rate"]
        self.chunk_seconds: int = self.config["chunk_seconds"]
        self.overlap_seconds: int = self.config.get("overlap_seconds", 5)
        self.device_name: str = self.config["device_name"]

    def _find_device_index(self) -> int:
        """Look up the sounddevice index for the configured device name."""
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if (
                self.device_name.lower() in device["name"].lower()
                and device["max_input_channels"] > 0
            ):
                log.info(f"Using audio device [{i}]: {device['name']}")
                return i
        available = [d["name"] for d in devices if d["max_input_channels"] > 0]
        raise ValueError(
            f"Audio device '{self.device_name}' not found. "
            f"Available input devices: {available}"
        )

    async def run(self):
        """Main capture loop. Records chunks and dispatches to silence detector and recognizer."""
        device_index = self._find_device_index()
        chunk_frames = self.chunk_seconds * self.sample_rate
        self._running = True

        log.info(
            f"Starting audio capture: {self.chunk_seconds}s chunks "
            f"at {self.sample_rate}Hz (overlap: {self.overlap_seconds}s)"
        )

        while self._running:
            try:
                # Record one chunk in a thread pool to avoid blocking the event loop
                audio = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: sd.rec(
                        chunk_frames,
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                        device=device_index,
                        blocking=True,
                    ),
                )
                audio_flat = audio.flatten()

                # Dispatch to silence detector (sync, fast)
                self.silence.process(audio_flat, self.sample_rate)

                # Enqueue for recognition (async, slow — handled by RecognitionLoop)
                await self.recognizer.enqueue(audio_flat, self.sample_rate)

                # Overlap: wait for (chunk - overlap) before recording next chunk.
                # Clamped to 0 so a misconfigured overlap_seconds >= chunk_seconds
                # doesn't produce a negative delay and spin the event loop.
                await asyncio.sleep(max(0, self.chunk_seconds - self.overlap_seconds))

            except Exception as e:
                log.error(f"Audio capture error: {e}")
                await asyncio.sleep(1)

    def stop(self):
        self._running = False
        log.info("Audio capture stopped.")
