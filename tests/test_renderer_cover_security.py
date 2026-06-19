"""Unit tests for cover-art download hardening (findings S-1, S-2, S-7).

S-1 — SSRF + unbounded download: cover URLs must be https, host-allow-listed,
      resolve to a public IP, follow only re-validated redirects, carry an
      image/* Content-Type, and abort past a byte cap.
S-2 — downloaded bytes are image-verified (type + pixel bounds) before caching.
S-7 — the host is resolved EXACTLY ONCE and the connection is pinned to that
      vetted IP, so a second attacker-controlled DNS answer can't rebind the
      socket to an internal host between check and fetch.  The whole hop is
      rejected if ANY resolved address is non-public.

No real network or DNS is used: socket resolution and the pinned-stream opener
(_open_cover_stream) are mocked.  The module-level helpers are pure functions,
so we test them directly without constructing a pygame-backed DisplayRenderer.
"""

import io
import os
import types
from urllib.parse import urlsplit

import pytest
from PIL import Image

import src.display.renderer as r
import src.display.palette as palette


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png_bytes(width=64, height=64, color=(180, 90, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    """Minimal stand-in for a streamed urllib3 HTTPResponse."""

    def __init__(self, *, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.released = False

    def stream(self, amt=65536, decode_content=False):
        for i in range(0, len(self._body), amt):
            yield self._body[i:i + amt]

    def release_conn(self):
        self.released = True

    def close(self):
        pass


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
    # Suffix-confusion lookalikes — must NOT match the apex allow-list.
    "evilcoverartarchive.org", "notcoverartarchive.org",
    "xmzstatic.com", "evilarchive.org", "coverartarchive.org.attacker.net",
])
def test_disallowed_hosts(host):
    assert r._host_is_allowed(host) is False


# ---------------------------------------------------------------------------
# _validated_public_ip — resolve ONCE, return the IP to pin (S-7)
# ---------------------------------------------------------------------------

def test_validated_public_ip_returns_global(monkeypatch):
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert r._validated_public_ip("i.discogs.com") == "93.184.216.34"


@pytest.mark.parametrize("ip", [
    "192.168.1.10", "127.0.0.1", "10.0.0.5", "169.254.1.1",
    # Non-private but still non-routable / dangerous space the classifier must
    # reject: multicast (224/4 reports is_global=True!), unspecified, broadcast,
    # and reserved (240/4).
    "224.0.0.1", "0.0.0.0", "255.255.255.255", "240.0.0.1",
])
def test_validated_public_ip_rejects_private(monkeypatch, ip):
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 0))],
    )
    assert r._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_rejects_mixed_set(monkeypatch):
    # A rebinding answer mixing a public and an internal IP must reject the WHOLE
    # hop — picking "the first public one" would let the attacker's private entry
    # be the one requests connects to (S-7).
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    assert r._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_rejects_ipv4_mapped_loopback(monkeypatch):
    # ::ffff:127.0.0.1 must not slip past the public/private check in v6 clothing.
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [(10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))],
    )
    assert r._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_normalizes_ipv4_mapped_public(monkeypatch):
    # A mapped PUBLIC address must be returned in its clean IPv4 form so the
    # pinned connection dials a connectable address, not "::ffff:8.8.8.8".
    monkeypatch.setattr(
        r.socket, "getaddrinfo",
        lambda *a, **k: [(10, 1, 6, "", ("::ffff:93.184.216.34", 0, 0, 0))],
    )
    assert r._validated_public_ip("i.discogs.com") == "93.184.216.34"


def test_validated_public_ip_fails_closed_on_dns_error(monkeypatch):
    def boom(*a, **k):
        raise r.socket.gaierror("no such host")
    monkeypatch.setattr(r.socket, "getaddrinfo", boom)
    assert r._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_fails_closed_on_empty(monkeypatch):
    monkeypatch.setattr(r.socket, "getaddrinfo", lambda *a, **k: [])
    assert r._validated_public_ip("i.discogs.com") is None


# ---------------------------------------------------------------------------
# _host_resolves_to_public_ip — thin yes/no predicate over the above
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
# _validate_cover_url — returns (fetch_url, host, pinned_ip)
# ---------------------------------------------------------------------------

