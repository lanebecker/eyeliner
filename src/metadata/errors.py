"""Metadata error taxonomy (A-6).

The resolver chain previously mixed three error idioms with no shared vocabulary
and no "expected miss vs. unexpected bug" distinction.  This module gives the
boundary one taxonomy:

  - **Transient** — "couldn't determine right now" (network blip, timeout, 429,
    5xx).  Expected; the album must stay retryable and uncached (see B-4/B-13).
  - **Permanent** — a definitive negative answer that won't change on retry.
  - **Unexpected** — anything else is a real bug, logged loudly.

External transient failures currently surface as `requests` exceptions (the
Discogs client and our REST calls are requests-based); `TRANSIENT_EXTERNAL_ERRORS`
lets the resolver classify those uniformly with our own raised errors.  The
typed exceptions below are the vocabulary for code that wants to *signal* these
conditions explicitly as adoption spreads.
"""
import requests


class MetadataError(Exception):
    """Base for errors in the metadata-resolution chain."""


class TransientMetadataError(MetadataError):
    """A temporary failure — retry later; do not cache a downgraded result."""


class PermanentMetadataError(MetadataError):
    """A definitive failure that will not change on retry."""


# External exception types that mean "transient / couldn't determine."
# requests.exceptions.RequestException is the base for Timeout, ConnectionError,
# HTTPError, etc. (the discogs client is requests-based); the builtin
# ConnectionError / TimeoutError cover socket-level network failures that aren't
# wrapped by requests.
TRANSIENT_EXTERNAL_ERRORS = (
    requests.exceptions.RequestException,
    ConnectionError,   # builtin (OSError subclass)
    TimeoutError,      # builtin
)


def is_transient(exc: BaseException) -> bool:
    """True if `exc` is an expected transient/couldn't-determine failure rather
    than an unexpected bug."""
    return isinstance(exc, (TransientMetadataError, *TRANSIENT_EXTERNAL_ERRORS))
