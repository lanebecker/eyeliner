"""Regression tests for #184 (R4:gap3-1) and #185 (R4:data-4) — split-detector
semantics.

#184: the B-4 carve-out deliberately returns a DISCOGS_DATABASE result
*uncached* when the collection tier fails transiently, so the next track
retries and resolves to the OWNED pressing — a different release id.  The
split detector's documented premise ("every track of an album resolves to
identical release IDs within a session") was revoked by that carve-out: one
transient blip on an album's first resolve caused a spurious split, and in
the closer-first case silently lost the Play Count.  Fix: the asymmetric
tier-upgrade rule — suppress the split only when the previous release id came
from a DATABASE-sourced track and the incoming track is COLLECTION-sourced
with the same resolver cache key (threaded onto TrackMetadata.resolve_key).
A genuine record swap still splits (different key), and the
collection→database direction still splits (unreachable in production — the
collection result is cached — so it stays conservative).

#185: re-dropping the SAME record within the 45s silence window produced
equal release ids, so no split fired and two complete playthroughs merged
into one credit.  Fix: a replay boundary — a same-release track arriving
AFTER potential_last_track armed (and not a consecutive re-identification of
the last logged track) splits exactly like a record change, crediting the
finished playthrough and starting a fresh session for the replay.
"""
from unittest.mock import MagicMock

import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, TrackMetadata
from src.tracking.listen_tracker import ListenTracker
from tests.test_listen_tracker import make_track, make_writer_mock

KEY = ("sonic youth", "sister")


def db_track(title, release_id=99999):
    """The same album resolved via the DATABASE tier (B-4 degraded resolve):
    different pressing id, no instance, same resolver cache key."""
    return make_track(
        title, release_id=release_id, instance_id=None,
        source=MetadataSource.DISCOGS_DATABASE, resolve_key=KEY,
    )


def coll_track(title):
    return make_track(title, resolve_key=KEY)


def _tracker():
    writer = make_writer_mock()
    return ListenTracker(writer), writer


# ---------------------------------------------------------------------------
# #184 — tier-upgrade suppression.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tier_upgrade_same_album_does_not_split():
    """DB-resolved track 1 (transient blip) then collection-resolved track 2
    of the SAME album: one session, no spurious split."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(db_track("Catholic Block"))
    session = tracker._session
    await tracker.on_track_identified(coll_track("Tuff Gnarl"))

    assert tracker._session is session            # no split
    assert session.last_release_id == 12345       # upgraded to the owned pressing
    assert session.album_release_id == 12345      # latch proceeded


@pytest.mark.asyncio
async def test_tier_upgrade_full_play_credits_once():
    """The #184 failure scenario end-to-end: degraded first resolve, then the
    rest of the album collection-resolved through the closer — exactly one
    credit, to the owned pressing."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(db_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Tuff Gnarl"))
    await tracker.on_track_identified(coll_track("Master-Dik"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_genuine_swap_from_degraded_album_still_splits():
    """A DB-resolved record followed by a DIFFERENT album's collection track
    (different resolver key) is a real swap — it must still split."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(db_track("Catholic Block"))
    session_a = tracker._session
    other = make_track("So What", release_id=555, instance_id=556,
                       resolve_key=("miles davis", "kind of blue"))
    await tracker.on_track_identified(other)

    assert tracker._session is not session_a      # split fired


@pytest.mark.asyncio
async def test_collection_to_db_direction_still_splits():
    """The rule is asymmetric: collection→database (unreachable in production,
    the collection result is cached) stays a split — conservative."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    session_a = tracker._session
    await tracker.on_track_identified(db_track("Tuff Gnarl"))

    assert tracker._session is not session_a


@pytest.mark.asyncio
async def test_keyless_tracks_never_suppress_a_split():
    """Tracks without a resolve_key (older construction paths, tests) keep
    the pre-#184 behaviour: differing ids split."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(
        make_track("Catholic Block", release_id=999, instance_id=None,
                   source=MetadataSource.DISCOGS_DATABASE)
    )
    session_a = tracker._session
    await tracker.on_track_identified(make_track("Tuff Gnarl"))

    assert tracker._session is not session_a


# ---------------------------------------------------------------------------
# #185 — the replay boundary.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_release_redrop_credits_both_playthroughs():
    """Two complete playthroughs of one record inside the silence window must
    credit twice: the replay boundary splits where equal ids used to merge."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Master-Dik"))     # closer arms
    await tracker.on_track_identified(coll_track("Catholic Block"))  # re-drop!
    await tracker.on_track_identified(coll_track("Master-Dik"))     # closer again
    await tracker.on_track_identified(coll_track("Catholic Block"))  # third drop
    await tracker.on_track_identified(coll_track("Master-Dik"))
    await tracker._end_session()

    assert writer.increment_play_count.call_count == 3


