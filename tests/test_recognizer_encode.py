"""Regression tests for P-6 — the per-chunk WAV encode runs in an executor.

sf.write is a blocking C call; doing it inline on the recognition loop briefly
stalls the event loop on every chunk.  recognize() now offloads it.
"""
import io
import sys
import threading
import types

import numpy as np
import pytest
import soundfile as sf

from src.audio.recognizer import ShazamIOBackend, RawRecognitionResult


@pytest.fixture
def fake_shazamio(monkeypatch):
    """Inject fake `shazamio` + `aiohttp_retry` modules so recognize() runs without
    the real (uninstalled) dependencies.  Mirrors the imports `_call_shazam` makes:
    Shazam(http_client=HTTPClient(retry_options=ExponentialRetry(...))) — PCONC-2."""
    mod = types.ModuleType("shazamio")

    class FakeShazam:
        def __init__(self, http_client=None):   # PCONC-2: accepts the pinned client
            self.http_client = http_client

        async def recognize(self, wav_bytes):
            FakeShazam.last_len = len(wav_bytes)
            return {"track": {
                "title": "So What",
                "subtitle": "Miles Davis",
                "sections": [{"metadata": [{"title": "Album", "text": "Kind of Blue"}]}],
            }}

    class FakeHTTPClient:
        def __init__(self, retry_options=None):
            self.retry_options = retry_options

    mod.Shazam = FakeShazam
    mod.HTTPClient = FakeHTTPClient
    monkeypatch.setitem(sys.modules, "shazamio", mod)

    retry_mod = types.ModuleType("aiohttp_retry")

    class FakeExponentialRetry:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    retry_mod.ExponentialRetry = FakeExponentialRetry
    monkeypatch.setitem(sys.modules, "aiohttp_retry", retry_mod)
    return FakeShazam


def test_encode_wav_round_trips():
    audio = np.sin(np.linspace(0, 50, 4000)).astype("float32")
    wav = ShazamIOBackend._encode_wav(audio, 8000)
    data, sr = sf.read(io.BytesIO(wav))
    assert sr == 8000
    assert len(data) == 4000


@pytest.mark.asyncio
async def test_recognize_encodes_off_the_event_loop_thread(fake_shazamio):
    """The WAV encode must run in an executor thread, not inline on the loop
    (P-6).  We record the thread the encode runs on and assert it isn't the
    main/event-loop thread."""
    backend = ShazamIOBackend()
    main_thread = threading.current_thread()
    seen = {}

    real_encode = ShazamIOBackend._encode_wav

    def spy(audio, sample_rate):
        seen["thread"] = threading.current_thread()
        return real_encode(audio, sample_rate)

    backend._encode_wav = spy  # instance shadow used by recognize()

    audio = np.zeros(4000, dtype="float32")
    result = await backend.recognize(audio, 8000)

    assert isinstance(result, RawRecognitionResult)
    assert result.title == "So What"
    assert result.artist == "Miles Davis"
    assert result.album == "Kind of Blue"
    assert seen["thread"] is not main_thread     # ran in an executor thread


@pytest.mark.asyncio
async def test_recognize_returns_none_when_no_track(fake_shazamio, monkeypatch):
    backend = ShazamIOBackend()

    class NoMatch:
        async def recognize(self, wav_bytes):
            return {"track": None}

    monkeypatch.setattr(sys.modules["shazamio"], "Shazam", NoMatch)
    result = await backend.recognize(np.zeros(4000, dtype="float32"), 8000)
    assert result is None
