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


# ---------------------------------------------------------------------------
# REC-3 — a track object with no USABLE TITLE is a no-match, not a recognition.
# The title is the track's identity; without it, two such junk responses would
# "confirm" as a real track and get committed. Parse must return None so the
# loop counts them as misses. (A missing/null title also previously crashed the
# dedup comparison in _same_track — the null-title half of REC-2.)
# ---------------------------------------------------------------------------

def test_parse_missing_title_is_none():
    # Track exists (so it's not the falsy-track path) but has no title key.
    assert ShazamIOBackend._parse_shazam({"track": {"subtitle": "Miles Davis"}}) is None


def test_parse_empty_title_is_none():
    assert ShazamIOBackend._parse_shazam({"track": {"title": "", "subtitle": "A"}}) is None


def test_parse_whitespace_title_is_none():
    assert ShazamIOBackend._parse_shazam({"track": {"title": "   ", "subtitle": "A"}}) is None


def test_parse_null_title_is_none():
    # JSON null (title present but null) — the .get default does NOT apply, so
    # the value is None; parse must treat it as a no-match, not build a result
    # with title=None that later crashes _same_track (null-title half of REC-2).
    assert ShazamIOBackend._parse_shazam({"track": {"title": None, "subtitle": "A"}}) is None
