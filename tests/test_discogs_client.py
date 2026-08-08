"""Unit tests for DiscogsClient.increment_play_count, _get_field_value,
and update_last_played.

All HTTP calls are mocked via unittest.mock — no real Discogs account required.

Covered scenarios — increment_play_count:
  ✓ Blank Play Count field → sets "1"
  ✓ Existing count "5" → sets "6"
  ✓ Existing count "1" → sets "2"
  ✓ Whitespace-only value → confirmed blank, treats as 0, sets "1"
  ✓ Present but non-integer value → ABORTS, no POST (META-2: never clobber to 1)
  ✓ Field not found in collection fields → returns False, no POST
  ✓ GET for current value returns 5xx → ABORTS, no POST (META-1: never clobber)
  ✓ ConnectionError on the value read → ABORTS, no POST (META-1)
  ✓ 200 but our instance missing from body → ABORTS, no POST (META-1: ambiguous)
  ✓ POST returns non-204 → returns False
  ✓ POST returns 401 → returns False
  ✓ Exception raised during POST → returns False, no crash

Covered scenarios — _get_field_value (three-state: value / confirmed-blank / unknown):
  ✓ Correct instance_id → returns value string
  ✓ instance_id not in response → returns _READ_FAILED (unknown, not blank)
  ✓ Non-200 GET → returns _READ_FAILED
  ✓ Exception during read → returns _READ_FAILED
  ✓ Instance found but field_id not in notes → returns None (confirmed blank)

Covered scenarios — update_last_played:
  ✓ last_played_field_name not configured → returns True, no API calls
  ✓ Configured, field found, POST 204 → returns True, posts today's ISO date
  ✓ Date written matches today's ISO format (YYYY-MM-DD)
  ✓ Field not found in collection fields → returns False, no POST
  ✓ POST returns non-204 → returns False
  ✓ POST returns 401 → returns False
  ✓ Exception raised during POST → returns False, no crash
"""
from datetime import date
from unittest.mock import MagicMock, patch

import logging

import pytest
import requests

from src.metadata.discogs.writer import _READ_FAILED
from tests.factories import make_discogs_config, make_discogs_writer, make_discogs_reader


# ---------------------------------------------------------------------------
# Helpers
#
# A-4: these are write-side tests, so "client" is a DiscogsCollectionWriter.
# The HTTP seam moved to the shared transport, so tests mock
# ``client._http.session.get`` / ``.post`` (was ``client._session.*``) and the
# rate-limit retry lives on ``client._http.request`` (was ``client._request``).
# ---------------------------------------------------------------------------

_BASE_CONFIG = make_discogs_config()

# Arbitrary integers used to keep mock request/response data internally
# consistent. All HTTP calls are mocked, so these never touch the real
# Discogs API — the actual field IDs on the account are irrelevant here.
_FIELD_ID = 6
_LAST_PLAYED_FIELD_ID = 7


def make_client():
    """A DiscogsCollectionWriter with the fields cache pre-populated."""
    writer = make_discogs_writer(config=_BASE_CONFIG)
    # Pre-populate the fields cache so tests don't need to stub the fields GET
    writer._collection_fields = {"Play Count": _FIELD_ID}
    return writer


def make_reader():
    """A DiscogsReader (read-side methods: search / year / build)."""
    return make_discogs_reader(config=_BASE_CONFIG)


def make_client_with_last_played():
    """A DiscogsCollectionWriter configured with last_played_field_name."""
    config = make_discogs_config(last_played_field_name="Last Played")
    writer = make_discogs_writer(config=config)
    # Pre-populate both fields in the cache
    writer._collection_fields = {
        "Play Count": _FIELD_ID,
        "Last Played": _LAST_PLAYED_FIELD_ID,
    }
    return writer


