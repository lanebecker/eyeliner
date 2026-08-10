"""BoundedCache — an insertion-ordered LRU-refresh cache with a size cap (arch-4).

Extracted from ``renderer.py`` (where it was ``_BoundedCache``) so that
``src/metadata`` can share the ONE implementation instead of hand-rolling a
second copy of the same eviction algorithm — the two had already drifted on
replace semantics (see ``put`` below), and a future fix (a TTL, a changed
get-on-miss contract) would otherwise land in one copy and silently miss the
other (arch-4 / #220).

Python dicts preserve insertion order, so the eviction candidate is always the
first key. ``get`` re-inserts a hit at the end ("LRU-ish"), matching the strategy
the renderer's palette cache has used since v1.3.2. Pure Python, no pygame / PIL
dependency — unit-tested in ``tests/test_util_cache.py`` and still exercised
through the renderer's caches (``tests/test_renderer_caches.py``, via the
``_BoundedCache`` re-export in renderer.py) and the resolver's album cache.
"""


class BoundedCache:
    """A small insertion-ordered cache with LRU-refresh-on-get and a size cap."""

    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._data: dict = {}

    def get(self, key):
        """Return the cached value (refreshing its eviction position), or None."""
        if key not in self._data:
            return None
        value = self._data.pop(key)
        self._data[key] = value
        return value

    def put(self, key, value):
        """Insert/replace a value, evicting oldest entries beyond the cap.

        A replaced key is refreshed to most-recently-used (the ``pop`` before the
        re-insert). NOTE this differs from the resolver's pre-arch-4 inline store,
        which left a replaced key in place — but that is moot because every store
        follows a cache MISS (nothing to replace), so the observable behaviour is
        unchanged by the migration (#220 verifier note).
        """
        self._data.pop(key, None)
        self._data[key] = value
        while len(self._data) > self.max_entries:
            self._data.pop(next(iter(self._data)))

    def pop(self, key, default=None):
        """Remove ``key`` and return its value, or ``default`` if absent.

        Added for the resolver's #191 downgrade-TTL eviction: a stale entry read
        via ``get`` must be dropped so the album re-resolves. dict-like signature
        so callers read naturally.
        """
        return self._data.pop(key, default)

    def __contains__(self, key) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)
