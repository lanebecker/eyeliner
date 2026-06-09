"""Unit tests for ChunkAssembler — the overlapping-window logic behind capture.

Through v1.3.2, "overlapping" chunks were recorded with a sleep between
blocking sd.rec() calls, leaving a dead gap of (chunk - overlap) seconds
between consecutive chunks.  v1.3.3 moved to continuous InputStream capture
with this assembler doing the windowing.  These tests pin down the contract:
a chunk_frames-long window is emitted every hop_frames, consecutive windows
genuinely share (chunk - hop) frames, and no audio is ever lost between
windows.

ChunkAssembler is pure numpy, so no audio hardware (or even sounddevice
installation) is required.

Verifies:
  ✓ Constructor validation (chunk_frames > 0, 1 <= hop_frames <= chunk_frames)
  ✓ No emission until a full chunk has accumulated
  ✓ Emitted windows start exactly hop_frames apart and overlap correctly
  ✓ A single oversized block can emit multiple chunks at once
  ✓ Emitted chunks are independent copies, exactly chunk_frames long
  ✓ buffered_frames / reset behavior
  ✓ 2-D input blocks are flattened defensively
"""
import numpy as np
import pytest

from src.audio.chunking import ChunkAssembler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ramp(start: int, length: int) -> np.ndarray:
    """A float32 ramp [start, start+length) — makes frame positions checkable."""
    return np.arange(start, start + length, dtype=np.float32)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

def test_chunk_frames_must_be_positive():
    with pytest.raises(ValueError):
        ChunkAssembler(chunk_frames=0, hop_frames=1)


def test_hop_frames_must_be_at_least_one():
    with pytest.raises(ValueError):
        ChunkAssembler(chunk_frames=10, hop_frames=0)


def test_hop_frames_must_not_exceed_chunk_frames():
    with pytest.raises(ValueError):
        ChunkAssembler(chunk_frames=10, hop_frames=11)


def test_hop_equal_to_chunk_is_valid_back_to_back_mode():
    a = ChunkAssembler(chunk_frames=10, hop_frames=10)
    chunks = a.feed(ramp(0, 20))
    assert len(chunks) == 2
    np.testing.assert_array_equal(chunks[0], ramp(0, 10))
    np.testing.assert_array_equal(chunks[1], ramp(10, 10))


# ---------------------------------------------------------------------------
# Accumulation and emission
# ---------------------------------------------------------------------------

def test_no_emission_until_full_chunk_accumulates():
    a = ChunkAssembler(chunk_frames=10, hop_frames=6)
    assert a.feed(ramp(0, 4)) == []
    assert a.feed(ramp(4, 5)) == []
    assert a.buffered_frames == 9


def test_emission_once_chunk_completes():
    a = ChunkAssembler(chunk_frames=10, hop_frames=6)
    a.feed(ramp(0, 9))
    chunks = a.feed(ramp(9, 1))
    assert len(chunks) == 1
    np.testing.assert_array_equal(chunks[0], ramp(0, 10))


def test_consecutive_chunks_overlap_by_chunk_minus_hop():
    """With chunk=10/hop=6, chunk N starts at frame N*6 and shares 4 frames
    with its predecessor — the actual overlap the config promises."""
    a = ChunkAssembler(chunk_frames=10, hop_frames=6)
    chunks = a.feed(ramp(0, 30))
    assert len(chunks) >= 3
    np.testing.assert_array_equal(chunks[0], ramp(0, 10))   # frames [0, 10)
    np.testing.assert_array_equal(chunks[1], ramp(6, 10))   # frames [6, 16)
    np.testing.assert_array_equal(chunks[2], ramp(12, 10))  # frames [12, 22)
    # The shared region really is identical audio
    np.testing.assert_array_equal(chunks[0][6:], chunks[1][:4])


def test_single_large_block_emits_multiple_chunks():
    a = ChunkAssembler(chunk_frames=4, hop_frames=2)
    chunks = a.feed(ramp(0, 12))
    starts = [int(c[0]) for c in chunks]
    assert starts == [0, 2, 4, 6, 8]
    assert all(len(c) == 4 for c in chunks)


def test_no_audio_lost_across_many_small_blocks():
    """Feeding 1-frame blocks must produce the same windows as one big feed."""
    big = ChunkAssembler(chunk_frames=8, hop_frames=3)
    expected = big.feed(ramp(0, 40))

    small = ChunkAssembler(chunk_frames=8, hop_frames=3)
    collected = []
    for i in range(40):
        collected.extend(small.feed(ramp(i, 1)))

    assert len(collected) == len(expected)
    for got, want in zip(collected, expected):
        np.testing.assert_array_equal(got, want)


def test_chunks_are_independent_copies():
    a = ChunkAssembler(chunk_frames=4, hop_frames=2)
    chunks = a.feed(ramp(0, 8))
    chunks[0][:] = -1.0  # Mutating an emitted chunk...
    more = a.feed(ramp(8, 4))
    # ...must not corrupt subsequently emitted audio
    assert all((c >= 0).all() for c in more)


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def test_buffered_frames_tracks_remainder():
    a = ChunkAssembler(chunk_frames=10, hop_frames=6)
    a.feed(ramp(0, 25))
    # Emitted at 0 and 6; buffer advanced by 12, holding frames [12, 25) = 13,
    # which is then reduced below chunk_frames by the final emission at 12.
    assert a.buffered_frames < 10


def test_reset_drops_buffered_audio():
    a = ChunkAssembler(chunk_frames=10, hop_frames=6)
    a.feed(ramp(0, 9))
    a.reset()
    assert a.buffered_frames == 0
    assert a.feed(ramp(0, 9)) == []  # Needs a full fresh chunk again


def test_two_dimensional_blocks_are_flattened():
    a = ChunkAssembler(chunk_frames=4, hop_frames=4)
    block = ramp(0, 4).reshape(4, 1)  # Shaped like raw sounddevice indata
    chunks = a.feed(block)
    assert len(chunks) == 1
    assert chunks[0].ndim == 1
    np.testing.assert_array_equal(chunks[0], ramp(0, 4))
