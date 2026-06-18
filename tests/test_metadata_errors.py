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
