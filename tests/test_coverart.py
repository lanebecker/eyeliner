"""Unit tests for the MusicBrainz cover-art fallback (TQ-3).

``get_cover_art_url`` parses untrusted MusicBrainz Cover Art Archive responses
into a URL that ``src/display/cover_cache.py`` then fetches, so it sits on a
security boundary and previously had NO test file at all.  These tests patch
``musicbrainzngs`` (no network) and cover the happy path, empty/missing lists,
per-release failures (``ResponseError`` AND malformed payloads), and the
return-type contract.

URL scheme/host validation is deliberately NOT this module's job — that is the
SSRF-hardened fetcher (``cover_cache._validate_cover_url``, exercised by the S-1
tests in ``test_cover_cache.py``).  Here we only require that a ``str`` (or
``None``) is returned and that one malformed release never aborts the whole
lookup.
"""

import pytest

import src.metadata.coverart as coverart
from src.metadata.coverart import CoverArtFallback

# The real musicbrainzngs is installed; use its real ResponseError so the
# except clause under test matches exactly.
RESPONSE_ERROR = coverart.musicbrainzngs.ResponseError


def _patch_mb(monkeypatch, *, search, images):
    """Patch ``search_releases`` → *search* and ``get_image_list(mbid)`` →
    ``images[mbid]``.  A value that is an ``Exception`` instance is raised
    instead of returned, to simulate MusicBrainz errors.
    """
    def fake_search(**kwargs):
        if isinstance(search, Exception):
            raise search
        return search

    def fake_get_image_list(mbid):
        value = images[mbid]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(coverart.musicbrainzngs, "search_releases", fake_search)
    monkeypatch.setattr(coverart.musicbrainzngs, "get_image_list", fake_get_image_list)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_large_thumbnail(monkeypatch):
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}]},
        images={"r0": {"images": [{
            "front": True,
            "thumbnails": {"large": "https://coverartarchive.org/r0/large.jpg"},
            "image": "https://coverartarchive.org/r0/front.jpg",
        }]}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r0/large.jpg"


def test_falls_back_to_image_without_large_thumbnail(monkeypatch):
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}]},
        images={"r0": {"images": [{
            "front": True,
            "thumbnails": {},
            "image": "https://coverartarchive.org/r0/front.jpg",
        }]}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r0/front.jpg"


# ---------------------------------------------------------------------------
# Empty / missing lists
# ---------------------------------------------------------------------------

def test_empty_release_list_returns_none(monkeypatch):
    _patch_mb(monkeypatch, search={"release-list": []}, images={})
    assert CoverArtFallback().get_cover_art_url("A", "B") is None


def test_missing_release_list_key_returns_none(monkeypatch):
    _patch_mb(monkeypatch, search={}, images={})
    assert CoverArtFallback().get_cover_art_url("A", "B") is None


def test_search_exception_returns_none(monkeypatch):
    _patch_mb(monkeypatch, search=RESPONSE_ERROR("musicbrainz down"), images={})
    assert CoverArtFallback().get_cover_art_url("A", "B") is None


# ---------------------------------------------------------------------------
# Per-release resilience — one bad release must never abort the whole loop
# ---------------------------------------------------------------------------

def test_response_error_on_first_release_then_success(monkeypatch):
    # The already-handled case, pinned: a ResponseError on r0 skips to r1.
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}, {"id": "r1"}]},
        images={
            "r0": RESPONSE_ERROR("no art for r0"),
            "r1": {"images": [{"front": True,
                               "image": "https://coverartarchive.org/r1/front.jpg"}]},
        },
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r1/front.jpg"


def test_malformed_nondict_images_skips_to_next_release(monkeypatch):
    # r0's images is a list of STRINGS (not dicts) — img.get('front') raises
    # AttributeError.  Before TQ-3 this escaped the inner handler and aborted the
    # whole loop, discarding r1's valid art.  It must now skip r0 and return r1.
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}, {"id": "r1"}]},
        images={
            "r0": {"images": ["not-a-dict", "also-not-a-dict"]},
            "r1": {"images": [{"front": True,
                               "image": "https://coverartarchive.org/r1/front.jpg"}]},
        },
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r1/front.jpg"


