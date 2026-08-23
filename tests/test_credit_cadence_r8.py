"""R8 Wave 1 (#345–#351) — credit correctness at the pipeline's REAL cadence.

Every wall-clock-guarded credit behavior in this file is exercised on a
realistic timeline: 15s chunks / 10s hop (one confirmation cycle ≥ ~20–35s,
modeled at 25s), 45s session-end silence.  This is the R8-04 (#347) harness:
both R7 Wave-1 flagship fixes shipped green against compressed-time tests
(every event in the same millisecond) and inverted at exactly this cadence —
R8-01 (flip-resume inert: the 300s window, anchored at the prior session's
START, always lost to fragment + gap + tail + silence) and R8-02 (the 45s
credited-memory window expiring between two ~25s confirmation cycles, letting
one physical spin double-credit).

The fixes under test (Lane, 2026-08-12, LOCKED; R9-08 wording corrected —
the memory lives in SpinMemory since R9-26 and is SWAPPED at the boundary
EVENT itself, not "cleared when the finalize completes"):
  • R8-02/#346  silence-boundary credited-memory: a per-spin SpinMemory,
    swapped at each genuine-silence boundary — timing-independent by
    construction.
  • R8-01/#345  flip-resume window bounds the GAP (`new.started_at -
    prev.ended_at`), with `ended_at` stamped at detach.
  • R8-16/#350  the #195 forced end (SESSION_ENDED_FORCED) credits but is NOT
    a spin boundary — the memory survives, so a locked groove can't re-credit.
  • R8-09/#348  the scrobble sink shares the spin memory (via
    should_scrobble/record_scrobble; commit-service wiring tested in
    test_main_wiring / here at tracker level).
  • R8-17/#351  drain() finalizes a live ARMED session behind the same gates.
  • R8-15/#349  an unarmed split-detach ends the `_prev_unarmed` chain.

HARNESS RULES (learned the hard way — see the R8 report):
  1. Patch the clock as `src.tracking.listen_tracker.time` with a
     SimpleNamespace — patching `time.monotonic` GLOBALLY freezes asyncio's
     loop clock and hangs every await/wait_for.
  2. `PlaySession.started_at`'s default_factory bound the REAL time.monotonic
     at class definition — stamp `started_at` explicitly after MUSIC_STARTED.
"""
import asyncio
import types

import pytest

from src.audio.silence import AudioEvent
from src.metadata.models import MetadataSource, TrackMetadata, TracklistEntry
from tests.test_listen_tracker import make_tracker, make_track
from tests.test_flip_resume_r7_03 import _t as gap_track


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 10_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Module-scoped monotonic clock for the tracker ONLY (rule 1 above)."""
    import src.tracking.listen_tracker as lt
    c = FakeClock()
    monkeypatch.setattr(lt, "time", types.SimpleNamespace(monotonic=c))
    return c


def start_session(tracker, clock):
    """MUSIC_STARTED + explicit started_at stamp (rule 2 above)."""
    tracker.on_silence_event(AudioEvent.MUSIC_STARTED)
    tracker._session.started_at = clock.now


async def settle():
    """Let fire-and-forget session-end tasks run (loop-clock independent)."""
    for _ in range(20):
        await asyncio.sleep(0)


# A foreign release whose tracks never arm a completion — the ping-pong's
# swing partner (same shape as test_credit_memory_r7_02).
_R2_TL = [
    TracklistEntry("A1", "X1"),
    TracklistEntry("A2", "X2"),
    TracklistEntry("B1", "X3-closer"),
]


def foreign(title):
    return TrackMetadata(
        title=title, artist="Other Band", album="Record Two",
        source=MetadataSource.DISCOGS_COLLECTION,
        discogs_release_id=555, discogs_instance_id=556, tracklist=_R2_TL,
    )


def r1_credits(writer):
    return [c.args for c in writer.increment_play_count.call_args_list
            if c.args == (12345, 67890)]


# ---------------------------------------------------------------------------
# R8-02 (#346): the ping-pong at real cadence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_02_pingpong_at_real_cadence_credits_once(clock):
    """RED before R8-02: at ~25s per confirmation cycle the gap between credit
    #1 and the next split's finalize exceeded the old 45s window, so one
    physical spin credited twice.  The silence-boundary memory has no window to
    expire."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(25)  # arms
    await tracker.on_track_identified(foreign("X1"));                clock.advance(25)  # split → credit #1
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(25)  # re-arm
    await tracker.on_track_identified(foreign("X2"));                clock.advance(25)  # split → suppressed
    await tracker._end_session()

    assert r1_credits(writer) == [(12345, 67890)], (
        f"one physical spin was double-credited at real cadence: {r1_credits(writer)}"
    )


