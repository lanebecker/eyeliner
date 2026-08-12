"""Startup-hardening tests for main.py — Wave 4 bundle 3.

Covers:
  - #198 (ops-1): verify_recognition_backend_importable — fail LOUD at startup
    (ConfigError) when the shazamio backend can't import (e.g. Python 3.13 missing
    the audioop module), instead of a silent per-chunk miss; the requirements.txt
    audioop-lts pin that makes 3.13 work.
  - #202 (sec-1): _SecretRedactingFilter / install_secret_redaction — the Discogs
    token (carried in the URL query by python3-discogs-client) is scrubbed from
    log records at the root handler.
"""
import io
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import ConfigError
import main as main_mod
from main import (
    verify_recognition_backend_importable,
    install_secret_redaction,
    _SecretRedactingFilter,
    _run_scrubbed,
)


# ---------------------------------------------------------------------------
# #198 — startup import probe
# ---------------------------------------------------------------------------

def test_probe_raises_configerror_when_shazamio_import_fails():
    """A shazamio ImportError (the Python-3.13/audioop case) must become an
    actionable ConfigError naming the fix, not propagate as a bare ImportError."""
    cfg = SimpleNamespace(recognition=SimpleNamespace(backend="shazamio"))

    def boom(name):
        raise ImportError("No module named 'pyaudioop'")

    with pytest.raises(ConfigError) as ei:
        verify_recognition_backend_importable(cfg, _import=boom)
    msg = str(ei.value)
    assert "shazamio" in msg
    assert "audioop" in msg                 # names the actual cause
    assert "requirements.txt" in msg        # names the fix
    assert "Legacy" in msg                  # names the alternative image


def test_probe_raises_configerror_on_non_import_error():
    """R7-18: the probe catches ANY import-time failure, not just ImportError — a
    broken native dependency raises OSError ("libFLAC.so: cannot open shared object
    file") at import, which must also become an actionable ConfigError, not a bare
    traceback that crash-loops past the exit-78 park."""
    cfg = SimpleNamespace(recognition=SimpleNamespace(backend="shazamio"))

    def boom(name):
        raise OSError("libFLAC.so.8: cannot open shared object file")

    with pytest.raises(ConfigError) as ei:
        verify_recognition_backend_importable(cfg, _import=boom)
    assert "shazamio" in str(ei.value)


def test_probe_passes_when_backend_importable():
    """Success path: an importable backend leaves the probe silent (no raise)."""
    cfg = SimpleNamespace(recognition=SimpleNamespace(backend="shazamio"))
    verify_recognition_backend_importable(cfg, _import=lambda name: object())


def test_probe_skips_non_shazamio_backend():
    """A non-shazamio backend must not trigger the shazamio import at all."""
    attempted = []
    cfg = SimpleNamespace(recognition=SimpleNamespace(backend="acrcloud"))
    verify_recognition_backend_importable(cfg, _import=lambda n: attempted.append(n))
    assert attempted == []


def test_requirements_pins_audioop_lts_for_py313():
    """#198 layer 2: requirements.txt must carry the audioop-lts backport gated on
    Python 3.13+, so a fresh install on the current default (Trixie) image works."""
    req = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    assert "audioop-lts" in req
    assert 'python_version >= "3.13"' in req


# ---------------------------------------------------------------------------
# #202 — secret-redacting log filter
# ---------------------------------------------------------------------------

def _record(msg, args=()):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_filter_scrubs_exact_secret_and_token_query():
    filt = _SecretRedactingFilter(["SUPERSECRETTOKEN"])
    rec = _record(
        "transient: /database/search?q=a&token=SUPERSECRETTOKEN (key SUPERSECRETTOKEN)"
    )
    assert filt.filter(rec) is True          # never drops the record
    out = rec.getMessage()
    assert "SUPERSECRETTOKEN" not in out      # both occurrences gone
    assert "token=<redacted>" in out
    assert rec.args == ()                      # args cleared so %-fmt can't reintroduce


def test_filter_masks_token_query_even_without_a_known_secret():
    """Belt-and-suspenders: an unknown token= value (future/rotated secret) is
    masked by the regex even if it isn't in the configured secret list."""
    filt = _SecretRedactingFilter([])
    rec = _record("Max retries: /database/search?q=a&token=abc123XYZ (err)")
    filt.filter(rec)
    out = rec.getMessage()
    assert "abc123XYZ" not in out
    assert "token=<redacted>" in out


