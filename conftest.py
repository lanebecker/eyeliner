"""pytest configuration for vinyl-now-playing.

asyncio_mode = auto (set in pytest.ini) means all async test functions
are automatically treated as asyncio coroutines — no need to decorate
each one with @pytest.mark.asyncio individually.

The manual, network-hitting Discogs diagnostic now lives at
scripts/discogs_live_check.py (CRIT-6) — outside testpaths=tests, with no
test_*.py filename — so pytest never collects it and the old collect_ignore
workaround (T-7) is no longer needed.

TQ-6: capture.py and main.py import ``sounddevice``, which needs the PortAudio
system library at import time.  This conftest is imported before any test
module, so installing a stub here (module scope) makes those imports succeed on
machines without PortAudio — replacing the two ``sys.modules.setdefault(...)``
lines that used to sit at module scope in test_capture.py and test_main_wiring.py
and were NEVER restored (a MagicMock leaked into sys.modules for the rest of the
process, visible to every other test module).  ``setdefault`` semantics are
preserved: a real sounddevice (a dev Mac with PortAudio) is left untouched, and
every test patches ``src.audio.capture.sd`` explicitly regardless.  The
``pytest_sessionfinish`` hook removes ONLY a stub we installed, so it can't leak
into a larger enclosing process.
"""
import sys
from unittest.mock import MagicMock

# Install before any test module imports capture.py / main.py. Record whether we
# actually installed it, so teardown never pops a real sounddevice.
_SOUNDDEVICE_STUB_INSTALLED = "sounddevice" not in sys.modules
if _SOUNDDEVICE_STUB_INSTALLED:
    sys.modules["sounddevice"] = MagicMock()


def pytest_sessionfinish(session, exitstatus):
    """Remove the sounddevice stub we planted, if any (TQ-6 teardown)."""
    if _SOUNDDEVICE_STUB_INSTALLED:
        sys.modules.pop("sounddevice", None)
