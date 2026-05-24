"""pytest configuration for vinyl-now-playing.

asyncio_mode = auto (set in pytest.ini) means all async test functions
are automatically treated as asyncio coroutines — no need to decorate
each one with @pytest.mark.asyncio individually.
"""
