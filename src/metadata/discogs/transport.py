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
import datetime as _dt
import logging
import time
from email.utils import parsedate_to_datetime
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
# The cap is 10s: request() runs on the DEDICATED 2-worker Discogs executor (#61,
# constructed below), so a long in-thread sleep here parks one of only two workers
# that serve every Discogs read+write (P-2).  A Retry-After WITHIN the cap is
# honoured and retried once, in-thread.  A Retry-After BEYOND the cap is NOT slept
# in-thread — a retry inside the cap would land in the same throttle window and 429
# again (META-10).  Beyond the cap the behaviour forks on honor_long_retry_after
# (#229): an opted-in idempotent write (the Play Count credit) raises
# DiscogsRateLimited so the ASYNC finalize layer waits the server-requested backoff
# out in the EVENT LOOP (cancellable, parking no thread) and re-issues the write —
# so a long Retry-After no longer loses the credit, and the futile in-window retry
# #163 used to fire is gone (this is the post-#229 reality that superseded the old
# "deferred to the dedicated executor" note).  Every other caller (all GETs, the
# single-shot Last Played write) gets the futile retry skipped and a distinct, loud
# error logged instead.  The P-1 collection index slashed Discogs request volume,
# making 429 bursts far less likely in the first place.
_RATE_LIMIT_MAX_WAIT = 10
_RATE_LIMIT_DEFAULT_WAIT = 2


