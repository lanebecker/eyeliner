"""Unit tests for cover-art download hardening (findings S-1, S-2).

S-1 — SSRF + unbounded download: cover URLs must be https, host-allow-listed,
      resolve to a public IP, follow only re-validated redirects, carry an
      image/* Content-Type, and abort past a byte cap.
S-2 — downloaded bytes are image-verified (type + pixel bounds) before caching.

No real network or DNS is used: requests.get and socket resolution are mocked.
The module-level helpers are pure functions, so we test them directly without
constructing a pygame-backed DisplayRenderer.
"""

import io
import os
import types

import pytest
from PIL import Image

import src.display.renderer as r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png_bytes(width=64, height=64, color=(180, 90, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, *, status_code=200, headers=None, body=b"", is_redirect=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.is_redirect = is_redirect
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise r.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


def _renderer_stub(tmp_path):
    """A bare object with just the attributes _download_cover_blocking touches."""
    stub = types.SimpleNamespace()
    stub.cache_dir = tmp_path
    # Bind the real method to our stub.
    stub._download_cover_blocking = types.MethodType(
        r.DisplayRenderer._download_cover_blocking, stub
    )
    return stub


# ---------------------------------------------------------------------------
# _host_is_allowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "i.discogs.com", "img.discogs.com", "api.discogs.com",
    "coverartarchive.org", "ia800200.us.archive.org", "is1-ssl.mzstatic.com",
])
def test_allowed_hosts(host):
    assert r._host_is_allowed(host) is True


@pytest.mark.parametrize("host", [
    "evil.com", "discogs.com.attacker.net", "192.168.1.1",
    "localhost", "", None, "notdiscogs.com",
])
def test_disallowed_hosts(host):
    assert r._host_is_allowed(host) is False


# ---------------------------------------------------------------------------
# _validate_cover_url
# ---------------------------------------------------------------------------

def test_validate_rejects_http(monkeypatch):
    monkeypatch.setattr(r, "_host_resolves_to_public_ip", lambda h: True)
    with pytest.raises(ValueError):
        r._validate_cover_url("http://i.discogs.com/cover.jpg")


def test_validate_rejects_disallowed_host(monkeypatch):
    monkeypatch.setattr(r, "_host_resolves_to_public_ip", lambda h: True)
    with pytest.raises(ValueError):
        r._validate_cover_url("https://evil.example/cover.jpg")


def test_validate_rejects_private_ip(monkeypatch):
    # Allow-listed host, but it resolves to a private address (rebinding/SSRF).
    monkeypatch.setattr(r, "_host_resolves_to_public_ip", lambda h: False)
    with pytest.raises(ValueError):
        r._validate_cover_url("https://i.discogs.com/cover.jpg")


def test_validate_accepts_good_url(monkeypatch):
    monkeypatch.setattr(r, "_host_resolves_to_public_ip", lambda h: True)
    assert r._validate_cover_url("https://i.discogs.com/cover.jpg") == "i.discogs.com"


# ---------------------------------------------------------------------------
# _host_resolves_to_public_ip
# ---------------------------------------------------------------------------

def test_public_ip_accepts_global(monkeypatch):
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert r._host_resolves_to_public_ip("i.discogs.com") is True


@pytest.mark.parametrize("ip", ["192.168.1.10", "127.0.0.1", "10.0.0.5", "169.254.1.1"])
def test_public_ip_rejects_private(monkeypatch, ip):
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 0))],
    )
    assert r._host_resolves_to_public_ip("i.discogs.com") is False


def test_public_ip_fails_closed_on_dns_error(monkeypatch):
    def boom(*a, **k):
        raise r.socket.gaierror("no such host")
    monkeypatch.setattr(r.socket, "getaddrinfo", boom)
    assert r._host_resolves_to_public_ip("i.discogs.com") is False


# ---------------------------------------------------------------------------
# _validate_image_file (S-2)
# ---------------------------------------------------------------------------

def test_image_validation_accepts_png(tmp_path):
    p = tmp_path / "ok.png"
    p.write_bytes(_png_bytes())
    r._validate_image_file(str(p))  # should not raise


def test_image_validation_rejects_non_image(tmp_path):
    p = tmp_path / "not.png"
    p.write_bytes(b"this is definitely not an image")
    with pytest.raises(ValueError):
        r._validate_image_file(str(p))


def test_image_validation_rejects_oversized(tmp_path, monkeypatch):
    # Drop the pixel cap below the test image so the bounds check trips.
    monkeypatch.setattr(r, "_MAX_IMAGE_PIXELS", 100)
    p = tmp_path / "big.png"
    p.write_bytes(_png_bytes(64, 64))  # 4096 px > 100
    with pytest.raises(ValueError):
        r._validate_image_file(str(p))


# ---------------------------------------------------------------------------
# _download_cover_blocking — end-to-end with mocked HTTP (S-1 + S-2)
# ---------------------------------------------------------------------------

def test_download_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url", lambda u: "i.discogs.com")
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())
    monkeypatch.setattr(r.requests, "get", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    stub._download_cover_blocking("https://i.discogs.com/cover.png", dest)

    assert dest.exists()
    assert dest.stat().st_size > 0
    # No leftover .part tempfiles.
    assert not any(n.startswith(".cover-") for n in os.listdir(tmp_path))


def test_download_rejects_non_image_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url", lambda u: "i.discogs.com")
    resp = _FakeResp(headers={"Content-Type": "text/html"}, body=b"<html>")
    monkeypatch.setattr(r.requests, "get", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()


def test_download_aborts_past_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url", lambda u: "i.discogs.com")
    monkeypatch.setattr(r, "_MAX_COVER_BYTES", 1024)
    big = b"\x89PNG\r\n" + b"\x00" * 5000  # > 1 KB cap (header doesn't matter; cap trips first)
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=big)
    monkeypatch.setattr(r.requests, "get", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()
    assert not any(n.startswith(".cover-") for n in os.listdir(tmp_path))


def test_download_rejects_malicious_image_bytes(tmp_path, monkeypatch):
    # Passes the Content-Type gate but is not a decodable image → S-2 verify trips.
    monkeypatch.setattr(r, "_validate_cover_url", lambda u: "i.discogs.com")
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=b"GIF-not-really" * 10)
    monkeypatch.setattr(r.requests, "get", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()


def test_download_follows_validated_redirect(tmp_path, monkeypatch):
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        if "coverartarchive.org" in url:
            return _FakeResp(
                status_code=307,
                headers={"Location": "https://ia800200.us.archive.org/cover.png"},
                is_redirect=True,
            )
        return _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())

    monkeypatch.setattr(r, "_validate_cover_url", lambda u: r.urlsplit(u).hostname)
    monkeypatch.setattr(r.requests, "get", fake_get)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    stub._download_cover_blocking("https://coverartarchive.org/release/x/front", dest)

    assert dest.exists()
    # Both the original and the redirect target were validated + fetched.
    assert any("coverartarchive.org" in u for u in seen)
    assert any("archive.org" in u for u in seen)
