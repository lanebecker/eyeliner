"""AudD recognition backend (#453).

_parse_audd is a pure, network-free parser (mirrors test_recognizer_parse.py);
recognize() orchestration is tested with transport + encoder mocked, so no
aiohttp / soundfile / network is required.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audio.recognizer import (
    AuddBackend, RawRecognitionResult, RecognitionLoop, _AuddApiError,
)
from tests.factories import make_recognition_config


def _ok(title="Where Is Everyone", artist="Lunar Vacation", album="Where Is Everyone"):
    return {"status": "success", "result": {
        "artist": artist, "title": title, "album": album,
        "release_date": "2021-10-05", "song_link": "https://lis.tn/x",
    }}


# --- pure parse ---

def test_parse_full_success():
    assert AuddBackend._parse_audd(_ok()) == \
        RawRecognitionResult("Where Is Everyone", "Lunar Vacation", "Where Is Everyone", None)

def test_parse_null_result_is_no_match():
    assert AuddBackend._parse_audd({"status": "success", "result": None}) is None

def test_parse_non_dict_is_none():
    assert AuddBackend._parse_audd(None) is None
    assert AuddBackend._parse_audd([1, 2]) is None
    assert AuddBackend._parse_audd({"status": "success", "result": [1]}) is None

def test_parse_empty_title_is_no_match():
    assert AuddBackend._parse_audd({"status": "success", "result": {"title": "  ", "artist": "A"}}) is None

def test_parse_missing_fields_coerce_to_empty():
    assert AuddBackend._parse_audd({"status": "success", "result": {"title": "T"}}) == \
        RawRecognitionResult("T", "", "", None)

def test_parse_numeric_fields_coerced_to_str():
    r = AuddBackend._parse_audd({"status": "success", "result": {"title": 123, "artist": 456, "album": 789}})
    assert (r.title, r.artist, r.album) == ("123", "456", "789")

def test_parse_error_status_raises():
    with pytest.raises(_AuddApiError):
        AuddBackend._parse_audd({"status": "error", "error": {"error_code": 901, "error_message": "no api_token"}})

def test_parse_unknown_status_is_none():
    assert AuddBackend._parse_audd({"status": "weird"}) is None


# --- recognize() orchestration (transport + encoder mocked) ---

@pytest.mark.asyncio
async def test_recognize_returns_parsed_result():
    b = AuddBackend("tok")
    with patch("src.audio.recognizer.ShazamIOBackend._encode_wav", return_value=b"wav"), \
         patch.object(b, "_call_audd", AsyncMock(return_value=_ok())):
        r = await b.recognize(object(), 44100)
    assert r.title == "Where Is Everyone"

@pytest.mark.asyncio
async def test_recognize_error_status_is_swallowed_as_miss():
    b = AuddBackend("tok")
    err = {"status": "error", "error": {"error_message": "quota exceeded"}}
    with patch("src.audio.recognizer.ShazamIOBackend._encode_wav", return_value=b"wav"), \
         patch.object(b, "_call_audd", AsyncMock(return_value=err)):
        assert await b.recognize(object(), 44100) is None

@pytest.mark.asyncio
async def test_recognize_transport_exception_is_swallowed():
    b = AuddBackend("tok")
    with patch("src.audio.recognizer.ShazamIOBackend._encode_wav", return_value=b"wav"), \
         patch.object(b, "_call_audd", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await b.recognize(object(), 44100) is None


# --- backend selection ---

def test_init_backend_selects_audd_with_token():
    cfg = make_recognition_config(backend="audd", audd_api_token="tok")
    loop = RecognitionLoop(cfg, MagicMock(), MagicMock())
    assert isinstance(loop.backend, AuddBackend)
    assert loop.backend._api_token == "tok"


# --- #454 step 1: surface AudD match offset (timecode) ---

def test_parse_populates_match_offset_from_timecode():
    r = AuddBackend._parse_audd({"status": "success", "result": {"title": "T", "timecode": "01:33"}})
    assert r.match_offset == 93.0

def test_parse_missing_timecode_offset_is_none():
    assert AuddBackend._parse_audd({"status": "success", "result": {"title": "T"}}).match_offset is None

def test_parse_malformed_timecode_offset_is_none():
    for bad in ("xx", "", "1", "1:2:3:4", "a:b"):
        r = AuddBackend._parse_audd({"status": "success", "result": {"title": "T", "timecode": bad}})
        assert r.match_offset is None, bad
