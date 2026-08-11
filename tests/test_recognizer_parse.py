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


# ---------------------------------------------------------------------------
# REC-5 — a null in the optional album metadata must not sink a valid match.
# ---------------------------------------------------------------------------

def test_parse_null_sections_keeps_valid_title_artist(caplog):
    """A JSON-null `sections` is a KNOWN Shazam shape → handled CLEANLY by the
    `or []` guard (no warning), not swallowed by the album try/except."""
    import logging
    r = {"track": {"title": "Real Title", "subtitle": "Real Artist", "sections": None}}
    with caplog.at_level(logging.WARNING, logger="src.audio.recognizer"):
        out = ShazamIOBackend._parse_shazam(r)
    assert out is not None
    assert out.title == "Real Title"
    assert out.artist == "Real Artist"
    assert out.album == ""
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


def test_parse_null_metadata_keeps_valid_title_artist(caplog):
    """A JSON-null `metadata` is likewise handled cleanly by the `or []` guard."""
    import logging
    r = {"track": {"title": "T", "subtitle": "A", "sections": [{"metadata": None}]}}
    with caplog.at_level(logging.WARNING, logger="src.audio.recognizer"):
        out = ShazamIOBackend._parse_shazam(r)
    assert out is not None
    assert out.title == "T"
    assert out.album == ""
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


def test_parse_malformed_album_section_does_not_sink_match(caplog):
    """The album try/except is the LAST-resort guard for a genuinely malformed
    album shape (here a section that isn't a dict): the valid title/artist still
    return with album="", and a warning is logged (distinguishing it from the
    clean null-guard path above)."""
    import logging
    r = {"track": {"title": "T", "subtitle": "A", "sections": [42]}}
    with caplog.at_level(logging.WARNING, logger="src.audio.recognizer"):
        out = ShazamIOBackend._parse_shazam(r)
    assert out is not None
    assert out.title == "T"
    assert out.album == ""
    assert any("album parse failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# #167 — a `track` present as a non-dict (e.g. a JSON list) must be a clean
# no-match, not an AttributeError that escapes _parse_shazam to recognize()'s
# broad except (where it logs a misleading "recognition failed" WARNING before
# the miss). The null-CONTAINER shapes this issue also named (null sections /
# metadata / list entries) are already handled by the REC-5 `or []` + album
# try/except above — see the three tests immediately preceding — so this
# non-dict `track` guard is the one remaining shape.
# ---------------------------------------------------------------------------

def test_parse_non_dict_track_is_a_clean_none_not_a_raise():
    """`track` truthy but not a dict (a list) must return None cleanly — before
    the fix, track.get('title') raised AttributeError out of _parse_shazam."""
    assert ShazamIOBackend._parse_shazam({"track": ["not", "a", "dict"]}) is None
    assert ShazamIOBackend._parse_shazam({"track": 42}) is None


# ---------------------------------------------------------------------------
# R5-10 (#239) — non-string fields in an untrusted Shazam payload must not
# crash the recognition loop.
# ---------------------------------------------------------------------------

from src.audio.recognizer import RecognitionLoop  # noqa: E402
from tests.factories import make_recognition_config  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402
import pytest  # noqa: E402


def test_numeric_subtitle_is_coerced_to_string():
    parsed = ShazamIOBackend._parse_shazam({"track": {"title": "Song", "subtitle": 12345}})
    assert parsed is not None
    assert parsed.artist == "12345"
    assert isinstance(parsed.artist, str)


def test_numeric_title_and_album_and_isrc_are_coerced():
    resp = {"track": {
        "title": 777, "subtitle": "Band", "isrc": 42,
        "sections": [{"metadata": [{"title": "Album", "text": 2020}]}],
    }}
    parsed = ShazamIOBackend._parse_shazam(resp)
    assert parsed.title == "777"
    assert parsed.album == "2020"
    assert parsed.isrc == "42"
    assert all(isinstance(x, str) for x in (parsed.title, parsed.artist, parsed.album, parsed.isrc))


def _loop(cr=2):
    cfg = make_recognition_config(confirmation_required=cr)
    st = MagicMock(); st.current_raw = None; st.current_track = None; st.session_epoch = 0
    with patch.object(RecognitionLoop, "_init_backend", return_value=MagicMock()):
        return RecognitionLoop(cfg, st, AsyncMock())


@pytest.mark.asyncio
async def test_numeric_subtitle_result_does_not_crash_the_loop():
    """RED before R5-10: a numeric subtitle produced artist=<int>, and the second
    identical result hit _norm(<int>).split() → AttributeError every chunk, so
    the track never confirmed and no miss was counted."""
    loop = _loop(2)
    r = ShazamIOBackend._parse_shazam({"track": {"title": "Song", "subtitle": 12345}})
    await loop._handle_result(r, 0)
    await loop._handle_result(r, 0)   # must not raise
    # two identical valid results confirm normally
    assert loop.on_confirmed.await_count == 1
