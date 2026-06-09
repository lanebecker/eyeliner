"""ChunkAssembler — turns a continuous audio stream into overlapping chunks.

This module is pure numpy with no sounddevice dependency, so the windowing
logic is fully unit-testable without audio hardware (tests/test_chunking.py).

Why this exists (v1.3.3)
------------------------
Through v1.3.2, AudioCapture recorded a chunk with a blocking sd.rec() call
and then slept for (chunk_seconds - overlap_seconds) before recording the
next one.  Nothing was captured during the sleep, so the "overlap" was
actually a *dead gap* between chunks — with the default 15s chunks and 5s
overlap, 10 seconds of every 25 went unheard.  Music/silence transitions
could be detected up to ~25s late, and a short track could in principle be
missed entirely.

AudioCapture now records continuously via sd.InputStream and feeds every
incoming block to a ChunkAssembler, which emits a chunk_frames-long window
every hop_frames (hop = chunk - overlap).  Consecutive chunks genuinely
share their last/first overlap_frames of audio:

    chunk 0: frames [0,           chunk)
    chunk 1: frames [hop,         hop + chunk)
    chunk 2: frames [2*hop, 2*hop + chunk)
    ...

No audio is ever dropped on the floor between chunks.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)


class ChunkAssembler:
    """Accumulates arbitrary-size audio blocks and emits overlapping chunks.

    Args:
        chunk_frames: Length of each emitted chunk, in frames. Must be > 0.
        hop_frames:   Stride between consecutive chunk start positions, in
                      frames.  Must satisfy 1 <= hop_frames <= chunk_frames.
                      hop == chunk means back-to-back chunks (no overlap);
                      hop < chunk means consecutive chunks share
                      (chunk_frames - hop_frames) frames of audio.

    Memory is bounded: after each emit the internal buffer holds strictly
    fewer than chunk_frames frames, plus at most one incoming block during
    feed().
    """

    def __init__(self, chunk_frames: int, hop_frames: int):
        if chunk_frames <= 0:
            raise ValueError(f"chunk_frames must be > 0, got {chunk_frames}")
        if not (1 <= hop_frames <= chunk_frames):
            raise ValueError(
                f"hop_frames must be in [1, chunk_frames={chunk_frames}], "
                f"got {hop_frames}"
            )
        self.chunk_frames = chunk_frames
        self.hop_frames = hop_frames
        self._buffer = np.zeros(0, dtype=np.float32)

    def feed(self, block: np.ndarray) -> list:
        """Append a block of mono samples; return all newly completed chunks.

        Each returned chunk is an independent copy (callers may hold onto it
        without pinning the rolling buffer), exactly chunk_frames long, in
        arrival order.  Returns an empty list while the buffer is still
        filling.
        """
        if block.ndim != 1:
            block = block.reshape(-1)
        self._buffer = np.concatenate([self._buffer, block.astype(np.float32, copy=False)])

        chunks = []
        while len(self._buffer) >= self.chunk_frames:
            chunks.append(self._buffer[: self.chunk_frames].copy())
            self._buffer = self._buffer[self.hop_frames :]
        return chunks

    @property
    def buffered_frames(self) -> int:
        """Number of frames currently waiting in the rolling buffer."""
        return len(self._buffer)

    def reset(self):
        """Drop any buffered audio (e.g. after a stream restart)."""
        self._buffer = np.zeros(0, dtype=np.float32)