def test_validate_rejects_http_to_disallowed_host(monkeypatch):
    # http is only upgraded for allow-listed hosts; an unknown host is rejected
    # regardless of scheme.
    monkeypatch.setattr(r, "_validated_public_ip", lambda h: "1.2.3.4")
    with pytest.raises(ValueError):
        r._validate_cover_url("http://evil.example/cover.jpg")


def test_validate_rejects_disallowed_host(monkeypatch):
    monkeypatch.setattr(r, "_validated_public_ip", lambda h: "1.2.3.4")
    with pytest.raises(ValueError):
        r._validate_cover_url("https://evil.example/cover.jpg")


def test_validate_rejects_private_ip(monkeypatch):
    # Allow-listed host, but it resolves to a non-public address (rebinding/SSRF).
    monkeypatch.setattr(r, "_validated_public_ip", lambda h: None)
    with pytest.raises(ValueError):
        r._validate_cover_url("https://i.discogs.com/cover.jpg")


def test_validate_accepts_good_url(monkeypatch):
    monkeypatch.setattr(r, "_validated_public_ip", lambda h: "93.184.216.34")
    # Returns (fetch_url, host, pinned_ip); url unchanged for already-https.
    assert r._validate_cover_url("https://i.discogs.com/cover.jpg") == (
        "https://i.discogs.com/cover.jpg", "i.discogs.com", "93.184.216.34"
    )


def test_validate_upgrades_http_to_https_for_allowlisted_host(monkeypatch):
    # Cover Art Archive sometimes returns http; we upgrade rather than reject.
    monkeypatch.setattr(r, "_validated_public_ip", lambda h: "93.184.216.34")
    out = r._validate_cover_url("http://coverartarchive.org/release/x/front")
    assert out == (
        "https://coverartarchive.org/release/x/front",
        "coverartarchive.org",
        "93.184.216.34",
    )


def test_validate_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setattr(r, "_validated_public_ip", lambda h: "1.2.3.4")
    with pytest.raises(ValueError):
        r._validate_cover_url("file:///etc/passwd")


# ---------------------------------------------------------------------------
# _open_cover_stream — the actual urllib3 wiring the S-7 pin turns on
# ---------------------------------------------------------------------------

def test_open_cover_stream_dials_ip_but_tls_for_hostname(monkeypatch):
    """Lock the kwarg contract: the pool must DIAL the pinned IP while keeping
    SNI + cert verification bound to the hostname.  Mocks urllib3 so no socket
    is opened, but asserts the exact construction the pin depends on — without
    this, a urllib3 upgrade that dropped server_hostname would leave every other
    test green while silently breaking the fix.
    """
    captured = {}

    class _FakePool:
        def __init__(self, host, **kwargs):
            captured["host"] = host
            captured["kwargs"] = kwargs

        def urlopen(self, method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["urlopen_kwargs"] = kwargs
            return "SENTINEL_RESPONSE"

    monkeypatch.setattr(r.urllib3, "HTTPSConnectionPool", _FakePool)

    out = r._open_cover_stream(
        "https://i.discogs.com/a/b.png?x=1", "i.discogs.com", "93.184.216.34", 15
    )

    assert out == "SENTINEL_RESPONSE"
    assert captured["host"] == "93.184.216.34"                      # dial the vetted IP
    assert captured["kwargs"]["server_hostname"] == "i.discogs.com"  # SNI -> hostname
    assert captured["kwargs"]["assert_hostname"] == "i.discogs.com"  # cert -> hostname
    assert captured["kwargs"]["cert_reqs"] == "CERT_REQUIRED"
    assert captured["path"] == "/a/b.png?x=1"                       # path + query preserved
    assert captured["method"] == "GET"
    assert captured["urlopen_kwargs"]["redirect"] is False          # we walk redirects ourselves


# ---------------------------------------------------------------------------
# validate_image_file (S-2) — relocated to src.display.palette (A-8)
# ---------------------------------------------------------------------------

def test_image_validation_accepts_png(tmp_path):
    p = tmp_path / "ok.png"
    p.write_bytes(_png_bytes())
    palette.validate_image_file(str(p))  # should not raise


def test_image_validation_rejects_non_image(tmp_path):
    p = tmp_path / "not.png"
    p.write_bytes(b"this is definitely not an image")
    with pytest.raises(ValueError):
        palette.validate_image_file(str(p))


def test_image_validation_rejects_oversized(tmp_path, monkeypatch):
    # Drop the pixel cap below the test image so the bounds check trips.
    monkeypatch.setattr(palette, "MAX_IMAGE_PIXELS", 100)
    p = tmp_path / "big.png"
    p.write_bytes(_png_bytes(64, 64))  # 4096 px > 100
    with pytest.raises(ValueError):
        palette.validate_image_file(str(p))


# ---------------------------------------------------------------------------
# _download_cover_blocking — end-to-end with mocked pinned stream (S-1 + S-2 + S-7)
# ---------------------------------------------------------------------------

def test_download_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "93.184.216.34"))
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())
    monkeypatch.setattr(r, "_open_cover_stream", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    stub._download_cover_blocking("https://i.discogs.com/cover.png", dest)

    assert dest.exists()
    assert dest.stat().st_size > 0
    assert resp.released  # connection handed back
    # No leftover .part tempfiles.
    assert not any(n.startswith(".cover-") for n in os.listdir(tmp_path))


def test_download_pins_connection_to_validated_ip(tmp_path, monkeypatch):
    # The CORE S-7 guarantee: the IP that _validate_cover_url vetted is the exact
    # address the connection is opened against — no second, independent resolve.
    seen = {}
    monkeypatch.setattr(r, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "93.184.216.34"))

    def fake_open(fetch_url, host, pinned_ip, timeout):
        seen["host"] = host
        seen["ip"] = pinned_ip
        return _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())

    monkeypatch.setattr(r, "_open_cover_stream", fake_open)

    stub = _renderer_stub(tmp_path)
    stub._download_cover_blocking("https://i.discogs.com/x.png", tmp_path / "c.png")

    assert seen["ip"] == "93.184.216.34"
    assert seen["host"] == "i.discogs.com"


