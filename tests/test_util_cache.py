"""BoundedCache — the shared bounded-LRU cache (arch-4 / #220).

The renderer's six caches are exercised via tests/test_renderer_caches.py (which
imports the class through the renderer re-export), and the resolver's album cache
via test_resolver.py::test_cache_is_bounded. These pin the class directly at its
new home, including the pop() added for the resolver's #191 downgrade-TTL eviction.
"""
from src.util.cache import BoundedCache
# The renderer's private alias must still resolve to the SAME class (re-export).
from src.display.renderer import _BoundedCache


def test_reexport_is_the_same_class():
    assert _BoundedCache is BoundedCache


def test_get_returns_none_on_miss():
    c = BoundedCache(2)
    assert c.get("nope") is None


def test_put_get_roundtrip_and_contains_len():
    c = BoundedCache(2)
    c.put("a", 1)
    assert c.get("a") == 1
    assert "a" in c and len(c) == 1


def test_evicts_oldest_beyond_cap():
    c = BoundedCache(2)
    c.put("a", 1); c.put("b", 2); c.put("c", 3)   # "a" is oldest → evicted
    assert "a" not in c and "b" in c and "c" in c
    assert len(c) == 2


def test_get_refreshes_lru_position_so_it_is_not_evicted_next():
    c = BoundedCache(2)
    c.put("a", 1); c.put("b", 2)
    assert c.get("a") == 1          # refresh "a" → "b" is now oldest
    c.put("c", 3)                   # evicts "b", not "a"
    assert "a" in c and "c" in c and "b" not in c


def test_put_refreshes_a_replaced_key_position():
    c = BoundedCache(2)
    c.put("a", 1); c.put("b", 2)
    c.put("a", 11)                  # replace "a" → "b" becomes oldest
    c.put("c", 3)                   # evicts "b"
    assert c.get("a") == 11 and "c" in c and "b" not in c


def test_pop_removes_and_returns_value_with_default():
    c = BoundedCache(2)
    c.put("a", 1)
    assert c.pop("a") == 1
    assert "a" not in c and len(c) == 0
    assert c.pop("gone") is None            # default
    assert c.pop("gone", "d") == "d"        # explicit default
