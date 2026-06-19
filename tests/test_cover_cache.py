"""Unit tests for CoverArtCache — disk cache + SSRF-hardened fetch (A-15).

Covers findings S-1, S-2, S-7 (fetch safety) and R-1, R-2 (disk hygiene),
relocated here when the cover plumbing moved out of renderer.py into
src/display/cover_cache.py.

S-1 — SSRF + unbounded download: cover URLs must be https, host-allow-listed,
      resolve to a public IP, follow only re-validated redirects, carry an
      image/* Content-Type, and abort past a byte cap.
S-2 — downloaded bytes are image-verified (type + pixel bounds) before caching.
S-7 — the host is resolved EXACTLY ONCE and the connection is pinned to that
      vetted IP; the whole hop is rejected if ANY resolved address is non-public.
R-1 — stale .cover-*.part tempfiles are swept on construction.
R-2 — the on-disk cache is bounded (mtime-LRU) by file count and total bytes.

No real network or DNS is used: socket resolution and the pinned-stream opener
(_open_cover_stream) are mocked.  The module is pygame-free, so these run with
no display.
"""

import io
import os
import time
from urllib.parse import urlsplit

import pytest
from PIL import Image

import src.display.cover_cache as cc
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


def _make_store(tmp_path, **kwargs):
    return cc.CoverArtCache(tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# _host_is_allowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "i.discogs.com", "img.discogs.com", "api.discogs.com",
    "coverartarchive.org", "ia800200.us.archive.org", "is1-ssl.mzstatic.com",
])
def test_allowed_hosts(host):
    assert cc._host_is_allowed(host) is True


@pytest.mark.parametrize("host", [
    "evil.com", "discogs.com.attacker.net", "192.168.1.1",
    "localhost", "", None, "notdiscogs.com",
    # Suffix-confusion lookalikes — must NOT match the apex allow-list.
    "evilcoverartarchive.org", "notcoverartarchive.org",
    "xmzstatic.com", "evilarchive.org", "coverartarchive.org.attacker.net",
])
def test_disallowed_hosts(host):
    assert cc._host_is_allowed(host) is False


# ---------------------------------------------------------------------------
# _validated_public_ip — resolve ONCE, return the IP to pin (S-7)
# ---------------------------------------------------------------------------

def test_validated_public_ip_returns_global(monkeypatch):
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert cc._validated_public_ip("i.discogs.com") == "93.184.216.34"


@pytest.mark.parametrize("ip", [
    "192.168.1.10", "127.0.0.1", "10.0.0.5", "169.254.1.1",
    # Non-private but still non-routable / dangerous space the classifier must
    # reject: multicast (224/4 reports is_global=True!), unspecified, broadcast,
    # and reserved (240/4).
    "224.0.0.1", "0.0.0.0", "255.255.255.255", "240.0.0.1",
])
def test_validated_public_ip_rejects_private(monkeypatch, ip):
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 0))],
    )
    assert cc._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_rejects_mixed_set(monkeypatch):
    # A rebinding answer mixing a public and an internal IP must reject the WHOLE
    # hop — picking "the first public one" would let the attacker's private entry
    # be the one we connect to (S-7).
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    assert cc._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_rejects_ipv4_mapped_loopback(monkeypatch):
    # ::ffff:127.0.0.1 must not slip past the public/private check in v6 clothing.
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [(10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))],
    )
    assert cc._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_normalizes_ipv4_mapped_public(monkeypatch):
    # A mapped PUBLIC address must be returned in its clean IPv4 form so the
    # pinned connection dials a connectable address, not "::ffff:8.8.8.8".
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [(10, 1, 6, "", ("::ffff:93.184.216.34", 0, 0, 0))],
    )
    assert cc._validated_public_ip("i.discogs.com") == "93.184.216.34"


def test_validated_public_ip_fails_closed_on_dns_error(monkeypatch):
    def boom(*a, **k):
        raise cc.socket.gaierror("no such host")
    monkeypatch.setattr(cc.socket, "getaddrinfo", boom)
    assert cc._validated_public_ip("i.discogs.com") is None


def test_validated_public_ip_fails_closed_on_empty(monkeypatch):
    monkeypatch.setattr(cc.socket, "getaddrinfo", lambda *a, **k: [])
    assert cc._validated_public_ip("i.discogs.com") is None


# ---------------------------------------------------------------------------
# _host_resolves_to_public_ip — thin yes/no predicate over the above
# ---------------------------------------------------------------------------

