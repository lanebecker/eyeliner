"""Architecture tests for the A-4 Discogs split.

These lock in the concern boundary the split exists to create: the reader holds
only read methods, the writer only write methods, and a reader + writer built on
one transport genuinely share it (the composition main.py performs).
"""
from tests.factories import make_discogs_http, make_discogs_reader, make_discogs_writer


def test_reader_has_read_methods_not_write_methods():
    reader = make_discogs_reader()
    for m in ("search_collection", "search_database", "get_tracklist", "get_original_year"):
        assert callable(getattr(reader, m)), m
    assert not hasattr(reader, "increment_play_count")
    assert not hasattr(reader, "update_last_played")


def test_writer_has_write_methods_not_read_methods():
    writer = make_discogs_writer()
    for m in ("increment_play_count", "update_last_played"):
        assert callable(getattr(writer, m)), m
    assert not hasattr(writer, "search_collection")
    assert not hasattr(writer, "get_tracklist")


def test_reader_and_writer_share_one_transport():
    http = make_discogs_http()
    reader = make_discogs_reader(http=http)
    writer = make_discogs_writer(http=http)
    assert reader._http is http
    assert writer._http is http  # one shared session, two narrow halves


def test_package_exports_the_three_collaborators():
    import src.metadata.discogs as pkg
    assert set(pkg.__all__) == {"DiscogsHttp", "DiscogsReader", "DiscogsCollectionWriter"}
