"""R7-03 — flip-resume: a full play of a MULTI-row closing side that is split
across two sessions by a mid-side silence gap must still credit.

Side-coverage (R7-01) already credits a full play whose closing side is a single
row (the *Meddle* "Echoes" shape) on the closer alone.  The residual R7-03
addresses is narrower: a closing side with several rows, broken by a silence gap
mid-side (a sleeve-cleaning pause longer than session_end_silence_seconds).  The
gap ends the first part UNARMED, and the armed session that follows holds only
the tail of the side — the mis-attributed-single signature — so side-coverage
suppresses the whole play.

The fix (Lane, 2026-08-11, LOCKED): the armed session inherits the immediately-
prior unarmed session's closing-side rows, bounded to a 5-minute window
(measured off the prior session's ``started_at`` — which R7-05 repurposes from
dead state) and to the SAME closing side, so the split play credits once while a
genuinely separate later listening, or a compilation's foreign rows, cannot.
"""
import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, PlaySession, TrackMetadata, TracklistEntry
from tests.test_listen_tracker import make_tracker


# A release whose CLOSING side (B) has three rows, so a play split mid-side
# genuinely fails side-coverage without flip-resume.
_TL = [
    TracklistEntry("A1", "Opener"),
    TracklistEntry("B1", "B-One"),
    TracklistEntry("B2", "B-Two"),
    TracklistEntry("B3", "B-Closer"),
]


def _t(title):
    return TrackMetadata(
        title=title, artist="Side-Long Band", album="Gapped LP",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=820, discogs_instance_id=821, tracklist=_TL,
    )


@pytest.mark.asyncio
async def test_mid_side_gap_full_play_credits_via_flip_resume():
    """RED before R7-03: side B is played B1, B2 → [45s+ cleaning gap] → B3.
    The gap ends the B1/B2 session unarmed; the B3 session covers only 1 of 3
    side-B rows and is suppressed.  Flip-resume inherits {B1, B2} so the full
    side credits exactly once."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_t("B-One"))     # B1
    await tracker.on_track_identified(_t("B-Two"))     # B2
    await tracker._end_session()                       # mid-side gap: unarmed end

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)  # needle back down on B3
    await tracker.on_track_identified(_t("B-Closer"))  # B3 closer → arms
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(820, 821)


@pytest.mark.asyncio
async def test_flip_resume_respects_the_5_minute_window():
    """Control: if the GAP between the prior unarmed session's end and the new
    session's start exceeds the flip-resume window, rows are NOT inherited —
    the split play stays suppressed (conservative missed-over-phantom).

    R8-01 (#345): the window bounds the GAP (`new.started_at - prev.ended_at`),
    not the elapsed time since the prior session STARTED — the old anchor made
    the feature inert at real track lengths.  Aging `ended_at` simulates a
    301s-old gap; scenario coverage at the real cadence lives in
    test_credit_cadence_r8.py.
    """
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_t("B-One"))
    await tracker.on_track_identified(_t("B-Two"))
    await tracker._end_session()

    # Age the prior session's END past the 5-minute window: the gap between
    # its detach and the new session's start is now > 300s.
    tracker._prev_unarmed.ended_at -= 301.0

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_t("B-Closer"))
    await tracker._end_session()

    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_flip_resume_is_closing_side_only():
    """Control: the prior session's rows on a DIFFERENT side (side A) do not
    count toward closing-side (B) coverage — only same-side rows are inherited,
    so an A-side stretch before a lone B-closer drop stays suppressed."""
    tracker, writer = make_tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_t("Opener"))    # A1 — not on the closing side
    await tracker._end_session()

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_t("B-Closer"))  # B3 closer alone (1 of 3)
    await tracker._end_session()

    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_flip_resume_does_not_rescue_a_comp_phantom():
    """Control: a prior unarmed session of a DIFFERENT release cannot lend rows —
    inheritance is scoped to the armed session's own release, so a compilation
    play preceding a lone owned-closer drop is not rescued into a phantom."""
    tracker, writer = make_tracker()
    # Three rows so the two played are NON-closers → the comp session ends
    # UNARMED (becomes the flip source) instead of self-completing.
    comp = lambda title: TrackMetadata(
        title=title, artist="Various", album="Best Of",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=990, discogs_instance_id=991,
        tracklist=[TracklistEntry("B1", "Comp One"), TracklistEntry("B2", "Comp Two"),
                   TracklistEntry("B3", "Comp Three")],
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(comp("Comp One"))   # foreign release, side B
    await tracker.on_track_identified(comp("Comp Two"))
    await tracker._end_session()                          # unarmed (neither is a closer)

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_t("B-Closer"))     # owned closer alone
    await tracker._end_session()

    writer.increment_play_count.assert_not_called()


def test_inherited_side_rows_satisfy_the_gate_at_the_session_level():
    """Unit: inherited closing-side rows count toward completion_supported."""
    closer = _t("B-Closer")
    s = PlaySession()
    s.album_release_id = closer.discogs_release_id
    s.log_track(closer)                      # only B3 identified (1 of 3)
    s.potential_last_track = True
    s.closing_track = closer
    assert s.completion_supported is False
    assert s.closing_side_coverage == (1, 3)

    # Inherit the two missing side-B rows (global indices 1 and 2).
    s.inherited_side_rows |= {1, 2}
    assert s.closing_side_coverage == (3, 3)
    assert s.completion_supported is True
