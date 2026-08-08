"""Metadata error taxonomy (A-6).

The resolver chain previously mixed three error idioms with no shared vocabulary
and no "expected miss vs. unexpected bug" distinction.  This module gives the
boundary one taxonomy:

  - **Transient** — "couldn't determine right now" (network blip, timeout, 429,
    5xx).  Expected; the album must stay retryable and uncached (see B-4/B-13).
  - **Permanent** — a definitive negative answer that won't change on retry.
  - **Unexpected** — anything else is a real bug, logged loudly.

External transient failures surface two ways.  Our own REST calls (the shared
`transport.py`) are requests-based and raise `requests` exceptions.  But the
python3-discogs-client library — used by the reader for search/release/master —
does its OWN fetching and raises its OWN `discogs_client.exceptions.HTTPError`
for a non-2xx status; that type does NOT inherit from
`requests.exceptions.RequestException` (META-6), so it must be listed
explicitly or a routine Discogs 429/5xx is misclassified as an unexpected bug.
`TRANSIENT_EXTERNAL_ERRORS` lists both families so the resolver classifies them
uniformly with our own raised errors.  The typed exceptions below are the
vocabulary for code that wants to *signal* these conditions explicitly as
adoption spreads.
"""
import discogs_client.exceptions
import musicbrainzngs
import requests


class MetadataError(Exception):
    """Base for errors in the metadata-resolution chain."""


class TransientMetadataError(MetadataError):
    """A temporary failure — retry later; do not cache a downgraded result."""


class PermanentMetadataError(MetadataError):
    """A definitive failure that will not change on retry."""


# External exception types that mean "transient / couldn't determine."
#  - requests.exceptions.RequestException: base for Timeout, ConnectionError,
#    HTTPError, etc. — our own REST calls (transport.py) are requests-based.
#  - discogs_client.exceptions.HTTPError: the python3-discogs-client library's
#    own non-2xx type (a Discogs 429/5xx from search/release/master). It does
#    NOT inherit from RequestException, so it must be listed explicitly (META-6).
#  - builtin ConnectionError / TimeoutError: socket-level network failures that
#    aren't wrapped by requests.
#  - musicbrainzngs.NetworkError: the MusicBrainz client is urllib-based (not
#    requests), and raises NetworkError for a connection failure, timeout, or
#    HTTP error talking to the service — i.e. "MusicBrainz is unreachable right
#    now," which is transient. Its sibling ResponseError / AuthenticationError
#    (an invalid/parse-failed response, or a 401) are definitive for that request
#    and are deliberately NOT listed here (#175).
TRANSIENT_EXTERNAL_ERRORS = (
    requests.exceptions.RequestException,
    discogs_client.exceptions.HTTPError,
    musicbrainzngs.NetworkError,
    ConnectionError,   # builtin (OSError subclass)
    TimeoutError,      # builtin
)


# HTTP statuses that mean "couldn't determine right now, retry later": 408
# Request Timeout, 429 Too Many Requests, and any 5xx server error.  Every
# OTHER status carried by an HTTPError is a definitive answer for this request
# (401/403 dead credential, 404/410 gone, 400/405 malformed) and is PERMANENT
# — it will not change on retry (#189).
_TRANSIENT_HTTP_STATUS = frozenset({408, 429})


def http_status(exc: BaseException):
    """Best-effort HTTP status from an exception, or None if it carries none.

    requests.HTTPError attaches the failing ``response`` (from
    ``raise_for_status``); python3-discogs-client's own HTTPError exposes
    ``status_code`` directly (it has no ``response``).  Connection/timeout
    errors and non-HTTP exceptions carry no status → None.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def is_transient(exc: BaseException) -> bool:
    """True if `exc` is an expected transient/couldn't-determine failure rather
    than a definitive (permanent) answer or an unexpected bug.

    #189: an HTTP error is judged BY STATUS — 408/429/5xx are transient, every
    other status is permanent — so a revoked Discogs token (401), wrong
    username (404), or malformed request (400) is no longer misclassified as a
    transient blip (which logged only as INFO "(transient)" and hid a dead
    credential for months).  Status-less errors (connection reset, timeout,
    MusicBrainz NetworkError, our own TransientMetadataError) keep the family
    classification below.
    """
    if isinstance(exc, PermanentMetadataError):
        return False
    status = http_status(exc)
    if status is not None:
        return status in _TRANSIENT_HTTP_STATUS or 500 <= status <= 599
    return isinstance(exc, (TransientMetadataError, *TRANSIENT_EXTERNAL_ERRORS))
