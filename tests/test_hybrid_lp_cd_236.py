"""R5-16 (#236) — hybrid LP+CD releases.

(b) `_SIDE_RE` allowed a two-letter label for the doubled AA/BB sides of
    multi-disc pressings, but "CD1"/"LP1"/"DV1" also matched, so a bonus-CD
    row rendered a fabricated `SIDE CD` caption. `_match_side` now requires the
    two-letter form to be the SAME letter twice.

(a) `is_last_track` anchored on the last tracklist ROW, so on a hybrid LP+CD the
    vinyl closer (followed by bonus CD/digital rows) never armed completion — a
    permanent lost Play Count for every hybrid edition. It now anchors on the
    last VINYL-SIDE row (Lane approved 2026-08-11); a numbered/CD-only tracklist
    with no vinyl sides falls back to the last row (B-10 unchanged).
"""
import pytest

from src.metadata.models import SideIndex, TracklistEntry


def _si(rows, title):
    return SideIndex.from_tracklist([TracklistEntry(p, t) for p, t in rows], title)


HYBRID = [
    ("A1", "One"), ("A2", "Two"), ("B1", "Three"), ("B2", "Vinyl Closer"),
    ("CD1", "Bonus One"), ("CD2", "Bonus Two"),
]


# --- (b) _match_side ---

def test_cd_label_is_not_a_vinyl_side():
    assert _si(HYBRID, "Bonus One").side_letter is None
    assert _si(HYBRID, "Bonus One").side_total is None


@pytest.mark.parametrize("label", ["CD1", "LP1", "DV1", "Cd2", "lp3"])
def test_disc_label_pairs_never_parse_as_a_side(label):
    rows = [(label, "x"), ("A1", "y")]
    assert _si(rows, "x").side_letter is None


@pytest.mark.parametrize("label,expected", [("AA1", "AA"), ("BB2", "BB"), ("A1", "A")])
def test_genuine_single_and_doubled_sides_still_parse(label, expected):
    rows = [("A1", "p"), ("AA1", "q"), ("BB1", "r"), ("BB2", "s"), (label, "hit")]
    # build a clean tracklist where `label` is present exactly once
    rows = [(label, "hit"), ("Z9", "other")]
    assert _si(rows, "hit").side_letter == expected


# --- (a) vinyl-side completion anchor ---

def test_vinyl_closer_arms_completion_on_a_hybrid_release():
    """RED before R5-16(a): the bonus CD rows followed the vinyl closer, so it
    never had global_index == last row and completion never armed."""
    assert _si(HYBRID, "Vinyl Closer").is_last_track is True


def test_bonus_cd_row_is_not_the_last_track():
    assert _si(HYBRID, "Bonus Two").is_last_track is False


def test_mid_vinyl_track_is_not_the_last_track():
    assert _si(HYBRID, "One").is_last_track is False


def test_vinyl_only_album_last_track_unchanged():
    v = [("A1", "One"), ("A2", "Two"), ("B1", "Three"), ("B2", "Closer")]
    assert _si(v, "Closer").is_last_track is True
    assert _si(v, "One").is_last_track is False


def test_numbered_tracklist_falls_back_to_last_row():
    """No vinyl sides at all → the B-10 numbered-tracklist last-row anchor is
    preserved (a CD-only / digital release still credits on its final track)."""
    n = [("1", "a"), ("2", "b"), ("3", "c")]
    assert _si(n, "c").is_last_track is True
    assert _si(n, "b").is_last_track is False


def test_doubled_side_release_last_track():
    d = [("AA1", "w"), ("AA2", "x"), ("BB1", "y"), ("BB2", "last")]
    assert _si(d, "last").is_last_track is True
