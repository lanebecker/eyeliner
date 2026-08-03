"""Shared Discogs REST transport (A-4).

`DiscogsHttp` is the one HTTP seam both halves of the old DiscogsClient now
share: an authenticated ``requests.Session`` plus a rate-limit-aware
``request()`` that honours a single HTTP 429 retry.  The read half
(:class:`~src.metadata.discogs.reader.DiscogsReader`) and the write half
(:class:`~src.metadata.discogs.writer.DiscogsCollectionWriter`) each hold a
reference to one of these; neither owns the transport, and neither can see the
other's caches or methods.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
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
# excess traffic with HTTP 429 + a Retry-After header (seconds).  request()
# honours the header for a single retry — but ONLY when the requested wait fits
# within the cap below.
#
# The cap is 10s: request() runs on the SHARED run_in_executor(None,…) pool,
# which also serves cover downloads and Last.fm scrobbles, so a long sleep here
# parks a worker those tasks could use (P-2).  A Retry-After WITHIN the cap is
# honoured and retried once.  A Retry-After BEYOND the cap is NOT retried — a
# retry inside the cap would land in the same throttle window and 429 again
# (META-10) — so the futile retry is skipped and a distinct, loud error is
# logged instead.  Actually waiting out a long Retry-After (so the write still
# lands) needs Discogs off the shared pool and is deferred to the dedicated
# executor (#61).  The P-1 collection index slashed Discogs request volume,
# making 429 bursts far less likely in the first place.
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

        # #61: Discogs blocking calls — every reader/writer method AND the 429
        # backoff time.sleep() inside request() — run on this DEDICATED pool,
        # never the shared default run_in_executor(None, …) executor. A rate-limit
        # sleep here therefore can NOT park a worker that cover downloads or
        # Last.fm scrobbles need (the P-2 concern that capped the backoff at 10s).
        # max_workers=2: the reader and writer halves each have at most one call
        # in flight in this single-turntable appliance, so two workers cover the
        # real concurrency without over-threading the Pi. Threads spawn lazily on
        # first submit, so constructing a DiscogsHttp stays cheap (tests that
        # build one but never dispatch pay nothing).
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="discogs")

    def request(
        self, method: str, url: str, retry_on_429: Optional[bool] = None, **kwargs
    ) -> requests.Response:
        """Issue a session request with rate-limit awareness (v1.3.3).

        All direct REST calls go through here so that an HTTP 429 from Discogs
        is retried at most once.  The retry sleeps for the server-suggested
        Retry-After (with _RATE_LIMIT_DEFAULT_WAIT as the fallback when the
        header is missing or unparseable) — but ONLY when that wait is within
        _RATE_LIMIT_MAX_WAIT.  A Retry-After beyond the cap is NOT retried (the
        retry would still be throttled); the futile retry is skipped and a
        distinct, loud error is logged instead (META-10).

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
                retry_after = int(resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT_WAIT))
            except (TypeError, ValueError):
                retry_after = _RATE_LIMIT_DEFAULT_WAIT

            if retry_after > _RATE_LIMIT_MAX_WAIT:
                # Discogs is asking us to back off LONGER than we are willing to
                # park this shared executor thread (P-2).  A retry inside our cap
                # would land in the same throttle window and 429 again, so skip the
                # futile retry entirely — no wasted sleep, no second request
                # hammering Discogs mid-backoff — and surface a DISTINCT, LOUD
                # outcome (META-10) instead of a capped wait the caller can't tell
                # apart from a generic failure.  Actually WAITING OUT a long
                # Retry-After so the write can still land needs Discogs off the
                # shared pool; that is deferred to the dedicated executor (#61).
                log.error(
                    "Discogs rate limit (429) for %s %s: server asked to wait %ss, "
                    "beyond our %ss cap — NOT retrying (it would still be throttled). "
                    "This request fails now and its caller has no further retry, so "
                    "the write (e.g. a Play Count credit) may be lost.",
                    method, _redact_url(url), retry_after, _RATE_LIMIT_MAX_WAIT,
                )
                return resp

            wait = max(1, retry_after)
            log.warning(
                "Discogs rate limit (429) for %s %s; retrying once in %ss "
                "(Retry-After=%ss).",
                method, _redact_url(url), wait, retry_after,
            )
            time.sleep(wait)
            resp = send(url, **kwargs)
            if resp.status_code == 429:
                # Still throttled after our one retry: a DISTINCT, LOUD outcome so
                # a lost credit is not silently conflated with a generic failure
                # (META-10).  Recovery (deferring the credit) is out of scope here.
                log.error(
                    "Discogs STILL rate-limiting (429) after the retry for %s %s — "
                    "this request cannot complete and its caller has no further "
                    "retry, so the write (e.g. a Play Count credit) may be lost.",
                    method, _redact_url(url),
                )
        return resp

    async def run(self, fn, *args):
        """Dispatch a blocking Discogs call on the DEDICATED executor (#61).

        Every Discogs reader/writer method is synchronous — it ends in a
        ``requests`` call, possibly with a 429 backoff ``time.sleep()``. The
        async pipeline (the resolver's collection/database searches, the listen
        tracker's Play Count / Last Played writes) invokes it through here
        instead of ``loop.run_in_executor(None, …)`` so the blocking work — and
        any backoff sleep — lands on THIS pool, isolated from the shared default
        executor that serves cover downloads and Last.fm scrobbles/loves.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def close(self) -> None:
        """Shut the dedicated executor down at composition-root teardown (#61).

        ``cancel_futures=True`` drops work still QUEUED (never handed to a
        thread); ``wait=False`` means we do NOT block shutdown on a call already
        running — a Discogs request is bounded by its 15s socket timeout plus at
        most one 10s backoff sleep, and the interpreter joins the worker at exit.
        Blocking here (``wait=True``) is deliberately avoided so this teardown can
        never hang shutdown the way the default executor can (CRIT-3, Wave 2).
        Idempotent: a second call is a harmless no-op on an already-shut pool.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
