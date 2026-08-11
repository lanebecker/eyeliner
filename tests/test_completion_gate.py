"""Regression tests for #182 (R4:gap1-3) — the ≥2-track completion gate.

The album-change auto-split treats ANY confirmed release-id change as a
physical record swap, and ``potential_last_track`` arms from any track's
``is_last_track``.  Composed with a routine Shazam behaviour — per-track album
attribution swinging to an owned compilation for a hit single — a
straight-through play of Album X minted a one-track split-off session whose
sole track latched the compilation AND armed the completion flag (chronological
compilations routinely end with the major hit), and the next Album-X track's
split finalized it: a phantom full-album Play Count + Last Played for a record
that never left its sleeve.

The gate (approved by Lane, 2026-08-08): a session with a latched release is
credit- and love-eligible only when it identified at least TWO tracks of that
release (the closer plus one supporting track) — with a carve-out for genuine
single-track releases, whose full play IS one track.  Suppression is logged
loudly so a deliberate closer-only needle drop is diagnosable.  Missed count
preferred over phantom count (the META-4 posture).
"""
from unittest.mock import MagicMock

import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, TrackMetadata, TracklistEntry
from src.tracking.listen_tracker import ListenTracker
from tests.test_listen_tracker import make_track, make_writer_mock


GH_TRACKLIST = [
    TracklistEntry("1", "Early Single"),
    TracklistEntry("2", "Album Cut"),
    TracklistEntry("3", "The Hit"),          # compilation closes with the hit
]


def swung_hit():
    """'The Hit' as Shazam mis-attributes it: the owned Greatest Hits
    compilation (release 200), whose tracklist it closes — is_last_track=True
    from one identification."""
    return TrackMetadata(
        title="The Hit",
        artist="Sonic Youth",
        album="Greatest Hits",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=200,
        discogs_instance_id=999,
        tracklist=GH_TRACKLIST,
    )


def _tracker(with_lastfm=False):
    writer = make_writer_mock()
    lastfm = None
    if with_lastfm:
        lastfm = MagicMock()
        lastfm.enabled = True                       # R5-22: an ACTIVE client
        lastfm.love_on_completion = True
        lastfm.love = MagicMock(return_value=True)
    tracker = ListenTracker(writer, lastfm)
    return tracker, writer, lastfm


# ---------------------------------------------------------------------------
# The headline phantom (executed in the audit): straight-through Album X play
# with one mid-album attribution swing must credit X once and NEVER credit the
# compilation.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attribution_swing_does_not_phantom_credit_the_compilation():
    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))   # X: A1
    await tracker.on_track_identified(make_track("Tuff Gnarl"))       # X: B1
    await tracker.on_track_identified(swung_hit())                    # swing → split #1
    await tracker.on_track_identified(make_track("Cotton Crown"))     # back to X → split #2
    await tracker.on_track_identified(make_track("Master-Dik"))       # X closer
    await tracker._end_session()

    calls = writer.increment_play_count.call_args_list
    assert [c.args for c in calls] == [(12345, 67890)], (
        f"expected exactly one credit for Album X, got {[c.args for c in calls]}"
    )


@pytest.mark.asyncio
async def test_phantom_session_love_is_also_suppressed(caplog):
    tracker, writer, lastfm = _tracker(with_lastfm=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(swung_hit())                    # split #1
    await tracker.on_track_identified(make_track("Cotton Crown"))     # split #2 finalizes phantom
    await tracker.on_track_identified(make_track("Master-Dik"))       # X closer
    await tracker._end_session()

    # Only the genuine Album X completion is loved — once, targeting X's closer.
    assert lastfm.love.call_count == 1
    assert lastfm.love.call_args[0][0].title == "Master-Dik"
    assert writer.increment_play_count.call_count == 1


# ---------------------------------------------------------------------------
# The approved behaviour change: closer-only sessions on multi-track releases
# are suppressed, loudly.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_closer_only_needle_drop_is_suppressed_and_logged(caplog):
    # last_played_field_name configured so the update_last_played assertion
    # is load-bearing, not vacuous (#182 cold-review note).
    writer = make_writer_mock(last_played_field_name="Last Played")
    tracker = ListenTracker(writer)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))       # closer only
    with caplog.at_level("INFO"):
        await tracker._end_session()

    writer.increment_play_count.assert_not_called()
    writer.update_last_played.assert_not_called()
    assert any("#182" in r.message for r in caplog.records), (
        "suppression must be loudly logged for diagnosability"
    )


