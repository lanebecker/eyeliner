"""Cover-art disk cache + SSRF-hardened fetch (A-15).

Extracted from ``renderer.py`` so the security-sensitive network boundary lives
in one small, pygame-free, independently testable place — the same split A-4 did
to the Discogs God-client.  The renderer consumes this; it does not reach into
network/socket/TLS internals any more.

Cover-art download safety (findings S-1 / S-2 / S-7)
---------------------------------------------------
``cover_art_url`` originates from untrusted external APIs (Discogs image ``uri``,
the MusicBrainz Cover Art Archive).  A poisoned entry — or a MITM, since we do
not pin certificates — could otherwise point the fetch at an internal LAN host
(SSRF), a multi-gigabyte response that fills the SD card, or a malicious image
that exploits a stale decoder.  :meth:`CoverArtCache.download` therefore:

  * requires https (http is upgraded for allow-listed hosts),
  * restricts the host to an allow-list of known cover-art providers,
  * resolves each hop's host EXACTLY ONCE, rejects the hop unless every resolved
    address is public, and then pins the connection to that one vetted IP — so
    the address we validate is the address we connect to.  This closes the
    validate-then-resolve-again DNS-rebinding TOCTOU (S-7): a second,
    attacker-controlled DNS answer can no longer steer the socket to an internal
    host between the check and the fetch.  TLS still verifies the certificate
    against the original hostname (SNI + assert_hostname), so pinning to an IP
    does not weaken authentication.
  * follows redirects manually so every hop is re-validated and re-pinned,
  * aborts after ``_MAX_COVER_BYTES`` on the wire, and rejects up front if the
    server DECLARES a Content-Length over that cap,
  * enforces a total wall-clock ``_DOWNLOAD_DEADLINE_SECONDS`` budget across all
    redirect hops and the body stream, so a slow-drip response is cut off
    instead of parking an executor worker indefinitely (SEC-4),
  * requires an image/* Content-Type, and
  * verifies the decoded image (type + pixel bounds) before it is cached.

Disk hygiene (findings R-1 / R-2)
---------------------------------
  * R-1: stale ``.cover-*.part`` tempfiles (left by a SIGKILL between write and
    atomic rename) are swept on construction.
  * R-2: the on-disk cache is bounded (mtime-LRU) by file count and total bytes,
    pruned on construction and after every successful download — so a large
    collection can't grow the cache without limit, matching the bounded-cache
    discipline every in-memory cache already follows.
"""

import hashlib
import ipaddress
import logging
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

from src.display.palette import validate_image_file

log = logging.getLogger(__name__)

# Apex domains we trust to serve cover art.  A host matches if it IS one of
# these or is a dotted subdomain of one — never merely a string that ends with
# one (so "evilcoverartarchive.org" is rejected, not allowed).  Cover Art
# Archive 307-redirects to the Internet Archive (archive.org), so that apex is
# included for the redirect hop.
_ALLOWED_COVER_APEX_DOMAINS = (
    "discogs.com",
    "coverartarchive.org",
    "archive.org",
    "mzstatic.com",
)

_MAX_COVER_BYTES = 10 * 1024 * 1024   # 10 MB ceiling on a downloaded cover
_MAX_COVER_REDIRECTS = 5              # cap redirect chains
_COVER_CONNECT_READ_TIMEOUT = 15     # seconds, per HTTP request
# _COVER_CONNECT_READ_TIMEOUT bounds a SINGLE socket read; it does nothing
# against a server that dribbles one byte just inside that window forever.
# _DOWNLOAD_DEADLINE_SECONDS is the total wall-clock budget for one cover —
# spanning every redirect hop AND the body stream — so a slow-drip fetch is
# aborted instead of parking an executor worker indefinitely (SEC-4).  45s is
# generous: a 10 MB cover on a 1 Mbit/s link is ~80s, but real covers are a few
# hundred KB, so this only ever bites a stall, never an honest slow link.
#
# #176 (KNOWN, ACCEPTED RESIDUAL — do not re-file):  the wall-clock deadline is a
# Python-level check BETWEEN blocking calls (between redirect hops, and between
# body read1() chunks — see download()).  It therefore cannot interrupt the
# synchronous response-HEADER parse inside pool.urlopen(), which is bounded only
# by the per-recv _COVER_CONNECT_READ_TIMEOUT above.  A host that dribbles
# response HEADERS one byte per <15s recv can still stall a single hop past the
# budget — the SEC-4 body-drip DoS class, in the header path SEC-4 didn't cover.
# Deliberately NOT fixed: reaching it requires a ROGUE allow-listed host or a
# MITM on the SSRF-pinned IP (the S-7 allow-list + IP pin + TLS hostname check
# gate both); on this single-user appliance the worst case is "covers stop
# loading", not data loss; and it was never reproduced (a hypothesis extrapolated
# from the confirmed body-drip).  The only COMPLETE fix — a watchdog thread that
# closes the socket at the deadline — adds more concurrency risk than the
# hypothesis warrants here.  Revisit if it is ever reproduced, or if this fetch
# moves somewhere multi-tenant / pool-starvation-sensitive (#176 cold-review).
_DOWNLOAD_DEADLINE_SECONDS = 45

