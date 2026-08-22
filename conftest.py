"""pytest configuration for vinyl-now-playing.

asyncio_mode = auto (set in pytest.ini) means all async test functions
are automatically treated as asyncio coroutines — no need to decorate
each one with @pytest.mark.asyncio individually.

The manual, network-hitting Discogs diagnostic now lives at
scripts/discogs_live_check.py (CRIT-6) — outside testpaths=tests, with no
test_*.py filename — so pytest never collects it and the old collect_ignore
workaround (T-7) is no longer needed.

TQ-6: capture.py and main.py import ``sounddevice``, which needs the PortAudio
system library at import time. This conftest is imported before any test module,
so it first tries the installed backend, then installs a fallback stub only when
that import fails. This replaces the two ``sys.modules.setdefault(...)`` lines
that used to sit at module scope in test_capture.py and test_main_wiring.py and
were NEVER restored (a MagicMock leaked into sys.modules for the rest of the
process, visible to every other test module). Every test patches
``src.audio.capture.sd`` explicitly regardless. The ``pytest_sessionfinish``
hook removes ONLY the exact stub object this conftest installed, so it cannot
remove a real or third-party-owned module.
"""
import importlib
import sys
from unittest.mock import MagicMock


def _load_sounddevice_or_install_stub(importer=importlib.import_module, modules=None):
    """Retain an importable backend, or install and return this conftest's stub."""
    if modules is None:
        modules = sys.modules

    try:
        return importer("sounddevice"), None
    except Exception:
        # A third party can have supplied a module after a failed import attempt.
        # It is not ours to overwrite or later remove.
        existing_module = modules.get("sounddevice")
        if existing_module is not None:
            return existing_module, None

        stub = MagicMock(name="sounddevice-test-fallback")
        modules["sounddevice"] = stub
        return stub, stub


def _remove_owned_sounddevice_stub(modules, owned_stub):
    """Remove the fallback only while it remains the exact object we installed."""
    if owned_stub is not None and modules.get("sounddevice") is owned_stub:
        modules.pop("sounddevice", None)


# Install before any test module imports capture.py / main.py. The CI workflow
# validates the real package in a separate process before pytest starts; this
# fallback keeps hardware-free unit tests runnable on development machines.
_SOUNDDEVICE_MODULE, _SOUNDDEVICE_STUB = _load_sounddevice_or_install_stub()


def pytest_sessionfinish(session, exitstatus):
    """Remove the sounddevice stub we planted, if any (TQ-6 teardown)."""
    _remove_owned_sounddevice_stub(sys.modules, _SOUNDDEVICE_STUB)