def test_download_rejects_non_image_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(headers={"Content-Type": "text/html"}, body=b"<html>")
    monkeypatch.setattr(r, "_open_cover_stream", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()


def test_download_rejects_http_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(status=404, headers={"Content-Type": "image/png"}, body=b"")
    monkeypatch.setattr(r, "_open_cover_stream", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()


def test_download_aborts_past_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    monkeypatch.setattr(r, "_MAX_COVER_BYTES", 1024)
    big = b"\x89PNG\r\n" + b"\x00" * 5000  # > 1 KB cap (cap trips first)
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=big)
    monkeypatch.setattr(r, "_open_cover_stream", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()
    assert not any(n.startswith(".cover-") for n in os.listdir(tmp_path))


def test_download_rejects_malicious_image_bytes(tmp_path, monkeypatch):
    # Passes the Content-Type gate but is not a decodable image → S-2 verify trips.
    monkeypatch.setattr(r, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=b"GIF-not-really" * 10)
    monkeypatch.setattr(r, "_open_cover_stream", lambda *a, **k: resp)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    with pytest.raises(ValueError):
        stub._download_cover_blocking("https://i.discogs.com/x", dest)
    assert not dest.exists()


def test_download_follows_and_repins_validated_redirect(tmp_path, monkeypatch):
    seen = []

    # Validate per hop, deriving the host from the URL; every hop is pinned.
    monkeypatch.setattr(
        r, "_validate_cover_url",
        lambda u: (u, urlsplit(u).hostname, "93.184.216.34"),
    )

    def fake_open(fetch_url, host, pinned_ip, timeout):
        seen.append((host, pinned_ip))
        if "coverartarchive.org" in fetch_url:
            return _FakeResp(
                status=307,
                headers={"Location": "https://ia800200.us.archive.org/cover.png"},
            )
        return _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())

    monkeypatch.setattr(r, "_open_cover_stream", fake_open)

    stub = _renderer_stub(tmp_path)
    dest = tmp_path / "cover.png"
    stub._download_cover_blocking("https://coverartarchive.org/release/x/front", dest)

    assert dest.exists()
    hosts = [h for h, _ in seen]
    # Both the original and the redirect target were validated + fetched.
    assert any("coverartarchive.org" in h for h in hosts)
    assert any("archive.org" in h for h in hosts)
    # EVERY hop was pinned to a vetted IP (re-validation per hop, S-7).
    assert all(ip == "93.184.216.34" for _, ip in seen)
