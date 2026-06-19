"""Shared Discogs REST transport (A-4).

`DiscogsHttp` is the one HTTP seam both halves of the old DiscogsClient now
share: an authenticated ``requests.Session`` plus a rate-limit-aware
``request()`` that honours a single HTTP 429 retry.  The read half
(:class:`~src.metadata.discogs.reader.DiscogsReader`) and the write half
(:class:`~src.metadata.discogs.writer.DiscogsCollectionWriter`) each hold a
reference to one of these; neither owns the transport, and neither can see the
other's caches or methods.
"""

import logging
import time
from typing import Optional
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

_API_BASE = "https://api.discogs.com"

# Network timeout for every Discogs HTTP call.  Discogs is normally well under a
# second, but a flaky network or a CDN hiccup can hang a TCP connection for
# minutes without one.  All session.get/post calls pass this explicitly so an
# executor thread can't sit indefinitely on a stalled socket.
_HTTP_TIMEOUT = 15

# Discogs allows 60 requests/minute for authenticated callers and answers
# excess traffic with HTTP 429 + a Retry-After header (seconds).  When that
# happens we honour the header once per request, capped so a pathological
# header value can't park an executor thread for long.
#
# The cap is 10s (was 30s): request() runs on the SHARED run_in_executor(None,…)
# pool, which also serves cover downloads and Last.fm scrobbles, so a long
# sleep here parks a worker those tasks could use (P-2).  10s still honours any
# realistic Retry-After (Discogs rarely asks for more than a few seconds) while
# bounding the worst-case stall — and the P-1 collection index slashed Discogs
# request volume, making 429 bursts far less likely in the first place.
_RATE_LIMIT_MAX_WAIT = 10
_RATE_LIMIT_DEFAULT_WAIT = 2


def _as_id(value, name: str) -> int:
    """Coerce an identifier to a positive int before it is interpolated into a
    write URL.

    `release_id`, `instance_id`, and `field_id` come from Discogs' own API
    responses and are interpolated directly into the collection-field POST path
    (finding S-5).  They are normally well-formed, but a corrupt or unexpected
    API response could otherwise build a surprising request path silently.
    Coercing here makes a malformed value fail loudly at the boundary instead.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if coerced <= 0:
        raise ValueError(f"{name} must be a positive integer, got {coerced}")
    return coerced


def _redact_url(url: str) -> str:
    """Return a log-safe version of a Discogs URL: path only, with the username
    segment masked and the query string dropped (finding S-4).

    The auth token rides in a header (never the URL), so this isn't a live leak
    today, but the full request path embeds the account username and any future
    query-string credential would otherwise land in the logs verbatim.
    """
    try:
        parts = urlsplit(url)
        segments = parts.path.split("/")
        # Mask the segment immediately after ".../users/" if present.
        for i, seg in enumerate(segments):
            if seg == "users" and i + 1 < len(segments) and segments[i + 1]:
                segments[i + 1] = "{user}"
                break
        return "/".join(segments) or url
    except Exception:
        return "<unparseable-url>"


class DiscogsHttp:
    """Authenticated Discogs REST session with a rate-limit-aware ``request()``.

    Shared by the reader and writer halves; the python3-discogs-client library
    (used by the reader for search/release/master) does its own fetching and is
    NOT routed through here.
    """

    def __init__(self, token: str):
        # Raw requests session — used for collection membership checks, the
        # collection index, master-year lookups, and the field-update POSTs.
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Discogs token={token}",
            "User-Agent": "vinyl-now-playing/1.0",
            "Content-Type": "application/json",
        })

    def request(
        self, method: str, url: str, retry_on_429: Optional[bool] = None, **kwargs
    ) -> requests.Response:
        """Issue a session request with rate-limit awareness (v1.3.3).

        All direct REST calls go through here so that an HTTP 429 from Discogs
        is retried exactly once, after sleeping for the server-suggested
        Retry-After (clamped to _RATE_LIMIT_MAX_WAIT, with
        _RATE_LIMIT_DEFAULT_WAIT as the fallback when the header is missing or
        unparseable).

        `retry_on_429` controls whether that one retry happens.  It defaults to
        True for GET (always safe to repeat) and False for POST: a blind POST
        retry is only safe when the body is an idempotent absolute-set, not a
        server-side increment (B-15).  The two POST callers (Play Count and Last
        Played) write absolute values, so they opt in explicitly; any future
        non-idempotent POST gets no surprise double-submit.

        This runs on an executor thread (every caller is dispatched via
        run_in_executor), so the time.sleep() here never blocks the event loop.

        Dispatches via self.session.get / self.session.post (rather than
        session.request) so tests can keep mocking those two methods as the
        single HTTP seam.
        """
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        if retry_on_429 is None:
            retry_on_429 = (method == "GET")
        send = self.session.get if method == "GET" else self.session.post
        resp = send(url, **kwargs)
        if resp.status_code == 429 and retry_on_429:
            try:
                wait = int(resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT_WAIT))
            except (TypeError, ValueError):
                wait = _RATE_LIMIT_DEFAULT_WAIT
            wait = max(1, min(wait, _RATE_LIMIT_MAX_WAIT))
            log.warning(
                f"Discogs rate limit hit (429) for {method} {_redact_url(url)}; "
                f"retrying once in {wait}s."
            )
            time.sleep(wait)
            resp = send(url, **kwargs)
        return resp