def make_get_response(status_code: int, json_body: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


def make_post_response(status_code: int, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


def instance_response(instance_id: int, field_id: int, value: str):
    """Build a /collection/releases/{id} response with one instance."""
    return {
        "releases": [
            {
                "instance_id": instance_id,
                "notes": [
                    {"field_id": field_id, "value": value}
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# increment_play_count — happy paths
# ---------------------------------------------------------------------------

def test_blank_field_sets_one():
    """A blank Play Count field should result in posting '1'."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, ""))
    post_resp = make_post_response(204)
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    # Assert the OUTGOING REQUEST shape (URL + body), not merely that a
    # particular session method fired (T-Quality): a future transport refactor
    # must still POST the incremented value to the right collection-field path.
    client._http.session.post.assert_called_once()
    args, kwargs = client._http.session.post.call_args
    assert args[0].endswith("/collection/folders/0/releases/111/instances/42/fields/6")
    assert kwargs["json"]["value"] == "1"


def test_existing_count_five_becomes_six():
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "5"))
    post_resp = make_post_response(204)
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "6"


def test_existing_count_one_becomes_two():
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "1"))
    post_resp = make_post_response(204)
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "2"


# ---------------------------------------------------------------------------
# increment_play_count — garbage / edge-case field values
# ---------------------------------------------------------------------------

def test_nonempty_garbage_value_aborts_without_writing(caplog):
    """A present but non-integer Play Count must NOT be reset to 1 (META-2).

    A successful read that returns prose (e.g. a hand-typed note) is a value we
    cannot safely increment, but it is REAL DATA — overwriting it with an
    absolute '1' destroys whatever the field held. The safe behaviour is to
    abort the write and return False, leaving the field untouched for the owner
    to inspect. The old behaviour (treat as 0, post '1') is the bug.
    """
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "not-a-number"))
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with caplog.at_level(logging.ERROR):
        result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()
    # Pin the META-2 path specifically (a present, non-integer value), so this
    # abort is not confused with the read-failure abort (META-1).
    assert "not an integer" in caplog.text


def test_whitespace_only_value_treated_as_zero():
    """Whitespace-only string → treat as 0, post '1'."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "   "))
    post_resp = make_post_response(204)
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "1"


# ---------------------------------------------------------------------------
# increment_play_count — field not found
# ---------------------------------------------------------------------------

def test_field_not_found_returns_false_no_post():
    """If 'Play Count' field doesn't exist in collection fields, return False."""
    client = make_client()
    client._collection_fields = {}  # Override: no fields at all

    client._http.session.post = MagicMock()

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()


# ---------------------------------------------------------------------------
# increment_play_count — UNTRUSTED READ must abort, never clobber (META-1)
#
# The increment is a read-modify-write ending in an ABSOLUTE set. If the read
# of the current value cannot be trusted, treating it as 0 resets the owner's
# accumulated Play Count to 1 — silently, with a success log. The only safe
# behaviour is to abort the write. These tests pin that for every read-failure
# mode: a 5xx, a connection error, and a 200 whose body does not contain the
# instance (a partial/paged/edited response we cannot interpret as "blank").
# ---------------------------------------------------------------------------

def test_get_current_value_5xx_aborts_without_writing(caplog):
    """A 5xx on the value read must abort the increment, not fall back to 0."""
    client = make_client()

    client._http.session.get = MagicMock(return_value=make_get_response(500, {}))
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with caplog.at_level(logging.ERROR):
        result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()
    # Pin the READ-failure abort path (META-1) specifically, so it is not
    # satisfied by the non-integer abort (META-2) accidentally catching the
    # sentinel's string form.
    assert "could not read the current value" in caplog.text


def test_get_current_value_connection_error_aborts_without_writing(caplog):
    """A ConnectionError on the value read must abort, not clobber to 1."""
    client = make_client()

    client._http.session.get = MagicMock(side_effect=ConnectionError("network gone"))
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with caplog.at_level(logging.ERROR):
        result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()
    assert "could not read the current value" in caplog.text


def test_get_current_value_instance_missing_aborts_without_writing(caplog):
    """A 200 whose body lacks our instance is ambiguous, not 'blank' — abort.

    The instance genuinely being absent, a paged response, or an edited
    collection all land here; none of them justify resetting the count to 1.
    """
    client = make_client()

    # 200, but the only instance in the body is a different one (99 != 42).
    get_resp = make_get_response(200, instance_response(99, _FIELD_ID, "12"))
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with caplog.at_level(logging.ERROR):
        result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()
    assert "could not read the current value" in caplog.text


# ---------------------------------------------------------------------------
# increment_play_count — POST failures
# ---------------------------------------------------------------------------

def test_post_non204_returns_false():
    """A non-204 POST response should return False."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "3"))
    post_resp = make_post_response(400, "Bad Request")
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False


def test_post_401_returns_false():
    """A 401 Unauthorized response should return False."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "2"))
    post_resp = make_post_response(401, "Unauthorized")
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False


def test_exception_during_post_returns_false_no_crash():
    """An exception raised during POST should be caught, return False."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "1"))
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(side_effect=ConnectionError("network gone"))

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False


# ---------------------------------------------------------------------------
# _get_field_value — direct unit tests
# ---------------------------------------------------------------------------

def test_get_field_value_returns_correct_value_for_matching_instance():
    """_get_field_value returns the value string for the correct instance_id."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "7"))
    client._http.session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result == "7"


def test_get_field_value_wrong_instance_id_returns_read_failed():
    """A 200 body that lacks our instance is UNKNOWN, not blank → _READ_FAILED.

    This is the value that keeps a partial/paged/edited response from being read
    as 0 and clobbering the count (META-1).
    """
    client = make_client()

    # Response has instance_id=99, but we're looking for instance_id=42
    get_resp = make_get_response(200, instance_response(99, _FIELD_ID, "3"))
    client._http.session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is _READ_FAILED


def test_get_field_value_non200_returns_read_failed():
    """_get_field_value returns _READ_FAILED (not None) on a non-200 GET (META-1)."""
    client = make_client()

    get_resp = make_get_response(404, {})
    client._http.session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is _READ_FAILED


def test_get_field_value_exception_returns_read_failed():
    """A network exception during the read is UNKNOWN, not blank → _READ_FAILED."""
    client = make_client()

    client._http.session.get = MagicMock(side_effect=ConnectionError("boom"))

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is _READ_FAILED


def test_get_field_value_field_unset_returns_none_confirmed_blank():
    """Instance found but this field unset is a CONFIRMED blank → None (safe 0)."""
    client = make_client()

    # Instance 42 present, but its only note is for a different field_id.
    body = {"releases": [{"instance_id": 42, "notes": [{"field_id": 999, "value": "x"}]}]}
    client._http.session.get = MagicMock(return_value=make_get_response(200, body))

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is None


# ---------------------------------------------------------------------------
# _get_collection_fields — the field-NAME → field-ID map that decides WHICH
# Discogs custom column a write lands in.  Previously EVERY writer test
# pre-seeded ``writer._collection_fields``, so this fetch/build path never
# executed under test at all (MUT-1): the map selecting the write column was
# entirely unpinned.  These exercise it directly.
# ---------------------------------------------------------------------------

def _fields_response(fields):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"fields": fields}
    return resp


def make_unseeded_writer():
    """A writer whose collection-fields cache is NOT pre-populated, so
    _get_collection_fields actually fetches."""
    writer = make_discogs_writer(config=_BASE_CONFIG)
    assert writer._collection_fields is None  # guard: the fetch path is live
    return writer


def test_get_collection_fields_fetches_and_maps_name_to_id():
    """The fetch builds {field_name: field_id} from the collection-fields
    endpoint — the mapping that later selects the write column."""
    writer = make_unseeded_writer()
    writer._http.request = MagicMock(return_value=_fields_response(
        [{"name": "Play Count", "id": 3}, {"name": "Last Played", "id": 4}]
    ))

    fields = writer._get_collection_fields()

    # name -> id (NOT id -> name): the value is what gets interpolated into the
    # write URL, so the direction matters.
    assert fields == {"Play Count": 3, "Last Played": 4}
    method, url = writer._http.request.call_args[0][:2]
    assert method == "GET"
    assert url.endswith("/users/testuser/collection/fields")


def test_get_collection_fields_is_cached_after_first_fetch():
    """Second call returns the same cached dict with no further HTTP (the
    _collection_fields-is-not-None guard)."""
    writer = make_unseeded_writer()
    writer._http.request = MagicMock(return_value=_fields_response(
        [{"name": "Play Count", "id": 3}]
    ))

    first = writer._get_collection_fields()
    second = writer._get_collection_fields()

    assert first == {"Play Count": 3}
    assert second is first
    assert writer._http.request.call_count == 1


def test_get_collection_fields_propagates_http_error_and_does_not_cache():
    """A failed fetch raises (couldn't determine the fields) and leaves the
    cache unset so a later call can retry."""
    writer = make_unseeded_writer()
    bad = MagicMock()
    bad.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    writer._http.request = MagicMock(return_value=bad)

    with pytest.raises(requests.exceptions.HTTPError):
        writer._get_collection_fields()
    assert writer._collection_fields is None


def test_increment_uses_the_fetched_field_id_as_the_write_column():
    """End-to-end with NO pre-seeded cache: the id the map resolves for
    'Play Count' is the field the POST targets — so a wrong name→id mapping
    would write to the wrong Discogs column, not just fail a lookup."""
    writer = make_unseeded_writer()

    def fake_request(method, url, **kwargs):
        if url.endswith("/collection/fields"):
            return _fields_response(
                [{"name": "Other", "id": 9}, {"name": "Play Count", "id": 3}]
            )
        if method == "GET":  # the current-value read
            return make_get_response(200, instance_response(42, 3, "5"))
        return make_post_response(204)  # the write

    writer._http.request = MagicMock(side_effect=fake_request)

    assert writer.increment_play_count(release_id=111, instance_id=42) is True
    post_urls = [c.args[1] for c in writer._http.request.call_args_list if c.args[0] == "POST"]
    assert post_urls, "no POST issued"
    assert post_urls[0].endswith("/fields/3")  # Play Count = 3, not Other = 9


def test_get_field_value_field_not_in_notes_returns_none():
    """_get_field_value returns None when instance is found but field_id isn't in notes."""
    client = make_client()

    response = {
        "releases": [
            {
                "instance_id": 42,
                "notes": [
                    {"field_id": 999, "value": "something-else"}  # wrong field_id
                ],
            }
        ]
    }
    get_resp = make_get_response(200, response)
    client._http.session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is None


# ---------------------------------------------------------------------------
# update_last_played — not configured (graceful no-op)
# ---------------------------------------------------------------------------

def test_update_last_played_not_configured_returns_true_no_api_calls():
    """When last_played_field_name is not set, update_last_played is a no-op."""
    client = make_client()  # last_played_field_name is None (not in config)

    client._http.session.post = MagicMock()
    client._http.session.get = MagicMock()

    result = client.update_last_played(release_id=111, instance_id=42)

    assert result is True
    client._http.session.post.assert_not_called()
    client._http.session.get.assert_not_called()


# ---------------------------------------------------------------------------
# update_last_played — happy path
# ---------------------------------------------------------------------------

def test_update_last_played_posts_todays_iso_date():
    """update_last_played POSTs today's ISO date string and returns True."""
    client = make_client_with_last_played()

    post_resp = make_post_response(204)
    client._http.session.post = MagicMock(return_value=post_resp)

    fake_today = date(2026, 5, 24)
    with patch("src.metadata.discogs.writer.date") as mock_date:
        mock_date.today.return_value = fake_today
        result = client.update_last_played(release_id=111, instance_id=42)

    assert result is True
    # Outgoing request shape (URL + body), not just the seam (T-Quality):
    # today's date must POST to the Last Played field's collection path.
    client._http.session.post.assert_called_once()
    args, kwargs = client._http.session.post.call_args
    assert args[0].endswith("/collection/folders/0/releases/111/instances/42/fields/7")
    assert kwargs["json"]["value"] == "2026-05-24"


def test_update_last_played_date_is_iso_format():
    """The posted value is always a valid ISO 8601 date string (YYYY-MM-DD)."""
    client = make_client_with_last_played()

    post_resp = make_post_response(204)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.update_last_played(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._http.session.post.call_args
    posted_value = kwargs["json"]["value"]
    # Verify format by parsing — raises ValueError if not valid ISO date
    parsed = date.fromisoformat(posted_value)
    assert str(parsed) == posted_value


# ---------------------------------------------------------------------------
# update_last_played — clock-sanity gate (STAB-2)
#
# The Pi has no RTC; a pre-NTP boot makes date.today() read the epoch or a stale
# fake-hwclock date. Writing that would stamp a wrong date over the real Last
# Played value. The write is gated on clock_is_trustworthy(); Play Count is NOT
# gated (it writes a count, not a date).
# ---------------------------------------------------------------------------

def test_update_last_played_skips_when_clock_untrustworthy():
    """A pre-NTP clock must NOT overwrite Last Played — skip the POST, return False."""
    client = make_client_with_last_played()
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with patch("src.metadata.discogs.writer.clock_is_trustworthy", return_value=False):
        result = client.update_last_played(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()  # no wrong date written


def test_update_last_played_writes_when_clock_trustworthy():
    """The gate lets a good clock through — today's date still POSTs."""
    client = make_client_with_last_played()
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with patch("src.metadata.discogs.writer.clock_is_trustworthy", return_value=True):
        result = client.update_last_played(release_id=111, instance_id=42)

    assert result is True
    client._http.session.post.assert_called_once()


def test_update_last_played_skip_logs_a_warning(caplog):
    """The skip leaves a WARNING breadcrumb so a missed update is explained."""
    import logging
    client = make_client_with_last_played()
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    with caplog.at_level(logging.WARNING), \
         patch("src.metadata.discogs.writer.clock_is_trustworthy", return_value=False):
        client.update_last_played(release_id=111, instance_id=42)

    assert any("clock is not yet trustworthy" in r.message for r in caplog.records)


def test_increment_play_count_is_not_gated_by_the_clock():
    """Play Count writes a count, not a date, so a wrong clock can't corrupt it —
    it is deliberately NOT gated and still increments even when the clock is
    untrustworthy (STAB-2 scope)."""
    writer = make_unseeded_writer()

    def fake_request(method, url, **kwargs):
        if url.endswith("/collection/fields"):
            return _fields_response([{"name": "Play Count", "id": 3}])
        if method == "GET":
            return make_get_response(200, instance_response(42, 3, "5"))
        return make_post_response(204)

    writer._http.request = MagicMock(side_effect=fake_request)

    with patch("src.metadata.discogs.writer.clock_is_trustworthy", return_value=False):
        assert writer.increment_play_count(release_id=111, instance_id=42) is True

    post_urls = [c.args[1] for c in writer._http.request.call_args_list if c.args[0] == "POST"]
    assert post_urls, "Play Count POST was suppressed by the clock gate — it must not be"


# ---------------------------------------------------------------------------
# update_last_played — field not found
# ---------------------------------------------------------------------------

def test_update_last_played_field_not_found_returns_false():
    """If 'Last Played' field doesn't exist in collection fields, return False."""
    client = make_client_with_last_played()
    client._collection_fields = {"Play Count": _FIELD_ID}  # Override: no Last Played field

    client._http.session.post = MagicMock()

    result = client.update_last_played(release_id=111, instance_id=42)

    assert result is False
    client._http.session.post.assert_not_called()


# ---------------------------------------------------------------------------
# update_last_played — POST failures
# ---------------------------------------------------------------------------

def test_update_last_played_post_non204_returns_false():
    """A non-204 POST response from update_last_played returns False."""
    client = make_client_with_last_played()

    post_resp = make_post_response(400, "Bad Request")
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.update_last_played(release_id=111, instance_id=42)

    assert result is False


def test_update_last_played_post_401_returns_false():
    """A 401 Unauthorized response from update_last_played returns False."""
    client = make_client_with_last_played()

    post_resp = make_post_response(401, "Unauthorized")
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.update_last_played(release_id=111, instance_id=42)

    assert result is False


def test_update_last_played_exception_returns_false_no_crash():
    """An exception raised during update_last_played POST is caught, returns False."""
    client = make_client_with_last_played()

    client._http.session.post = MagicMock(side_effect=ConnectionError("network gone"))

    result = client.update_last_played(release_id=111, instance_id=42)

    assert result is False


# ---------------------------------------------------------------------------
# Rate-limit handling — _request (v1.3.3)
#
# Discogs answers excess traffic with HTTP 429 + Retry-After (seconds).
# _request retries exactly once, honoring the header clamped to
# [1, _RATE_LIMIT_MAX_WAIT], with _RATE_LIMIT_DEFAULT_WAIT as fallback.
# time.sleep is patched throughout — these tests never actually wait.
# ---------------------------------------------------------------------------

def make_429_response(retry_after=None):
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {} if retry_after is None else {"Retry-After": retry_after}
    return resp


def test_request_retries_once_on_429_honoring_retry_after():
    client = make_client()
    ok = make_get_response(200, {})
    client._http.session.get = MagicMock(side_effect=[make_429_response("3"), ok])

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        resp = client._http.request("GET", "https://api.discogs.com/anything")

    assert resp is ok
    assert client._http.session.get.call_count == 2
    mock_sleep.assert_called_once_with(3)


def test_request_429_uses_default_wait_when_header_missing():
    from src.metadata.discogs.transport import _RATE_LIMIT_DEFAULT_WAIT
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response(), make_get_response(200, {})]
    )

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        client._http.request("GET", "https://api.discogs.com/anything")

    mock_sleep.assert_called_once_with(_RATE_LIMIT_DEFAULT_WAIT)


def test_request_429_uses_default_wait_when_header_unparseable():
    from src.metadata.discogs.transport import _RATE_LIMIT_DEFAULT_WAIT
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response("soon-ish"), make_get_response(200, {})]
    )

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        client._http.request("GET", "https://api.discogs.com/anything")

    mock_sleep.assert_called_once_with(_RATE_LIMIT_DEFAULT_WAIT)


def test_request_429_over_cap_skips_the_futile_retry(caplog):
    """META-10: when Discogs asks to wait LONGER than our cap, a retry inside the
    cap would still be throttled — so skip it (no wasted sleep, no second request,
    no pool parking) and surface a distinct, loud error."""
    import logging
    from src.metadata.discogs.transport import _RATE_LIMIT_MAX_WAIT
    client = make_client()
    over_cap = str(_RATE_LIMIT_MAX_WAIT + 50)   # e.g. "60" — beyond our cap
    client._http.session.get = MagicMock(return_value=make_429_response(over_cap))

    with caplog.at_level(logging.ERROR), \
         patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        resp = client._http.request("GET", "https://api.discogs.com/anything")

    assert resp.status_code == 429
    assert client._http.session.get.call_count == 1    # NO futile retry
    mock_sleep.assert_not_called()                      # no pool parking
    assert any("NOT retrying" in r.getMessage() for r in caplog.records)


def test_persistent_429_after_retry_logs_a_distinct_error(caplog):
    """META-10: a 429 whose Retry-After is within the cap IS retried once; if it
    is STILL rate-limited, surface a distinct, loud error so a lost credit is not
    silently conflated with a generic failure."""
    import logging
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response("3"), make_429_response("3")]
    )

    with caplog.at_level(logging.ERROR), \
         patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        resp = client._http.request("GET", "https://api.discogs.com/anything")

    assert resp.status_code == 429
    assert client._http.session.get.call_count == 2    # retried once
    mock_sleep.assert_called_once_with(3)               # honored the in-cap wait
    assert any("STILL rate-limiting" in r.getMessage() for r in caplog.records)


def test_request_429_at_exactly_the_cap_still_retries():
    """META-10 boundary: a Retry-After equal to the cap is WITHIN budget — it must
    retry (honoring the wait), not be treated as over-cap and skipped. Pins the
    `>` vs `>=` boundary against a future off-by-one."""
    from src.metadata.discogs.transport import _RATE_LIMIT_MAX_WAIT
    client = make_client()
    ok = make_get_response(200, {})
    client._http.session.get = MagicMock(
        side_effect=[make_429_response(str(_RATE_LIMIT_MAX_WAIT)), ok]
    )

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        resp = client._http.request("GET", "https://api.discogs.com/anything")

    assert resp is ok
    assert client._http.session.get.call_count == 2         # retried, not skipped
    mock_sleep.assert_called_once_with(_RATE_LIMIT_MAX_WAIT)  # waited exactly the cap


def test_request_429_retry_after_zero_still_sleeps_at_least_one():
    """MUT-9: the retry sleep floor is `max(1, retry_after)`.  A `Retry-After: 0`
    must NOT collapse into an instant retry against an API that just throttled the
    device — the `max(1, ...)` -> `max(0, ...)` mutant.  Assert the sleep is 1."""
    client = make_client()
    ok = make_get_response(200, {})
    client._http.session.get = MagicMock(side_effect=[make_429_response("0"), ok])

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        resp = client._http.request("GET", "https://api.discogs.com/anything")

    assert resp is ok
    assert client._http.session.get.call_count == 2
    mock_sleep.assert_called_once_with(1)   # floored to 1, never 0


def test_transport_rate_limit_constants_are_the_shipped_values():
    """MUT-9: the HTTP timeout and 429 wait bounds are asserted nowhere else, so a
    units slip shipped green.  Pin the shipped values directly."""
    from src.metadata.discogs import transport
    assert transport._HTTP_TIMEOUT == 15
    assert transport._RATE_LIMIT_MAX_WAIT == 10
    assert transport._RATE_LIMIT_DEFAULT_WAIT == 2


def test_request_normalizes_lowercase_get(tmp_path):
    """LB-2: a lowercase 'get' must dispatch via session.get AND retry on 429 like
    GET (both keyed off `method == "GET"`).  Before, `"get" == "GET"` failed
    twice — it POSTed and silently lost the 429 retry."""
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response("1"), make_get_response(200, {})]
    )
    client._http.session.post = MagicMock()

    with patch("src.metadata.discogs.transport.time.sleep"):
        resp = client._http.request("get", "https://api.discogs.com/x")

    assert resp.status_code == 200
    client._http.session.post.assert_not_called()      # dispatched as GET, not POST
    assert client._http.session.get.call_count == 2     # retried once (GET default)


