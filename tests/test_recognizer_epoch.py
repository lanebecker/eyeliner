"""Regression tests for the B-1 session-epoch mechanics on PlayerState.

The B-1 guard itself (a commit discarding itself when the session ends mid-
resolve) moved with the commit sequence to tests/test_track_commit_service.py
(A-9).  What remains here is the underlying PlayerState contract the guard
relies on: clear() bumps the epoch, set_track() does not.
"""
from unittest.mock import MagicMock

from src.state.player_state import PlayerState


# ---------------------------------------------------------------------------
# PlayerState epoch mechanics
# ---------------------------------------------------------------------------

def test_clear_bumps_session_epoch():
    s = PlayerState()
    before = s.session_epoch
    s.clear()
    assert s.session_epoch == before + 1


def test_set_track_does_not_bump_epoch():
    s = PlayerState()
    before = s.session_epoch
    s.set_track(MagicMock())
    assert s.session_epoch == before
