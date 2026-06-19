"""Typed-config factories for tests (A-2).

After the A-2 typed-config refactor, components take typed section dataclasses
(:class:`AudioConfig`, :class:`RecognitionConfig`, …) instead of raw dicts.
These factories build fully-valid section objects with sensible defaults so a
test only has to name the field it actually cares about::

    cfg = make_audio_config(silence_threshold_rms=0.99)   # everything else valid

Each factory omits the dataclass's own optional fields from its defaults, so a
no-argument call exercises the real dataclass default (e.g. overlap_seconds=5).
"""

from unittest.mock import patch

from src.config import (
    AudioConfig,
    DiscogsConfig,
    DisplayConfig,
    LastFmConfig,
    RecognitionConfig,
)
from src.metadata.discogs import (
    DiscogsHttp,
    DiscogsReader,
    DiscogsCollectionWriter,
)


def make_audio_config(**overrides) -> AudioConfig:
    base = dict(
        device_name="USB Audio Codec",
        sample_rate=44100,
        chunk_seconds=15,
        silence_threshold_rms=0.01,
        session_end_silence_seconds=45,
    )
    base.update(overrides)
    return AudioConfig(**base)


def make_recognition_config(**overrides) -> RecognitionConfig:
    base = dict(poll_interval_seconds=30)
    base.update(overrides)
    return RecognitionConfig(**base)


def make_discogs_config(**overrides) -> DiscogsConfig:
    base = dict(
        user_token="fake-token",
        username="testuser",
        play_count_field_name="Play Count",
    )
    base.update(overrides)
    return DiscogsConfig(**base)


def make_lastfm_config(**overrides) -> LastFmConfig:
    return LastFmConfig(**overrides)


def make_display_config(**overrides) -> DisplayConfig:
    base = dict(width=1024, height=600)
    base.update(overrides)
    return DisplayConfig(**base)


# ---------------------------------------------------------------------------
# Discogs (A-4 split): transport / reader / writer
#
# All three share the single HTTP seam — DiscogsHttp.session — so a test mocks
# transport by assigning ``http.session.get`` / ``.post`` (exactly as the old
# DiscogsClient tests assigned ``client._session.get``).  The reader also holds
# the python3-discogs-client library Client, which is patched out at build time.
# ---------------------------------------------------------------------------

def make_discogs_http(token: str = "fake-token") -> DiscogsHttp:
    return DiscogsHttp(token)


def make_discogs_reader(http=None, config=None) -> DiscogsReader:
    http = http if http is not None else make_discogs_http()
    with patch("src.metadata.discogs.reader.discogs_client.Client"):
        reader = DiscogsReader(http, config or make_discogs_config())
    return reader


def make_discogs_writer(http=None, config=None) -> DiscogsCollectionWriter:
    http = http if http is not None else make_discogs_http()
    return DiscogsCollectionWriter(http, config or make_discogs_config())