def test_filter_leaves_clean_message_and_args_untouched():
    filt = _SecretRedactingFilter(["SECRET"])
    rec = _record("nothing %s here", ("sensitive-free",))
    filt.filter(rec)
    assert rec.getMessage() == "nothing sensitive-free here"
    assert rec.args == ("sensitive-free",)     # untouched → args preserved


def test_filter_drops_malformed_record_without_raising():
    """A filter runs before emit()'s fault isolation, so it must never let a
    malformed %-format record (getMessage() → TypeError) propagate to the caller —
    on the 24/7 pipeline that would kill a coroutine leg. It must DROP it (return
    False), not pass it to handleError(), which would dump raw args (a possible
    secret) to stderr."""
    filt = _SecretRedactingFilter(["SECRET"])
    rec = _record("release %d", ("not-an-int",))   # %d with a str → getMessage() raises
    assert filt.filter(rec) is False               # dropped, no exception, no raw emit


def test_filter_ignores_non_string_secrets():
    """A None/non-str credential (mistyped config, or a MagicMock in tests) must
    not crash the log path."""
    filt = _SecretRedactingFilter([None, 123, "", "REALSECRET"])
    rec = _record("has REALSECRET in it")
    filt.filter(rec)                            # must not raise
    assert "REALSECRET" not in rec.getMessage()


def test_install_attaches_to_root_handler_and_scrubs_propagated_record():
    """install_secret_redaction must attach to the root HANDLER (not the logger),
    so a token-bearing record propagating up from a child logger (resolver) is
    scrubbed on the way out."""
    root = logging.getLogger()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root.addHandler(handler)
    cfg = SimpleNamespace(
        discogs=SimpleNamespace(user_token="TIP_SECRET_TOKEN"),
        lastfm=SimpleNamespace(api_key="", api_secret="", session_key=""),
    )
    installed = None
    try:
        installed = install_secret_redaction(cfg)
        assert installed in handler.filters      # attached to the handler
        logging.getLogger("src.metadata.resolver").warning(
            "couldn't determine (transient): /database/search?q=x&token=TIP_SECRET_TOKEN"
        )
        handler.flush()
        out = buf.getvalue()
        assert "TIP_SECRET_TOKEN" not in out
        assert "<redacted>" in out
    finally:
        for h in list(root.handlers):
            if installed is not None:
                h.removeFilter(installed)
        root.removeHandler(handler)


# ---------------------------------------------------------------------------
# R7-20 — _run_scrubbed must not leak a secret-bearing exception chain when
# Ctrl+C lands mid-error (the KeyboardInterrupt __context__ leak).
# ---------------------------------------------------------------------------

def test_run_scrubbed_ki_with_secret_context_is_scrubbed_and_severed(monkeypatch, capsys):
    """R7-20: if Ctrl+C interrupts a token-bearing exception, a BARE KI re-raise
    lets Python's default excepthook render the whole __context__ chain raw to
    stderr → journald. _run_scrubbed must scrub-and-print the chain here, then
    re-raise a CONTEXT-SEVERED KeyboardInterrupt so nothing secret is left to
    print — SIGINT exit behaviour preserved."""
    secret = "TOKEN_ABC_SECRET_XYZ"

    async def boom_main():
        try:
            raise ValueError(f"discogs request failed: /x?token={secret}")
        except ValueError:
            raise KeyboardInterrupt()

    monkeypatch.setattr(main_mod, "main", boom_main)
    monkeypatch.setattr(main_mod, "_REDACTOR", _SecretRedactingFilter([secret]))

    with pytest.raises(KeyboardInterrupt) as ei:
        _run_scrubbed()

    assert ei.value.__suppress_context__ is True    # chain severed for the excepthook
    err = capsys.readouterr().err
    assert secret not in err                         # our scrubbed print does not leak it
    assert "redacted" in err                         # …and something WAS scrubbed


def test_run_scrubbed_plain_ki_reraises_untouched(monkeypatch, capsys):
    """Control: a plain Ctrl+C with no in-flight error (no __context__) re-raises
    untouched, printing nothing."""
    async def clean_ki():
        raise KeyboardInterrupt()

    monkeypatch.setattr(main_mod, "main", clean_ki)
    with pytest.raises(KeyboardInterrupt):
        _run_scrubbed()
    assert capsys.readouterr().err == ""
