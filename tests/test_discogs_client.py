"""Unit tests for DiscogsClient.increment_play_count, _get_field_value,
and update_last_played.

All HTTP calls are mocked via unittest.mock — no real Discogs account required.

Covered scenarios — increment_play_count:
  ✓ Blank Play Count field → sets "1"
  ✓ Existing count "5" → sets "6"
  ✓ Existing count "1" → sets "2"
  ✓ Garbage string value → treats as 0, sets "1"
  ✓ Whitespace-only value → treats as 0, sets "1"
  ✓ Field not found in collection fields → returns False, no POST
  ✓ GET for current value returns non-200 → falls back to 0, still writes "1"
  ✓ POST returns non-204 → returns False
  ✓ POST returns 401 → returns False
  ✓ Exception raised during POST → returns False, no crash

Covered scenarios — _get_field_value:
  ✓ Correct instance_id → returns value string
  ✓ instance_id not in response → returns None
  ✓ Non-200 GET → returns None
  ✓ Instance found but field_id not in notes → returns None

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
    client._http.session.post.assert_called_once()
    _, kwargs = client._http.session.post.call_args
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

def test_garbage_string_value_treated_as_zero():
    """Non-integer value → log warning, treat as 0, post '1'."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "not-a-number"))
    post_resp = make_post_response(204)
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "1"


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
# increment_play_count — GET failure fallback
# ---------------------------------------------------------------------------

def test_get_current_value_non200_falls_back_to_zero_and_still_writes():
    """If GET for current value returns non-200, fall back to 0 and still POST '1'."""
    client = make_client()

    get_resp = make_get_response(500, {})
    post_resp = make_post_response(204)
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "1"


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


def test_get_field_value_wrong_instance_id_returns_none():
    """_get_field_value returns None when the response has a different instance_id."""
    client = make_client()

    # Response has instance_id=99, but we're looking for instance_id=42
    get_resp = make_get_response(200, instance_response(99, _FIELD_ID, "3"))
    client._http.session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is None


def test_get_field_value_non200_returns_none():
    """_get_field_value returns None on a non-200 GET response."""
    client = make_client()

    get_resp = make_get_response(404, {})
    client._http.session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is None


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
    client._http.session.post.assert_called_once()
    _, kwargs = client._http.session.post.call_args
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


def test_request_429_wait_is_capped():
    from src.metadata.discogs.transport import _RATE_LIMIT_MAX_WAIT
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response("9999"), make_get_response(200, {})]
    )

    with patch("src.metadata.discogs.transport.time.sleep") as mock_sleep:
        client._http.request("GET", "https://api.discogs.com/anything")

    mock_sleep.assert_called_once_with(_RATE_LIMIT_MAX_WAIT)


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
    """End-to-end: a 429 on the field-update POST still results in success."""
    client = make_client()
    get_resp = make_get_response(200, {"releases": []})  # No prior value → 0
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(
        side_effect=[make_429_response("1"), make_post_response(204)]
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


def test_original_year_none_when_fetch_raises():
    client = make_reader()
    client._http.session.get = MagicMock(side_effect=ConnectionError("network down"))
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
