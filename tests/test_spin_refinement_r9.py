"""R9 Wave 1 (#378–#384) — spin-memory refinement at real cadence.

Design LOCKED (Lane, 2026-08-13; R9-03 reworked to count-aware same day):
  • R9-01/#378  drop-on-genuine-credit: a DIFFERENT release landing a genuine
    credit drops other releases' credit entries AND scrobble tallies — a
    fast-swap evening (every gap under the 45s silence threshold) no longer
    suppresses a record's genuine second play or its scrobbles.  Ping-pong
    noise cannot trigger the drop (1-track swings never pass the completion
    gate).
  • R9-02/#379  a ZERO-track session (noise blip in the flip gap) touches the
    `_prev_unarmed` chain not at all — neither clearing nor overwriting.
  • R9-03/#380  count-aware scrobble cap: up to N scrobbles per key per spin,
    N = tracklist rows sharing the folded title (the originally-locked
    row-aware key was inert: B-5 resolves duplicate titles to the FIRST row).
  • R9-26/#384  SpinMemory owns swap/judge/record/drop.

Harness rules (R8-04): patch the clock as the tracker module's `time`
attribute (never global time.monotonic); stamp `started_at` explicitly.
"""
import types

import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, TrackMetadata, TracklistEntry
from src.tracking.spin_memory import SpinMemory
from tests.test_listen_tracker import make_tracker, make_track
from tests.test_flip_resume_r7_03 import _t as gap_track


