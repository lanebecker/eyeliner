"""Regression tests for B-15 and B-16 (Discogs client robustness).

B-15 — _request must not blindly retry a POST on 429 unless the caller asserts
       the body is an idempotent absolute-set (retry_on_429=True).  GET still
       retries by default.
B-16 — a numeric (JSON-number) Play Count value must not AttributeError on
       .strip() and silently skip the increment.
"""
from unittest.mock import MagicMock, patch

from tests.test_discogs_client import (
    make_client, make_post_response, make_get_response, make_429_response,
    instance_response, _FIELD_ID,
)


# ---------------------------------------------------------------------------
# B-15 — POST 429 retry is opt-in
# ---------------------------------------------------------------------------

def test_post_does_not_retry_on_429_by_default():
    client = make_client()
    client._session.post = MagicMock(
        side_effect=[make_429_response("1"), make_post_response(204)]
    )
    with patch("src.metadata.discogs_client.time.sleep") as sleep:
        resp = client._request("POST", "https://api.discogs.com/x", json={"value": "1"})

    assert resp.status_code == 429              # returned as-is, NOT retried
    assert client._session.post.call_count == 1
    sleep.assert_not_called()


def test_post_retries_on_429_when_opted_in():
    client = make_client()
    client._session.post = MagicMock(
        side_effect=[make_429_response("1"), make_post_response(204)]
    )
    with patch("src.metadata.discogs_client.time.sleep"):
        resp = client._request(
            "POST", "https://api.discogs.com/x", retry_on_429=True, json={"value": "1"}
        )

    assert resp.status_code == 204
    assert client._session.post.call_count == 2


def test_get_still_retries_on_429_by_default():
    client = make_client()
    client._session.get = MagicMock(
        side_effect=[make_429_response("1"), make_get_response(200, {})]
    )
    with patch("src.metadata.discogs_client.time.sleep"):
        resp = client._request("GET", "https://api.discogs.com/x")

    assert resp.status_code == 200
    assert client._session.get.call_count == 2


# ---------------------------------------------------------------------------
# B-16 — numeric Play Count value is coerced, not dropped
# ---------------------------------------------------------------------------

def test_numeric_field_value_increments_correctly():
    """Discogs returns the Play Count as a JSON number (5, not "5") — the
    increment must still run and post "6", not silently treat it as 0/skip."""
    client = make_client()
    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, 5))  # int value
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=make_post_response(204))

    assert client.increment_play_count(release_id=111, instance_id=42) is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "6"


def test_numeric_zero_field_value_becomes_one():
    client = make_client()
    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, 0))  # int 0
    client._session.get = MagicMock(return_value=get_resp)
    client._session.post = MagicMock(return_value=make_post_response(204))

    assert client.increment_play_count(release_id=111, instance_id=42) is True
    _, kwargs = client._session.post.call_args
    assert kwargs["json"]["value"] == "1"