@pytest.mark.asyncio
async def test_consecutive_closer_reidentification_does_not_split():
    """The closer re-identified across overlapping chunks (identity-equal,
    consecutive) is NOT a replay boundary — one session, one credit."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Master-Dik"))
    session = tracker._session
    await tracker.on_track_identified(coll_track("Master-Dik"))     # same chunk overlap

    assert tracker._session is session      # no split fired (sharper than the
                                            # credit count, which the #182 gate
                                            # would mask on a spurious split)
    await tracker._end_session()
    writer.increment_play_count.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_fallback_track_after_closer_is_not_a_replay_boundary():
    """Release-less tracks keep never triggering splits; the #181 love-target
    scenario (closer then FALLBACK swap in one session) is preserved."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Master-Dik"))
    session = tracker._session
    swapped = TrackMetadata(
        title="Unrelated Song", artist="Different Band", album="Different Album",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None, discogs_instance_id=None, tracklist=[],
    )
    await tracker.on_track_identified(swapped)

    assert tracker._session is session            # no split
    await tracker._end_session()
    writer.increment_play_count.assert_called_once()


@pytest.mark.asyncio
async def test_stale_mid_album_reidentification_does_not_split_or_double_credit():
    """#184/#185 cold-review regression (the double-credit sequence): a stale
    NON-OPENER re-identification after the closer must not be a boundary —
    a looser trigger split there, and the still-playing closer's own re-id
    then re-armed the remainder into a SECOND credit for one playthrough."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Master-Dik"))     # closer arms
    session = tracker._session
    await tracker.on_track_identified(coll_track("Cotton Crown"))   # stale mid-album re-id
    assert tracker._session is session                              # no split
    await tracker.on_track_identified(coll_track("Master-Dik"))     # closer tail re-id
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_side_b_replay_remains_a_conservative_merge():
    """Accepted residual, pinned: re-dropping straight into a LATER track
    (side-B replay) is not an opener arrival — playthroughs merge and credit
    once, the documented pre-#185 undercount for that slice."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Master-Dik"))     # closer arms
    await tracker.on_track_identified(coll_track("Tuff Gnarl"))     # re-drop at B1 (not opener)
    await tracker.on_track_identified(coll_track("Master-Dik"))
    await tracker._end_session()

    writer.increment_play_count.assert_called_once_with(12345, 67890)


