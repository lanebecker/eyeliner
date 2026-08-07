"""pytest configuration for vinyl-now-playing.

asyncio_mode = auto (set in pytest.ini) means all async test functions
are automatically treated as asyncio coroutines — no need to decorate
each one with @pytest.mark.asyncio individually.

The manual, network-hitting Discogs diagnostic now lives at
scripts/discogs_live_check.py (CRIT-6) — outside testpaths=tests, with no
test_*.py filename — so pytest never collects it and the old collect_ignore
workaround (T-7) is no longer needed.
"""