@pytest.mark.asyncio
async def test_decorated_reidentified_closer_is_not_supporting_evidence():
    """#182 cold-review regression: the closer re-confirmed under a decorated
    Shazam catalogue title ("The Hit - 2011 Remaster") — same release via the
    album cache, invisible to both the recognizer's and log_track's dedups —
    must count as the SAME track, not as supporting evidence.  Without
    distinctness the phantom compilation credit reopens through this slice."""
    decorated_hit = TrackMetadata(
        title="The Hit - 2011 Remaster",
        artist="Sonic Youth",
        album="Greatest Hits",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=200,
        discogs_instance_id=999,
        tracklist=GH_TRACKLIST,
    )
    assert decorated_hit.is_last_track is True   # #180 tier-2 keeps it armed

    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(swung_hit())                    # split-off phantom
    await tracker.on_track_identified(decorated_hit)                  # re-identified closer
    await tracker.on_track_identified(make_track("Cotton Crown"))     # X → split finalizes phantom
    await tracker.on_track_identified(make_track("Master-Dik"))       # X closer
    await tracker._end_session()

    calls = writer.increment_play_count.call_args_list
    assert [c.args for c in calls] == [(12345, 67890)], (
        f"phantom compilation credit reopened: {[c.args for c in calls]}"
    )


@pytest.mark.asyncio
async def test_single_track_release_full_play_still_credits():
    """Carve-out: a single-track release's full play IS one track."""
    tracker, writer, _ = _tracker()
    single = TrackMetadata(
        title="One Long Piece",
        artist="Some Artist",
        album="The Single Sided LP",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=300,
        discogs_instance_id=301,
        tracklist=[TracklistEntry("A1", "One Long Piece")],
    )
    assert single.is_last_track is True
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(single)
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(300, 301)


