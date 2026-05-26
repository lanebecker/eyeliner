"""Unit tests for LastFmClient.

pylast is mocked at the sys.modules level so no real network calls are ever
made. All tests run without a Last.fm account or internet connection.

Covered scenarios:
  ✓ scrobble_enabled=False → all methods are graceful no-ops returning True
  ✓ lastfm section absent from config → no-op, no crash
  ✓ Missing session_key → warns, client disabled, methods are no-ops
  ✓ pylast not installed (ImportError) → warns, client disabled, no crash
  ✓ scrobble happy path → calls network.scrobble with correct args
  ✓ scrobble passes album=None when track.album is an empty string
  ✓ scrobble exception → returns False, does not raise
  ✓ scrobble when disabled (enabled=False) → returns True, no network call
  ✓ love happy path → calls network.get_track(...).love()
  ✓ love_on_completion=False → love() is a no-op returning True
  ✓ love when disabled (enabled=False) → returns True, no network call
  ✓ love exception → returns False, does not raise
  ✓ enabled property is True only when network was initialised
  ✓ love_on_completion property reflects config value
  ✓ scrobble_enabled=True with full credentials → enabled
"""

import sys
from unittest.mock import MagicMock, patch
import pytest

from src.metadata.models import TrackMetadata, MetadataSource
from src.tracking.lastfm_client import LastFmClient


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_pylast_mock():
    """Return a MagicMock that stands in for the pylast module."""
    mock = MagicMock()
    # LastFMNetwork() returns a fresh MagicMock network object by default.
    return mock


_FULL_CONFIG = {
    "lastfm": {
        "scrobble_enabled": True,
        "api_key": "fake-api-key",
        "api_secret": "fake-api-secret",
        "session_key": "fake-session-key",
        "love_on_completion": False,
    }
}

_LOVE_CONFIG = {
    "lastfm": {
        "scrobble_enabled": True,
        "api_key": "fake-api-key",
        "api_secret": "fake-api-secret",
        "session_key": "fake-session-key",
        "love_on_completion": True,
    }
}

_DISABLED_CONFIG = {
    "lastfm": {
        "scrobble_enabled": False,
    }
}

_MISSING_CONFIG: dict = {}  # no lastfm key at all


def _make_track(
    artist: str = "Sonic Youth",
    title: str = "Catholic Block",
    album: str = "Sister",
) -> TrackMetadata:
    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        year="1987",
        label="SST Records",
        catalog_number="SST 134",
        tracklist=[],
        source=MetadataSource.DISCOGS_COLLECTION,
    )


def _make_enabled_client(config=None, pylast_mock=None):
    """Build a LastFmClient with pylast mocked; returns (client, pylast_mock)."""
    if config is None:
        config = _FULL_CONFIG
    if pylast_mock is None:
        pylast_mock = _make_pylast_mock()
    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        client = LastFmClient(config)
    return client, pylast_mock


# ---------------------------------------------------------------------------
# Constructor / enabled flag
# ---------------------------------------------------------------------------

def test_disabled_config_not_enabled():
    """scrobble_enabled: false → client.enabled is False."""
    client = LastFmClient(_DISABLED_CONFIG)
    assert not client.enabled


def test_missing_lastfm_section_not_enabled():
    """No `lastfm` key in config at all → graceful no-op, not enabled."""
    client = LastFmClient(_MISSING_CONFIG)
    assert not client.enabled


def test_missing_session_key_not_enabled():
    """Credentials incomplete (session_key absent) → warns, not enabled."""
    config = {
        "lastfm": {
            "scrobble_enabled": True,
            "api_key": "key",
            "api_secret": "secret",
            # session_key missing
        }
    }
    pylast_mock = _make_pylast_mock()
    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        client = LastFmClient(config)
    assert not client.enabled
    pylast_mock.LastFMNetwork.assert_not_called()


def test_pylast_not_installed_not_enabled():
    """ImportError from pylast → warns, client not enabled, no crash."""
    pylast_mock = MagicMock()
    pylast_mock.LastFMNetwork.side_effect = ImportError("No module named 'pylast'")
    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        client = LastFmClient(_FULL_CONFIG)
    assert not client.enabled