def test_request_rejects_unsupported_verb_instead_of_posting():
    """LB-2: DELETE/PUT/PATCH/etc. must raise loudly, not silently issue a POST
    (this transport is the one that WRITES to the real collection)."""
    client = make_client()
    client._http.session.get = MagicMock()
    client._http.session.post = MagicMock()

    for verb in ("DELETE", "PUT", "PATCH", "HEAD", "delete"):
        with pytest.raises(ValueError, match="unsupported HTTP method"):
            client._http.request(verb, "https://api.discogs.com/x")

    client._http.session.get.assert_not_called()
    client._http.session.post.assert_not_called()       # never silently POSTs


def test_request_does_not_retry_on_success():
    client = make_client()
    client._http.session.get = MagicMock(return_value=make_get_response(200, {}))

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        client._http.request("GET", "https://api.discogs.com/anything")

    assert client._http.session.get.call_count == 1
    mock_sleep.assert_not_called()


def test_request_gives_up_after_second_429():
    """No infinite retry loops: a second consecutive 429 is returned as-is."""
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response("1"), make_429_response("1")]
    )

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        resp = client._http.request("GET", "https://api.discogs.com/anything")

    assert resp.status_code == 429
    assert client._http.session.get.call_count == 2
    mock_sleep.assert_called_once()  # Slept for the first retry only