class FakeClock:
    def __init__(self, start: float = 50_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    import src.tracking.listen_tracker as lt
    c = FakeClock()
    monkeypatch.setattr(lt, "time", types.SimpleNamespace(monotonic=c))
    return c


def start_session(tracker, clock):
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    tracker._session.started_at = clock.now


_B_TL = [TracklistEntry("A1", "B-Opener"), TracklistEntry("B1", "B-Closer")]


def rec_b(title):
    return TrackMetadata(
        title=title, artist="Band B", album="Record B",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=200, discogs_instance_id=201, tracklist=_B_TL,
    )


_DUP_TL = [
    TracklistEntry("A1", "Interlude"),
    TracklistEntry("A2", "Song X"),
    TracklistEntry("B1", "Interlude"),
]


def dup(title):
    return TrackMetadata(
        title=title, artist="Rapper", album="LP",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=900, discogs_instance_id=901, tracklist=_DUP_TL,
    )


def credits_for(writer, rid, iid):
    return [c.args for c in writer.increment_play_count.call_args_list
            if c.args == (rid, iid)]


# ---------------------------------------------------------------------------
# R9-01 (#378) — drop-on-genuine-credit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r9_01_fast_swap_aba_credits_both_plays_of_a(clock):
    """RED before R9-01: an A→B→A evening with every gap under 45s was ONE
    eternal spin — A's second genuine full play was suppressed.  A different
    release (B) landing a genuine credit now advances the spin."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(150)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(30)   # A armed
    await tracker.on_track_identified(rec_b("B-Opener"));            clock.advance(150)  # swap → A credit #1
    await tracker.on_track_identified(rec_b("B-Closer"));            clock.advance(30)   # B armed
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(150)  # swap → B credit
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(30)   # A armed AGAIN
    clock.advance(45)
    await tracker._end_session()

    assert len(credits_for(writer, 12345, 67890)) == 2, "A's 2nd genuine play must credit"
    assert len(credits_for(writer, 200, 201)) == 1


@pytest.mark.asyncio
async def test_r9_01_drop_frees_the_scrobble_tallies_too(clock):
    """The locked rule covers BOTH sinks: after B's genuine credit, A's tracks
    scrobble again on A's second play (checked BEFORE the terminal boundary)."""
    tracker, writer = make_tracker()
    a_track = make_track("Catholic Block")
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(150)
    tracker.record_scrobble(a_track)                                 # A1 scrobbled
    assert not tracker.should_scrobble(a_track)                      # spin dedup active
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(30)
    await tracker.on_track_identified(rec_b("B-Opener"));            clock.advance(150)  # A credits
    await tracker.on_track_identified(rec_b("B-Closer"));            clock.advance(30)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(30)   # B credits on this split

    assert tracker.should_scrobble(a_track), (
        "B's genuine credit must free A's scrobble tallies (the spin moved on)"
    )
    clock.advance(45)
    await tracker._end_session()


@pytest.mark.asyncio
async def test_r9_01_pingpong_noise_cannot_trigger_the_drop(clock):
    """R8-02 stays closed: foreign 1-TRACK swing sessions never pass the
    completion gate, never land a credit, never drop A's memory — one physical
    spin still credits exactly once through a full ping-pong."""
    tracker, writer = make_tracker()

    # A foreign release whose swing tracks are never closers.
    noise_tl = [TracklistEntry("A1", "X1"), TracklistEntry("A2", "X2"),
                TracklistEntry("B1", "X3-closer")]

    def noise(title):
        return TrackMetadata(
            title=title, artist="Other Band", album="Record Two",
            source=MetadataSource.DISCOGS_COLLECTION,
            discogs_release_id=555, discogs_instance_id=556, tracklist=noise_tl,
        )

    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(25)  # arms
    await tracker.on_track_identified(noise("X1"));                  clock.advance(25)  # split → credit #1
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(25)  # re-arm
    await tracker.on_track_identified(noise("X2"));                  clock.advance(25)  # split → suppressed
    clock.advance(45)
    await tracker._end_session()

    assert credits_for(writer, 12345, 67890) == [(12345, 67890)], (
        "the ping-pong regression must stay closed (R8-02)"
    )


def test_r9_01_spinmemory_drop_keeps_own_entries():
    """Unit: record_credit(rid) drops OTHER releases' entries (credits +
    scrobble tallies, FALLBACK None-release tallies included) but keeps rid's
    own — the ping-pong guard."""
    m = SpinMemory()
    m.record_credit(100, 1.0)
    m.record_scrobble(("t1", "a1", 100))
    m.record_scrobble(("t2", "a2", 555))
    m.record_scrobble(("t3", "a3", None))

    m.record_credit(200, 2.0)   # a DIFFERENT release lands a genuine credit

    assert not m.is_duplicate_credit(100), "other releases' credits drop"
    assert m.is_duplicate_credit(200)
    assert m.scrobble_count(("t1", "a1", 100)) == 0
    assert m.scrobble_count(("t3", "a3", None)) == 0, "FALLBACK tallies drop too"

    m.record_scrobble(("t9", "a9", 200))
    m.record_credit(200, 3.0)   # the SAME release credits again (replay path)
    assert m.scrobble_count(("t9", "a9", 200)) == 1, "own tallies survive"


# ---------------------------------------------------------------------------
# R9-02 (#379) — the blip guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r9_02_noise_blip_does_not_clobber_the_flip_chain(clock):
    """RED before R9-02: a one-chunk transient during the flip gap minted an
    EMPTY session whose unarmed end overwrote (first cut: cleared) the
    fragment — the flip-resume credit died.  An empty session now touches the
    chain not at all."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-One")); clock.advance(100)
    await tracker.on_track_identified(gap_track("B-Two")); clock.advance(100)
    clock.advance(45)
    await tracker._end_session()                       # fragment seeds the chain

    clock.advance(20)
    start_session(tracker, clock)                      # the blip: ZERO tracks
    clock.advance(45)
    await tracker._end_session()
    assert tracker._prev_unarmed is not None, "an empty session must not clear the chain"
    assert [t.title for t in tracker._prev_unarmed.identified_tracks] == ["B-One", "B-Two"]

    clock.advance(20)
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-Closer")); clock.advance(240)
    clock.advance(45)
    await tracker._end_session()

    assert credits_for(writer, 820, 821) == [(820, 821)], (
        "the split full play must still credit through the blip"
    )


@pytest.mark.asyncio
async def test_r9_02_nonempty_unarmed_sessions_still_overwrite_the_chain(clock):
    """Control: the R8-15 invariant is otherwise unchanged — a NON-empty
    unarmed terminal end still overwrites the chain."""
    tracker, _ = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-One"))
    clock.advance(45)
    await tracker._end_session()
    first = tracker._prev_unarmed

    clock.advance(20)
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-Two"))
    clock.advance(45)
    await tracker._end_session()

    assert tracker._prev_unarmed is not first, "a non-empty unarmed end overwrites"
    assert [t.title for t in tracker._prev_unarmed.identified_tracks] == ["B-Two"]


# ---------------------------------------------------------------------------
# R9-03 (#380) — the count-aware scrobble cap
# ---------------------------------------------------------------------------

def test_r9_03_duplicated_title_scrobbles_up_to_its_row_count():
    tracker, _ = make_tracker()
    x = dup("Interlude")                    # 2 rows share this title
    assert tracker._same_title_occurrences(x) == 2
    assert tracker.should_scrobble(x)
    tracker.record_scrobble(x)
    assert tracker.should_scrobble(x), "the album's SECOND 'Interlude' must scrobble"
    tracker.record_scrobble(x)
    assert not tracker.should_scrobble(x), "an N+1th commit is a swing-back"


def test_r9_03_unique_title_keeps_the_plain_dedup():
    tracker, _ = make_tracker()
    u = dup("Song X")                       # unique title → cap 1
    assert tracker._same_title_occurrences(u) == 1
    tracker.record_scrobble(u)
    assert not tracker.should_scrobble(u)


def test_r9_03_no_tracklist_caps_at_one():
    tracker, _ = make_tracker()
    f = TrackMetadata(title="Mystery", artist="Someone", album="?",
                      source=MetadataSource.FALLBACK)
    assert tracker._same_title_occurrences(f) == 1
    tracker.record_scrobble(f)
    assert not tracker.should_scrobble(f)


def test_r9_03_cap_uses_tier1_folded_equality_case_insensitively():
    """The cap counts rows by tier-1 folded equality (casefold + NFKC +
    whitespace collapse) — so pure CASE variants of a duplicated title collide,
    matching the scrobble key's own fold.  Punctuation/decoration variants are
    tier-1-DISTINCT and deliberately do NOT collide (the matcher's tier-2
    decoration strip requires a unique folded title, so genuine tracklist
    duplicates only ever arise as tier-1 equals — a punctuation-variant count
    that comes out low fails toward one fewer scrobble, the missed-over-phantom
    posture)."""
    tracker, _ = make_tracker()
    # Case-only duplicates DO collide (both fold to "interlude").
    tl_case = [TracklistEntry("A1", "Interlude"), TracklistEntry("B1", "INTERLUDE")]
    x = TrackMetadata(title="interlude", artist="Band", album="LP",
                      source=MetadataSource.DISCOGS_COLLECTION,
                      discogs_release_id=77, discogs_instance_id=78, tracklist=tl_case)
    assert tracker._same_title_occurrences(x) == 2

    # Punctuation variants are tier-1-distinct → count is by exact fold only.
    tl_punct = [TracklistEntry("A1", "Intro!"), TracklistEntry("B1", "Intro")]
    y = TrackMetadata(title="Intro", artist="Band", album="LP",
                      source=MetadataSource.DISCOGS_COLLECTION,
                      discogs_release_id=79, discogs_instance_id=80, tracklist=tl_punct)
    assert tracker._same_title_occurrences(y) == 1


@pytest.mark.asyncio
async def test_r9_03_replay_boundary_resets_tallies_not_caps(clock):
    """A #185 re-drop resets the release's tallies — the duplicated title gets
    its full cap again on the replay."""
    tracker, _ = make_tracker()
    x = dup("Interlude")
    start_session(tracker, clock)
    await tracker.on_track_identified(dup("Interlude")); clock.advance(25)
    tracker.record_scrobble(x)
    tracker.record_scrobble(x)
    assert not tracker.should_scrobble(x)
    await tracker.on_track_identified(dup("Song X"));    clock.advance(25)
    await tracker.on_track_identified(dup("Interlude"))  # opener after... (no arm: not closer)
    # Simulate the replay-boundary clear directly (the split fires only after
    # arming; the SpinMemory contract is what's under test here).
    tracker._spin.clear_release_scrobbles(900)
    assert tracker.should_scrobble(x), "a replay restores the full cap"
    clock.advance(45)
    await tracker._end_session()
