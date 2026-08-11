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
from unittest.mock import MagicMock

import pytest
import requests

from src.tracking.listen_tracker import ListenTracker
from src.metadata.models import PlaySession
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
