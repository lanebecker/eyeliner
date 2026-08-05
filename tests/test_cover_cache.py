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
import warnings
from pathlib import Path
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
    # Pin the WHOLE streaming-kwarg contract, not just redirect (MUT-8): each of
    # these was independently mutable with the suite green.
    assert captured["urlopen_kwargs"]["redirect"] is False           # we walk redirects ourselves
    assert captured["urlopen_kwargs"]["retries"] is False            # no implicit urllib3 retries
    # preload_content=False is the load-bearing one: with True, urllib3 reads the
    # ENTIRE body into RAM before download() can apply its _MAX_COVER_BYTES chunk
    # cap — a few-hundred-MB cover URL would then exhaust memory on a 2 GB Pi even
    # though the on-disk file stays bounded. Stream, never preload.
    assert captured["urlopen_kwargs"]["preload_content"] is False
    assert captured["urlopen_kwargs"]["decode_content"] is False     # raw bytes, no re-inflation


def test_real_https_pool_forwards_the_s7_pinning_kwargs():
    # The test above MOCKS urllib3, so it proves only that our code PASSES the
    # pinning kwargs — it can never notice a urllib3 upgrade that stops honouring
    # them.  Build a GENUINE HTTPSConnectionPool + connection (no socket is opened
    # by _new_conn) and assert both kwargs actually reach the connection: a major
    # bump that removed/renamed them would raise here (assert_hostname is an
    # explicit pool param; server_hostname is forwarded via conn_kw to a
    # connection that rejects unknown kwargs), and one that silently stopped
    # forwarding them would trip the attribute asserts below.  Either way it
    # fails HERE in CI, not on the Pi.  requirements.txt caps urllib3 <3 for the
    # same reason. (TQ-4 / #116)
    pool = cc.urllib3.HTTPSConnectionPool(
        "93.184.216.34",
        port=443,
        server_hostname="i.discogs.com",
        assert_hostname="i.discogs.com",
        cert_reqs="CERT_REQUIRED",
        ca_certs=cc.certifi.where(),
        timeout=cc.urllib3.Timeout(connect=1, read=1),
    )
    conn = pool._new_conn()  # constructs the HTTPSConnection; opens NO socket
    try:
        assert conn.server_hostname == "i.discogs.com", \
            "urllib3 no longer forwards server_hostname — S-7 SNI pin is broken"
        assert conn.assert_hostname == "i.discogs.com", \
            "urllib3 no longer honours assert_hostname — S-7 cert-hostname pin is broken"
    finally:
        conn.close()


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
    # 4096 px against a cap of 100 is 40x — well past Pillow's own 2x
    # DecompressionBomb *error* threshold, so Pillow raises inside Image.open()
    # and this is caught by the generic `except` branch (message: "not a
    # decodable image: …"). This exercises the >2x backstop path, NOT the
    # explicit dimension guard — see the 1x-2x band test below (MUT-2).
    monkeypatch.setattr(palette, "MAX_IMAGE_PIXELS", 100)
    p = tmp_path / "big.png"
    p.write_bytes(_png_bytes(64, 64))  # 4096 px > 100
    with pytest.raises(ValueError):
        palette.validate_image_file(str(p))


def test_image_validation_accepts_jpeg(tmp_path):
    # A valid, small JPEG (an allowed non-PNG format) passes cleanly. Guards the
    # format allow-list against a mutant that drops JPEG from the accepted set,
    # and exercises the accept path for a second real format (MUT-2).
    p = tmp_path / "ok.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), (120, 60, 30)).save(buf, format="JPEG")
    p.write_bytes(buf.getvalue())
    palette.validate_image_file(str(p))  # should not raise


def test_image_validation_rejects_disallowed_format(tmp_path):
    # A valid, fully decodable image in a format outside the allow-list (TIFF).
    # verify() succeeds, so control reaches the format check — the guard line
    # that had zero test executions before MUT-2 (palette.py format allow-list).
    # A plain `pytest.raises(ValueError)` would also pass via other branches, so
    # match the specific message to pin THIS branch.
    p = tmp_path / "cover.tiff"
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, format="TIFF")
    p.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="unexpected image format"):
        palette.validate_image_file(str(p))


