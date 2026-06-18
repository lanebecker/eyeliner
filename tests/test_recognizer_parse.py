"""Tests for A-13 — recognize() split out a pure, testable _parse_shazam.

The fragile Shazam response-shape knowledge is now isolated from transport and
unit-testable against captured-shape JSON (no network, no shazamio import).
"""
from src.audio.recognizer import ShazamIOBackend, RawRecognitionResult


def _response(title="So What", artist="Miles Davis", album="Kind of Blue", isrc="USSM15900001"):
    return {"track": {
        "title": title,
        "subtitle": artist,
        "isrc": isrc,
        "sections": [{"metadata": [{"title": "Album", "text": album}]}],
    }}


def test_parse_full_response():
    r = ShazamIOBackend._parse_shazam(_response())
    assert r == RawRecognitionResult("So What", "Miles Davis", "Kind of Blue", "USSM15900001")


def test_parse_no_track_returns_none():
    assert ShazamIOBackend._parse_shazam({"track": None}) is None
    assert ShazamIOBackend._parse_shazam({}) is None
    assert ShazamIOBackend._parse_shazam(None) is None


def test_parse_missing_album_is_empty_string():
    resp = {"track": {"title": "T", "subtitle": "A", "sections": []}}
    r = ShazamIOBackend._parse_shazam(resp)
    assert r.title == "T"
    assert r.artist == "A"
    assert r.album == ""


def test_parse_finds_album_in_a_later_section():
    resp = {"track": {"title": "T", "subtitle": "A", "sections": [
        {"metadata": [{"title": "Released", "text": "1959"}]},
        {"metadata": [{"title": "Album", "text": "Kind of Blue"}]},
    ]}}
    assert ShazamIOBackend._parse_shazam(resp).album == "Kind of Blue"


def test_parse_empty_track_dict_is_none():
    # A falsy (empty) track means "no match".
    assert ShazamIOBackend._parse_shazam({"track": {}}) is None


def test_parse_partial_track_defaults_safely():
    # A track with only a title must not raise; missing fields default.
    r = ShazamIOBackend._parse_shazam({"track": {"title": "Only Title"}})
    assert r == RawRecognitionResult("Only Title", "", "", None)
