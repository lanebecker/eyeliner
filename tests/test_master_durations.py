"""#467: fill missing per-track durations from the album MASTER, so the per-track
polling scheduler can predict next-track boundaries even when the resolved release's
Discogs entry carries no durations (the collection copy that skipped tracks live)."""
from unittest.mock import MagicMock

from src.metadata.models import TracklistEntry
from tests.factories import make_discogs_reader


def _rel(rid=555, master_id=999, tracklist=None):
    r = MagicMock()
    r.id = rid
    r.images = []; r.labels = []; r.year = 1990; r.styles = ["Rock"]; r.genres = ["Rock"]
    r.master = MagicMock(id=master_id) if master_id else None
    r.tracklist = tracklist if tracklist is not None else [
        MagicMock(type_=None, position="A1", title="One", duration=None),
    ]
    return r


def _trow(position, title="x", duration=None, type_=None):
    return MagicMock(type_=type_, position=position, title=title, duration=duration)


def _master_resp(rows):
    resp = MagicMock(); resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"tracklist": rows}
    return resp


def _mrow(position, duration, title="x", type_=None):
    d = {"position": position, "title": title, "duration": duration}
    if type_:
        d["type_"] = type_
    return d


def test_missing_durations_filled_from_master_by_position():
    reader = make_discogs_reader()
    reader.get_original_year = MagicMock(return_value=None)   # isolate: skip the year master GET
    reader._http.request = MagicMock(return_value=_master_resp(
        [_mrow("A1", "3:04"), _mrow("A2", "3:15")]
    ))
    rel = _rel(tracklist=[_trow("A1", "Peddler", None), _trow("A2", "Shrug", None)])
    result = reader._build_result(rel, instance_id=None)
    durs = {e.position: e.duration for e in result["tracklist"]}
    assert durs == {"A1": "3:04", "A2": "3:15"}


def test_no_master_fetch_when_release_has_all_durations():
    reader = make_discogs_reader()
    reader.get_original_year = MagicMock(return_value=None)
    reader._http.request = MagicMock()                       # would be the master GET
    rel = _rel(tracklist=[_trow("A1", "One", "3:00")])
    reader._build_result(rel, instance_id=None)
    reader._http.request.assert_not_called()                 # efficiency: no master GET


def test_title_match_when_position_schemes_differ():
    """Master uses numbered positions, the vinyl pressing uses A1/A2 — same order,
    different scheme. Position match misses; the unique-title match fills them."""
    reader = make_discogs_reader()
    reader.get_master_tracklist = MagicMock(return_value=[
        TracklistEntry(position="1", title="Peddler", duration="3:04"),
        TracklistEntry(position="2", title="Shrug", duration="3:15"),
    ])
    tl = [TracklistEntry("A1", "Peddler", None), TracklistEntry("A2", "Shrug", None)]
    out = reader._overlay_master_durations(_rel(), tl)
    assert [e.duration for e in out] == ["3:04", "3:15"]


def test_no_master_leaves_durations_none():
    reader = make_discogs_reader()
    tl = [TracklistEntry("A1", "One", None)]
    out = reader._overlay_master_durations(_rel(master_id=None), tl)
    assert out[0].duration is None


def test_master_fetch_failure_degrades_gracefully():
    reader = make_discogs_reader()
    reader._http.request = MagicMock(side_effect=ConnectionError("blip"))
    assert reader.get_master_tracklist(_rel()) == []
    tl = [TracklistEntry("A1", "One", None)]
    assert reader._overlay_master_durations(_rel(), tl)[0].duration is None


def test_title_match_when_positions_and_ORDER_both_differ():
    """#467 cold-review #1: a side-balanced reorder (equal counts, different order
    AND positions) must match by TITLE, never by ordinal — an index guess would
    assign a wrong (off-by-a-track) duration."""
    reader = make_discogs_reader()
    reader.get_master_tracklist = MagicMock(return_value=[
        TracklistEntry(position="1", title="Alpha", duration="3:00"),
        TracklistEntry(position="2", title="Bravo", duration="4:00"),
        TracklistEntry(position="3", title="Charlie", duration="5:00"),
    ])
    # vinyl playback order Alpha, Charlie, Bravo (side-balanced), no durations
    tl = [
        TracklistEntry("A1", "Alpha", None),
        TracklistEntry("A2", "Charlie", None),
        TracklistEntry("B1", "Bravo", None),
    ]
    out = reader._overlay_master_durations(_rel(), tl)
    assert {e.title: e.duration for e in out} == {
        "Alpha": "3:00", "Charlie": "5:00", "Bravo": "4:00",
    }


def test_ambiguous_duplicate_title_stays_none():
    """A title duplicated in the master is ambiguous, so a position-less/differently-
    positioned row with that title keeps None rather than borrowing a wrong duration."""
    reader = make_discogs_reader()
    reader.get_master_tracklist = MagicMock(return_value=[
        TracklistEntry(position="1", title="Reprise", duration="1:00"),
        TracklistEntry(position="2", title="Reprise", duration="2:00"),
    ])
    tl = [TracklistEntry("A1", "Reprise", None)]
    out = reader._overlay_master_durations(_rel(), tl)
    assert out[0].duration is None