def test_request_routes_post_through_session_post():
    client = make_client()
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    resp = client._http.request("POST", "https://api.discogs.com/anything", json={"value": "1"})

    assert resp.status_code == 204
    client._http.session.post.assert_called_once()
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"] == {"value": "1"}
    assert "timeout" in kwargs  # _HTTP_TIMEOUT applied by default


def test_increment_play_count_survives_one_rate_limit_on_post():
    """End-to-end: a 429 on the field-update POST still results in success.

    The read must return a TRUSTED value so the increment reaches the POST —
    this test exercises the POST 429-retry, not the read. (Previously it stubbed
    an empty ``releases`` body, which now correctly aborts as an unknown read —
    META-1 — so it no longer reached the POST it was written to test.)
    """
    client = make_client()
    get_resp = make_get_response(200, instance_response(222, _FIELD_ID, "5"))
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(
        side_effect=[make_429_response("6"), make_post_response(204)]
    )

    with patch("src.metadata.discogs.transport.time.sleep"):
        assert client.increment_play_count(111, 222) is True

    assert client._http.session.post.call_count == 2


# ---------------------------------------------------------------------------
# get_original_year — original vs. pressing year (new in v1.4.2)
# ---------------------------------------------------------------------------
# A Discogs release carries its PRESSING year; the master carries the
# original. The display prefers the original (DESIGN.md §7), so
# get_original_year fetches /masters/{id} via the rate-limited _request
# helper and _build_result falls back to release.year when it returns None.

