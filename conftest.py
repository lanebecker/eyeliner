"""pytest configuration for vinyl-now-playing.

asyncio_mode = auto (set in pytest.ini) means all async test functions
are automatically treated as asyncio coroutines — no need to decorate
each one with @pytest.mark.asyncio individually.

test_discogs_live.py is a manual, network-hitting diagnostic script run by hand
against real Discogs credentials — not part of the unit suite.  Its filename
matches pytest's test_*.py glob, so a stray `pytest test_discogs_live.py` (or
`pytest .`) would try to collect it; collect_ignore keeps it out.  Its
functions were also renamed test_* → check_* so they aren't mistaken for
tests (T-7).
"""

collect_ignore = ["test_discogs_live.py"]