# #230: minimum age before the in-uptime periodic sweep (called from _prune,
# i.e. after every download) will remove a ``.part`` orphan. A legitimately
# in-flight download's tempfile can only be as old as _DOWNLOAD_DEADLINE_SECONDS
# (45s); this is a comfortable multiple, so concurrent downloads on the shared
# executor are never swept out from under each other, while genuine orphans
# (minutes/hours/days old) are still cleared without waiting for the next boot.
_PARTIAL_SWEEP_MIN_AGE_SECONDS = 300

# R-2 disk-cache bounds.  A serious collection touches hundreds of covers; at
# ~50-400 KB each these defaults (≈ a few hundred MB) hold a very large library
# while still bounding unbounded growth on a small SD card.
_DEFAULT_MAX_CACHE_FILES = 500
_DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024  # 256 MB


def _host_is_allowed(host: Optional[str]) -> bool:
    """True if `host` is an allow-listed apex domain or a dotted subdomain of one.

    Matching is exact-or-dot-boundary (`host == apex` or `host.endswith("." +
    apex)`), never a bare suffix test — otherwise "evilcoverartarchive.org"
    would be accepted as if it were "coverartarchive.org".
    """
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(
        host == apex or host.endswith("." + apex)
        for apex in _ALLOWED_COVER_APEX_DOMAINS
    )


# NAT64 (RFC 6052 well-known prefix) and 6to4 wrap an IPv4 address inside an
# IPv6 one.  Modern CPython (≥3.11.9 / ≥3.9.19, via gh-113171) already flags
# these as is_reserved / is_private so the battery below rejects them — but a
# security boundary must not DEPEND on the stdlib's version-specific handling of
# exotic ranges, so we also decode the embedded IPv4 and re-classify it (SEC-5).
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_6TO4_PREFIX = ipaddress.ip_network("2002::/16")