def _make_release(master_id=151481, pressing_year=2026):
    """Mock python3-discogs-client Release: a 2026 reissue of a 2005 album."""
    release = MagicMock()
    release.year = pressing_year
    if master_id is None:
        release.master = None
    else:
        release.master = MagicMock()
        release.master.id = master_id
    return release


def _mock_master_response(year):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"id": 151481, "year": year}
    return resp


def test_original_year_prefers_master_year():
    client = make_reader()
    client._http.session.get = MagicMock(return_value=_mock_master_response(2005))
    assert client.get_original_year(_make_release()) == "2005"
    assert "masters/151481" in client._http.session.get.call_args[0][0]


def test_original_year_none_when_no_master():
    client = make_reader()
    client._http.session.get = MagicMock()
    assert client.get_original_year(_make_release(master_id=None)) is None
    client._http.session.get.assert_not_called()


def test_original_year_none_when_master_year_is_zero():
    """Discogs uses 0 for unknown years — must not display '0'."""
    client = make_reader()
    client._http.session.get = MagicMock(return_value=_mock_master_response(0))
    assert client.get_original_year(_make_release()) is None


def test_original_year_degrades_to_none_on_transient_master_fetch():
    """#188 cold review: original-year is DISPLAY-ONLY with a valid pressing-year
    fallback, UNLIKE the tracklist which gates the Play Count. So a transient
    master-fetch blip degrades to None (→ pressing year) rather than re-raising
    and discarding an otherwise credit-capable resolve."""
    client = make_reader()
    client._http.session.get = MagicMock(side_effect=ConnectionError("network down"))
    assert client.get_original_year(_make_release()) is None


