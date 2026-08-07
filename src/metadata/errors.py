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


def is_transient(exc: BaseException) -> bool:
    """True if `exc` is an expected transient/couldn't-determine failure rather
    than an unexpected bug."""
    return isinstance(exc, (TransientMetadataError, *TRANSIENT_EXTERNAL_ERRORS))