def _is_disallowed_ip(ip) -> bool:
    """The maximal 'not safe to pin' classification (S-7).

    is_global alone is not enough — multicast (224/4) reports is_global=True —
    so the rest are belt-and-suspenders for private / loopback / link-local /
    multicast / reserved / unspecified space.
    """
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _embedded_ipv4(ip):
    """Return the IPv4 address embedded in a NAT64 (64:ff9b::/96) or 6to4
    (2002::/16) IPv6 address, else None.  Lets the classifier reject an internal
    IPv4 hidden inside such an address on ANY Python version (SEC-5)."""
    if ip.version != 6:
        return None
    if ip in _NAT64_PREFIX:                       # last 32 bits are the IPv4
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4_PREFIX:                         # bits 16..48 are the IPv4
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def _validated_public_ip(host: str) -> Optional[str]:
    """Resolve `host` ONCE and return a single public IP to pin to, else None.

    Returning the concrete address to connect to — rather than a yes/no — is what
    lets the download pin the socket to the exact IP that was vetted, closing the
    validate-then-resolve-again DNS-rebinding TOCTOU (S-7).  The whole hop is
    rejected if ANY resolved address is non-public, which also defeats a single
    rebinding answer that mixes a public and an internal IP.  Fails closed: any
    resolution / parse error returns None.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    if not infos:
        return None
    pinned: Optional[str] = None
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        # Normalize an IPv4-mapped IPv6 address (e.g. ::ffff:127.0.0.1) to its
        # IPv4 view so a private/loopback address can't be smuggled past the
        # classification dressed up in v6 clothing.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if _is_disallowed_ip(ip):
            return None
        # SEC-5: also re-classify the IPv4 embedded in a NAT64 / 6to4 address, so
        # an internal IPv4 hidden inside one is rejected even on a Python whose
        # stdlib does not (yet) flag these prefixes.
        embedded = _embedded_ipv4(ip)
        if embedded is not None and _is_disallowed_ip(embedded):
            return None
        if pinned is None:
            pinned = str(ip)
    return pinned


def _host_resolves_to_public_ip(host: str) -> bool:
    """True iff every DNS result for `host` is a global (public) address.

    Thin predicate over :func:`_validated_public_ip`, kept for callers/tests that
    only need the yes/no.  The download path uses the IP-returning function so it
    can pin the connection to the vetted address (S-7).
    """
    return _validated_public_ip(host) is not None


def _validate_cover_url(url: str) -> Tuple[str, str, str]:
    """Validate one cover-art URL hop; return ``(fetch_url, host, pinned_ip)``.

    - The host must be allow-listed (S-1).
    - The host is resolved here EXACTLY ONCE and every resolved address must be
      public; the returned ``pinned_ip`` is the address the caller MUST connect
      to so the socket is pinned to precisely what was vetted (S-7).
    - http is upgraded to https for allow-listed hosts (the MusicBrainz Cover
      Art Archive sometimes returns http URLs; upgrading only ever makes the
      request more secure, and avoids silently dropping every fallback cover).

    Raises ValueError if the scheme is not http(s), the host is not allow-listed,
    or the host does not resolve to a usable public address.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"cover URL scheme not allowed: {parts.scheme!r}")
    if not _host_is_allowed(host):
        raise ValueError(f"cover URL host not allow-listed: {host!r}")
    pinned_ip = _validated_public_ip(host)
    if pinned_ip is None:
        raise ValueError(f"cover URL host resolves to a non-public address: {host!r}")
    # R5-29: upgrade http→https AND drop any explicit port (and userinfo).
    # Preserving the port let `http://host:80/x` become `https://host:80/x` and
    # then dial TLS to port 80 (a guaranteed failure → the cover silently never
    # loads); more generally a poisoned metadata URL could steer the TLS dial to
    # ANY port on the allow-listed host's pinned IP (port-probing — the IP pin
    # holds, but a non-443 port is never legitimate for a cover CDN). netloc=host
    # is hostname-only, so `_open_cover_stream`'s `parts.port or 443` resolves to
    # 443 for every hop.
    if parts.scheme == "http":
        parts = parts._replace(scheme="https")
    url = parts._replace(netloc=host).geturl()
    return url, host, pinned_ip


def _open_cover_stream(fetch_url: str, host: str, pinned_ip: str, timeout: int):
    """Open a streaming GET to `fetch_url`, dialing the pre-validated `pinned_ip`
    but performing TLS for `host` (SNI + certificate hostname check).

    This is the seam that makes the S-7 pin real: urllib3 connects to the exact
    address vetted by :func:`_validate_cover_url`, while ``server_hostname`` /
    ``assert_hostname`` keep certificate verification bound to the original
    hostname — so pinning to an IP doesn't weaken authentication.  Redirects are
    NOT followed here; :meth:`CoverArtCache.download` walks and re-validates each
    hop.  Returns a urllib3 ``HTTPResponse`` opened with ``preload_content=False``
    for streaming.
    """
    parts = urlsplit(fetch_url)
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    # STAB-3: the pool is single-use — one freshly-pinned IP per hop, never
    # reused — so close it as soon as the streaming response is checked out.
    # The already-opened response keeps its own connection and streams fine
    # after the pool closes; download()'s later release_conn() then simply
    # closes that socket instead of returning it to a pool nobody reuses.
    with urllib3.HTTPSConnectionPool(
        pinned_ip,
        port=port,
        server_hostname=host,   # TLS SNI presented to the server
        assert_hostname=host,   # certificate must match the real hostname
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where(),
        timeout=urllib3.Timeout(connect=timeout, read=timeout),
    ) as pool:
        return pool.urlopen(
            "GET",
            path,
            headers={"Host": host, "User-Agent": "vinyl-now-playing/1.0"},
            redirect=False,
            retries=False,
            preload_content=False,
            decode_content=False,
        )


