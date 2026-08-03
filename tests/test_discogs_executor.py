"""#61 — the dedicated Discogs ThreadPoolExecutor.

DiscogsHttp now owns a bounded, dedicated pool so a Discogs blocking call — and
in particular the 429 backoff ``time.sleep()`` inside ``request()`` — never
parks a worker on the SHARED default executor that also serves cover downloads
and Last.fm scrobbles/loves (the P-2 concern, follow-up #61).

These tests pin the transport-level contract: ``run()`` executes on a
"discogs"-named thread (not the default pool), threads its args/return value
through, and ``close()`` shuts the pool down (and is idempotent). The
resolver/tracker call-site wiring is pinned in test_resolver.py /
test_listen_tracker.py.
"""
import asyncio
import threading

import pytest

from tests.factories import make_discogs_http


def _current_thread_name():
    return threading.current_thread().name


@pytest.mark.asyncio
async def test_run_dispatches_on_the_dedicated_discogs_thread():
    """run() executes the callable on the dedicated pool (thread name prefixed
    'discogs'), NOT the shared default executor."""
    http = make_discogs_http()
    try:
        name = await http.run(_current_thread_name)
        assert name.startswith("discogs"), f"ran on {name!r}, not the dedicated pool"
    finally:
        http.close()


@pytest.mark.asyncio
async def test_run_passes_args_through_and_returns_the_result():
    """run(fn, *args) forwards positional args and returns fn's result."""
    http = make_discogs_http()
    try:
        assert await http.run(lambda a, b: a + b, 2, 3) == 5
    finally:
        http.close()


@pytest.mark.asyncio
async def test_dedicated_pool_is_a_distinct_thread_from_the_default_executor():
    """The isolation is real: the thread a default-pool call runs on is NOT the
    'discogs' pool, and vice-versa — so a Discogs backoff sleep cannot park a
    default-pool worker."""
    http = make_discogs_http()
    try:
        loop = asyncio.get_running_loop()
        default_name = await loop.run_in_executor(None, _current_thread_name)
        discogs_name = await http.run(_current_thread_name)
        assert not default_name.startswith("discogs")
        assert discogs_name.startswith("discogs")
        assert default_name != discogs_name
    finally:
        http.close()


def test_close_shuts_down_the_executor():
    """close() shuts the dedicated executor down — no new work can be scheduled."""
    http = make_discogs_http()
    http.close()
    with pytest.raises(RuntimeError):
        http._executor.submit(lambda: None)


def test_close_is_idempotent():
    """A second close() on an already-shut pool is a harmless no-op (the
    composition root's finally may run after other teardown)."""
    http = make_discogs_http()
    http.close()
    http.close()  # must not raise