@pytest.mark.asyncio
async def test_r8_02_slow_pingpong_still_suppressed(clock):
    """The old guard's worst case: a full FIVE MINUTES between the two splits
    (missed chunks, slow swings).  No wall-clock window exists to expire —
    still exactly one credit, because no silence boundary intervened."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(30)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(30)
    await tracker.on_track_identified(foreign("X1"));                clock.advance(300)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(300)
    await tracker.on_track_identified(foreign("X2"))
    await tracker._end_session()

    assert r1_credits(writer) == [(12345, 67890)]


@pytest.mark.asyncio
async def test_r8_02_genuine_second_spin_credits_after_silence_boundary(clock):
    """The memory is swapped at the genuine-silence boundary event (R9-08
    wording): a real second spin (needle lift, >45s silence, fresh drop)
    credits again."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(45)
    await tracker._end_session()          # terminal silence → credit #1 + boundary

    clock.advance(120)                    # the record sat on the platter a while
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(45)
    await tracker._end_session()          # a genuine second spin

    assert len(r1_credits(writer)) == 2, "a real second spin must credit again"


@pytest.mark.asyncio
async def test_r8_02_replay_boundary_exemption_survives_at_cadence(clock):
    """#185 re-drop inside the silence window: the replay session is a REAL
    second spin and stays exempt from the spin memory (regression guard for the
    R7-02 exemption under the new semantics)."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(25)  # arms
    # Re-drop: the OPENER arrives after the closer armed — a #185 replay split.
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(45)
    await tracker._end_session()

    assert len(r1_credits(writer)) == 2, (
        "a genuine #185 re-drop is a second physical spin and must credit"
    )


# ---------------------------------------------------------------------------
# R8-01 (#345): flip-resume at real timelines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_01_flip_resume_fires_at_realistic_timeline(clock):
    """RED before R8-01: 200s fragment (B1+B2), 45s silence, 60s sleeve-cleaning
    gap, 240s closer, 45s trailing silence.  Only the GAP (60s ≤ 300s) may
    matter.  The timeline deliberately makes BOTH wrong anchors exceed the
    window so their mutants die: prior-START → new-START is 200+45+60 = 305s
    (kills a `prev.started_at` far-edge revert), and prior-END → finalize-NOW
    is 60+240+45 = 345s (kills a `time.monotonic()` near-edge revert).  Only
    `new.started_at - prev.ended_at` = 60s passes — the fix's exact quantity."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-One")); clock.advance(100)
    await tracker.on_track_identified(gap_track("B-Two")); clock.advance(100)
    clock.advance(45)
    await tracker._end_session()                       # unarmed end (the gap begins)

    clock.advance(60)                                  # one flip / cleaning pause
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-Closer")); clock.advance(240)
    clock.advance(45)
    await tracker._end_session()

    assert [c.args for c in writer.increment_play_count.call_args_list] == [(820, 821)]