@pytest.mark.asyncio
async def test_collection_sourced_id_drift_with_same_key_still_splits():
    """#184's asymmetry conjunct, pinned directly: the suppression requires
    the PREVIOUS id to be DATABASE-sourced.  Collection-sourced id drift
    under one key (unreachable in production — collection results cache) must
    stay a conservative split."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    session_a = tracker._session
    drifted = make_track("Tuff Gnarl", release_id=777, instance_id=778, resolve_key=KEY)
    await tracker.on_track_identified(drifted)

    assert tracker._session is not session_a


# ---------------------------------------------------------------------------
# resolve_key threading (#184).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolver_threads_its_cache_key_onto_every_tier():
    from src.metadata.resolver import MetadataResolver

    class Raw:
        artist = "Sonic Youth"
        album = "Sister"
        title = "Schizophrenia"

    reader = MagicMock()
    calls = {"n": 0}

    async def run(fn, *args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("blip")          # collection tier: transient
        return {"release_id": 999, "album": "Sister", "tracklist": []}

    reader.run = run
    coverart = MagicMock()
    coverart.get_cover_art_url = MagicMock(return_value=None)
    resolver = MetadataResolver(reader, coverart=coverart)

    track = await resolver.resolve(Raw())
    assert track.source is MetadataSource.DISCOGS_DATABASE
    assert track.resolve_key == ("sonic youth", "sister")


@pytest.mark.asyncio
async def test_resolver_threads_the_key_on_cached_fallback_rebuilds():
    """The _from_cache FALLBACK branch must thread the key too."""
    from src.metadata.resolver import MetadataResolver

    class Raw:
        artist = "Sonic Youth"
        album = "Sister"
        title = "Schizophrenia"

    reader = MagicMock()

    async def run(fn, *args):
        return None                       # both Discogs tiers complete cleanly, no hit

    reader.run = run
    coverart = MagicMock()
    coverart.get_cover_art_url = MagicMock(return_value="http://cover")
    resolver = MetadataResolver(reader, coverart=coverart)

    first = await resolver.resolve(Raw())      # caches the FALLBACK entry
    second = await resolver.resolve(Raw())     # rebuilt from cache
    assert first.resolve_key == ("sonic youth", "sister")
    assert second.resolve_key == ("sonic youth", "sister")


@pytest.mark.asyncio
async def test_single_track_album_chunk_overlap_does_not_double_credit():
    """On a single-track release the opener IS the closer, so the consecutive
    re-identification guard is load-bearing: a chunk-overlap re-id must not
    be a replay boundary (which would split, credit via the carve-out, then
    re-arm the remainder into a second credit)."""
    from src.metadata.models import TracklistEntry
    tl = [TracklistEntry("A1", "One Long Piece")]
    def single():
        return make_track("One Long Piece", release_id=300, instance_id=301,
                          tracklist=tl, resolve_key=("some artist", "single lp"))
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(single())
    session = tracker._session
    await tracker.on_track_identified(single())     # chunk overlap re-id

    assert tracker._session is session              # no split
    await tracker._end_session()
    writer.increment_play_count.assert_called_once_with(300, 301)


@pytest.mark.asyncio
async def test_unarmed_session_opener_reid_is_not_a_boundary():
    """The armed flag is load-bearing: a stale non-consecutive re-id of the
    opener BEFORE the closer played must not split the session."""
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Tuff Gnarl"))
    session = tracker._session
    await tracker.on_track_identified(coll_track("Catholic Block"))  # stale opener re-id

    assert tracker._session is session              # no split (not armed)


# ---------------------------------------------------------------------------
# R6-05 (#270) — single-(vinyl-)row release: opener IS the closer, so a foreign
# mis-attribution mid-spin must NOT let the single's own re-id fire the replay
# boundary and double-credit one physical play.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_row_foreign_sandwich_does_not_double_credit():
    """R6-05 (HIGH): the #227 mechanism in SINGLES shape. On a one-row release
    the sole row is BOTH opener (row 0) and closer (is_last_track). A confirmed
    FALLBACK mis-attribution mid-spin breaks the consecutive-dedup chain, so the
    still-playing single's own re-identification is row 0 AND non-consecutive →
    the #185 boundary declared a re-drop → split → carve-out credit → the
    restarted session re-armed and credited AGAIN at SESSION_ENDED: ONE physical
    play, TWO Play Count increments. Unlike the chunk-overlap case this needs no
    consecutive re-id, so the consecutive guard can't catch it — the
    single-playable-row exemption must."""
    from src.metadata.models import TracklistEntry
    tl = [TracklistEntry("A1", "One Long Piece")]

    def single():
        return make_track("One Long Piece", release_id=300, instance_id=301,
                          tracklist=tl, resolve_key=("some artist", "single lp"))

    foreign = TrackMetadata(
        title="Someone Else's Hit", artist="Another Band", album="A Compilation",
        source=MetadataSource.FALLBACK,
        discogs_release_id=None, discogs_instance_id=None, tracklist=[],
    )
    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(single())     # arms (opener == closer)
    await tracker.on_track_identified(foreign)      # FALLBACK sandwich breaks the dedup chain
    await tracker.on_track_identified(single())     # single re-id: row 0, non-consecutive
    await tracker._end_session()

    assert writer.increment_play_count.call_count == 1, (
        f"one physical play double-credited: {writer.increment_play_count.call_count}"
    )
    writer.increment_play_count.assert_called_with(300, 301)


# ---------------------------------------------------------------------------
# R6-08 (#273) — the replay boundary anchors on the FIRST VINYL row, not row 0,
# so a hybrid with leading non-vinyl rows still splits a genuine re-drop.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hybrid_leading_nonvinyl_rows_still_fires_replay_boundary():
    """R6-08: R5-16(a) made only the CLOSER vinyl-aware. On a hybrid whose vinyl
    opener isn't tracklist row 0 (a leading CD/file row), the boundary — anchored
    on global_index==0 — never fired, so a genuine re-drop merged into one credit
    for two plays. Anchoring on the first VINYL row fixes it."""
    from src.metadata.models import TracklistEntry
    tl = [
        TracklistEntry("CD1", "Bonus Track"),   # leading non-vinyl row (global_index 0)
        TracklistEntry("A1", "Opener"),         # first VINYL row (global_index 1)
        TracklistEntry("A2", "Closer"),         # last vinyl row → the closer
    ]

    def t(title):
        return make_track(title, release_id=800, instance_id=801, tracklist=tl,
                          resolve_key=("band", "hybrid lp"))

    tracker, writer = _tracker()
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(t("Opener"))   # A1 (global_index 1)
    await tracker.on_track_identified(t("Closer"))   # A2 closer arms
    await tracker.on_track_identified(t("Opener"))   # genuine re-drop of the vinyl opener
    await tracker.on_track_identified(t("Closer"))
    await tracker._end_session()

    assert writer.increment_play_count.call_count == 2, (
        f"hybrid re-drop merged instead of splitting: {writer.increment_play_count.call_count}"
    )


# ---------------------------------------------------------------------------
# R6-10 (#275) — the #195 tripwire is softened for the benign same-turn
# SESSION_ENDED + MUSIC_STARTED interleave.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tripwire_no_session_is_softened_and_names_the_benign_interleave(caplog):
    """R6-10: recognition with no active session self-heals (a session is
    started, no play lost). The tripwire must NAME the benign
    SESSION_ENDED/MUSIC_STARTED interleave at INFO instead of crying
    'check the wiring' at WARNING on every occurrence."""
    import logging
    tracker, writer = _tracker()
    # No MUSIC_STARTED → _session is None when the track arrives (tripwire path).
    with caplog.at_level(logging.INFO):
        await tracker.on_track_identified(coll_track("Catholic Block"))
    assert tracker._session is not None                    # started (defense in depth)
    trip = [r for r in caplog.records if "R6-10" in r.message]
    assert len(trip) == 1                                  # names the benign case
    assert trip[0].levelno == logging.INFO                 # softened from WARNING


# ---------------------------------------------------------------------------
# R6-11 (#276) — a raising detached split-finalize is reported ONCE.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_split_finalize_raise_is_reported_once_not_twice(caplog, monkeypatch):
    """R6-11: a rare raise past the detached finalize's own containment is logged
    by its done-callback (_on_end_session_done); it must NOT also propagate
    through the shielded await in the recognition leg and be re-raised / logged a
    second time."""
    import asyncio
    import logging
    tracker, writer = _tracker()

    async def boom(session):
        raise RuntimeError("kaboom in finalize")

    monkeypatch.setattr(tracker, "_finalize_detached", boom)
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    await tracker.on_track_identified(coll_track("Catholic Block"))
    await tracker.on_track_identified(coll_track("Master-Dik"))     # closer arms
    other = make_track("So What", release_id=555, instance_id=556,
                       resolve_key=("miles davis", "kind of blue"))
    with caplog.at_level(logging.ERROR):
        # Album-change split → creditable detached finalize (which now raises).
        # Must NOT propagate out of on_track_identified.
        await tracker.on_track_identified(other)
        await asyncio.sleep(0)                                      # let the done-callback run
    errors = [r for r in caplog.records if "kaboom" in r.message]
    assert len(errors) == 1, f"expected one report, got {len(errors)}"