def test_image_validation_rejects_dimension_bomb_below_pillow_backstop(tmp_path, monkeypatch):
    # The 1x-2x "bomb" band MUT-2 flags: an image whose pixel count exceeds our
    # MAX_IMAGE_PIXELS but stays under Pillow's own 2x DecompressionBomb *error*
    # threshold. Pillow only *warns* (verify() passes), so the explicit
    # `width * height > MAX_IMAGE_PIXELS` guard is the SOLE line of defense.
    # 64*64 = 4096 px; cap = 3000 -> 1.37x: over our cap, under Pillow's 2x=6000.
    # Match the specific message to pin the explicit guard (not the generic
    # except branch, whose message is "not a decodable image: …").
    monkeypatch.setattr(palette, "MAX_IMAGE_PIXELS", 3000)
    p = tmp_path / "band.png"
    p.write_bytes(_png_bytes(64, 64))
    with warnings.catch_warnings():
        # Keep the (expected) DecompressionBombWarning out of the report, and
        # stay robust if the suite ever flips warnings-into-errors: without this
        # the warning would divert into the except branch and mask the guard.
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(ValueError, match="dimensions out of bounds"):
            palette.validate_image_file(str(p))


def test_image_validation_rejects_truncated_jpeg(tmp_path):
    # A JPEG cut off mid-scan (a Wi-Fi drop mid-download) still opens and reports
    # its header dimensions fine, and PIL's verify() is a per-format no-op for
    # JPEG — so before DISP-3 this sailed through validation and the half-decoded
    # cover was os.replace'd into the cache and displayed forever, with
    # extract_palette deriving the whole 5-colour scheme from the garbage half.
    # The validator now forces a real decode (load()), which raises on the short
    # read (LOAD_TRUNCATED_IMAGES stays False). (DISP-3 / #110)
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (120, 60, 30)).save(buf, format="JPEG")
    full = buf.getvalue()
    p = tmp_path / "half.jpg"
    p.write_bytes(full[: len(full) // 2])  # ~50%: header intact, scan data cut
    with pytest.raises(ValueError, match="not a decodable image"):
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
    with pytest.raises(ValueError, match="unexpected Content-Type"):  # pin THIS guard (MUT-10)
        store.download(url)
    assert not store.exists(url)


def test_download_rejects_http_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(status=404, headers={"Content-Type": "image/png"}, body=b"")
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/x"
    with pytest.raises(ValueError, match="HTTP 404"):  # pin THIS guard (MUT-10)
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
    with pytest.raises(ValueError, match="byte cap"):  # pin THIS guard (MUT-10)
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
    with pytest.raises(ValueError, match="not a decodable image"):  # pin THIS guard (MUT-10)
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


def test_download_rejects_http_status_400_boundary(tmp_path, monkeypatch):
    # The status guard is `>= 400`; a `>` mutation would let HTTP 400 through.
    # Serve a 400 with a VALID png body + image Content-Type so that, if the guard
    # were bypassed, the download would otherwise SUCCEED — proving the guard is
    # what stopped it (the >= boundary, MUT-10).
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, "i.discogs.com", "1.2.3.4"))
    resp = _FakeResp(status=400, headers={"Content-Type": "image/png"}, body=_png_bytes())
    monkeypatch.setattr(cc, "_open_cover_stream", lambda *a, **k: resp)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/x"
    with pytest.raises(ValueError, match="HTTP 400"):
        store.download(url)
    assert not store.exists(url)


def test_download_rejects_too_many_redirects(tmp_path, monkeypatch):
    # Drive a redirect chain that never terminates: every hop returns a 307 to
    # another allow-listed URL.  The loop caps at _MAX_COVER_REDIRECTS + 1 hops,
    # then raises.  Pins the `too many redirects` guard (MUT-10) AND that the cap
    # is actually _MAX_COVER_REDIRECTS, exercised to MAX+1 hops (MUT-9).
    monkeypatch.setattr(cc, "_validate_cover_url",
                        lambda u: (u, urlsplit(u).hostname or "i.discogs.com", "1.2.3.4"))
    calls = {"n": 0}

    def always_redirect(fetch_url, host, pinned_ip, timeout):
        calls["n"] += 1
        return _FakeResp(status=307,
                         headers={"Location": f"https://i.discogs.com/hop{calls['n']}"})

    monkeypatch.setattr(cc, "_open_cover_stream", always_redirect)

    store = _make_store(tmp_path)
    url = "https://i.discogs.com/start"
    with pytest.raises(ValueError, match="too many redirects"):
        store.download(url)
    # Exactly MAX+1 hops were attempted before giving up (pins the cap value).
    assert calls["n"] == cc._MAX_COVER_REDIRECTS + 1
    assert not store.exists(url)


def test_cover_fetch_constants_are_the_shipped_values(monkeypatch):
    # The fetch/redirect/timeout caps are asserted nowhere else, so a units slip
    # or a refactor that loosened them shipped green (MUT-9).  Pin the shipped
    # values directly.
    assert cc._MAX_COVER_BYTES == 10 * 1024 * 1024   # 10 MB
    assert cc._MAX_COVER_REDIRECTS == 5
    assert cc._COVER_CONNECT_READ_TIMEOUT == 15


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