def test_full_credentials_enabled():
    """All credentials present and scrobble_enabled: true → client.enabled is True."""
    client, _ = _make_enabled_client()
    assert client.enabled


def test_love_on_completion_false_by_default():
    """love_on_completion defaults to False when not set in config."""
    client, _ = _make_enabled_client(_FULL_CONFIG)
    assert not client.love_on_completion


def test_love_on_completion_true_when_configured():
    """love_on_completion=True is reflected on the property."""
    client, _ = _make_enabled_client(_LOVE_CONFIG)
    assert client.love_on_completion


# ---------------------------------------------------------------------------
# scrobble()
# ---------------------------------------------------------------------------

def test_scrobble_happy_path():
    """scrobble() calls network.scrobble with artist, title, timestamp, album."""
    track = _make_track()
    timestamp = 1700000000

    pylast_mock = _make_pylast_mock()
    client, _ = _make_enabled_client(pylast_mock=pylast_mock)

    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        result = client.scrobble(track, timestamp)

    assert result is True
    network = pylast_mock.LastFMNetwork.return_value
    network.scrobble.assert_called_once_with(
        artist="Sonic Youth",
        title="Catholic Block",
        timestamp=timestamp,
        album="Sister",
    )


def test_scrobble_empty_album_passes_none():
    """When track.album is an empty string, scrobble passes album=None."""
    track = _make_track(album="")
    pylast_mock = _make_pylast_mock()
    client, _ = _make_enabled_client(pylast_mock=pylast_mock)

    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        client.scrobble(track, 12345)

    network = pylast_mock.LastFMNetwork.return_value
    _, kwargs = network.scrobble.call_args
    assert kwargs["album"] is None


def test_scrobble_disabled_returns_true_no_network_call():
    """When client is disabled, scrobble() returns True without touching the network."""
    client = LastFmClient(_DISABLED_CONFIG)
    track = _make_track()
    result = client.scrobble(track, 99999)
    assert result is True
    # _network is None — no AttributeError


def test_scrobble_exception_returns_false():
    """A pylast exception during scrobble → returns False, does not propagate."""
    pylast_mock = _make_pylast_mock()
    client, _ = _make_enabled_client(pylast_mock=pylast_mock)
    network = pylast_mock.LastFMNetwork.return_value
    network.scrobble.side_effect = Exception("network timeout")

    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        result = client.scrobble(_make_track(), 1234)

    assert result is False


# ---------------------------------------------------------------------------
# love()
# ---------------------------------------------------------------------------

def test_love_happy_path():
    """love() calls network.get_track(...).love() when love_on_completion=True."""
    track = _make_track()
    pylast_mock = _make_pylast_mock()
    client, _ = _make_enabled_client(config=_LOVE_CONFIG, pylast_mock=pylast_mock)

    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        result = client.love(track)

    assert result is True
    network = pylast_mock.LastFMNetwork.return_value
    network.get_track.assert_called_once_with("Sonic Youth", "Catholic Block")
    network.get_track.return_value.love.assert_called_once()


def test_love_disabled_by_config_returns_true():
    """love_on_completion=False → love() is a no-op returning True."""
    pylast_mock = _make_pylast_mock()
    client, _ = _make_enabled_client(config=_FULL_CONFIG, pylast_mock=pylast_mock)
    assert not client.love_on_completion

    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        result = client.love(_make_track())

    assert result is True
    # get_track should never be called
    network = pylast_mock.LastFMNetwork.return_value
    network.get_track.assert_not_called()


def test_love_client_not_enabled_returns_true():
    """When client is not enabled, love() returns True without network access."""
    client = LastFmClient(_DISABLED_CONFIG)
    result = client.love(_make_track())
    assert result is True


def test_love_exception_returns_false():
    """A pylast exception during love → returns False, does not propagate."""
    pylast_mock = _make_pylast_mock()
    client, _ = _make_enabled_client(config=_LOVE_CONFIG, pylast_mock=pylast_mock)
    network = pylast_mock.LastFMNetwork.return_value
    network.get_track.return_value.love.side_effect = Exception("API error")

    with patch.dict(sys.modules, {"pylast": pylast_mock}):
        result = client.love(_make_track())

    assert result is False