def test_public_ip_accepts_global(monkeypatch):
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert cc._host_resolves_to_public_ip("i.discogs.com") is True


@pytest.mark.parametrize("ip", ["192.168.1.10", "127.0.0.1", "10.0.0.5", "169.254.1.1"])
def test_public_ip_predicate_rejects_private(monkeypatch, ip):
    monkeypatch.setattr(
        cc.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 0))],
    )
    assert cc._host_resolves_to_public_ip("i.discogs.com") is False


def test_public_ip_fails_closed_on_dns_error(monkeypatch):
    def boom(*a, **k):
        raise cc.socket.gaierror("no such host")
    monkeypatch.setattr(cc.socket, "getaddrinfo", boom)
    assert cc._host_resolves_to_public_ip("i.discogs.com") is False


# ---------------------------------------------------------------------------
# _validate_cover_url — returns (fetch_url, host, pinned_ip)
# ---------------------------------------------------------------------------

def test_validate_rejects_http_to_disallowed_host(monkeypatch):
    monkeypatch.setattr(cc, "_validated_public_ip", lambda h: "1.2.3.4")
    with pytest.raises(ValueError):
        cc._validate_cover_url("http://evil.example/cover.jpg")


def test_validate_rejects_disallowed_host(monkeypatch):
    monkeypatch.setattr(cc, "_validated_public_ip", lambda h: "1.2.3.4")
    with pytest.raises(ValueError):
        cc._validate_cover_url("https://evil.example/cover.jpg")


def test_validate_rejects_private_ip(monkeypatch):
    monkeypatch.setattr(cc, "_validated_public_ip", lambda h: None)
    with pytest.raises(ValueError):
        cc._validate_cover_url("https://i.discogs.com/cover.jpg")


def test_validate_accepts_good_url(monkeypatch):
    monkeypatch.setattr(cc, "_validated_public_ip", lambda h: "93.184.216.34")
    assert cc._validate_cover_url("https://i.discogs.com/cover.jpg") == (
        "https://i.discogs.com/cover.jpg", "i.discogs.com", "93.184.216.34"
    )


def test_validate_upgrades_http_to_https_for_allowlisted_host(monkeypatch):
    monkeypatch.setattr(cc, "_validated_public_ip", lambda h: "93.184.216.34")
    out = cc._validate_cover_url("http://coverartarchive.org/release/x/front")
    assert out == (
        "https://coverartarchive.org/release/x/front",
        "coverartarchive.org",
        "93.184.216.34",
    )


def test_validate_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setattr(cc, "_validated_public_ip", lambda h: "1.2.3.4")
    with pytest.raises(ValueError):
        cc._validate_cover_url("file:///etc/passwd")


# ---------------------------------------------------------------------------
# _open_cover_stream — the actual urllib3 wiring the S-7 pin turns on
# ---------------------------------------------------------------------------