# ---------------------------------------------------------------------------
# R-2 default disk-cache bounds (MUT-6) — the values the real appliance uses
# ---------------------------------------------------------------------------

def test_cover_cache_default_bounds(tmp_path):
    # The appliance builds CoverArtCache(cache_dir) with NO explicit bounds, so
    # the live max_files / max_bytes are the module defaults — yet every other
    # test passes explicit bounds, leaving the default values (and the
    # 256*1024*1024 arithmetic on cover_cache.py) asserted nowhere. A units slip
    # (MB read as bytes) or a `*` -> `/` typo would ship green and, on the Pi,
    # prune the whole cover cache to zero every boot. (MUT-6 / #111)
    store = cc.CoverArtCache(tmp_path)
    assert store.max_files == 500
    assert store.max_bytes == 256 * 1024 * 1024  # 256 MB


def test_cover_cache_default_max_files_prunes_to_limit(tmp_path):
    # Exercise the DEFAULT file-count bound inside _prune (not an explicit
    # override): seed one cover over the limit and confirm the prune returns to
    # exactly the default. Counts are hard-coded (not read from store.max_files)
    # so a mutation of the 500 default is caught behaviourally here too. (MUT-6)
    store = cc.CoverArtCache(tmp_path)  # default max_files = 500
    for i in range(501):                # one *.jpg cover over the default limit
        (tmp_path / f"cover{i:04d}.jpg").write_bytes(b"x")
    store._prune()
    remaining = sum(
        1 for p in tmp_path.iterdir()
        if p.is_file() and p.suffix == ".jpg" and not p.name.startswith(".cover-")
    )
    assert remaining == 500


# ---------------------------------------------------------------------------
# _prune eviction loop runs MANY times (MUT-7) — the whole suite otherwise
# never evicts more than one file, so 'i += 1' -> 'i += 0' (infinite loop in
# __init__) and 'file_count -= 1' -> '-= 2' (silent under-evict) both survive.
# ---------------------------------------------------------------------------

def _survivor_names(tmp_path):
    return sorted(
        p.name for p in tmp_path.iterdir()
        if p.is_file() and p.suffix == ".jpg" and not p.name.startswith(".cover-")
    )


def _seed_covers(tmp_path, count, size=1):
    # count *.jpg covers with strictly increasing mtimes so eviction order is
    # deterministic (oldest = c00, newest = c{count-1}).
    base = time.time() - 10_000
    for i in range(count):
        p = tmp_path / f"c{i:02d}.jpg"
        p.write_bytes(b"x" * size)
        os.utime(p, (base + i, base + i))


def test_prune_evicts_many_over_file_bound_keeping_newest(tmp_path):
    # Seed 10 covers, prune to max_files=3: the loop must evict SEVEN files and
    # exactly the 3 newest survive, by name. Kills 'i += 0' (which re-picks the
    # same victim forever -> hang) and 'file_count -= 2' (which stops early and
    # leaves 6 files). (MUT-7 / #112)
    store = _make_store(tmp_path, max_files=3, max_bytes=10**9)
    _seed_covers(tmp_path, 10)
    store._prune()
    assert _survivor_names(tmp_path) == ["c07.jpg", "c08.jpg", "c09.jpg"]


def test_prune_evicts_many_over_byte_bound_keeping_newest(tmp_path):
    # Same, but the BYTE bound drives eviction: 10 covers × 100 B = 1000 B,
    # max_bytes=300 keeps the 3 newest (300 B; the bound is '>' so 300 is OK).
    # File bound is effectively disabled. (MUT-7 / #112)
    store = _make_store(tmp_path, max_files=10**6, max_bytes=300)
    _seed_covers(tmp_path, 10, size=100)
    store._prune()
    assert _survivor_names(tmp_path) == ["c07.jpg", "c08.jpg", "c09.jpg"]


def test_prune_terminates_and_skips_when_a_victim_unlink_raises(tmp_path, monkeypatch):
    # A victim that can't be unlinked (OSError) must be SKIPPED and the loop must
    # keep going — never spin on it. Seed 5 over a file bound of 1 and make the
    # oldest victim's unlink raise: prune must terminate, leave the un-evictable
    # file, and evict the other four. Directly exercises the 'except OSError:
    # continue' path together with the 'i += 1' advance. (MUT-7 / #112)
    store = _make_store(tmp_path, max_files=1, max_bytes=10**9)
    _seed_covers(tmp_path, 5)

    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "c00.jpg":
            raise OSError("cannot unlink")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    store._prune()  # must RETURN, not hang

    # c00 raised so it survives; c01..c04 were evicted down to the bound.
    assert _survivor_names(tmp_path) == ["c00.jpg"]