@pytest.mark.asyncio
async def test_r8_01_long_gap_still_suppressed_at_realistic_timeline(clock):
    """Control at the same timeline: a 301s gap is a separate listening — the
    tail-only session stays suppressed (missed-over-phantom)."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-One")); clock.advance(90)
    await tracker.on_track_identified(gap_track("B-Two")); clock.advance(90)
    clock.advance(45)
    await tracker._end_session()

    clock.advance(301)                                 # too long to be one flip
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-Closer")); clock.advance(240)
    clock.advance(45)
    await tracker._end_session()

    writer.increment_play_count.assert_not_called()


# ---------------------------------------------------------------------------
# R8-16 (#350): forced ends are not spin boundaries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_16_locked_groove_hourly_forced_ends_credit_once(clock):
    """RED before R8-16: hour-long continuous music forces a session end
    (SESSION_ENDED_FORCED); the still-identified locked-groove closer re-arms a
    fresh session and, an hour later, the next forced end re-credited.  The
    spin memory survives forced ends, so the phantom hourly credit is
    suppressed until a REAL silence boundary."""
    tl = [TracklistEntry("A1", "Drone"), TracklistEntry("B1", "Groove")]

    def g(title):
        return TrackMetadata(
            title=title, artist="D", album="G",
            source=MetadataSource.DISCOGS_COLLECTION,
            discogs_release_id=700, discogs_instance_id=701, tracklist=tl,
        )

    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(g("Drone"));  clock.advance(1800)
    await tracker.on_track_identified(g("Groove")); clock.advance(1800)   # closer arms
    tracker.on_silence_event(AudioEvent.SESSION_ENDED_FORCED)             # hour 1
    await settle()

    start_session(tracker, clock)                     # groove still spinning
    await tracker.on_track_identified(g("Groove")); clock.advance(3600)
    tracker.on_silence_event(AudioEvent.SESSION_ENDED_FORCED)             # hour 2
    await settle()

    credits = [c.args for c in writer.increment_play_count.call_args_list]
    assert credits == [(700, 701)], f"locked groove re-credited hourly: {credits}"


@pytest.mark.asyncio
async def test_r8_16_real_silence_after_forced_end_reopens_crediting(clock):
    """After the needle finally lifts (a genuine SESSION_ENDED), the boundary
    clears the memory and a later real spin credits again."""
    tl = [TracklistEntry("A1", "Drone"), TracklistEntry("B1", "Groove")]

    def g(title):
        return TrackMetadata(
            title=title, artist="D", album="G",
            source=MetadataSource.DISCOGS_COLLECTION,
            discogs_release_id=700, discogs_instance_id=701, tracklist=tl,
        )

    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(g("Drone"));  clock.advance(1800)
    await tracker.on_track_identified(g("Groove")); clock.advance(1800)
    tracker.on_silence_event(AudioEvent.SESSION_ENDED_FORCED)             # forced
    await settle()

    start_session(tracker, clock)                     # groove still going…
    await tracker.on_track_identified(g("Groove")); clock.advance(600)
    tracker.on_silence_event(AudioEvent.SESSION_ENDED)                    # needle lifts
    await settle()                                    # suppressed + BOUNDARY clears

    clock.advance(3600)                               # next evening: a real spin
    start_session(tracker, clock)
    await tracker.on_track_identified(g("Drone"));  clock.advance(1200)
    await tracker.on_track_identified(g("Groove")); clock.advance(1200)
    await tracker._end_session()

    credits = [c.args for c in writer.increment_play_count.call_args_list]
    assert credits == [(700, 701), (700, 701)], (
        "after a real silence boundary a genuine spin must credit again"
    )


# ---------------------------------------------------------------------------
# R8-09 (#348): the scrobble sink shares the spin memory (tracker level)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_09_scrobble_memory_suppresses_within_spin_and_clears_at_boundary(clock):
    tracker, _ = make_tracker()
    t = make_track("Catholic Block")
    start_session(tracker, clock)

    assert tracker.should_scrobble(t)
    tracker.record_scrobble(t)
    clock.advance(60)                                  # swing-back a minute later
    assert not tracker.should_scrobble(t), (
        "a swing-back re-commit within one spin must not re-scrobble"
    )

    await tracker._end_session()                       # terminal silence boundary
    assert tracker.should_scrobble(t), "the boundary must clear the scrobble memory"


@pytest.mark.asyncio
async def test_r8_09_forced_end_does_not_clear_scrobble_memory(clock):
    tracker, _ = make_tracker()
    t = make_track("Catholic Block")
    start_session(tracker, clock)
    tracker.record_scrobble(t)

    tracker.on_silence_event(AudioEvent.SESSION_ENDED_FORCED)
    await settle()

    assert not tracker.should_scrobble(t), (
        "a forced end is not a spin boundary — the scrobble memory must survive"
    )


@pytest.mark.asyncio
async def test_r8_09_replay_boundary_clears_the_redropped_releases_keys(clock):
    """A #185 re-drop REPLAYS the record — its tracks scrobble again.

    R9 update: in this scenario the #185 split ALSO lands a genuine credit for
    the re-dropped release (its side completed), so R9-01's
    drop-on-genuine-credit additionally frees the unrelated release's tallies
    (the spin moved on to a completed record).  The pure per-release #185
    clear — where NO credit intervenes — is pinned directly on SpinMemory in
    tests/test_spin_refinement_r9.py (test_r9_03_replay_boundary_resets_...
    and test_r9_01_spinmemory_drop_keeps_own_entries)."""
    tracker, _ = make_tracker()
    r1 = make_track("Catholic Block")                  # release 12345 (opener)
    other = foreign("X1")                              # release 555
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(25)  # arms
    tracker.record_scrobble(r1)
    tracker.record_scrobble(other)

    # The opener arrives after the closer armed → #185 replay split (which here
    # credits release 12345, whose side is covered).
    await tracker.on_track_identified(make_track("Catholic Block"))

    assert tracker.should_scrobble(r1), (
        "a re-dropped release's tracks legitimately scrobble again"
    )
    assert tracker.should_scrobble(other), (
        "R9-01: 12345's genuine credit advances the spin, freeing 555's tally"
    )
    await tracker._end_session()


@pytest.mark.asyncio
async def test_r8_09_commit_service_consults_the_scrobble_memory(clock):
    """Pins the WIRING (the R7-14 lesson: an unpinned driver is a surviving
    mutant): TrackCommitService.commit must consult should_scrobble/
    record_scrobble, so a swing-back re-commit of the same physical play
    dispatches exactly ONE Last.fm scrobble.

    R10-09 (#422): the scrobble is now handed to a ScrobbleDispatcher, so this
    drives a REAL dispatcher (with a mock client returning DELIVERED) and drains
    it before asserting — the dedup still lives in commit(), so a swing-back
    still yields exactly one delivered scrobble for the physical play."""
    from unittest.mock import MagicMock
    from src.state.player_state import PlayerState
    from src.app.track_commit_service import TrackCommitService
    from src.audio.recognizer import RawRecognitionResult
    from src.tracking.lastfm_client import ScrobbleResult
    from src.tracking.scrobble_dispatcher import ScrobbleDispatcher

    tracker, _ = make_tracker()
    state = PlayerState()
    lastfm = MagicMock()
    lastfm.enabled = True
    lastfm.love_on_completion = False
    lastfm.scrobble_result = MagicMock(return_value=ScrobbleResult.DELIVERED)

    dispatcher = ScrobbleDispatcher(lastfm, backoff=())
    dispatcher.start()

    class Resolver:
        async def resolve(self, raw):
            if raw.artist == "Other Band":
                return foreign(raw.title)
            return make_track(raw.title)

    svc = TrackCommitService(
        state=state, resolver=Resolver(), tracker=tracker,
        scrobble_dispatcher=dispatcher,
    )

    async def commit(title, artist="Sonic Youth"):
        await svc.commit(
            RawRecognitionResult(title=title, artist=artist, album="x"),
            state.session_epoch,
        )

    start_session(tracker, clock)
    await commit("Catholic Block");        clock.advance(25)
    await commit("Master-Dik");            clock.advance(25)   # arms
    await commit("X1", "Other Band");      clock.advance(25)   # foreign swing
    await commit("Catholic Block");        clock.advance(25)   # swing-back re-commit
    await tracker._end_session()
    await dispatcher.drain()   # flush the queue so delivered scrobbles are recorded

    cb = [c for c in lastfm.scrobble_result.call_args_list
          if getattr(c.args[0], "title", "") == "Catholic Block"]
    assert len(cb) == 1, (
        f"swing-back re-commit scrobbled the same physical play "
        f"{len(cb)} times (want 1)"
    )


# ---------------------------------------------------------------------------
# R8-17 (#351): finalize-at-drain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_17_armed_session_is_finalized_at_drain(clock):
    """RED before R8-17: closer played, coverage complete, systemctl stop lands
    inside the 45s silence window — the credit was silently discarded.  Drain
    now finalizes the armed session behind the same gates."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(10)

    await tracker.drain()

    assert r1_credits(writer) == [(12345, 67890)]
    assert tracker._session is None, "the armed session must be detached by drain"


@pytest.mark.asyncio
async def test_r8_17_drain_respects_the_completion_gate(clock):
    """Drain uses the SAME gates: a closer-only session on a MULTI-row closing
    side (1/3 coverage — the mis-attributed-single shape; the default fixture's
    single-row closer would legitimately credit via the R6-05 carve-out) is
    still suppressed at shutdown — no phantom credit minted by stopping the
    service."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-Closer"))   # 1 of 3 side-B rows

    await tracker.drain()

    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_r8_17_drain_discards_an_unarmed_session(clock):
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block"))  # no closer

    await tracker.drain()

    writer.increment_play_count.assert_not_called()


@pytest.mark.asyncio
async def test_r8_17_drain_does_not_double_credit_an_already_ended_session(clock):
    """A SESSION_ENDED already finalized the spin; drain right after must not
    find anything to credit (idempotent with the normal path)."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(45)
    await tracker._end_session()

    await tracker.drain()

    assert len(r1_credits(writer)) == 1


# ---------------------------------------------------------------------------
# R8-16 cold-review catches (F1/F2/F3) — regression pins
# ---------------------------------------------------------------------------

def test_f1_forced_end_clears_player_state_and_bumps_epoch():
    """Cold-review F1 (HIGH, caught pre-commit): SESSION_ENDED_FORCED must run
    the SAME player-state half as SESSION_ENDED — card cleared, IDLE, epoch
    bumped (B-1) so an in-flight commit for the force-ended session is
    discarded.  The first cut updated only the tracker consumer and stranded
    the card on screen with a stale epoch."""
    from main import apply_state_silence_effect
    from src.state.player_state import PlayerState, PlayerStatus

    state = PlayerState()
    state.set_track(make_track("Groove"))
    epoch_before = state.session_epoch

    apply_state_silence_effect(AudioEvent.SESSION_ENDED_FORCED, state)

    assert state.status is PlayerStatus.IDLE
    assert state.current_track is None
    assert state.session_epoch == epoch_before + 1, (
        "the forced end must bump the session epoch (B-1) like a genuine end"
    )


def test_f2_detector_rearms_silence_timer_after_forced_end():
    """Cold-review F2: after a forced end, continued NON-music (a stuck input
    decaying into the hysteresis dead band — never re-crossing the music enter
    threshold) must still produce a GENUINE SESSION_ENDED one silence window
    later, so the tracker's spin memory gets its boundary.  The first cut
    latched the detector closed (`_silence_since=None; _session_ended=True`)
    and no event could ever fire again."""
    import src.audio.silence as silence_mod
    from tests.test_recognition_gate import _Clock, make_audio_config, _chunk

    clock = _Clock()
    real = silence_mod.time
    silence_mod.time = types.SimpleNamespace(monotonic=clock)
    try:
        cfg = make_audio_config(silence_threshold_rms=0.01)
        det = silence_mod.SilenceDetector(cfg)
        events = []
        det.on_event(events.append)

        det.process(_chunk(0.5), cfg.sample_rate)               # music starts
        clock.advance(silence_mod._MAX_MUSIC_SECONDS)
        det.process(_chunk(0.5), cfg.sample_rate)               # forced end fires
        assert AudioEvent.SESSION_ENDED_FORCED in events
        assert AudioEvent.SESSION_ENDED not in events

        # The input decays to true silence WITHOUT re-crossing the enter
        # threshold — the timer re-armed at the forced end must deliver a
        # genuine boundary one window later.
        clock.advance(cfg.session_end_silence_seconds + 1)
        det.tick()
        assert AudioEvent.SESSION_ENDED in events, (
            "continued non-music after a forced end must still yield a genuine "
            "SESSION_ENDED (the tracker's spin boundary)"
        )
    finally:
        silence_mod.time = real


@pytest.mark.asyncio
async def test_f2_boundary_with_no_session_still_clears_spin_memory(clock):
    """Cold-review F2, tracker half: a genuine SESSION_ENDED arriving with NO
    live session (the forced-end-then-decay shape) must still clear the spin
    memory — otherwise the dead spin's credited release suppresses the next
    genuine play."""
    tracker, writer = make_tracker()
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(1800)
    tracker.on_silence_event(AudioEvent.SESSION_ENDED_FORCED)       # credit #1
    await settle()
    assert len(r1_credits(writer)) == 1

    # Input decays; the re-armed detector delivers a genuine SESSION_ENDED with
    # no session to end.
    clock.advance(50)
    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    await settle()

    clock.advance(3600)                                             # next evening
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block")); clock.advance(25)
    await tracker.on_track_identified(make_track("Master-Dik"));     clock.advance(45)
    await tracker._end_session()

    assert len(r1_credits(writer)) == 2, (
        "a no-session genuine boundary must clear the spin memory so the next "
        "real play credits"
    )


@pytest.mark.asyncio
async def test_f3_late_boundary_finalize_does_not_wipe_next_spins_keys(clock):
    """Cold-review F3: the spin-memory swap happens SYNCHRONOUSLY at the
    boundary, not when the (possibly minutes-slow, Retry-After-honouring)
    terminal finalize completes — so keys the NEXT spin records while the old
    finalize is still in flight must survive it."""
    tracker, _ = make_tracker()
    gate = asyncio.Event()
    real_finalize = tracker._finalize_session

    async def slow_finalize(session, spin=None):   # R9-26: matches _finalize_session
        await gate.wait()                       # park the terminal finalize
        await real_finalize(session, spin)

    tracker._finalize_session = slow_finalize

    # Spin 1 ends; its terminal finalize parks on the gate.
    start_session(tracker, clock)
    await tracker.on_track_identified(make_track("Catholic Block"))
    tracker.on_silence_event(AudioEvent.SESSION_ENDED)
    await settle()                              # the end task ran up to the gate

    # Spin 2 begins and scrobbles a track while spin 1's finalize is in flight.
    clock.advance(60)
    start_session(tracker, clock)
    t = make_track("Catholic Block")
    assert tracker.should_scrobble(t), "the boundary swap must already have run"
    tracker.record_scrobble(t)

    gate.set()                                  # spin 1's finalize completes late
    await settle()

    assert not tracker.should_scrobble(t), (
        "spin 2's freshly-recorded scrobble key was wiped by spin 1's late "
        "boundary finalize — the R8-09 double-scrobble reopened"
    )
    await tracker._end_session()


# ---------------------------------------------------------------------------
# R8-15 (#349): the _prev_unarmed chain ends at an unarmed split-detach
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_15_unarmed_split_detach_ends_the_flip_resume_chain(clock):
    """RED before R8-15: the #166 short-circuit bypassed _finalize_session, so
    a STALE prior-unarmed session survived an intervening unarmed split,
    falsifying the documented invariant.  The chain now ends there (a split is
    attribution noise, not a flip), so the later armed tail stays suppressed."""
    tracker, writer = make_tracker()
    # Terminal unarmed end seeds the chain (B1+B2 of the gapped LP).
    start_session(tracker, clock)
    await tracker.on_track_identified(gap_track("B-One")); clock.advance(90)
    await tracker.on_track_identified(gap_track("B-Two")); clock.advance(90)
    clock.advance(45)
    await tracker._end_session()
    assert tracker._prev_unarmed is not None

    # An unarmed foreign fragment split-detaches (the #166 short-circuit).
    clock.advance(30)
    start_session(tracker, clock)
    await tracker.on_track_identified(foreign("X1")); clock.advance(25)
    await tracker.on_track_identified(gap_track("B-Closer"))   # split: X1 detached unarmed

    assert tracker._prev_unarmed is None, (
        "an unarmed split-detach must end the flip-resume chain (R8-15)"
    )
    clock.advance(45)
    await tracker._end_session()
    # …and the tail-only closer session therefore stays suppressed.
    assert [c.args for c in writer.increment_play_count.call_args_list] == []
