"""Discogs access, split by concern (A-4).

The former ~650-line ``DiscogsClient`` God object is now three collaborators:

  * :class:`~src.metadata.discogs.transport.DiscogsHttp` — the shared
    authenticated session + rate-limit-aware ``request()``.
  * :class:`~src.metadata.discogs.reader.DiscogsReader` — read-only search /
    tracklist / year / result assembly (the resolver's dependency).
  * :class:`~src.metadata.discogs.writer.DiscogsCollectionWriter` — Play Count
    and Last Played writes (the tracker's dependency).

The resolver and tracker each depend only on the half they use; they're wired
together (sharing one transport) at the composition root in ``main.py``.
"""

from src.metadata.discogs.transport import DiscogsHttp
from src.metadata.discogs.reader import DiscogsReader
from src.metadata.discogs.writer import DiscogsCollectionWriter

__all__ = ["DiscogsHttp", "DiscogsReader", "DiscogsCollectionWriter"]
