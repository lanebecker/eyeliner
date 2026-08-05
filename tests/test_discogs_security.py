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