class CoverArtCache:
    """URL→disk cache for cover art, with an SSRF-hardened fetch and bounded,
    self-cleaning storage.

    Pure: no pygame, no palette.  The renderer holds one of these and asks for a
    path (``path_for`` / ``exists``) or triggers a fetch (``download``, run in an
    executor).  Image decoding / scaling and palette extraction stay in the
    renderer — they're render-loop concerns.
    """

    def __init__(
        self,
        cache_dir,
        *,
        max_files: int = _DEFAULT_MAX_CACHE_FILES,
        max_bytes: int = _DEFAULT_MAX_CACHE_BYTES,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self._sweep_partials()   # R-1: clear stale .part files from a hard kill
        self._prune()            # R-2: enforce the disk bound at startup

    # -- paths -------------------------------------------------------------

    def path_for(self, url: str) -> Path:
        """Deterministic on-disk path for a cover URL (md5 of the URL)."""
        return self.cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".jpg")

    def exists(self, url: str) -> bool:
        """True if this cover is already cached on disk."""
        return self.path_for(url).exists()

    # -- hygiene -----------------------------------------------------------

    def _sweep_partials(self, *, older_than_seconds: Optional[float] = None) -> None:
        """R-1: remove orphaned ``.cover-*.part`` tempfiles.

        A SIGKILL or power-loss between the tempfile write and the atomic rename
        strands a partial (download()'s own except-handler unlinks its tempfile
        on an ordinary exception, so those do NOT strand). Called with no age
        filter on construction (no download can be in flight then, so sweep all —
        this clears any partial left by the previous run's hard kill) and, per
        #230, also from _prune() after every download so a partial stranded
        *within* one long uptime is cleared without waiting for the next boot
        (the appliance runs 24/7, #191 — it is NOT restarted daily).

        ``older_than_seconds``: when set (the in-uptime call), only sweep
        partials at least that old. Cover downloads run concurrently on the
        shared executor, so a blanket sweep here would unlink a CONCURRENT
        download's in-flight tempfile and fail it; a legitimately in-flight
        partial can only be as old as _DOWNLOAD_DEADLINE_SECONDS, so the age gate
        spares it while still clearing true orphans (#230).
        """
        try:
            partials = list(self.cache_dir.glob(".cover-*.part"))
        except OSError:
            return
        now = time.time()
        for p in partials:
            if older_than_seconds is not None:
                try:
                    if now - p.stat().st_mtime < older_than_seconds:
                        continue   # too fresh — may be a concurrent in-flight download
                except OSError:
                    continue       # vanished / unstattable — skip
            try:
                p.unlink()
            except OSError:
                pass

    def _cover_files(self) -> list:
        """The cover files this cache manages (``*.jpg``), excluding tempfiles."""
        try:
            return [
                p for p in self.cache_dir.iterdir()
                if p.is_file() and p.suffix == ".jpg" and not p.name.startswith(".cover-")
            ]
        except OSError:
            return []

    def _prune(self, protect: Optional[Path] = None) -> None:
        """R-2: evict oldest covers until within both the file-count and total-
        byte bounds (mtime-LRU).  Cheap and bounded; called on init and after
        each successful download.  Also runs the age-gated ``.part`` orphan sweep
        (#230) so in-uptime orphans are cleared on the same cadence.

        ``protect`` (the path just written by :meth:`download`) is counted toward
        the bounds but is NEVER a candidate for eviction.  Without this, two
        covers sharing an mtime — common on a coarse-resolution SD card, or with
        concurrent downloads on the shared executor — could let a prune delete
        the very file the triggering download just cached, forcing an immediate
        re-fetch.  Ties are otherwise broken by name so eviction is deterministic.
        """
        # #230: also clear aged .part orphans in-uptime (age-gated so a
        # concurrent in-flight download's fresh tempfile is spared). Cheap: one
        # extra glob per prune, which already runs only after a download or at init.
        self._sweep_partials(older_than_seconds=_PARTIAL_SWEEP_MIN_AGE_SECONDS)

        protect_name = protect.name if protect is not None else None
        candidates = []   # evictable: (mtime, name, size, path)
        file_count = 0
        total_bytes = 0
        for p in self._cover_files():
            try:
                st = p.stat()
            except OSError:
                continue
            file_count += 1            # protected file still occupies a slot...
            total_bytes += st.st_size  # ...and still counts toward the byte cap
            if p.name == protect_name:
                continue               # ...but is never evicted
            candidates.append((st.st_mtime, p.name, st.st_size, p))
        candidates.sort(key=lambda t: (t[0], t[1]))  # oldest first; name = tiebreak

        i = 0
        n = len(candidates)
        while (file_count > self.max_files or total_bytes > self.max_bytes) and i < n:
            _, _, size, victim = candidates[i]
            i += 1
            try:
                victim.unlink()
            except OSError:
                continue  # couldn't evict this one; leave its bytes counted, try next
            file_count -= 1
            total_bytes -= size

    # -- fetch -------------------------------------------------------------

    def download(self, url: str) -> Path:
        """Synchronous cover-art download — must run in an executor.

        Hardened against SSRF (incl. DNS rebinding), oversized responses, and
        malicious images (S-1 / S-2 / S-7); see the module docstring.  Each hop
        is validated AND resolved once, the connection is pinned to that vetted
        IP (TLS still verified against the hostname), redirects are re-validated
        and re-pinned, the body is byte-capped and image-verified, then atomically
        renamed into place.  Prunes the disk cache (R-2) after a successful add.
        Returns the cache path on success; raises on any failure.
        """
        cache_path = self.path_for(url)
        current_url = url
        resp = None
        # SEC-4: one wall-clock budget for the entire fetch — redirect hops and
        # body stream alike — so a slow-drip response cannot park us forever.
        deadline = time.monotonic() + _DOWNLOAD_DEADLINE_SECONDS
        try:
            for _ in range(_MAX_COVER_REDIRECTS + 1):
                if time.monotonic() > deadline:
                    raise ValueError(
                        f"cover art download exceeded "
                        f"{_DOWNLOAD_DEADLINE_SECONDS}s deadline"
                    )
                fetch_url, host, pinned_ip = _validate_cover_url(current_url)
                resp = _open_cover_stream(
                    fetch_url, host, pinned_ip, _COVER_CONNECT_READ_TIMEOUT
                )
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    resp.release_conn()
                    resp = None
                    if not location:
                        raise ValueError("redirect with no Location header")
                    # Resolve relative redirects against the current hostname URL.
                    current_url = urljoin(fetch_url, location)
                    continue
                break
            else:
                raise ValueError("too many redirects fetching cover art")

            if resp is None:
                raise ValueError("no response fetching cover art")
            if resp.status >= 400:
                raise ValueError(f"cover art fetch returned HTTP {resp.status}")

            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                raise ValueError(f"unexpected Content-Type for cover art: {content_type!r}")

            # SEC-4: if the server DECLARES a body over the cap, reject up front
            # rather than streaming to the cap to find out.  A missing or
            # non-numeric header just means "stream and count" (the loop below
            # still enforces the true byte cap on the wire).
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_len = int(declared)
                except (TypeError, ValueError):
                    declared_len = None
                if declared_len is not None and declared_len > _MAX_COVER_BYTES:
                    raise ValueError(
                        f"cover art declared Content-Length {declared_len} "
                        f"exceeds {_MAX_COVER_BYTES} byte cap"
                    )

            # delete=False so we can rename after closing; we clean up manually on error
            tmp = tempfile.NamedTemporaryFile(
                dir=str(self.cache_dir),
                prefix=".cover-",
                suffix=".part",
                delete=False,
            )
            try:
                total = 0
                with tmp as f:
                    # SEC-4: read with read1(), NOT stream().  stream(amt) delegates
                    # to a buffered read() that blocks until `amt` bytes accumulate
                    # or the connection closes, so a peer dripping one byte per
                    # sub-timeout interval keeps a SINGLE read() running effectively
                    # forever and the deadline check below never gets a turn.
                    # read1() returns as soon as one underlying socket read yields
                    # data (up to amt), so control returns here after each recv and
                    # the wall-clock deadline can actually fire.  Do NOT "optimise"
                    # this back to stream() — that silently re-opens the slow-drip
                    # DoS this guard exists to close.
                    while True:
                        if time.monotonic() > deadline:
                            raise ValueError(
                                f"cover art download exceeded "
                                f"{_DOWNLOAD_DEADLINE_SECONDS}s deadline"
                            )
                        chunk = resp.read1(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_COVER_BYTES:
                            raise ValueError(
                                f"cover art exceeds {_MAX_COVER_BYTES} byte cap"
                            )
                        f.write(chunk)
                # Validate the decoded image before exposing it to the cache (S-2).
                validate_image_file(tmp.name)
                os.replace(tmp.name, str(cache_path))  # atomic on POSIX
            except Exception:
                # Clean up partial / rejected file before re-raising
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
        finally:
            if resp is not None:
                resp.release_conn()

        # R-2: keep the disk cache within bounds — but never evict the cover we
        # just wrote, even if it shares an mtime with an existing one.
        self._prune(protect=cache_path)
        return cache_path