def test_non_dict_image_within_list_is_skipped(monkeypatch):
    # A bare string and a valid front dict in the SAME images list: the string is
    # skipped, the dict is used.
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}]},
        images={"r0": {"images": [
            "junk",
            {"front": True, "image": "https://coverartarchive.org/r0/front.jpg"},
        ]}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r0/front.jpg"


def test_malformed_release_object_skips_to_next(monkeypatch):
    # A release entry that is not a dict (no 'id') must not abort the loop.
    _patch_mb(
        monkeypatch,
        search={"release-list": ["not-a-release", {"id": "r1"}]},
        images={"r1": {"images": [{"front": True,
                                   "image": "https://coverartarchive.org/r1/front.jpg"}]}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r1/front.jpg"


def test_all_releases_malformed_returns_none_without_crashing(monkeypatch):
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}, {"id": "r1"}]},
        images={"r0": {"images": ["x"]}, "r1": {"images": "not-even-a-list"}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") is None


# ---------------------------------------------------------------------------
# #175 — a TRANSPORT error mid-loop: classify transient vs permanent.
#   • permanent / per-release (AuthenticationError, a ResponseError/404 for this
#     MBID) → skip to the next candidate (a later release may still have art).
#   • transient / service-down (NetworkError) → abort the loop; trying later
#     releases would just hammer an unreachable service.
# ---------------------------------------------------------------------------

def test_permanent_transport_error_on_first_release_skips_to_next(monkeypatch):
    """A PERMANENT per-release transport error (AuthenticationError on r0) must
    skip to r1, not abort. Before the fix it escaped the inner except (which only
    caught ResponseError + parse errors) and returned None, discarding r1's art."""
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}, {"id": "r1"}]},
        images={
            "r0": coverart.musicbrainzngs.AuthenticationError("401 for r0"),
            "r1": {"images": [{"front": True,
                               "image": "https://coverartarchive.org/r1/front.jpg"}]},
        },
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r1/front.jpg"


def test_transient_error_aborts_the_loop_and_propagates(monkeypatch):
    """A TRANSIENT error (NetworkError = MusicBrainz unreachable) aborts the whole
    loop — later releases would fail the same way, so they are never queried —
    and PROPAGATES rather than flattening to None (#190): the resolver relies on
    the raise to leave the album uncached/retryable instead of pinning a coverless
    FALLBACK for the session. Pins abort-on-transient without hammering a down
    service."""
    queried = []

    def fake_get_image_list(mbid):
        queried.append(mbid)
        raise coverart.musicbrainzngs.NetworkError("MB unreachable")

    monkeypatch.setattr(coverart.musicbrainzngs, "search_releases",
                        lambda **k: {"release-list": [{"id": "r0"}, {"id": "r1"}]})
    monkeypatch.setattr(coverart.musicbrainzngs, "get_image_list", fake_get_image_list)

    with pytest.raises(coverart.musicbrainzngs.NetworkError):
        CoverArtFallback().get_cover_art_url("A", "B")
    assert queried == ["r0"]          # aborted after the transient error; r1 never tried


def test_non_transient_error_skips_to_next_release(monkeypatch):
    """#175: a NON-transient error that isn't a service-down signal (here a
    RuntimeError, standing in for any unexpected/malformed-payload failure that
    is definitive for THIS release) is treated as best-effort-fallback breakage
    of one candidate: skip it and try the next, which succeeds. It does NOT abort
    the whole lookup (only a transient NetworkError does that)."""
    def fake_get_image_list(mbid):
        if mbid == "r0":
            raise RuntimeError("this release's payload is broken")   # not transient
        return {"images": [{"front": True,
                            "image": "https://coverartarchive.org/r1/front.jpg"}]}

    monkeypatch.setattr(coverart.musicbrainzngs, "search_releases",
                        lambda **k: {"release-list": [{"id": "r0"}, {"id": "r1"}]})
    monkeypatch.setattr(coverart.musicbrainzngs, "get_image_list", fake_get_image_list)

    assert CoverArtFallback().get_cover_art_url("A", "B") == \
        "https://coverartarchive.org/r1/front.jpg"


# ---------------------------------------------------------------------------
# Return-type contract
# ---------------------------------------------------------------------------

def test_non_string_url_is_not_returned(monkeypatch):
    # front.image is a non-str (a mis-typed payload).  The fetcher expects a str,
    # so coverart must return None rather than hand a non-str downstream. (TQ-3)
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}]},
        images={"r0": {"images": [{"front": True, "image": {"nested": "dict"}}]}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") is None


def test_file_scheme_url_is_passed_through_to_the_fetcher_gate(monkeypatch):
    # coverart is metadata extraction, NOT the SSRF gate: a file:// URL from a
    # malformed/hostile MusicBrainz payload is returned as a str, and the
    # cover_cache fetcher rejects it (S-1, tested in test_cover_cache.py).  This
    # documents the boundary — validation lives in the fetcher, not here.
    _patch_mb(
        monkeypatch,
        search={"release-list": [{"id": "r0"}]},
        images={"r0": {"images": [{"front": True, "image": "file:///etc/passwd"}]}},
    )
    assert CoverArtFallback().get_cover_art_url("A", "B") == "file:///etc/passwd"