def test_original_year_degrades_to_none_on_permanent_master_error():
    """A permanent/malformed master lookup also degrades to None."""
    client = make_reader()
    client._http.session.get = MagicMock(side_effect=RuntimeError("malformed"))
    assert client.get_original_year(_make_release()) is None


def test_original_year_none_when_master_attr_raises():
    """The lazy .master property can raise on a failed lib fetch."""
    client = make_reader()
    release = MagicMock()
    type(release).master = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get_original_year(release) is None


def _make_full_release(pressing_year=2026):
    """Release mock complete enough for _build_result."""
    release = _make_release(pressing_year=pressing_year)
    release.id = 36664639
    release.title = "Apologies To The Queen Mary"
    release.images = []
    release.labels = []
    release.styles = ["Indie Rock"]
    release.genres = ["Rock"]
    return release


def test_build_result_uses_original_year_over_pressing_year():
    client = make_reader()
    client.get_tracklist = MagicMock(return_value=[])
    client.get_original_year = MagicMock(return_value="2005")
    result = client._build_result(_make_full_release(pressing_year=2026), instance_id=None)
    assert result["year"] == "2005"


def test_build_result_falls_back_to_pressing_year():
    client = make_reader()
    client.get_tracklist = MagicMock(return_value=[])
    client.get_original_year = MagicMock(return_value=None)
    result = client._build_result(_make_full_release(pressing_year=2026), instance_id=None)
    assert result["year"] == "2026"