def _parse_retry_after(resp, default: int = _RATE_LIMIT_DEFAULT_WAIT) -> int:
    """Parse an HTTP ``Retry-After`` header into whole seconds.

    RFC 7231 allows TWO forms and Discogs sends the first today, but a fronting
    CDN can legitimately send either:

      * ``delay-seconds`` — a non-negative integer (``"60"``).
      * ``HTTP-date`` — an absolute time (``"Wed, 21 Oct 2026 07:28:00 GMT"``);
        the wait is that instant minus now.

    ``int()`` alone raised on the date form and the caller fell back to the tiny
    ``_RATE_LIMIT_DEFAULT_WAIT`` — turning a two-minute server backoff into a 2s
    in-window retry that just 429s again (R5-31/#232).  This handles both, clamps
    a past/negative result to 0, and returns ``default`` only when the header is
    absent or genuinely unparseable.
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return default
    raw = str(raw).strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(raw)  # tz-aware or naive-UTC per RFC
    except (TypeError, ValueError, IndexError):
        return default
    if when is None:
        return default
    # parsedate_to_datetime yields a tz-aware datetime for a conforming GMT/offset
    # header; some non-conforming inputs come back naive, which per RFC 7231 we
    # read as UTC. Compare in the datetime's own awareness to avoid a naive/aware
    # subtraction TypeError, and use timezone-aware UTC (utcnow() is deprecated on
    # the 3.13 target).
    now = _dt.datetime.now(when.tzinfo) if when.tzinfo else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    return max(0, int((when - now).total_seconds()))


class DiscogsRateLimited(Exception):
    """A 429 whose ``Retry-After`` exceeds ``_RATE_LIMIT_MAX_WAIT`` on a caller
    that OPTED IN to honoring it (``honor_long_retry_after=True``) — #229.

    Rather than park this executor thread for a minute (the P-2 concern the cap
    protects against) or fire futile in-window retries, ``request()`` raises this
    so the ASYNC caller can wait the server-requested backoff out in the event
    loop (cancellable, thread-free) and re-issue the write.  Only the idempotent
    absolute-set Play Count POST opts in, so honoring the wait can never
    double-credit (#186).  ``retry_after`` is the server's requested wait in
    seconds; the async layer decides its own honored cap.
    """

    def __init__(self, retry_after: int, method: str = "", url: str = ""):
        self.retry_after = retry_after
        self.method = method
        self.url = url
        super().__init__(
            f"Discogs rate limit (429) on {method} {url}: "
            f"server asked to wait {retry_after}s (beyond the {_RATE_LIMIT_MAX_WAIT}s "
            f"in-thread cap); deferring the wait to the event loop."
        )


def _as_id(value, name: str) -> int:
    """Coerce an identifier to a positive int before it is interpolated into a
    write URL.

    `release_id`, `instance_id`, and `field_id` come from Discogs' own API
    responses and are interpolated directly into the collection-field POST path
    (finding S-5).  They are normally well-formed, but a corrupt or unexpected
    API response could otherwise build a surprising request path silently.
    Coercing here makes a malformed value fail loudly at the boundary instead.

    R5-37 (#263): the coercion must reject values that ``int()`` would *silently
    reshape* rather than reject — otherwise the "fail loudly" guarantee leaks.
    Two such inputs get through a bare ``int(value)``:

    * ``bool`` — ``True``/``False`` are ``int`` subclasses, so ``int(True)`` is
      ``1``. A stray boolean would build the path ``…/instances/1/…`` instead of
      failing, writing to a real, wrong record.
    * non-integer ``float`` — ``int(3.9)`` truncates to ``3``, so a corrupt
      ``3.9`` would silently target release ``3``.

    So accept only a genuine integer (explicitly excluding ``bool``) or a string
    that is *exactly* an integer literal; reject everything else — including any
    ``float`` — at the boundary.
    """
    if isinstance(value, bool):
        # bool is a subclass of int; int(True) == 1 would pass silently.
        raise ValueError(f"{name} must be an integer, got bool {value!r}")
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, str):
        text = value.strip()
        # Accept only a clean integer literal (optional leading sign). Reject
        # floats ("3.9"), separators, and anything non-numeric. The <= 0 check
        # below then rejects "0"/"-5".
        sign, digits = ("", text)
        if text[:1] in "+-":
            sign, digits = text[:1], text[1:]
        # ASCII-only: str.isdigit() is True for fullwidth ("４２") and other
        # Unicode digit forms, and superscripts ("²") are isdigit()-True but
        # int()-unparseable (would leak a raw "invalid literal" ValueError past
        # the tidy boundary message). Require plain ASCII 0-9 so this is a
        # genuinely CLEAN integer literal.
        if not (digits.isascii() and digits.isdigit()):
            raise ValueError(f"{name} must be an integer, got {value!r}")
        coerced = int(sign + digits)
    else:
        # float, Decimal, None, arbitrary objects — no silent int() reshaping.
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if coerced <= 0:
        raise ValueError(f"{name} must be a positive integer, got {coerced}")
    return coerced


def _redact_url(url: str) -> str:
    """Return a log-safe version of a Discogs URL: path only, with the username
    segment masked and the query string dropped (finding S-4).

    In THIS transport the auth token rides in an Authorization header, not the
    URL, so dropping the query here is defence-in-depth against a future
    query-string credential rather than a live leak. Note the OTHER half of
    Discogs traffic is different: the python3-discogs-client library
    (DiscogsReader's search/release/tracklist calls) authenticates with the token
    as a URL QUERY parameter, so its exception text DOES embed the token — that
    leak is handled centrally by main.py's _SecretRedactingFilter (#202), not
    here, because those library exceptions never pass through this function.
    """
    try:
        parts = urlsplit(url)
        segments = parts.path.split("/")
        # Mask the segment immediately after ".../users/" if present.
        for i, seg in enumerate(segments):
            if seg == "users" and i + 1 < len(segments) and segments[i + 1]:
                segments[i + 1] = "{user}"
                break
        # Never fall back to the raw `url` (SEC-2): when parts.path is empty,
        # "/".join([""]) is "" — returning `url` there would leak the query
        # string the redaction exists to drop.  A bare "/" is the safe default.
        return "/".join(segments) or "/"
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
        self,
        method: str,
        url: str,
        retry_on_429: Optional[bool] = None,
        honor_long_retry_after: bool = False,
        **kwargs,
    ) -> requests.Response:
        """Issue a session request with rate-limit awareness (v1.3.3).

        All direct REST calls go through here so that an HTTP 429 from Discogs
        is retried at most once.  The retry sleeps for the server-suggested
        Retry-After (with _RATE_LIMIT_DEFAULT_WAIT as the fallback when the
        header is missing or unparseable) — but ONLY when that wait is within
        _RATE_LIMIT_MAX_WAIT.  A Retry-After beyond the cap is NOT retried
        in-thread (the retry would still be throttled).  Beyond the cap the
        behaviour forks on ``honor_long_retry_after`` (#229): callers that opt in
        — only the idempotent Play Count POST — get a raised
        :class:`DiscogsRateLimited` carrying the server's wait, so the async
        finalize layer can honour it in the event loop; everyone else (all GETs,
        Last Played) gets the futile retry skipped and a distinct, loud error
        logged instead (META-10).

        `retry_on_429` controls whether that one retry happens.  It defaults to
        True for GET (always safe to repeat) and False for POST: a blind POST
        retry is only safe when the body is an idempotent absolute-set, not a
        server-side increment (B-15).  The two POST callers (Play Count and Last
        Played) write absolute values, so they opt in explicitly; any future
        non-idempotent POST gets no surprise double-submit.

        This runs on an executor thread (every caller is dispatched via
        run_in_executor), so the time.sleep() here never blocks the event loop.

        The method is normalised to upper-case and dispatched via
        self.session.get / self.session.post (rather than session.request) so
        tests can keep mocking those two methods as the single HTTP seam.  Only
        GET and POST are supported — any other verb raises rather than silently
        falling through to a POST (LB-2); this is the transport that WRITES to
        the real collection, so a wrong verb must fail loudly.
        """
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        method = method.upper()
        if retry_on_429 is None:
            retry_on_429 = (method == "GET")
        if method == "GET":
            send = self.session.get
        elif method == "POST":
            send = self.session.post
        else:
            raise ValueError(f"unsupported HTTP method: {method!r}")
        resp = send(url, **kwargs)
        if resp.status_code == 429 and retry_on_429:
            retry_after = _parse_retry_after(resp)

            if retry_after > _RATE_LIMIT_MAX_WAIT:
                # Discogs is asking us to back off LONGER than we are willing to
                # park this executor thread (P-2). A retry inside our cap would land
                # in the same throttle window and 429 again, so we never sleep the
                # long wait IN-THREAD.
                if honor_long_retry_after:
                    # #229: an idempotent write POST (the Play Count credit) opted
                    # in to honoring the long wait. Raise so the ASYNC finalize
                    # layer can await it in the event loop — cancellable at shutdown,
                    # parking no thread — and re-POST the same absolute value (#186,
                    # so no double-credit). Do NOT sleep here.
                    log.warning(
                        "Discogs rate limit (429) for %s %s: server asked to wait "
                        "%ss (beyond the %ss in-thread cap); deferring the wait to "
                        "the event loop so the credit can still land (#229).",
                        method, _redact_url(url), retry_after, _RATE_LIMIT_MAX_WAIT,
                    )
                    raise DiscogsRateLimited(retry_after, method, _redact_url(url))
                # Not opted in (every GET, and the non-honored writes): skip the
                # futile retry entirely — no wasted sleep, no second request
                # hammering Discogs mid-backoff — and surface a DISTINCT, LOUD
                # outcome (META-10) instead of a capped wait the caller can't tell
                # apart from a generic failure.
                log.error(
                    "Discogs rate limit (429) for %s %s: server asked to wait %ss, "
                    "beyond our %ss cap — NOT retrying in-thread (it would still be "
                    "throttled). This attempt fails now; whether it is re-tried is the "
                    "caller's: a single-shot write (e.g. Last Played) has no further "
                    "retry and may be lost, while the finalize-retried, honor-capable "
                    "Play Count credit is re-attempted (and a long wait is honored via "
                    "#229, so it does not reach this branch).",
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
                # Still throttled after our one in-thread retry.  The second 429
                # carries its OWN Retry-After, and inside a real throttle window
                # that is often the LONG one — so an opted-in idempotent write must
                # honor it here too, exactly as the first-429 branch does.  Before
                # R5-06/#231 this branch ignored both the header and
                # honor_long_retry_after and just returned the 429, so the finalize
                # layer fell back to its short in-window backoff — the futile-retry
                # loss #229 exists to prevent, one hop later.
                retry_after = _parse_retry_after(resp)
                if retry_after > _RATE_LIMIT_MAX_WAIT and honor_long_retry_after:
                    log.warning(
                        "Discogs STILL rate-limiting (429) after the retry for %s %s: "
                        "server now asks to wait %ss (beyond the %ss in-thread cap); "
                        "deferring the wait to the event loop so the credit can still "
                        "land (#229/#231).",
                        method, _redact_url(url), retry_after, _RATE_LIMIT_MAX_WAIT,
                    )
                    raise DiscogsRateLimited(retry_after, method, _redact_url(url))
                # Not opted in, or a short second wait: a DISTINCT, LOUD outcome so
                # a lost credit is not silently conflated with a generic failure
                # (META-10).  We have already spent our one in-thread retry, so we
                # do NOT sleep-and-retry a second time here.
                log.error(
                    "Discogs STILL rate-limiting (429) after the one in-thread retry "
                    "for %s %s — this attempt cannot complete. Whether it is re-tried "
                    "is the caller's: a single-shot write (e.g. Last Played) has none "
                    "and may be lost; the finalize-retried Play Count credit is "
                    "re-attempted (and a long second wait is honored via #229/#231).",
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
