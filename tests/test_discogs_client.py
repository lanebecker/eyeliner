"""Unit tests for DiscogsClient.increment_play_count and _get_field_value.

All HTTP calls are mocked via unittest.mock — no real Discogs account required.

Covered scenarios:
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
  ✓ _get_field_value: correct instance_id → returns value string
  ✓ _get_field_value: instance_id not in response → returns None
  ✓ _get_field_value: non-200 GET → returns None
"""
from unittest.mock import MagicMock, patch, call
import pytest

from src.metadata.discogs_client import DiscogsClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "discogs": {
        "username": "testuser",
        "user_token": "fake-token",
        "play_count_field_name": "Play Count",
    }
}

_FIELD_ID = 6  # Matches what a real Discogs account would return


def make_client():
    """Build a DiscogsClient with all HTTP interactions mocked out."""
    with patch("src.metadata.discogs_client.discogs_client.Client"):
        client = DiscogsClient(_BASE_CONFIG)
    # Pre-populate the fields cache so tests don't need to stub the fields GET
    client._collection_fields = {"Play Count": _FIELD_ID}
    return client


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
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    client._session.post.assert_called_once()
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "1"


def test_existing_count_five_becomes_six():
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "5"))
    post_resp = make_post_response(204)
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "6"


def test_existing_count_one_becomes_two():
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "1"))
    post_resp = make_post_response(204)
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "2"


# ---------------------------------------------------------------------------
# increment_play_count — garbage / edge-case field values
# ---------------------------------------------------------------------------

def test_garbage_string_value_treated_as_zero():
    """Non-integer value → log warning, treat as 0, post '1'."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "not-a-number"))
    post_resp = make_post_response(204)
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "1"


def test_whitespace_only_value_treated_as_zero():
    """Whitespace-only string → treat as 0, post '1'."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "   "))
    post_resp = make_post_response(204)
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "1"


# ---------------------------------------------------------------------------
# increment_play_count — field not found
# ---------------------------------------------------------------------------

def test_field_not_found_returns_false_no_post():
    """If 'Play Count' field doesn't exist in collection fields, return False."""
    client = make_client()
    client._collection_fields = {}  # Override: no fields at all

    client._session.post = MagicMock()

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False
    client._session.post.assert_not_called()


# ---------------------------------------------------------------------------
# increment_play_count — GET failure fallback
# ---------------------------------------------------------------------------

def test_get_current_value_non200_falls_back_to_zero_and_still_writes():
    """If GET for current value returns non-200, fall back to 0 and still POST '1'."""
    client = make_client()

    get_resp = make_get_response(500, {})
    post_resp = make_post_response(204)
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "1"


# ---------------------------------------------------------------------------
# increment_play_count — POST failures
# ---------------------------------------------------------------------------

def test_post_non204_returns_false():
    """A non-204 POST response should return False."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "3"))
    post_resp = make_post_response(400, "Bad Request")
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False


def test_post_401_returns_false():
    """A 401 Unauthorized response should return False."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "2"))
    post_resp = make_post_response(401, "Unauthorized")
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=post_resp)

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False


def test_exception_during_post_returns_false_no_crash():
    """An exception raised during POST should be caught, return False."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "1"))
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(side_effect=ConnectionError("network gone"))

    result = client.increment_play_count(release_id=111, instance_id=42)

    assert result is False


# ---------------------------------------------------------------------------
# _get_field_value — direct unit tests
# ---------------------------------------------------------------------------

def test_get_field_value_returns_correct_value_for_matching_instance():
    """_get_field_value returns the value string for the correct instance_id."""
    client = make_client()

    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, "7"))
    client._session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result == "7"


def test_get_field_value_wrong_instance_id_returns_none():
    """_get_field_value returns None when the response has a different instance_id."""
    client = make_client()

    # Response has instance_id=99, but we're looking for instance_id=42
    get_resp = make_get_response(200, instance_response(99, _FIELD_ID, "3"))
    client._session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is None


def test_get_field_value_non200_returns_none():
    """_get_field_value returns None on a non-200 GET response."""
    client = make_client()

    get_resp = make_get_response(404, {})
    client._session.get = MagicMock(return_value=get_resp)

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
    client._session.get = MagicMock(return_value=get_resp)

    result = client._get_field_value(release_id=111, instance_id=42, field_id=_FIELD_ID)

    assert result is None