# ---------------------------------------------------------------------------
# SEC-7 — the account username is interpolated into every Discogs collection
# URL and must be percent-encoded so a value containing a URL-reserved
# character ('/', '?', '#', space, …) stays ONE path segment instead of
# silently reshaping the request path. The username is operator-authored in
# config.yaml (a robustness nit, not an attack surface), but a stray '/' would
# add path segments, a '?' would start a query string, and a '#' a fragment.
# One test per distinct URL-building site so a per-site regression is caught.
# ---------------------------------------------------------------------------

# A username exercising all three of the reserved characters the finding names.
# quote(..., safe="") maps '/'→%2F, '?'→%3F, '#'→%23; alphanumerics pass through.
_SPECIAL_USERNAME = "a/b?c#d"
_ENCODED_USERNAME = "a%2Fb%3Fc%23d"


def test_increment_play_count_percent_encodes_username_in_both_urls():
    """SEC-7: increment is a read-then-write, so the username must be encoded in
    BOTH the value-read GET and the increment POST (two distinct URL sites)."""
    writer = make_discogs_writer(config=make_discogs_config(username=_SPECIAL_USERNAME))
    writer._collection_fields = {"Play Count": _FIELD_ID}
    writer._http.session.get = MagicMock(
        return_value=make_get_response(200, instance_response(42, _FIELD_ID, "5"))
    )
    writer._http.session.post = MagicMock(return_value=make_post_response(204))

    assert writer.increment_play_count(release_id=111, instance_id=42) is True

    get_url = writer._http.session.get.call_args[0][0]
    post_url = writer._http.session.post.call_args[0][0]
    for url in (get_url, post_url):
        assert f"/users/{_ENCODED_USERNAME}/collection" in url
        # the raw, path-reshaping form must be gone
        assert f"/users/{_SPECIAL_USERNAME}/" not in url