def test_open_cover_stream_dials_ip_but_tls_for_hostname(monkeypatch):
    """Lock the kwarg contract: the pool must DIAL the pinned IP while keeping
    SNI + cert verification bound to the hostname.  Mocks urllib3 so no socket
    is opened, but asserts the exact construction the pin depends on.
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

    monkeypatch.setattr(cc.urllib3, "HTTPSConnectionPool", _FakePool)

    out = cc._open_cover_stream(
        "https://i.discogs.com/a/b.png?x=1", "i.discogs.com", "93.184.216.34", 15
    )

    assert out == "SENTINEL_RESPONSE"
    assert captured["host"] == "93.184.216.34"                       # dial the vetted IP
    assert captured["kwargs"]["server_hostname"] == "i.discogs.com"  # SNI -> hostname
    assert captured["kwargs"]["assert_hostname"] == "i.discogs.com"  # cert -> hostname
    assert captured["kwargs"]["cert_reqs"] == "CERT_REQUIRED"
    assert captured["path"] == "/a/b.png?x=1"                        # path + query preserved
    assert captured["method"] == "GET"
    assert captured["urlopen_kwargs"]["redirect"] is False           # we walk redirects ourselves


# ---------------------------------------------------------------------------
# validate_image_file (S-2) — lives in src.display.palette (A-8)
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
    monkeypatch.setattr(palette, "MAX_IMAGE_PIXELS", 100)
    p = tmp_path / "big.png"
    p.write_bytes(_png_bytes(64, 64))  # 4096 px > 100
    with pytest.raises(ValueError):
        palette.validate_image_file(str(p))


# ---------------------------------------------------------------------------
# CoverArtCache.path_for / exists
# ---------------------------------------------------------------------------

def test_path_for_is_deterministic_and_under_cache_dir(tmp_path):
    store = _make_store(tmp_path)
    url = "https://i.discogs.com/cover.png"
    p1 = store.path_for(url)
    p2 = store.path_for(url)
    assert p1 == p2
    assert p1.parent == tmp_path
    assert p1.suffix == ".jpg"
    assert store.exists(url) is False
    p1.write_bytes(_png_bytes())
    assert store.exists(url) is True


# ---------------------------------------------------------------------------
# CoverArtCache.download — end-to-end with mocked pinned stream (S-1 + S-2 + S-7)
# ---------------------------------------------------------------------------

def test_download_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "93.184.216.34"))
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/cover.png"
    out = store.download(url)

    assert out == store.path_for(url)
    assert out.exists() and out.stat().st_size > 0
    assert resp.released
    assert not any(n.startswith(".cover-") for n in os.listdir(tmp_path))


def test_download_pins_connection_to_validated_ip(tmp_path, monkeypatch):
    # The CORE S-7 guarantee: the IP that _validate_cover_url vetted is the exact
    # address the connection is opened against — no second, independent resolve.
    seen = {}
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "93.184.216.34"))

    def fake_open(fetch_url, host, pinned_ip, timeout):
        seen["host"] = host
        seen["ip"] = pinned_ip
        return _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())

    monkeypatch.setattr(cc, "_open_cover_stream", fake_open)

    store = _make_store(tmp_path)
    store.download("https://i.discogs.com/x.png")

    assert seen["ip"] == "93.184.216.34"
    assert seen["host"] == "i.discogs.com"


def test_download_rejects_non_image_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(headers={"Content-Type": "text/html"}, body=b"<html>")
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/x"
    with pytest.raises(ValueError):
        store.download(url)
    assert not store.exists(url)


def test_download_rejects_http_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(status=404, headers={"Content-Type": "image/png"}, body=b"")
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/x"
    with pytest.raises(ValueError):
        store.download(url)
    assert not store.exists(url)


def test_download_aborts_past_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    monkeypatch.setattr(cc, "_MAX_COVER_BYTES", 1024)
    big = b"\x89PNG\r\n" + b"\x00" * 5000  # > 1 KB cap (cap trips first)
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=big)
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/x"
    with pytest.raises(ValueError):
        store.download(url)
    assert not store.exists(url)
    assert not any(n.startswith(".cover-") for n in os.listdir(tmp_path))


def test_download_rejects_malicious_image_bytes(tmp_path, monkeypatch):
    # Passes the Content-Type gate but is not a decodable image → S-2 verify trips.
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=b"GIF-not-really" * 10)
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/x"
    with pytest.raises(ValueError):
        store.download(url)
    assert not store.exists(url)


def test_download_follows_and_repins_validated_redirect(tmp_path, monkeypatch):
    seen = []

    monkeypatch.setattr(
        cc, "_validate_cover_url",
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

    monkeypatch.setattr(cc, "_open_cover_stream", fake_open)

    store = _make_store(tmp_path)
    url = "https://coverartarchive.org/release/x/front"
    store.download(url)

    assert store.exists(url)
    hosts = [h for h, _ in seen]
    assert any("coverartarchive.org" in h for h in hosts)
    assert any("archive.org" in h for h in hosts)
    assert all(ip == "93.184.216.34" for _, ip in seen)


# ---------------------------------------------------------------------------
# R-1 — .part sweep on construction
# ---------------------------------------------------------------------------

def test_init_sweeps_stale_part_files(tmp_path):
    stale = tmp_path / ".cover-abc123.part"
    stale.write_bytes(b"partial")
    keep = tmp_path / "deadbeef.jpg"
    keep.write_bytes(_png_bytes())

    _make_store(tmp_path)

    assert not stale.exists()   # swept (R-1)
    assert keep.exists()        # real covers untouched


# ---------------------------------------------------------------------------
# R-2 — bounded on-disk cache (mtime-LRU prune)
# ---------------------------------------------------------------------------

def _write_cover(tmp_path, name, size_bytes, mtime):
    p = tmp_path / f"{name}.jpg"
    p.write_bytes(b"\x00" * size_bytes)
    os.utime(p, (mtime, mtime))
    return p


def test_prune_evicts_oldest_beyond_file_cap(tmp_path):
    now = time.time()
    old = _write_cover(tmp_path, "old", 10, now - 300)
    mid = _write_cover(tmp_path, "mid", 10, now - 200)
    new = _write_cover(tmp_path, "new", 10, now - 100)

    # Construction prunes; cap of 2 should drop the single oldest.
    _make_store(tmp_path, max_files=2, max_bytes=10**9)

    assert not old.exists()
    assert mid.exists() and new.exists()


def test_prune_evicts_oldest_beyond_byte_cap(tmp_path):
    now = time.time()
    old = _write_cover(tmp_path, "old", 600, now - 300)
    new = _write_cover(tmp_path, "new", 600, now - 100)

    # 1 KB byte cap, each file 600 B → must evict the oldest to get under.
    _make_store(tmp_path, max_files=100, max_bytes=1024)

    assert not old.exists()
    assert new.exists()


def test_prune_leaves_non_cover_files_alone(tmp_path):
    now = time.time()
    _write_cover(tmp_path, "old", 10, now - 300)
    _write_cover(tmp_path, "new", 10, now - 100)
    other = tmp_path / "notes.txt"
    other.write_text("not a cover")

    _make_store(tmp_path, max_files=1, max_bytes=10**9)

    # The non-.jpg file is never a prune candidate.
    assert other.exists()
    # Exactly one cover survived the cap.
    assert sum(1 for p in tmp_path.glob("*.jpg")) == 1


def test_download_prunes_after_add(tmp_path, monkeypatch):
    # A fresh download that pushes the cache over the file cap triggers a prune.
    now = time.time()
    old = _write_cover(tmp_path, "old", 10, now - 300)

    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path, max_files=1, max_bytes=10**9)
    # 'old' is the only file and within cap=1 at construction; adding one more
    # should evict it (it's older than the just-written cover).
    store.download("https://i.discogs.com/fresh.png")

    assert not old.exists()
    assert sum(1 for p in tmp_path.glob("*.jpg")) == 1


def test_prune_protects_named_file_on_mtime_tie(tmp_path):
    # Two covers sharing an mtime; the protected one survives even though it
    # sorts FIRST by name (so it would otherwise be the eviction victim).
    store = _make_store(tmp_path, max_files=100)
    now = time.time()
    a = tmp_path / "aaa.jpg"; a.write_bytes(b"x" * 10); os.utime(a, (now, now))
    b = tmp_path / "bbb.jpg"; b.write_bytes(b"x" * 10); os.utime(b, (now, now))

    store.max_files = 1
    store._prune(protect=a)

    assert a.exists()       # protected → kept despite the tie
    assert not b.exists()   # the other was evicted instead


def test_prune_unprotected_tie_breaks_by_name(tmp_path):
    # Control for the test above: with no protection, an mtime tie is broken
    # deterministically by name (so the result is stable, not iterdir-random).
    store = _make_store(tmp_path, max_files=100)
    now = time.time()
    a = tmp_path / "aaa.jpg"; a.write_bytes(b"x" * 10); os.utime(a, (now, now))
    b = tmp_path / "bbb.jpg"; b.write_bytes(b"x" * 10); os.utime(b, (now, now))

    store.max_files = 1
    store._prune()

    assert not a.exists()    # 'aaa' sorts first → evicted first
    assert b.exists()


def test_prune_keeps_files_at_exact_byte_cap(tmp_path):
    # total == max_bytes must NOT evict (the bound is '>' not '>=').
    store = _make_store(tmp_path, max_files=100)
    now = time.time()
    a = _write_cover(tmp_path, "a", 512, now - 2)
    b = _write_cover(tmp_path, "b", 512, now - 1)

    store.max_bytes = 1024
    store._prune()

    assert a.exists() and b.exists()


def test_download_protects_fresh_cover_even_on_mtime_tie(tmp_path, monkeypatch):
    # The real-path guard: a pre-existing cover sharing the fresh download's mtime
    # tick must not steal its survival — the just-written file is protected.
    now = time.time()
    old = _write_cover(tmp_path, "old", 10, now)  # same coarse tick as the download

    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(headers={"Content-Type": "image/png"}, body=_png_bytes())
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path, max_files=1, max_bytes=10**9)
    out = store.download("https://i.discogs.com/fresh.png")

    assert out.exists()       # the fresh cover is protected from its own prune
    assert not old.exists()   # the older (tied) cover was the eviction victim
