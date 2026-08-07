"""Tests for A-6 — the metadata error taxonomy + expected/unexpected classify."""
import requests

from src.metadata.errors import (
    is_transient, MetadataError, TransientMetadataError, PermanentMetadataError,
)


def test_hierarchy():
    assert issubclass(TransientMetadataError, MetadataError)
    assert issubclass(PermanentMetadataError, MetadataError)


def test_requests_errors_classify_as_transient():
    assert is_transient(requests.exceptions.Timeout("t"))
    assert is_transient(requests.exceptions.ConnectionError("c"))
    assert is_transient(requests.exceptions.HTTPError("h"))


def test_builtin_network_errors_classify_as_transient():
    assert is_transient(ConnectionError("socket"))
    assert is_transient(TimeoutError("slow"))


def test_our_transient_error_classifies_as_transient():
    assert is_transient(TransientMetadataError("x"))


def test_unexpected_and_permanent_are_not_transient():
    assert not is_transient(ValueError("a real bug"))
    assert not is_transient(KeyError("a real bug"))
    assert not is_transient(PermanentMetadataError("definitive"))


def test_discogs_client_http_error_classifies_as_transient():
    """META-6: python3-discogs-client raises its OWN HTTPError for a non-2xx
    status, and it does NOT inherit from requests.exceptions.RequestException,
    so a routine Discogs 429/5xx during a search must still classify as
    transient rather than an unexpected bug."""
    import discogs_client.exceptions

    # Pin the premise the finding rests on: it is NOT a requests exception.
    assert not issubclass(
        discogs_client.exceptions.HTTPError, requests.exceptions.RequestException
    )
    assert is_transient(discogs_client.exceptions.HTTPError("rate limited", 429))
    assert is_transient(discogs_client.exceptions.HTTPError("server error", 502))


def test_musicbrainz_network_error_is_transient_but_response_and_auth_are_not():
    """#175: MusicBrainz is urllib-based, so its NetworkError (unreachable /
    timeout / HTTP error) must classify as transient — a service-down signal that
    aborts the cover-art release loop. Its siblings ResponseError (invalid/parse-
    failed response for one MBID) and AuthenticationError (401) are definitive for
    that request and must NOT be transient, so they skip to the next release."""
    import musicbrainzngs

    assert is_transient(musicbrainzngs.NetworkError("MB unreachable"))
    assert not is_transient(musicbrainzngs.ResponseError("bad response"))
    assert not is_transient(musicbrainzngs.AuthenticationError("401"))
