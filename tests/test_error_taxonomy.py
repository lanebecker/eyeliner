"""Regression tests for Wave 2 bundle 1 — the error-taxonomy trio.

#189 (err-3): is_transient() must discriminate by HTTP status — a definitive
  4xx (401/403/404: revoked token, wrong username, deleted resource) is
  PERMANENT, while 429/408/5xx and status-less connection/timeout errors stay
  transient.  The old code blanket-classified every requests.HTTPError and
  discogs_client HTTPError as transient, so a dead credential logged only as
  INFO "(transient)" and was indistinguishable from a wifi blip.

#188 (err-1): a transient failure during _build_result's lazy release fetch
  (tracklist/master/etc.) must PROPAGATE to the resolve boundary so the album
  stays uncached/retryable (B-4/B-13) — not get swallowed per-field into a
  degraded result (no cover, empty tracklist) that resolve() then caches
  session-long as a Discogs hit, silently forfeiting the Play Count.  The bug
  must not just move down a tier: search_database's per-candidate catch must
  re-raise transient too.

#190 (err-4): a transient MusicBrainz outage must not be flattened to None by
  CoverArtFallback and then cached as the album's FALLBACK payload — see the
  additions in test_resolver_error_no_cache.py.
"""
import json

from unittest.mock import MagicMock, PropertyMock

import discogs_client.exceptions
import pytest
import requests

from src.metadata.errors import is_transient
from tests.factories import make_discogs_reader


def _status_response(status):
    resp = requests.Response()
    resp.status_code = status
    return resp


# ---------------------------------------------------------------------------
# #189 — status discrimination.
# ---------------------------------------------------------------------------

def _requests_http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"HTTP {status}", response=resp)


@pytest.mark.parametrize("status", [401, 403, 404, 400, 410, 405])
def test_definitive_4xx_is_permanent(status):
    assert is_transient(_requests_http_error(status)) is False
    assert is_transient(discogs_client.exceptions.HTTPError("x", status)) is False


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retryable_statuses_stay_transient(status):
    assert is_transient(_requests_http_error(status)) is True
    assert is_transient(discogs_client.exceptions.HTTPError("x", status)) is True


def test_status_less_network_errors_stay_transient():
    # Connection/timeout carry no HTTP status → still transient.
    assert is_transient(requests.exceptions.ConnectionError("c")) is True
    assert is_transient(requests.exceptions.Timeout("t")) is True
    assert is_transient(ConnectionError("socket")) is True
    assert is_transient(TimeoutError("slow")) is True


def test_httperror_with_no_response_falls_back_to_transient():
    # A requests.HTTPError whose response is None (rare) has no status to judge;
    # default to transient (the family's historical classification).
    assert is_transient(requests.exceptions.HTTPError("no response")) is True


def test_non_http_errors_unchanged():
    from src.metadata.errors import TransientMetadataError, PermanentMetadataError
    assert is_transient(TransientMetadataError("x")) is True
    assert is_transient(PermanentMetadataError("x")) is False
    assert is_transient(ValueError("bug")) is False


# ---------------------------------------------------------------------------
# #188 — transient during enrichment propagates; the album stays uncached.
# ---------------------------------------------------------------------------

def _release_raising_on_tracklist(exc):
    rel = MagicMock()
    rel.id = 555
    rel.title = "Sister"
    rel.images = []
    rel.labels = []
    rel.year = 1987
    rel.styles = []
    rel.genres = []
    rel.master = None
    type(rel).tracklist = PropertyMock(side_effect=exc)
    return rel


def test_get_tracklist_reraises_transient():
    reader = make_discogs_reader()
    reader._client.release = MagicMock(
        side_effect=discogs_client.exceptions.HTTPError("rate limited", 429)
    )
    with pytest.raises(discogs_client.exceptions.HTTPError):
        reader.get_tracklist(555)


def test_get_tracklist_still_degrades_on_permanent_error():
    reader = make_discogs_reader()
    reader._client.release = MagicMock(side_effect=RuntimeError("malformed"))
    assert reader.get_tracklist(555) == []


def test_build_result_propagates_a_transient_tracklist_fetch():
    reader = make_discogs_reader()
    rel = _release_raising_on_tracklist(
        discogs_client.exceptions.HTTPError("rate limited", 429)
    )
    # R5-19: _build_result reads tracklist off the ALREADY-FETCHED release, so
    # the transient must come from `rel.tracklist` (set above); no second fetch.
    with pytest.raises(discogs_client.exceptions.HTTPError):
        reader._build_result(rel, instance_id=99)


def test_search_database_reraises_transient_instead_of_returning_none():
    """The bug must not move one tier down: a transient failure building every
    candidate must RAISE, not return None (which resolve() would cache as a
    clean database miss)."""
    reader = make_discogs_reader()
    cand = MagicMock()
    cand.id = 100
    cand.title = "X"
    reader._database_search = MagicMock(return_value=[cand])
    reader._build_result = MagicMock(
        side_effect=requests.exceptions.HTTPError(
            "429", response=_status_response(429)
        )
    )
    with pytest.raises(requests.exceptions.HTTPError):
        reader.search_database("A", "B")


def test_search_database_still_skips_a_permanently_malformed_candidate():
    reader = make_discogs_reader()
    good = MagicMock(); good.id = 2; good.title = "Good"
    bad = MagicMock(); bad.id = 1; bad.title = "Bad"
    reader._database_search = MagicMock(return_value=[bad, good])

    def build(release, instance_id):
        if release.id == 1:
            raise ValueError("malformed candidate")
        return {"release_id": release.id, "album": release.title}

    reader._build_result = MagicMock(side_effect=build)
    result = reader.search_database("A", "B")
    assert result["release_id"] == 2


def test_build_result_degrades_year_on_transient_master_but_keeps_credit():
    """#188 cold review: a transient master-year blip must NOT abort the whole
    resolve — the credit-capable fields (instance_id, tracklist) survive and
    the year degrades to the pressing year."""
    reader = make_discogs_reader()
    rel = MagicMock()
    rel.id = 700
    rel.title = "Sister"
    rel.images = []
    rel.labels = []
    rel.year = 1987
    rel.styles = []
    rel.genres = []
    # master access blips transiently → get_original_year degrades to None
    type(rel).master = PropertyMock(
        side_effect=discogs_client.exceptions.HTTPError("rate limited", 429)
    )
    # R5-19: tracklist is read off `rel` directly; a clean (empty) tracklist here.
    rel.tracklist = []

    result = reader._build_result(rel, instance_id=42)

    assert result["instance_id"] == 42          # credit-capable, not discarded
    assert result["year"] == "1987"             # degraded to the pressing year


def test_json_decode_error_is_transient():
    # #228: python3-discogs-client json.loads() the response body BEFORE building
    # its HTTPError, so a 429/5xx carrying a non-JSON body (Cloudflare/HTML error
    # page) raises JSONDecodeError — a genuine transient outage that was otherwise
    # misclassified permanent.
    err = json.JSONDecodeError("Expecting value", "<html>503</html>", 0)
    assert is_transient(err) is True


def test_plain_value_error_is_not_transient():
    # Scoped precisely: JSONDecodeError subclasses ValueError, but a BARE
    # ValueError (a real programming error) must stay non-transient — we listed
    # json.JSONDecodeError, not ValueError.
    assert is_transient(ValueError("a real bug")) is False

