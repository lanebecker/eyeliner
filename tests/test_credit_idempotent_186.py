"""#186 (R5-02) — the end-of-session Play Count credit must read ONCE and retry
only the ABSOLUTE set, so an ambiguous POST (applied server-side but response
lost) credits a completed play exactly +1, never +2.

This is the seam-level reproduction the R4 verifier asked for: a real
ListenTracker and a real DiscogsCollectionWriter, with only the HTTP seam
(session.get / session.post) faked so the first POST applies then raises. The
pre-#186 code re-read the incremented value on retry and posted current+1 again
(6 -> 7 for one play); the fixed code re-POSTs the same absolute target (6).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from src.tracking.listen_tracker import ListenTracker
from src.metadata.models import PlaySession
from src.metadata.discogs.outcomes import CollectionIdentity
from tests.factories import make_discogs_writer, make_discogs_http, make_discogs_config


def _writer_with_seam(first_post="lost", start=5):
    """Real writer; server ALWAYS applies a POST, but the first response is
    optionally lost. Returns (writer, server_state, post_counter)."""
    http = make_discogs_http()
    writer = make_discogs_writer(
        http=http, config=make_discogs_config(last_played_field_name=None)
    )
    writer._collection_fields = {"Play Count": 3}
    server = {"count": start}
    posts = {"n": 0}

    def fake_get(url, **kw):
        r = MagicMock(); r.status_code = 200
        r.json.return_value = {"releases": [
            {"instance_id": 777, "notes": [{"field_id": 3, "value": str(server["count"])}]}
        ]}
        return r

    def fake_post(url, **kw):
        posts["n"] += 1
        server["count"] = int(kw["json"]["value"])   # server always applies
        if posts["n"] == 1 and first_post == "lost":
            raise requests.exceptions.ReadTimeout("read timed out (after apply)")
        r = MagicMock(); r.status_code = 204
        return r

    http.session.get = fake_get
    http.session.post = fake_post
    return writer, server, posts


def _session():
    s = PlaySession(); s.album_release_id = 999; s.album_instance_id = 777
    return s


@pytest.mark.asyncio
async def test_ambiguous_applied_post_credits_exactly_once():
    writer, server, posts = _writer_with_seam(first_post="lost", start=5)
    tracker = ListenTracker(writer=writer)
    session = _session()

    await tracker._credit_completed_album(session)

    # RED before #186: server["count"] == 7 (double credit).
    assert server["count"] == 6, f"expected +1 (6), got {server['count']} — double credit"
    assert posts["n"] == 2, "both POSTs fire, but both write the same absolute 6"
    assert session.credited is True


@pytest.mark.asyncio
async def test_clean_credit_reads_once_posts_once():
    writer, server, posts = _writer_with_seam(first_post="ok", start=5)
    tracker = ListenTracker(writer=writer)
    session = _session()

    await tracker._credit_completed_album(session)

    assert server["count"] == 6
    assert posts["n"] == 1          # no spurious extra write on the happy path
    assert session.credited is True


@pytest.mark.asyncio
async def test_unreadable_value_aborts_without_writing():
    """META-1: if the current value can't be read, the credit aborts without any
    POST and leaves `credited` False — never clobbers with an absolute 1."""
    http = make_discogs_http()
    writer = make_discogs_writer(
        http=http, config=make_discogs_config(last_played_field_name=None)
    )
    writer._collection_fields = {"Play Count": 3}
    posts = {"n": 0}

    def fake_get(url, **kw):
        r = MagicMock(); r.status_code = 500      # read fails → _READ_FAILED
        return r

    def fake_post(url, **kw):
        posts["n"] += 1
        r = MagicMock(); r.status_code = 204
        return r

    http.session.get = fake_get
    http.session.post = fake_post

    tracker = ListenTracker(writer=writer)
    session = _session()
    await tracker._credit_completed_album(session)

    assert posts["n"] == 0, "must not POST when the current value is unknown (META-1)"
    assert session.credited is False


@pytest.mark.asyncio
async def test_transient_read_then_success_still_credits_once():
    """A transient read failure on attempt 1 must be re-read on a later attempt
    (the GET is idempotent) and still credit exactly once — read robustness is
    not sacrificed by the read-once memo."""
    http = make_discogs_http()
    writer = make_discogs_writer(
        http=http, config=make_discogs_config(last_played_field_name=None)
    )
    writer._collection_fields = {"Play Count": 3}
    server = {"count": 5}
    gets = {"n": 0}
    posts = {"n": 0}

    def fake_get(url, **kw):
        gets["n"] += 1
        r = MagicMock()
        if gets["n"] == 1:
            r.status_code = 503                    # transient read blip
            return r
        r.status_code = 200
        r.json.return_value = {"releases": [
            {"instance_id": 777, "notes": [{"field_id": 3, "value": str(server["count"])}]}
        ]}
        return r

    def fake_post(url, **kw):
        posts["n"] += 1
        server["count"] = int(kw["json"]["value"])
        r = MagicMock(); r.status_code = 204
        return r

    http.session.get = fake_get
    http.session.post = fake_post

    tracker = ListenTracker(writer=writer)
    session = _session()
    await tracker._credit_completed_album(session)

    assert server["count"] == 6, "credited exactly +1 after the read recovered"
    assert posts["n"] == 1
    assert session.credited is True


@pytest.mark.asyncio
async def test_definitively_missing_instance_skips_post_last_played_and_retry():
    """A complete snapshot proving removal is terminal until recovery can repair it."""
    http = make_discogs_http()
    writer = make_discogs_writer(
        http=http, config=make_discogs_config(last_played_field_name="Last Played")
    )
    writer._collection_fields = {"Play Count": 3, "Last Played": 7}
    calls = {"get": 0, "post": 0, "last_played": 0}

    def fake_get(url, **kw):
        calls["get"] += 1
        response = MagicMock(); response.status_code = 200
        response.json.return_value = {
            "pagination": {"page": 1, "pages": 1, "items": 1, "per_page": 1},
            "releases": [{
                "instance_id": 888, "folder_id": 0,
                "basic_information": {"id": 999}, "notes": [],
            }],
        }
        return response

    def fake_post(url, **kw):
        calls["post"] += 1
        response = MagicMock(); response.status_code = 204
        return response

    http.session.get = fake_get
    http.session.post = fake_post
    original_last_played = writer.update_last_played

    def count_last_played(*args):
        calls["last_played"] += 1
        return original_last_played(*args)

    writer.update_last_played = count_last_played
    tracker = ListenTracker(writer=writer)
    session = _session()

    with patch("src.tracking.listen_tracker.asyncio.sleep", new=AsyncMock()) as sleep:
        await tracker._credit_completed_album(session)

    assert calls == {"get": 1, "post": 0, "last_played": 0}
    sleep.assert_not_awaited()
    assert session.credited is False


@pytest.mark.asyncio
async def test_ambiguous_old_instance_post_never_invokes_recovery():
    """A response-lost absolute POST is not missing-instance evidence."""
    writer, _server, _posts = _writer_with_seam(first_post="lost", start=5)
    recovery = AsyncMock(return_value=CollectionIdentity(999, 888))
    tracker = ListenTracker(writer=writer, recover_collection_instance=recovery)
    session = _session()

    await tracker._credit_completed_album(session)

    recovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_absolute_post_retry_reuses_one_target():
    """A recovered credit still retries only one precomputed absolute value."""
    http = make_discogs_http()
    writer = make_discogs_writer(
        http=http, config=make_discogs_config(last_played_field_name=None)
    )
    writer._collection_fields = {"Play Count": 3}
    server = {"count": 5}
    post_bodies = []
    gets = {"n": 0}

    def fake_get(url, **kw):
        gets["n"] += 1
        response = MagicMock(); response.status_code = 200
        if gets["n"] == 1:
            response.json.return_value = {
                "pagination": {"page": 1, "pages": 1, "items": 1, "per_page": 1},
                "releases": [{
                    "instance_id": 888, "folder_id": 0,
                    "basic_information": {"id": 999}, "notes": [],
                }],
            }
        else:
            response.json.return_value = {"releases": [{
                "instance_id": 888,
                "notes": [{"field_id": 3, "value": str(server["count"])}],
            }]}
        return response

    def fake_post(url, **kw):
        post_bodies.append(kw["json"])
        server["count"] = int(kw["json"]["value"])
        if len(post_bodies) == 1:
            raise requests.exceptions.ReadTimeout("response lost after apply")
        response = MagicMock(); response.status_code = 204
        return response

    http.session.get = fake_get
    http.session.post = fake_post
    recovery = AsyncMock(return_value=CollectionIdentity(999, 888))
    tracker = ListenTracker(writer=writer, recover_collection_instance=recovery)
    session = _session()
    session.album_resolve_key = ("sonic youth", "sister")

    await tracker._credit_completed_album(session)

    recovery.assert_awaited_once_with(("sonic youth", "sister"), 999, 777, (888,))
    assert gets["n"] == 2
    assert post_bodies == [{"value": "6"}, {"value": "6"}]
    assert server["count"] == 6
    assert session.credited is True
