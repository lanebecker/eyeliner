"""Regression tests for B-15 and B-16 (Discogs client robustness).

B-15 — _request must not blindly retry a POST on 429 unless the caller asserts
       the body is an idempotent absolute-set (retry_on_429=True).  GET still
       retries by default.
B-16 — a numeric (JSON-number) Play Count value must not AttributeError on
       .strip() and silently skip the increment.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.metadata.discogs.transport import DiscogsRateLimited, _RATE_LIMIT_MAX_WAIT
from tests.test_discogs_client import (
    make_client, make_post_response, make_get_response, make_429_response,
    instance_response, _FIELD_ID,
)


# ---------------------------------------------------------------------------
# B-15 — POST 429 retry is opt-in
# ---------------------------------------------------------------------------

def test_post_does_not_retry_on_429_by_default():
    client = make_client()
    client._http.session.post = MagicMock(
        side_effect=[make_429_response("1"), make_post_response(204)]
    )
    with patch("src.metadata.discogs.transport.time.sleep") as sleep:
        resp = client._http.request("POST", "https://api.discogs.com/x", json={"value": "1"})

    assert resp.status_code == 429              # returned as-is, NOT retried
    assert client._http.session.post.call_count == 1
    sleep.assert_not_called()


def test_post_retries_on_429_when_opted_in():
    client = make_client()
    client._http.session.post = MagicMock(
        side_effect=[make_429_response("1"), make_post_response(204)]
    )
    with patch("src.metadata.discogs.transport.time.sleep"):
        resp = client._http.request(
            "POST", "https://api.discogs.com/x", retry_on_429=True, json={"value": "1"}
        )

    assert resp.status_code == 204
    assert client._http.session.post.call_count == 2


def test_get_still_retries_on_429_by_default():
    client = make_client()
    client._http.session.get = MagicMock(
        side_effect=[make_429_response("1"), make_get_response(200, {})]
    )
    with patch("src.metadata.discogs.transport.time.sleep"):
        resp = client._http.request("GET", "https://api.discogs.com/x")

    assert resp.status_code == 200
    assert client._http.session.get.call_count == 2


# ---------------------------------------------------------------------------
# B-16 — numeric Play Count value is coerced, not dropped
# ---------------------------------------------------------------------------

def test_numeric_field_value_increments_correctly():
    """Discogs returns the Play Count as a JSON number (5, not "5") — the
    increment must still run and post "6", not silently treat it as 0/skip."""
    client = make_client()
    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, 5))  # int value
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    assert client.increment_play_count(release_id=111, instance_id=42) is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "6"


def test_numeric_zero_field_value_becomes_one():
    client = make_client()
    get_resp = make_get_response(200, instance_response(42, _FIELD_ID, 0))  # int 0
    client._http.session.get = MagicMock(return_value=get_resp)
    client._http.session.post = MagicMock(return_value=make_post_response(204))

    assert client.increment_play_count(release_id=111, instance_id=42) is True
    _, kwargs = client._http.session.post.call_args
    assert kwargs["json"]["value"] == "1"


# ---------------------------------------------------------------------------
# #229 — honor a long Retry-After for the Play Count credit: transport raises a
# typed signal (only for opted-in idempotent writes) so the async finalize layer
# can wait it out in the event loop instead of burning in-window retries.
# ---------------------------------------------------------------------------

_LONG = str(_RATE_LIMIT_MAX_WAIT + 50)   # a Retry-After beyond the in-thread cap


def test_honored_post_raises_rate_limited_on_long_retry_after():
    """A POST that opts in (honor_long_retry_after=True) and gets a 429 whose
    Retry-After exceeds the in-thread cap RAISES DiscogsRateLimited carrying the
    server wait — it does NOT sleep in-thread and does NOT fire a second POST.
    RED before #229 (the branch returned the 429 response instead)."""
    client = make_client()
    client._http.session.post = MagicMock(return_value=make_429_response(_LONG))
    with patch("src.metadata.discogs.transport.time.sleep") as sleep:
        with pytest.raises(DiscogsRateLimited) as ei:
            client._http.request(
                "POST", "https://api.discogs.com/x",
                retry_on_429=True, honor_long_retry_after=True, json={"value": "1"},
            )
    assert ei.value.retry_after == _RATE_LIMIT_MAX_WAIT + 50
    assert client._http.session.post.call_count == 1   # no futile in-window retry
    sleep.assert_not_called()                           # no in-thread parking