def test_update_last_played_percent_encodes_username_in_url():
    """SEC-7: the Last Played POST site (writer.py) encodes the username."""
    writer = make_discogs_writer(config=make_discogs_config(
        username=_SPECIAL_USERNAME, last_played_field_name="Last Played"))
    writer._collection_fields = {"Play Count": _FIELD_ID, "Last Played": _LAST_PLAYED_FIELD_ID}
    writer._http.session.post = MagicMock(return_value=make_post_response(204))

    # update_last_played is clock-gated (STAB-2); force trustworthy so the POST fires.
    with patch("src.metadata.discogs.writer.clock_is_trustworthy", return_value=True):
        assert writer.update_last_played(release_id=111, instance_id=42) is True

    post_url = writer._http.session.post.call_args[0][0]
    assert f"/users/{_ENCODED_USERNAME}/collection" in post_url
    assert f"/users/{_SPECIAL_USERNAME}/" not in post_url


def test_get_collection_fields_percent_encodes_username_in_url():
    """SEC-7: the collection-fields GET site (writer.py) encodes the username."""
    writer = make_discogs_writer(config=make_discogs_config(username=_SPECIAL_USERNAME))
    assert writer._collection_fields is None  # force the live fetch
    writer._http.request = MagicMock(return_value=_fields_response(
        [{"name": "Play Count", "id": 3}]))

    writer._get_collection_fields()

    method, url = writer._http.request.call_args[0][:2]
    assert method == "GET"
    assert url.endswith(f"/users/{_ENCODED_USERNAME}/collection/fields")
    assert f"/users/{_SPECIAL_USERNAME}/" not in url


def test_reader_collection_index_percent_encodes_username_in_url():
    """SEC-7: the reader's collection-index GET site (reader.py) encodes the username."""
    reader = make_discogs_reader(config=make_discogs_config(username=_SPECIAL_USERNAME))
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"releases": [], "pagination": {"pages": 1}}
    reader._http.request = MagicMock(return_value=resp)

    reader._get_collection_index()

    method, url = reader._http.request.call_args[0][:2]
    assert method == "GET"
    assert f"/users/{_ENCODED_USERNAME}/collection/folders/0/releases" in url
    assert f"/users/{_SPECIAL_USERNAME}/" not in url


def test_get_collection_fields_public_accessor_delegates_to_private():
    """CRIT-6: the PUBLIC get_collection_fields() is the supported seam for
    operator tooling (scripts/discogs_live_check.py) — it returns the same
    name→id map as the private impl, so the smoke test no longer reaches into a
    private method from outside the package."""
    writer = make_unseeded_writer()
    writer._http.request = MagicMock(return_value=_fields_response(
        [{"name": "Play Count", "id": 3}, {"name": "Last Played", "id": 4}]
    ))

    fields = writer.get_collection_fields()

    assert fields == {"Play Count": 3, "Last Played": 4}
    # It is genuinely a facade over the private impl: the private accessor now
    # returns the same cached object the public one just populated.
    assert fields is writer._get_collection_fields()