# ---------------------------------------------------------------------------
# Unchanged behaviour (controls).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_full_play_still_credits_and_loves():
    tracker, writer, lastfm = _tracker(with_lastfm=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Catholic Block"))
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(12345, 67890)
    assert lastfm.love.call_count == 1


@pytest.mark.asyncio
async def test_fallback_album_love_is_unchanged_by_the_gate():
    """The gate applies only where a release is latched; a FALLBACK album's
    completion love (no release id, no split possible) keeps its existing
    behaviour."""
    tracker, writer, lastfm = _tracker(with_lastfm=True)
    fallback_closer = TrackMetadata(
        title="Fallback Closer",
        artist="Someone",
        album="Fallback Album",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None,
        discogs_instance_id=None,
        tracklist=[],
    )
    # Fallback tracks can't carry is_last_track (empty tracklist), so arm the
    # session the way a mixed session would: simulate via a direct session poke
    # is NOT possible through the public path — instead this control documents
    # that a fallback-only session (never armed) is simply not loved, before
    # and after the gate.
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(fallback_closer)
    await tracker._end_session()

    lastfm.love.assert_not_called()
    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_same_base_sibling_variants_still_credit():
    """#182 second-pass regression: genuinely DIFFERENT tracklist rows whose
    titles share a decoration base ("Golden Hour" + "Golden Hour (Acoustic)")
    are two rows — a real completed play must credit.  A decoration-base
    distinctness rule wrongly suppressed this."""
    tl = [TracklistEntry("A1", "Golden Hour"), TracklistEntry("B2", "Golden Hour (Acoustic)")]
    def gh(title):
        return TrackMetadata(
            title=title, artist="Band", album="Golden Hour",
            source=MetadataSource.DISCOGS_COLLECTION,
            discogs_release_id=500, discogs_instance_id=501, tracklist=tl,
        )
    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(gh("Golden Hour"))
    await tracker.on_track_identified(gh("Golden Hour (Acoustic)"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(500, 501)


@pytest.mark.asyncio
async def test_variants_only_ep_still_credits():
    """#182 second-pass regression: a 12\" whose tracklist is all variants of
    one song must remain creditable — every variant is its own row."""
    tl = [
        TracklistEntry("A1", "Hit (Extended Mix)"),
        TracklistEntry("B1", "Hit (Radio Edit)"),
        TracklistEntry("B2", "Hit (Instrumental)"),
    ]
    def v(title):
        return TrackMetadata(
            title=title, artist="Band", album="Hit EP",
            source=MetadataSource.DISCOGS_COLLECTION,
            discogs_release_id=600, discogs_instance_id=601, tracklist=tl,
        )
    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(v("Hit (Extended Mix)"))
    await tracker.on_track_identified(v("Hit (Radio Edit)"))
    await tracker.on_track_identified(v("Hit (Instrumental)"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(600, 601)


@pytest.mark.asyncio
async def test_row_unresolvable_identification_is_not_support():
    """An identification whose title resolves to NO tracklist row cannot vouch
    for a completed side — closer + one unplaceable variant must still be
    suppressed (the phantom would otherwise reopen through unmatched
    re-identifications)."""
    unplaceable = TrackMetadata(
        title="The Hit - Alternate",      # no decoration keyword: matches no GH row
        artist="Sonic Youth", album="Greatest Hits",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=200, discogs_instance_id=999,
        tracklist=GH_TRACKLIST,
    )
    assert unplaceable.side_index.global_index is None

    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(swung_hit())
    await tracker.on_track_identified(unplaceable)
    await tracker.on_track_identified(make_track("Cotton Crown"))     # split finalizes phantom
    await tracker.on_track_identified(make_track("Master-Dik"))
    await tracker._end_session()

    calls = writer.increment_play_count.call_args_list
    assert [c.args for c in calls] == [(12345, 67890)]


def test_supporting_row_count_is_scoped_to_the_latched_release():
    """Defence in depth at the PlaySession level (the tracker's split makes
    this unreachable in production, but the property must not count another
    release's rows as support if that invariant ever weakens)."""
    from src.metadata.models import PlaySession
    session = PlaySession()
    session.log_track(swung_hit())                       # release 200, its closer row
    other = TrackMetadata(
        title="Catholic Block", artist="Sonic Youth", album="Sister",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=300, discogs_instance_id=301,
        tracklist=[TracklistEntry("A1", "Catholic Block"), TracklistEntry("A2", "Closer")],
    )
    assert other.side_index.global_index is not None
    session.log_track(other)                             # different release's row

    assert session.album_release_id == 200
    assert session.supporting_row_count == 1             # 300's row is not support
    assert session.completion_supported is False


@pytest.mark.asyncio
async def test_release_less_tracks_do_not_count_as_support():
    """The supporting count is per-release: a FALLBACK track appended to the
    session (no release id) must not satisfy the gate for the latched
    release's closer."""
    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Master-Dik"))       # closer only
    filler = TrackMetadata(
        title="Static Between Records", artist="Nobody", album="Nothing",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None, discogs_instance_id=None, tracklist=[],
    )
    await tracker.on_track_identified(filler)
    await tracker._end_session()

    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_two_track_release_side_flip_still_credits():
    """A two-track album (one per side) identified fully: exactly the ≥2
    threshold — must credit."""
    tracker, writer, _ = _tracker()
    tl = [TracklistEntry("A", "Side One"), TracklistEntry("B", "Side Two")]
    a = TrackMetadata(
        title="Side One", artist="Band", album="Two Sider",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=400, discogs_instance_id=401, tracklist=tl,
    )
    b = TrackMetadata(
        title="Side Two", artist="Band", album="Two Sider",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=400, discogs_instance_id=401, tracklist=tl,
    )
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(a)
    await tracker.on_track_identified(b)
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(400, 401)


# ---------------------------------------------------------------------------
# R5-05 (#234) — the single-track carve-out must check the closer's RELEASE.
# ---------------------------------------------------------------------------

from src.metadata.models import PlaySession   # noqa: E402

_ALBUM_TL = [
    TracklistEntry("A1", "One"), TracklistEntry("A2", "Two"),
    TracklistEntry("A3", "Three"), TracklistEntry("B1", "Four"),
]
_SINGLE_TL = [TracklistEntry("A", "Foreign Hit")]


def _album_track(title):
    return TrackMetadata(
        title=title, artist="Band", album="Real Album",
        source=MetadataSource.DISCOGS_COLLECTION, discogs_release_id=100,
        discogs_instance_id=1, tracklist=_ALBUM_TL,
    )


def _foreign_single():
    return TrackMetadata(
        title="Foreign Hit", artist="Someone Else", album="Foreign Hit",
        source=MetadataSource.DISCOGS_COLLECTION, discogs_release_id=200,
        discogs_instance_id=2, tracklist=_SINGLE_TL,
    )


def test_foreign_single_track_release_does_not_satisfy_the_carve_out():
    """RED before R5-05: a multi-track album latched with only ONE supporting
    row, plus a Shazam swing to a DIFFERENT one-track single (whose sole row is
    is_last_track), passed the len==1 carve-out and phantom-credited the album."""
    s = PlaySession()
    first = _album_track("One")
    s.album_release_id = first.discogs_release_id
    s.log_track(first)
    foreign = _foreign_single()
    s.log_track(foreign)
    s.potential_last_track = True
    s.closing_track = foreign

    assert s.supporting_row_count == 1
    assert s.completion_supported is False


def test_genuine_single_track_release_still_credits():
    """The carve-out still fires for a real one-row release: closer IS the
    latched release."""
    tl = [TracklistEntry("A", "Only Track")]
    t = TrackMetadata(
        title="Only Track", artist="Band", album="The Single",
        source=MetadataSource.DISCOGS_COLLECTION, discogs_release_id=300,
        discogs_instance_id=3, tracklist=tl,
    )
    s = PlaySession()
    s.album_release_id = t.discogs_release_id
    s.log_track(t)
    s.potential_last_track = True
    s.closing_track = t

    assert s.completion_supported is True



# ---------------------------------------------------------------------------
# R5-22 (#251) — a DISABLED Last.fm client must not report a false love
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_lastfm_client_does_not_falsely_love(caplog):
    """love_on_completion=True but the client is disabled (scrobble off / bad
    creds): love() is a graceful no-op returning True, so the pre-R5-22 gate
    logged '✅ Last.fm loved' and latched loved=True while nothing was sent."""
    import logging as _logging
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.enabled = False
    lastfm.love_on_completion = True
    lastfm.love = MagicMock(return_value=True)
    tracker = ListenTracker(writer, lastfm)

    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(make_track("Cotton Crown"))
    await tracker.on_track_identified(make_track("Master-Dik"))
    session = tracker._session
    with caplog.at_level(_logging.INFO):
        await tracker._finalize_session(session)

    lastfm.love.assert_not_called()          # never entered the love branch
    assert session.loved is False            # no false latch
    assert not any("✅ Last.fm loved" in r.message for r in caplog.records)


def test_disabled_love_warns_once_at_startup(caplog):
    """The misconfigured combo (love wanted, client disabled) surfaces one
    startup warning rather than silently doing nothing."""
    import logging as _logging
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.enabled = False
    lastfm.love_on_completion = True
    with caplog.at_level(_logging.WARNING):
        ListenTracker(writer, lastfm)
    assert any("love on completion" in r.message.lower() for r in caplog.records)


def test_active_client_with_love_off_does_not_warn(caplog):
    import logging as _logging
    writer = make_writer_mock()
    lastfm = MagicMock()
    lastfm.enabled = True
    lastfm.love_on_completion = False
    with caplog.at_level(_logging.WARNING):
        ListenTracker(writer, lastfm)
    assert not any("love on completion" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# R6-07 (#272) — the single-track carve-out counts VINYL rows, not CD/bonus.
# A hybrid LP+CD edition whose sole VINYL row is a side-long piece must be
# creditable on a full vinyl play; len(tracklist) counted the never-playable
# bonus-CD rows and suppressed it forever.
# ---------------------------------------------------------------------------

_HYBRID_TL = [
    TracklistEntry("A1", "Side-Long Piece"),    # the only vinyl side
    TracklistEntry("CD1", "Bonus Track One"),   # never plays on the platter
    TracklistEntry("CD2", "Bonus Track Two"),
]


def _hybrid_closer():
    return TrackMetadata(
        title="Side-Long Piece", artist="Post Rock Band",
        album="One Long Piece (+ Bonus CD)",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=700, discogs_instance_id=701, tracklist=_HYBRID_TL,
    )


def test_single_vinyl_row_hybrid_is_completion_supported():
    """R6-07: the carve-out mirrors the R5-16(a) vinyl anchor — one VINYL row is
    a complete play even when never-playable CD rows follow it. Pre-fix the
    carve-out tested ``len(closer.tracklist) == 1`` (== 3 here) → the full vinyl
    play was ALWAYS suppressed."""
    closer = _hybrid_closer()
    assert closer.is_last_track is True             # vinyl anchor → A1 is the closer
    s = PlaySession()
    s.album_release_id = closer.discogs_release_id
    s.log_track(closer)
    s.potential_last_track = True
    s.closing_track = closer
    assert s.supporting_row_count == 1              # only the one vinyl row exists
    assert s.completion_supported is True


@pytest.mark.asyncio
async def test_single_vinyl_row_hybrid_full_play_credits():
    """R6-07 end-to-end: the full vinyl play credits instead of being suppressed."""
    tracker, writer, _ = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_hybrid_closer())
    await tracker._end_session()
    writer.increment_play_count.assert_called_once_with(700, 701)


def test_multi_vinyl_row_hybrid_still_needs_two_rows():
    """Control: a hybrid with TWO vinyl rows is NOT a single-track release — a
    closer-only needle drop stays suppressed (the carve-out must not fire just
    because CD rows were dropped from the count)."""
    tl = [
        TracklistEntry("A1", "Movement I"), TracklistEntry("B1", "Movement II"),
        TracklistEntry("CD1", "Bonus"),
    ]
    closer = TrackMetadata(
        title="Movement II", artist="Band", album="Two Movements (+CD)",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=710, discogs_instance_id=711, tracklist=tl,
    )
    assert closer.is_last_track is True             # B1 is the last vinyl row
    s = PlaySession()
    s.album_release_id = closer.discogs_release_id
    s.log_track(closer)
    s.potential_last_track = True
    s.closing_track = closer
    assert s.supporting_row_count == 1
    assert s.completion_supported is False          # two vinyl rows → needs the supporting one


# ---------------------------------------------------------------------------
# R6-06 (#271) — the love gate must not fire on a lone UNLATCHED (DB-tier) closer.
# ---------------------------------------------------------------------------

def _db_comp_closer():
    """'The Hit' resolved at the DATABASE tier (unowned compilation): a release
    id but NO instance id, so the session never latches ``album_release_id``."""
    return TrackMetadata(
        title="The Hit", artist="Sonic Youth", album="Greatest Hits",
        source=MetadataSource.DISCOGS_DATABASE,
        discogs_release_id=200, discogs_instance_id=None, tracklist=GH_TRACKLIST,
    )


@pytest.mark.asyncio
async def test_unlatched_db_closer_is_not_loved():
    """R6-06: an unowned / DB-resolved album whose closer identifies ONCE, with
    zero supporting rows, must NOT be Loved. The credit path already skips
    unlatched sessions, but the love branch reused ``completion_supported``,
    whose ``album_release_id is None`` escape hatch waved it through."""
    tracker, writer, lastfm = _tracker(with_lastfm=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(_db_comp_closer())
    assert tracker._session.album_release_id is None      # never latched (no instance id)
    await tracker._end_session()
    lastfm.love.assert_not_called()
    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_unlatched_db_full_side_is_still_loved():
    """Control: an unlatched DB album whose side genuinely completed (≥2 distinct
    resolved rows of the closer's release) is STILL loved — the tightening is
    about EVIDENCE, not about refusing all unlatched loves."""
    tracker, writer, lastfm = _tracker(with_lastfm=True)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)

    def db(title):
        return TrackMetadata(
            title=title, artist="Sonic Youth", album="Greatest Hits",
            source=MetadataSource.DISCOGS_DATABASE,
            discogs_release_id=200, discogs_instance_id=None, tracklist=GH_TRACKLIST,
        )

    await tracker.on_track_identified(db("Early Single"))   # row 0 (support)
    await tracker.on_track_identified(db("The Hit"))         # row 2 (closer)
    assert tracker._session.album_release_id is None
    await tracker._end_session()
    assert lastfm.love.call_count == 1
    assert lastfm.love.call_args[0][0].title == "The Hit"