def test_non_honored_post_returns_429_on_long_retry_after_unchanged():
    """A POST that did NOT opt in keeps the pre-#229 behaviour: the long-429 is
    returned as-is (no raise, no retry), so update_last_played and any other
    write is untouched by #229."""
    client = make_client()
    client._http.session.post = MagicMock(return_value=make_429_response(_LONG))
    with patch("src.metadata.discogs.transport.time.sleep") as sleep:
        resp = client._http.request(
            "POST", "https://api.discogs.com/x", retry_on_429=True, json={"value": "1"},
        )
    assert resp.status_code == 429
    assert client._http.session.post.call_count == 1
    sleep.assert_not_called()


def test_get_never_raises_rate_limited_on_long_retry_after():
    """Honoring is scoped to the opted-in write; a GET past the cap keeps the
    pre-#229 log-and-return behaviour (no raise into the reader/resolver path)."""
    client = make_client()
    client._http.session.get = MagicMock(return_value=make_429_response(_LONG))
    with patch("src.metadata.discogs.transport.time.sleep"):
        resp = client._http.request("GET", "https://api.discogs.com/x")   # honor flag defaults False
    assert resp.status_code == 429
    assert client._http.session.get.call_count == 1


def test_honored_post_within_cap_still_sleeps_in_thread_and_retries():
    """The opt-in changes only the BEYOND-cap branch: a Retry-After WITHIN the
    cap still takes the in-thread sleep-and-retry-once path (no raise)."""
    client = make_client()
    within = str(_RATE_LIMIT_MAX_WAIT - 1)
    client._http.session.post = MagicMock(
        side_effect=[make_429_response(within), make_post_response(204)]
    )
    with patch("src.metadata.discogs.transport.time.sleep") as sleep:
        resp = client._http.request(
            "POST", "https://api.discogs.com/x",
            retry_on_429=True, honor_long_retry_after=True, json={"value": "1"},
        )
    assert resp.status_code == 204
    assert client._http.session.post.call_count == 2
    sleep.assert_called_once()


def test_increment_play_count_propagates_rate_limited_not_false():
    """The writer must PROPAGATE DiscogsRateLimited (its broad except swallows
    everything else to False); swallowing it would drop the credit the finalize
    layer means to retry.  RED before the writer's explicit re-raise."""
    client = make_client()
    client._http.session.get = MagicMock(
        return_value=make_get_response(200, instance_response(42, _FIELD_ID, 5))
    )
    client._http.session.post = MagicMock(return_value=make_429_response(_LONG))
    with patch("src.metadata.discogs.transport.time.sleep"):
        with pytest.raises(DiscogsRateLimited):
            client.increment_play_count(release_id=111, instance_id=42)


def test_increment_propagates_rate_limited_when_the_READ_is_throttled():
    """#229 Finding-1 fix: in a real throttle window the pre-write READ 429s too.
    The credit read now opts into honoring, so a long-429 on the GET RAISES
    DiscogsRateLimited (propagated) instead of returning _READ_FAILED → False —
    which would have lost the credit to the short in-window backoff.  RED before
    the read opted in."""
    client = make_client()
    client._http.session.get = MagicMock(return_value=make_429_response(_LONG))
    client._http.session.post = MagicMock(return_value=make_post_response(204))
    with patch("src.metadata.discogs.transport.time.sleep"):
        with pytest.raises(DiscogsRateLimited):
            client.increment_play_count(release_id=111, instance_id=42)
    client._http.session.post.assert_not_called()   # never reached the write


def test_cold_fields_cache_read_also_honors_the_wait():
    """#229 Finding-1 fix: the first credit of a session cold-loads the fields
    map; a long-429 there must also honor the wait (raise), not abort the credit.
    RED before the fields GET opted in (it raised a plain HTTPError → swallowed
    to False)."""
    client = make_client()
    client._collection_fields = None                # cold cache
    client._http.session.get = MagicMock(return_value=make_429_response(_LONG))
    with patch("src.metadata.discogs.transport.time.sleep"):
        with pytest.raises(DiscogsRateLimited):
            client.increment_play_count(release_id=111, instance_id=42)
