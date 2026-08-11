"""Unit tests for the Discogs security hardening (findings S-4, S-5).

S-5 — write-URL IDs are coerced to positive ints at the boundary so a corrupt
      API response fails loudly instead of building a surprising request path.
S-4 — request URLs are redacted (username masked, query dropped) before they
      reach the logs.

All HTTP interaction is mocked; nothing here touches the real Discogs API.
"""

import pytest

from src.metadata.discogs.transport import _as_id, _redact_url


# ---------------------------------------------------------------------------
# S-5 — _as_id boundary coercion
# ---------------------------------------------------------------------------

def test_as_id_accepts_positive_int():
    assert _as_id(111, "release_id") == 111


def test_as_id_accepts_numeric_string():
    # Discogs sometimes serialises IDs as strings; a clean numeric string is fine.
    assert _as_id("42", "instance_id") == 42


@pytest.mark.parametrize("bad", ["", "abc", "12; DROP TABLE", "1/../2", None, object()])
def test_as_id_rejects_non_integer(bad):
    with pytest.raises(ValueError):
        _as_id(bad, "field_id")


@pytest.mark.parametrize("bad", [0, -1, -999])
def test_as_id_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        _as_id(bad, "release_id")


# R5-37 (#263): int() silently reshapes bool and float — these must be REJECTED,
# not coerced, or a corrupt value builds a valid-but-wrong write path.
@pytest.mark.parametrize("bad", [True, False])
def test_as_id_rejects_bool(bad):
    # int(True) == 1 / int(False) == 0 would otherwise pass or hit the <=0 guard
    # for the wrong reason; reject as a bool at the boundary.
    with pytest.raises(ValueError):
        _as_id(bad, "instance_id")


@pytest.mark.parametrize("bad", [3.9, 42.0, 1.0, "3.9", "42.0", float("nan")])
def test_as_id_rejects_float(bad):
    # int(3.9) truncates to 3; a float ID is itself a corruption signal.
    with pytest.raises(ValueError):
        _as_id(bad, "release_id")


def test_as_id_accepts_signed_numeric_string():
    # A leading '+' is a clean integer literal; '-5' parses but fails the <=0 guard.
    assert _as_id("+7", "field_id") == 7
    with pytest.raises(ValueError):
        _as_id("-5", "field_id")


@pytest.mark.parametrize("bad", ["４２", "３４２", "²", "٤٢", "1_000", "0x10"])
def test_as_id_rejects_non_ascii_and_nonliteral_digit_strings(bad):
    # str.isdigit() is True for fullwidth/superscript/other-script digits; require
    # a plain ASCII 0-9 literal so only a clean integer string is accepted.
    with pytest.raises(ValueError):
        _as_id(bad, "release_id")


# ---------------------------------------------------------------------------
# S-4 — _redact_url
# ---------------------------------------------------------------------------

def test_redact_url_masks_username():
    url = (
        "https://api.discogs.com/users/lanebecker/collection"
        "/folders/0/releases/111/instances/42/fields/6"
    )
    out = _redact_url(url)
    assert "lanebecker" not in out
    assert "{user}" in out
    # The structural path (IDs) is preserved for debuggability.
    assert "releases/111" in out


def test_redact_url_drops_query_string():
    out = _redact_url("https://api.discogs.com/users/bob/collection?token=secret")
    assert "token" not in out
    assert "secret" not in out


def test_redact_url_handles_garbage_without_raising():
    # Must never raise from a logging path.
    assert isinstance(_redact_url("not a url"), str)
    assert isinstance(_redact_url(""), str)


def test_redact_url_drops_query_on_empty_path_url():
    # SEC-2: when parts.path is empty, "/".join([""]) is falsy, and the old
    # `... or url` fallback returned the RAW url — query string and all. An
    # origin-only or query-bearing URL must still redact to a bare path, never
    # leak the query into the logs.
    out = _redact_url("https://api.discogs.com?token=SECRETTOKEN")
    assert "SECRETTOKEN" not in out
    assert "token" not in out
    assert "api.discogs.com" not in out   # path-only: no scheme/host either
    assert out == "/"


def test_redact_url_masks_username_even_with_a_query_string():
    # The masked path is returned and the query is dropped, together.
    out = _redact_url(
        "https://api.discogs.com/users/lanebecker/collection?token=SECRETTOKEN"
    )
    assert "lanebecker" not in out
    assert "{user}" in out
    assert "SECRETTOKEN" not in out
    assert "?" not in out
