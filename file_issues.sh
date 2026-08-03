#!/usr/bin/env bash
# file_issues.sh — files all 88 Round-3 audit findings into 5 milestones on GitHub.
# Run from anywhere with `gh` authenticated (repo scope). Idempotent-ish: labels and
# milestones use guards, but RE-RUNNING creates DUPLICATE issues — run once. The
# finding-id -> issue-number map is written to r9-issue-map.tsv as issues are created.
set -uo pipefail
REPO=lanebecker/vinyl-now-playing
MAP=r9-issue-map.tsv
: > "$MAP"
echo "Filing Round-3 audit issues into $REPO"

# ---- labels (idempotent) ----------------------------------------------------
gh label create code-review --color ededed --force --repo "$REPO" >/dev/null 2>&1 || gh label edit code-review --color ededed --repo "$REPO" >/dev/null 2>&1 || true
gh label create bug --color d73a4a --force --repo "$REPO" >/dev/null 2>&1 || gh label edit bug --color d73a4a --repo "$REPO" >/dev/null 2>&1 || true
gh label create security --color b60205 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit security --color b60205 --repo "$REPO" >/dev/null 2>&1 || true
gh label create architecture --color 0e8a16 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit architecture --color 0e8a16 --repo "$REPO" >/dev/null 2>&1 || true
gh label create performance --color fbca04 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit performance --color fbca04 --repo "$REPO" >/dev/null 2>&1 || true
gh label create testing --color 1d76db --force --repo "$REPO" >/dev/null 2>&1 || gh label edit testing --color 1d76db --repo "$REPO" >/dev/null 2>&1 || true
gh label create tech-debt --color c5def5 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit tech-debt --color c5def5 --repo "$REPO" >/dev/null 2>&1 || true
gh label create data-integrity --color 5319e7 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit data-integrity --color 5319e7 --repo "$REPO" >/dev/null 2>&1 || true
gh label create severity:critical --color b60205 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit severity:critical --color b60205 --repo "$REPO" >/dev/null 2>&1 || true
gh label create severity:high --color d93f0b --force --repo "$REPO" >/dev/null 2>&1 || gh label edit severity:high --color d93f0b --repo "$REPO" >/dev/null 2>&1 || true
gh label create severity:medium --color fbca04 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit severity:medium --color fbca04 --repo "$REPO" >/dev/null 2>&1 || true
gh label create severity:low --color 0e8a16 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit severity:low --color 0e8a16 --repo "$REPO" >/dev/null 2>&1 || true
gh label create severity:nit --color c2e0c6 --force --repo "$REPO" >/dev/null 2>&1 || gh label edit severity:nit --color c2e0c6 --repo "$REPO" >/dev/null 2>&1 || true

# ---- milestones (guarded; "already exists" is fine) --------------------------
gh api repos/"$REPO"/milestones -f title='Wave 1 — Collection data integrity' -f state=open -f description='Every issue here ends in a wrong value written to real, unrecoverable Discogs collection data. The only irreversible wave — must land before the Pi is powered on.' >/dev/null 2>&1 && echo "  milestone created: Wave 1 — Collection data integrity" || echo "  milestone exists (ok): Wave 1 — Collection data integrity"
gh api repos/"$REPO"/milestones -f title='Wave 2 — Hardware bring-up blockers' -f state=open -f description='Failure modes that end in a black screen or a silently dead appliance nobody is watching. Fix before hardware bring-up.' >/dev/null 2>&1 && echo "  milestone created: Wave 2 — Hardware bring-up blockers" || echo "  milestone exists (ok): Wave 2 — Hardware bring-up blockers"
gh api repos/"$REPO"/milestones -f title='Wave 3 — Untrusted input & credential hardening' -f state=open -f description='Gaps around the (sound) S-7 SSRF boundary, plus the guard paths the suite never executes.' >/dev/null 2>&1 && echo "  milestone created: Wave 3 — Untrusted input & credential hardening" || echo "  milestone exists (ok): Wave 3 — Untrusted input & credential hardening"
gh api repos/"$REPO"/milestones -f title='Wave 4 — Display correctness & contrast' -f state=open -f description='Display correctness and the contrast guarantee — including the unreadable-album-title fix.' >/dev/null 2>&1 && echo "  milestone created: Wave 4 — Display correctness & contrast" || echo "  milestone exists (ok): Wave 4 — Display correctness & contrast"
gh api repos/"$REPO"/milestones -f title='Wave 5 — Architecture, docs & test debt' -f state=open -f description='Architecture, documentation and test-suite debt. No direct operator impact.' >/dev/null 2>&1 && echo "  milestone created: Wave 5 — Architecture, docs & test debt" || echo "  milestone exists (ok): Wave 5 — Architecture, docs & test debt"

# ---- helper: create one issue, capture its number into the map --------------
emit() {  # $1=id  $2=title  $3=labels  $4=milestone   (body on stdin)
  local id="$1" url num
  url=$(gh issue create --repo "$REPO" --title "$2" --label "$3" --milestone "$4" --body-file -) || {
    echo "  !! FAILED to create $id" >&2; return 1; }
  num=$(printf "%s" "$url" | grep -oE "[0-9]+$")
  printf "%s\t%s\t%s\n" "$id" "$num" "$url" | tee -a "$MAP"
}

echo "== Wave 1 — Collection data integrity =="
emit META-1 '[META-1] A failed READ of the Play Count field makes the absolute write reset it to 1' code-review,bug,data-integrity,severity:critical 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** CRITICAL · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/writer.py:67`

### What is wrong

_get_field_value returns None for every failure mode (non-200 at writer.py:201, any exception at writer.py:220, instance-not-found at writer.py:215), and increment_play_count cannot distinguish that from 'field is blank', so it sets current_count=0 and POSTs the ABSOLUTE value 1. Any 5xx, persistent 429, connection reset or malformed body on the read therefore overwrites the operator's accumulated Play Count with 1. The write is a separate POST issued milliseconds later, so a failed read does not stop it.

### How it fails

Record has Play Count 47. Side completes; the GET to /users/{u}/collection/releases/111 returns 503 (or 429 twice, or raises ConnectionError); the POST then succeeds and writes {'value': '1'}. increment_play_count returns True, so listen_tracker.py:189 logs '✅ Discogs Play Count incremented successfully.' Ten years of counts gone, silently, with a success message.

### Suggested fix

Make _get_field_value return a three-state result (value / confirmed-absent / read-failed) and abort the write on read-failed; only a confirmed-blank field may start from 0.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-1**._
ISSUE_BODY_EOF_92f1
emit CONC-1 '[CONC-1] Shutdown cancels the fire-and-forget _end_session task mid-write: Play Count incremented, Last Played never updated, silently and unretryably' code-review,bug,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/tracking/listen_tracker.py:86`

### What is wrong

The end-of-session Discogs credit runs as a fire-and-forget task that no shutdown path awaits: run_pipeline (main.py:106) waits only on the three pipeline legs, and asyncio.run (main.py:170) then cancels every remaining task. Because _finalize_session latches session.credited = True before the first await (listen_tracker.py:177), a cancellation between the two Discogs writes leaves the collection permanently half-updated with nothing to retry it and no log line.

### How it fails

Side B finishes, the needle lifts, SESSION_ENDED fires and _end_session starts writing to Discogs; within the next second or two the owner presses ESC or systemd sends SIGTERM. increment_play_count completes in its executor thread (asyncio.run calls shutdown_default_executor), but the awaiting coroutine is already cancelled, so update_last_played and the Last.fm love never run. Discogs is left with Play Count +1 and a stale Last Played, and credited=True means no path retries it.

### Suggested fix

Expose the tracker's _bg_tasks at the composition root and drain them with a bounded asyncio.wait(..., timeout=N) in run_pipeline's finally before capture.stop(); shielding the write pair alone still races the loop closing.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CONC-1**._
ISSUE_BODY_EOF_92f1
emit META-2 '[META-2] A non-integer stored Play Count is also clobbered with an absolute 1' code-review,bug,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/writer.py:71`

### What is wrong

On a ValueError the handler logs a warning, sets current_count=0 and proceeds to write the absolute value 1 over whatever was there. The Play Count field is free text on the Discogs side, so an operator who maintained it by hand before this appliance existed can hold values like '1,024' or '47 plays'.

### How it fails

Field contains '1,024'. Side completes. Log says "contains non-integer value '1,024'; treating as 0" and the POST writes {'value': '1'}, destroying the human-maintained value.

### Suggested fix

On a parse failure, log and return False without writing — a value you cannot read is not a value you may replace.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-2**._
ISSUE_BODY_EOF_92f1
emit META-4 '[META-4] A duplicated position string makes an early track claim is_last_track, arming a phantom play-count credit' code-review,bug,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/models.py:177`

### What is wrong

is_last_track is `current.position == tracklist[-1].position` — a raw string equality, not a row-identity check. Any tracklist where an earlier row repeats the final row's position arms potential_last_track at that early track, and potential_last_track plus a latched release id is the entire gate on the collection write (listen_tracker.py:173). The reader filters only headings and blank positions (reader.py:147-151), so duplicates pass straight through unvalidated.

### How it fails

Community-edited tracklist [A1 Intro, A2 Song Two, B1 Song Three, A1 Hidden Track]. The operator plays two minutes of side A; the FIRST track reports is_last_track=True; the needle is lifted; on session end the record is credited a full play and Last Played is stamped. This is the phantom-last-track class the docstring at models.py:110-116 claims was designed out.

### Suggested fix

Compare row identity (index of the matched entry == len(tracklist)-1) rather than the position string, and have the reader reject or flag tracklists containing duplicate positions before they gate a write.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-4**._
ISSUE_BODY_EOF_92f1
emit MUT-1 '[MUT-1] _get_collection_fields() never executes: the field-name -> field-ID map that selects WHICH Discogs custom field is written is unpinned' code-review,testing,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/writer.py:170`

### What is wrong

Six mutants survive in _get_collection_fields (L170 guard -> True, L174 URL strings, L179 'name'/'id'/'fields' keys), and coverage proves lines 173-182 never execute at all. Every writer test pre-seeds writer._collection_fields, so the one function deciding which numeric custom-field ID the play-count POST targets has zero test executions.

### How it fails

A refactor keys the map on the wrong JSON member or hits the wrong endpoint. Suite stays green. On the Pi the app writes incrementing integers into the owner's Notes or Media Condition field on his real Discogs collection, unattended, for hours.

### Suggested fix

Add a test that leaves _collection_fields=None, stubs the transport with a realistic {"fields":[{"id":3,"name":"Play Count"}]} body, and asserts the GET URL, the resulting map, and that the POST URL contains /fields/3.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-1**._
ISSUE_BODY_EOF_92f1
emit PCONC-1 '[PCONC-1] The B-1 session-epoch guard is sampled one await too late, so a stale commit double-credits a release' code-review,bug,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** CONC.prev-run.md (reconciled — closes CRIT-11)

**Location:** `src/app/track_commit_service.py:74`

### What is wrong

commit_epoch is sampled at line 74, inside commit() — but the audio it commits has already waited in a maxsize=5 queue AND been through a network recognition call. The epoch is never bound to the audio, so the guard covers only "the session ended during resolve()" and misses "the session ended while this chunk was queued or being recognised". The stale commit then spawns a fresh PlaySession carrying the previous release id, which the next record's album-split finalises — crediting that release twice.

### How it fails

Recognition lag exceeds session_end_silence_seconds (reachable via PCONC-2, since backend.recognize() has no timeout and shazamio defaults to attempts=20/max_timeout=60). Reproduced end-to-end against real components: increments=[1001, 1001]. The display also resurrects the finished track, and silence.py:65 only clears _session_ended when music RESUMES, so the wrong card persists indefinitely.

### Suggested fix

Stamp the epoch onto the audio chunk at enqueue time and carry it through to commit(), comparing against that instead of re-sampling; give PlaySession a credited-release guard so a fresh session cannot re-credit a release the previous one already credited.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **PCONC-1**._
ISSUE_BODY_EOF_92f1
emit REC-3 '[REC-3] Two consecutive Shazam responses with empty title and artist confirm as a real track' code-review,bug,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/recognizer.py:123`

**Severity moved during adversarial review:** MEDIUM → HIGH

### What is wrong

_parse_shazam returns a RawRecognitionResult whenever `track` is merely truthy, so a response with no title/subtitle yields ("", "", ""). _same_track treats two of those as identical, so the confirmation gate commits an empty track.

### How it fails

Shazam returns {"track": {"key": "1"}} on two consecutive chunks: _pending_count reaches confirmation_required, on_confirmed fires, TrackCommitService resolves and calls set_track / on_track_identified / scrobble with an empty artist and title, and the journal logs `Track confirmed:  — `.

### Suggested fix

In _parse_shazam, return None unless both title and artist are non-empty after stripping — a Shazam match with no title is not a match.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **REC-3**._
ISSUE_BODY_EOF_92f1
emit SEC-1 '[SEC-1] Empty/short Shazam album+artist strings make the collection fuzzy-match pick an arbitrary owned record, and that record is the Play Count / Last Played write target' code-review,security,data-integrity,severity:high 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** security · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/reader.py:102`

**Absorbs:** META-3, VNEW-2 — same root cause, found independently by another lens.

### What is wrong

search_collection strategy 2 matches with a bare `in` substring test on both artist and album. Shazam sets album to "" whenever the response has no album metadata section (recognizer.py:114) and resolver.py:127 passes it through unguarded, so `album_lower in title` degenerates to always-true and the first (most recently added) collection entry whose artist merely contains the Shazam artist is returned — along with its instance_id, which is what the Discogs collection writer POSTs to.

### How it fails

Owner plays Kind Of Blue; Shazam returns title='So What', artist='Miles Davis', album='' (common for singles/comps/live cuts). The reader returns Doo-Bop (a different owned Miles Davis record, newest in the collection). The display shows the wrong album/cover/tracklist and, on side completion, Play Count is incremented and Last Played stamped on Doo-Bop in the owner's real Discogs collection. Only a debug-level log records it, and main.py runs at INFO.

### Suggested fix

Return None from strategy 2 when album or artist is empty/implausibly short, and replace the bare `in` test with normalised equality or a scored similarity threshold; log strategy-2 matches at INFO so a wrong write leaves a breadcrumb.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SEC-1**._
ISSUE_BODY_EOF_92f1
emit CRIT-4 '[CRIT-4] The systemd unit orders on network.target with no time-sync.target -- the actual root cause of STAB-2, META-5 and VNEW-1, cited by no finding' code-review,bug,data-integrity,severity:medium 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** no · **Source:** completeness critic

**Location:** `docs/pi-setup-guide.md:342`

### What is wrong

The documented unit is 'After=network.target graphical.target'. network.target only means the network stack was configured, not that it is up (that is network-online.target), and nothing orders after time-sync.target or systemd-time-wait-sync.service. Three separate findings (STAB-2, META-5, verifier VNEW-1) describe the unset-clock write corruption and all three cite src/metadata/discogs/writer.py:133 -- the symptom. The cause is one missing After= line in a doc no finding references. The same line explains why a boot with no network reaches the Discogs code paths at all. A reviewer handed those three findings would patch writer.py and add an NTP gate in Python, when the correct fix is two words in the unit file.

### Suggested fix

Change to 'After=network-online.target time-sync.target graphical.target' plus 'Wants=network-online.target', and document setting the timezone via raspi-config (covers VNEW-1 too).

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-4**._
ISSUE_BODY_EOF_92f1
emit LB-1 '[LB-1] A tracker exception after set_raw strands the track permanently: never tracked, never scrobbled, never retried' code-review,bug,data-integrity,severity:medium 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** proved during orchestrator triage

**Location:** `src/app/track_commit_service.py:86`

### What is wrong

set_raw() advances the dedup key at line 86, BEFORE the await of tracker.on_track_identified() at line 87. The B-11 ordering invariant protects against RESOLVER failure but not TRACKER failure. If on_track_identified raises, the exception propagates past the scrobble to run()'s except handler, while current_raw has already advanced — so the dedup at recognizer.py:252 treats the track as already playing and never re-attempts it.

### How it fails

A Discogs write error or an unexpected shape in the album-split path raises; that track is silently never recorded and never scrobbled, and nothing retries it. Reproduced: tracker recorded=False, scrobbled=False, current_raw advanced=True, dedup suppresses retry=True.

### Suggested fix

Advance set_raw only after the full commit succeeds, or catch tracker failures separately so the scrobble still runs and the dedup key is not poisoned by a partial commit.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **LB-1**._
ISSUE_BODY_EOF_92f1
emit MUT-15 '[MUT-15] The duplicate-position fallback in from_tracklist is populated but never consumed by any test' code-review,testing,data-integrity,severity:medium 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/models.py:175`

### What is wrong

Four mutants survive on lines 172/174 ('is' -> 'is not', condition -> True, condition -> False on both). The comment says this exists to be robust 'if two rows ever share a position string' - a real shape for Discogs tracklists edited by strangers - and no test supplies one.

### How it fails

A Discogs release with two rows both at position 'A1' resolves global_index to the wrong row; prev/next neighbours on the Museum Card point at the wrong tracks, and any regression in the fallback selection is invisible.

### Suggested fix

Add a tracklist with two entries sharing position 'A1' and different titles; assert the title-matching row wins, and that the fallback index is used when neither title matches.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-15**._
ISSUE_BODY_EOF_92f1
emit STAB-2 '[STAB-2] Wall clock trusted with no NTP gate — a pre-NTP boot writes a wrong Last Played date into the operator'"'"'s real Discogs collection' code-review,bug,data-integrity,severity:medium 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/writer.py:133`

**Severity moved during adversarial review:** HIGH → MEDIUM

**Absorbs:** META-5, VNEW-1 — same root cause, found independently by another lens.

### What is wrong

update_last_played writes date.today().isoformat() as an absolute set with no clock-sanity check, and TrackCommitService uses int(time.time()) for the scrobble timestamp (track_commit_service.py:72). The Pi 4 has no RTC; fake-hwclock restores the last shutdown time at boot and the clock only corrects once NTP settles. The documented systemd unit orders on network.target, not time-sync.target, and main.py checks nothing.

### How it fails

Pi powers on with no network (or NTP not yet settled), a side plays through, and the writer POSTs {'value': '1970-01-01'} — or a stale fake-hwclock date — over the correct Last Played value in the owner's real collection, returning True and logging success. Scrobbles submitted in the same window are silently dropped by Last.fm (>14 days stale / future) or land at the wrong point in listening history, with LastFmClient.scrobble reporting success either way.

### Suggested fix

Gate both writes on clock trustworthiness (e.g. /run/systemd/timesync/synchronized, or time.time() above a compiled-in build-date floor); skip the Last Played write and the scrobble with one WARNING rather than writing a wrong value, and add After=time-sync.target + systemd-time-wait-sync to the documented unit.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-2**._
ISSUE_BODY_EOF_92f1
emit CONC-6 '[CONC-6] The B-1 epoch guard covers only the first of the commit path'"'"'s two awaits; on_track_identified is unguarded and can resurrect a dead session' code-review,bug,data-integrity,severity:low 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/app/track_commit_service.py:87`

### What is wrong

commit() captures the epoch at :74 and re-checks it after resolve() at :76, but the second await — await self.tracker.on_track_identified(metadata) at :87 — has no re-check; the check at :99 gates only the scrobble. Since that await can park on the contended _lifecycle_lock for seconds to a minute (CONC-2), a SESSION_ENDED landing inside it lets the tracker create a brand-new session at listen_tracker.py:257-258 for audio that already stopped.

### How it fails

A Discogs write from the previous session is in flight holding the lock; a track confirms and commit blocks on on_track_identified; 45s of silence elapses so SESSION_ENDED fires, clear() bumps the epoch and _end_session nulls the session; on_track_identified then acquires the lock, sees _session is None, and starts a phantom session containing a stale track. It persists until music restarts and stops again, and FALLBACK tracks without a release_id cannot trigger the split that would clean it up.

### Suggested fix

Pass commit_epoch into on_track_identified (or re-check it right after the lock is acquired) and have the tracker drop a stale-epoch track instead of starting a session for it.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CONC-6**._
ISSUE_BODY_EOF_92f1
emit META-10 '[META-10] Persistent 429 loses the play silently; Retry-After is clamped below what Discogs asks for' code-review,bug,data-integrity,severity:low 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/metadata/discogs/transport.py:132`

### What is wrong

One retry, wait clamped to 10s (transport.py:40), while Discogs commonly answers Retry-After: 60 — so the retry lands inside the same throttling window. There is no handling for 'the retry also failed': on the Play Count POST the caller logs the status, returns False, and the session is already credited and destroyed, so the play is lost with no retry anywhere. On the field-value GET the same condition feeds META-1.

### How it fails

Discogs throttles with Retry-After: 60. The POST retries at 10s, gets 429 again, logs 'Discogs field update returned 429.' and returns False. The completed side is never credited and nothing will ever retry it.

### Suggested fix

Distinguish 'still rate-limited after the retry' from other failures and surface it as a distinct, loud outcome; consider deferring the credit rather than dropping it.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-10**._
ISSUE_BODY_EOF_92f1
emit META-7 '[META-7] Partial write leaves Play Count incremented and Last Played stale, with no retry' code-review,bug,data-integrity,severity:low 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/tracking/listen_tracker.py:193`

### What is wrong

The two collection writes are independent back-to-back POSTs; session.credited is latched before the first (listen_tracker.py:177) and the session object is destroyed immediately after, so a Last Played failure after a successful increment is never retried or reconciled, and is reported only as a second unrelated warning line.

### How it fails

Play Count POST succeeds; the immediately following Last Played POST gets a 429 whose single retry also fails. The collection now shows an incremented count with a stale date, and nothing will correct it until the record is played again.

### Suggested fix

Log a single explicit divergence warning when the two writes disagree, so the operator can spot the inconsistent item.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-7**._
ISSUE_BODY_EOF_92f1
emit SEC-7 '[SEC-7] username interpolated into Discogs write URLs without percent-encoding' code-review,security,data-integrity,severity:nit 'Wave 1 — Collection data integrity' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** security · **Reproduced:** no · **Source:** lens

**Location:** `src/metadata/discogs/writer.py:82`

### What is wrong

writer.py:82, :137, :198 and reader.py:232 build request paths with f"{_API_BASE}/users/{self.username}/collection/...". Every numeric id is hardened through _as_id (transport.py:44-60), but username is not encoded. It is operator-authored, so this is robustness rather than an attack.

### How it fails

A username containing /, ? or # (a typo, or a copy-paste with a trailing fragment) silently reshapes the request path — the POST targets a different endpoint — instead of failing loudly at the boundary the way _as_id makes ids fail.

### Suggested fix

Wrap with urllib.parse.quote(self.username, safe="") at each interpolation site, or validate the username once in DiscogsConfig.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SEC-7**._
ISSUE_BODY_EOF_92f1

echo "== Wave 2 — Hardware bring-up blockers =="
emit CONC-5 '[CONC-5] A PortAudio stream that goes quiet after starting is never detected — run() parks on blocks.get() forever with no error and no restart' code-review,bug,severity:high 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/audio/capture.py:213`

**Severity moved during adversarial review:** MEDIUM → HIGH

### What is wrong

Blocks reach run() only via the PortAudio callback's call_soon_threadsafe. If the device disappears or the callback raises (CFFI aborts the stream), nothing raises in the consuming coroutine, so the except Exception retry-with-a-fresh-stream path at :221 is never reached and await blocks.get() waits forever. The ticker docstring (:152-158) and the module docstring (:22-23) both assume errors surface as exceptions in run(), which holds only for stream construction.

### How it fails

The bus-powered UCA222 browns out or is unplugged mid-album on the Pi. PortAudio stops invoking the callback; capture is permanently dead while the process stays alive. The B-6 silence ticker keeps firing SESSION_ENDED so the display drops to IDLE and stays there indefinitely — no ERROR state, no recovery, nothing in the journal.

### Suggested fix

Wrap the consumer in asyncio.wait_for(blocks.get(), timeout=N * _BLOCK_SECONDS) and on timeout tear down and rebuild the stream via the existing retry path; also try/except the callback body so it logs instead of aborting from CFFI.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CONC-5**._
ISSUE_BODY_EOF_92f1
emit CRIT-1 '[CRIT-1] config.py validates types but never value domains; a plausible hand-edit crashes the capture leg outside its retry guard and systemd turns it into a permanent crash loop' code-review,bug,severity:high 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** completeness critic

**Location:** `src/config.py:263`

### What is wrong

AppConfig.from_dict performs type-only validation. There is no range check, no cross-field check and no component-level validation anywhere downstream. audio.overlap_seconds >= audio.chunk_seconds (or sample_rate 0) yields hop_frames <= 0, and ChunkAssembler is constructed at capture.py:199 INSIDE the outer try/finally but BEFORE the inner try/except Exception at :200-223, so the ValueError escapes run() entirely, faults the capture task, is re-raised by run_pipeline and exits the process with a raw traceback. The documented unit sets Restart=on-failure and RestartSec=10, so the result is a permanent 10-second crash loop and a black screen the owner discovers hours later. It is also a SPEC failure: config.py's docstring calls itself 'the single source of truth' and main.py:115 promises 'one friendly startup failure here, not a KeyError deep in a constructor'. Value-domain errors get neither. Nine lenses read config.py and four listed it; none tested its value domain. AUD came closest and scoped it to a negative overlap only, never connecting it to the config layer or to systemd.

### Suggested fix

Add a __post_init__ value-domain check to each section dataclass (sample_rate > 0, chunk_seconds > 0, 0 <= overlap_seconds < chunk_seconds, width/height > 0, poll_interval_seconds > 0, confirmation_required >= 1, error_after_misses >= 1) accumulated into the same errors list, so a bad value is reported as one friendly ConfigError alongside type errors.

### Note

The inner try is at capture.py:202 with its except at 221-225; the structural claim (ChunkAssembler constructed at :199, before the inner try) is verified correct.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-1**._
ISSUE_BODY_EOF_92f1
emit CRIT-2 '[CRIT-2] A documented recognition.backend value raises from RecognitionLoop.__init__ at main.py:135, outside main()'"'"'s only try/except, into a systemd Restart=on-failure loop' code-review,bug,severity:high 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** completeness critic

**Location:** `main.py:135`

**Absorbs:** ARCH-2 — same root cause, found independently by another lens.

### What is wrong

recognizer.py:180 calls self._init_backend() from __init__, and _init_backend raises ValueError for anything other than 'shazamio'. config.example.yaml and the docs advertise acrcloud/audd. main.py's only try/except (lines 116-120) catches ConfigError around load_config and nothing else, so RecognitionLoop construction at main.py:135 produces a raw traceback and a non-zero exit. ARCH-2 found the crash and rated it MEDIUM as docs/config drift; nobody rated it against the unit file. With Restart=on-failure and RestartSec=10 this is not a startup error message, it is a permanent black screen that restarts every ten seconds forever. Same class as CRIT-1 and the same one-line fix covers both.

### Suggested fix

Validate recognition.backend against the implemented set inside RecognitionConfig.from_dict so it lands in the aggregated ConfigError, or widen main()'s try to cover component construction and log a friendly message before sys.exit(1).

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-2**._
ISSUE_BODY_EOF_92f1
emit REC-1 '[REC-1] A miss wipes the pending candidate, so a track Shazam identifies every other chunk never confirms and the display latches to ERROR' code-review,bug,severity:high 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/recognizer.py:247`

### What is wrong

_handle_result zeroes _pending_result and _pending_count on every None result, so confirmation requires N results with no intervening miss. On vinyl (surface noise, worn side) a hit/miss/hit/miss pattern is the normal failure mode, and in it the same track is recognised repeatedly but never committed while _register_miss drives the player to ERROR.

### How it fails

Twelve chunks alternating [Track A, None] x6: Shazam identifies Track A six times, zero commits occur, and PlayerStatus ends at ERROR ("NO MATCH FOUND") — no now-playing card, no Discogs play count, no Last.fm scrobble. Recovery needs a manual needle reposition.

### Suggested fix

Do not discard the pending candidate on a miss — decay _pending_count by one instead of zeroing it, or count "N matching of the last M"; at minimum prevent _register_miss from reaching ERROR while a pending candidate is alive.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **REC-1**._
ISSUE_BODY_EOF_92f1
emit STAB-1 '[STAB-1] One un-decodable/unreadable cached cover becomes an unbounded download+unlink+log loop at 8.7 Hz' code-review,bug,severity:high 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:1465`

**Absorbs:** ARCH-1, DISP-4 — same root cause, found independently by another lens.

### What is wrong

_render_now_playing calls _load_cover on every frame (renderer.py:542) and re-arms the loop each frame (renderer.py:562), and _load_cover's except clause treats ANY exception as 'cached file is corrupt': it unlinks the file and spawns a fresh download, with no failure counter, no backoff, no negative cache and no spawn dedup. The re-download re-lands the same bytes, bumps _cover_version and sets _dirty, so the cycle is self-sustaining. Crucially the handler also fires for non-file errors — pygame.image.load(...).convert() raises 'cannot convert without video mode' on an HDMI/X event — so a transient display glitch deletes a perfectly good cover and starts hammering.

### How it fails

A PLAYING track whose cached cover cannot be decoded (SD read error on a worn card, lost video mode after HDMI hotplug, or an SDL_image codec gap) leaves the appliance issuing ~31,000 HTTPS GETs/hour, ~31,000 SD unlinks/hour, ~9.4 GB/hour of SD writes at a typical 300 KB cover, and ~31,000 journald WARNING lines/hour, indefinitely, with nobody watching.

### Suggested fix

Add a bounded per-URL failure counter: unlink+refetch at most once, then mark the URL bad and stop retrying until state changes; do not delete the file on pygame.error for a missing video mode, and dedupe prefetch spawns against in-flight downloads for the same URL.

### Note

renderer.py:562 is guarded by `if not self.reduced_motion` — the loop is re-armed per frame only when reduced motion is off.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-1**._
ISSUE_BODY_EOF_92f1
emit CONC-2 '[CONC-2] _lifecycle_lock is held across up to three network round trips, stalling the entire recognition pipeline behind it (measured 3.01s; ~120s worst case)' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/tracking/listen_tracker.py:130`

### What is wrong

_end_session holds _lifecycle_lock for the whole of _finalize_session, which awaits three executor-dispatched HTTP calls (increment_play_count :182, update_last_played :194, lastfm.love :232). on_track_identified's first statement is that same lock (:256), and it is awaited inline by TrackCommitService.commit -> RecognitionLoop._handle_result -> RecognitionLoop.run, so the lock hold time is one-for-one dead time for track recognition.

### How it fails

Side A ends and its Play Count write is in flight over a slow domestic link. The owner drops side B within 20s. Every confirmed track from side B blocks in on_track_identified waiting for the lock; RecognitionLoop stops draining _audio_queue (maxsize 5) and drops the oldest chunks, so the opening minute of side B is never identified and never displayed.

### Suggested fix

_finalize_session already operates on a detached local session (its own docstring says so at :152-157), so have _end_session_locked return the detached session and finalize it after releasing the lock; the lock then guards only the null-and-restart that B-2 actually needed.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CONC-2**._
ISSUE_BODY_EOF_92f1
emit CONC-4 '[CONC-4] except asyncio.TimeoutError: pass swallows genuine TimeoutErrors from recognize()/commit() with zero logging (asyncio.TimeoutError IS builtins.TimeoutError on 3.11)' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/recognizer.py:222`

### What is wrong

wait_for's timeout applies only to the queue get(), but backend.recognize() and _handle_result() sit inside the same try. On Python 3.11 asyncio.TimeoutError is the builtin TimeoutError (== socket.timeout, an OSError subclass), so any bare TimeoutError raised anywhere under the commit path is classified as 'No audio queued — fine' and produces no log output, while the sibling except Exception two lines down would have logged it and backed off.

### How it fails

A raw socket timeout (or aiohttp ServerTimeoutError, which subclasses asyncio.TimeoutError) propagates out of the resolve/commit path during _handle_result. The loop treats it as an idle poll, logs nothing, and immediately retries — hot-spinning on a failing network with no breadcrumb in a journal nobody is watching.

### Suggested fix

Narrow the try so only the wait_for is covered by the TimeoutError handler, moving recognize()/_handle_result() outside it.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CONC-4**._
ISSUE_BODY_EOF_92f1
emit CRIT-3 '[CRIT-3] Process exit is gated on the default ThreadPoolExecutor, not on task cancellation, so SIGTERM can hang past systemd'"'"'s timeout and convert CONC-1 into a SIGKILL' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** completeness critic

**Location:** `main.py:170`

### What is wrong

run_pipeline cancels the three legs and its finally calls capture.stop() and display.stop(); main() then returns. But asyncio.run() on Python 3.11 (verified 3.11.15 here; Pi OS Bookworm ships 3.11) then awaits loop.shutdown_default_executor(), which calls executor.shutdown(wait=True) with no timeout. Every blocking call in this app is on that default executor: DiscogsHttp.request (15s timeout plus up to a 10s time.sleep per 429 retry), writer.increment_play_count, writer.update_last_played, lastfm.scrobble, lastfm.love, and cover downloads which have per-read timeouts but no overall deadline (SEC-4). The documented unit sets no TimeoutStopSec, so systemd SIGKILLs at the 90s default -- which is exactly the moment CONC-1's 'Play Count incremented, Last Played never updated' becomes permanent and unlogged. SEC-4 identified the parked executor worker and CONC audited shutdown; neither joined them, and the verifier lowered SEC-4 MEDIUM to LOW without noticing it gates process exit.

### Suggested fix

Own the executor: create a bounded ThreadPoolExecutor at the composition root, pass it explicitly to every run_in_executor, and shut it down with cancel_futures=True in run_pipeline's finally before main() returns. Also set TimeoutStopSec in the documented unit.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-3**._
ISSUE_BODY_EOF_92f1
emit CRIT-5 '[CRIT-5] SESSION_ENDED'"'"'s two effects live in one Signal listener with log-and-continue: a tracker fault silently strands the display, and even on the happy path the two halves are non-atomic across the lifecycle lock' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** no · **Source:** completeness critic

**Location:** `main.py:66`

### What is wrong

handle_silence_event is a SINGLE Signal listener performing two independent effects in a fixed order -- tracker.on_silence_event(event) at :66 then state.clear() at :71. Signal.emit (src/util/signal.py:36-40) is log-and-continue, so A-11's guarantee protects listeners from each other but does not protect the second half of one listener from the first: if tracker.on_silence_event raises, state.clear() never runs, the session epoch never bumps, the B-1 guard is defeated and the now-playing card is stranded on screen, with only a log.error in a journal nobody reads. I could not construct a realistic raise today, so that half is latent fragility, not a live bug. The reachable half is the ordering: state.clear() is synchronous while _end_session(expected=target) is a create_task that must then acquire _lifecycle_lock, which CONC-2 measured held for 3.01s and argued up to ~120s. For that whole interval the display reports IDLE while the tracker still owns an un-finalised session, and on_track_identified (listen_tracker.py:256-258) will _start_session() or log a track into whichever session wins the lock. CONC owns task lifecycle and META owns write correctness; this interval belongs to neither.

### Suggested fix

Split into two separately registered listeners so Signal's log-and-continue actually applies between them, and clear the player state BEFORE scheduling the tracker end so the epoch bump and the session detach are not separated by a lock wait.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-5**._
ISSUE_BODY_EOF_92f1
emit PCONC-2 '[PCONC-2] No timeout around backend.recognize(); the only wait_for guards the queue get, not the network call' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** no · **Source:** CONC.prev-run.md (reconciled)

**Location:** `src/audio/recognizer.py:219`

### What is wrong

asyncio.wait_for wraps _audio_queue.get() but not the recognition call that follows it. shazamio's default retry policy is attempts=20 with max_timeout=60, so a single degraded call can occupy the loop far longer than a chunk interval. This is the mechanism that makes PCONC-1 reachable in practice.

### How it fails

Flaky Pi wifi makes one recognize() call run for minutes; the audio queue saturates and the consumer works on audio 40-50s old, which is the lag PCONC-1 requires.

### Suggested fix

Wrap backend.recognize() in its own asyncio.wait_for with a timeout derived from the chunk interval, and pin the backend retry policy explicitly rather than inheriting the library default.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **PCONC-2**._
ISSUE_BODY_EOF_92f1
emit REC-2 '[REC-2] A Shazam response with "title": null makes _same_track raise, silently stalling the loop forever with no miss counted' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/recognizer.py:124`

### What is wrong

_parse_shazam uses track.get("title", ""), which returns None (not "") when the key is present with a JSON null. That result parses cleanly, then _same_track calls a.title.strip() and raises AttributeError inside _handle_result.

### How it fails

Shazam returns {"track": {"title": null, "subtitle": "X"}}. The AttributeError escapes to run()'s broad handler (log.error + 2s sleep), so _miss_count never increments, ERROR is never surfaced, and the display sits on the IDENTIFYING spinner indefinitely while the journal fills once per chunk.

### Suggested fix

Coerce in the parser with track.get("title") or "" (same for subtitle), and make _same_track null-safe via (a.title or "").strip().lower().

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **REC-2**._
ISSUE_BODY_EOF_92f1
emit SIL-1 '[SIL-1] session_end_silence_seconds: 45 actually fires at 60-70s because the silence timer is armed at chunk-processing time' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/silence.py:71`

### What is wrong

_silence_since is set to time.monotonic() when a below-threshold chunk is processed, but a chunk is a 15s trailing window emitted every 10s, so the first silent chunk lands 15-25s after the needle actually lifted. The configured 45s is measured from that late point.

### How it fails

Needle lifts at t=60.25s with the documented 45s setting; SESSION_ENDED fires at t=130.0s — a 69.75s latency, 55% longer than config.example.yaml:14-15 promises. The Discogs play-count credit and return to IDLE are correspondingly late, and the setting cannot be tuned meaningfully.

### Suggested fix

Arm the timer at now - chunk_seconds (where the silence genuinely began), or document the real formula (session_end + chunk .. + chunk + hop) in config.example.yaml and the SESSION_ENDED docstring.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SIL-1**._
ISSUE_BODY_EOF_92f1
emit STAB-4 '[STAB-4] Crash loop re-pages the entire Discogs collection on every restart; paging loop has no absolute page cap' code-review,bug,severity:medium 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/metadata/discogs/reader.py:209`

### What is wrong

_get_collection_index walks the whole collection at per_page=100 in an unbounded 'while True' and caches it in memory only, so it is rebuilt from zero on every process start. The documented unit uses Restart=on-failure with RestartSec=10. The loop's only exit is 'page >= pagination.get("pages", 1)' with no absolute page ceiling.

### How it fails

A crash loop that reaches the lazy index build costs ceil(collection/100) Discogs GETs every ~10s; a 1,000-item collection means 60 requests/minute — exactly the authenticated rate limit — so the appliance sits permanently in 429 territory, each 429 also sleeping up to 10s in a shared executor worker.

### Suggested fix

Persist the collection index to disk with a TTL, cap the page loop absolutely, and add StartLimitIntervalSec/StartLimitBurst to the unit so systemd stops a hot restart loop.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-4**._
ISSUE_BODY_EOF_92f1
emit ARCH-10 '[ARCH-10] display.start() and component construction are unguarded in main(), so first-boot display failure is a raw traceback' code-review,architecture,severity:low 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** no · **Source:** lens

**Location:** `main.py:143`

### What is wrong

Only load_config() is wrapped in a try in main(); component construction (lines 122-136) and display.start() (line 143) are not. A pygame display-init failure on the Pi surfaces as a bare pygame.error traceback with no mention of DISPLAY, the HDMI cable, or the SDL env vars renderer.py:112-113 quietly sets.

### How it fails

First power-on with HDMI not detected or X not up: the owner finds a black screen and, in journalctl, a pygame stack trace that names no remedy. The brief notes the app has never run on the physical Pi, so this is the most probable first failure.

### Suggested fix

Wrap display.start() (and ideally the construction block) and log an actionable message pointing at docs/first-boot-checklist.md before re-raising.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-10**._
ISSUE_BODY_EOF_92f1
emit CONC-3 '[CONC-3] Fire-and-forget _end_session task has no exception handling — a raising writer aborts the credit permanently and buries the traceback in GC' code-review,bug,severity:low 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/tracking/listen_tracker.py:88`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

add_done_callback(self._bg_tasks.discard) drops the reference and nothing else: task.exception() is never retrieved and no error is logged. If _end_session raises, the only trace is asyncio's 'Task exception was never retrieved' emitted from the GC at an arbitrary later time, detached from the causing event.

### How it fails

increment_play_count raises rather than returning False (a malformed Discogs response, an unhandled requests exception). credited was already latched at :177, so update_last_played and the Last.fm love are skipped forever for that session, and the operator sees no error where the same file logs every other write outcome.

### Suggested fix

Replace the bare discard with a done-callback that discards and logs task.exception(), or wrap _end_session's body in try/except with an explicit error log.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CONC-3**._
ISSUE_BODY_EOF_92f1
emit SIL-2 '[SIL-2] NaN in the audio stream is classified as silence, faking a needle lift mid-record' code-review,bug,severity:low 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/silence.py:60`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

rms is computed with no finiteness guard and compared via `rms >= self.threshold`; since nan >= x is False in IEEE-754, any NaN in the 15s window sends the whole chunk down the silence branch, emitting MUSIC_STOPPED and arming the end-of-session timer.

### How it fails

One NaN sample out of 661500 during playback flips _is_music to False and arms _silence_since. Because _silence_ticker evaluates the timer independently of chunk flow, a NaN burst ~45s before a side ends can fire SESSION_ENDED early, clearing the display and (unproven, downstream) crediting an unfinished side to Discogs.

### Suggested fix

Guard explicitly: if not math.isfinite(rms), log a warning and return without changing state — a corrupt chunk is not evidence of silence.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SIL-2**._
ISSUE_BODY_EOF_92f1
emit SIL-4 '[SIL-4] No hysteresis on the RMS threshold — the detector flaps and each flap re-arms the end-of-session timer' code-review,bug,severity:low 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/silence.py:60`

### What is wrong

A single threshold with no dead band and no dwell requirement means an RMS hovering at the boundary produces an unbounded MUSIC_STARTED/MUSIC_STOPPED alternation. Each return to music clears _session_ended and each drop re-arms _silence_since from scratch.

### How it fails

RMS alternating 0.010001/0.009999 across chunks emits eight events from a 0.000002 amplitude difference. A long fade-out or locked groove sitting at the threshold can hold SESSION_ENDED off indefinitely, so the finished side is never credited. Blast radius is limited because main.py only enters LISTENING from IDLE/ERROR and MUSIC_STOPPED has no consumer.

### Suggested fix

Use separate enter/exit thresholds (e.g. exit at 0.5x enter) or require N consecutive chunks on the new side before transitioning.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SIL-4**._
ISSUE_BODY_EOF_92f1
emit DISP-8 '[DISP-8] _on_state_change calls create_task unguarded while _load_cover guards the identical call' code-review,bug,severity:nit 'Wave 2 — Hardware bring-up blockers' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/display/renderer.py:469`

**Severity moved during adversarial review:** LOW → NIT

### What is wrong

_on_state_change is a synchronous PlayerState callback that calls self._spawn(self._prefetch_cover(url)) -> asyncio.create_task. _load_cover wraps the identical call in an explicit asyncio.get_running_loop()/except RuntimeError guard (renderer.py:1474-1479) precisely because that site may run without a loop. The two call sites disagree and one of them is wrong.

### How it fails

If a state change is ever delivered from an executor thread or before the loop starts, RuntimeError('no running event loop') propagates out of the renderer's callback and back into the notifying recognition pipeline, which is not expecting the display layer to raise.

### Suggested fix

Apply the same get_running_loop guard in _spawn itself so every call site is protected once, rather than at one of two sites.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-8**._
ISSUE_BODY_EOF_92f1

echo "== Wave 3 — Untrusted input & credential hardening =="
emit MUT-2 '[MUT-2] Both rejection paths of validate_image_file (S-2 decompression-bomb + format allow-list) are never executed; the guard can be deleted with the suite green' code-review,testing,severity:high 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** HIGH · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/palette.py:50`

### What is wrong

Twelve genuine mutants survive on palette.py:21,48-51, including 'condition -> False' and 'delete raise' on BOTH guards. Coverage confirms lines 49 and 51 have zero executions across 632 tests. cover_cache.download() depends on this as its last check before a downloaded file enters the cache.

### How it fails

A Discogs image uri (user-editable by strangers) points at a 40000x40000 PNG. Pillow's own MAX_IMAGE_PIXELS backstop only raises above 2x the threshold, so a 1.5x bomb decodes: multi-GB allocation on a Pi 4, black screen. The format allow-list has no backstop at all.

### Suggested fix

Three tests: a valid small JPEG accepted; an image declaring dimensions over MAX_IMAGE_PIXELS raising ValueError (match the message); a valid-but-disallowed format (TIFF/ICO) raising.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-2**._
ISSUE_BODY_EOF_92f1
emit DISP-3 '[DISP-3] validate_image_file'"'"'s docstring claims verify() rejects truncated files; PIL'"'"'s verify() is a no-op for JPEG' code-review,bug,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/palette.py:32`

**Absorbs:** SEC-6 — same root cause, found independently by another lens.

### What is wrong

palette.py:29-34 documents 'Uses Pillow's verify() to reject truncated / malformed files' and line 44 comments 'structural integrity check', but PIL.Image.Image.verify() is a base-class no-op overridden only by PngImageFile. For JPEG — the format the cache is named for (path_for returns <md5>.jpg) — no structural check happens at all. This is the only gate between the network and the cache (cover_cache.py:385), and the download loop never reconciles bytes against Content-Length.

### How it fails

A Wi-Fi drop mid-fetch writes a 55%-complete JPEG; it passes validation, is os.replace'd into the cache, and CoverArtCache.exists() never re-validates. The half-decoded cover is then displayed for every future play of that album, and extract_palette derives the album's entire five-colour scheme from the garbage half and caches it by URL. Both failures are completely silent.

### Suggested fix

Force a real decode in the validator (Image.open(path).load() inside the existing try, bounded by MAX_IMAGE_PIXELS, with LOAD_TRUNCATED_IMAGES left False) and correct the docstring/comment to say what verify() actually does per format.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-3**._
ISSUE_BODY_EOF_92f1
emit MUT-6 '[MUT-6] Cover-cache default bounds never exercised: 256*1024*1024 can become 256/1024/1024 with the suite green' code-review,testing,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/cover_cache.py:78`

### What is wrong

Ten mutants survive on lines 77-78 including both '*' -> '/' mutations. Every test in test_cover_cache.py constructs CoverArtCache with explicit max_files=/max_bytes=, so the defaults that the real appliance actually uses are asserted nowhere.

### How it fails

A units slip (MB read as bytes, or the * / typo) ships green. On the Pi, every boot prunes the entire cover cache to zero, so every album re-downloads: SD-card write amplification, and with no network a permanently coverless display.

### Suggested fix

One test constructing CoverArtCache(tmpdir) with no keyword bounds asserting max_files==500 and max_bytes==256*1024*1024, plus one seeding N+1 files and asserting the count returns to N.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-6**._
ISSUE_BODY_EOF_92f1
emit MUT-7 '[MUT-7] _prune never evicts more than one file in the entire suite; i += 0 and file_count -= 2 both survive' code-review,testing,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/cover_cache.py:313`

### What is wrong

'i += 1' -> 'i += 0' surviving is only possible if the eviction while-loop body runs at most once anywhere in the suite; otherwise the second iteration re-picks the same victim, unlink raises OSError, continue skips the decrement, and it spins forever. 'file_count -= 1' -> '-= 2' surviving says no test asserts the resulting file count after a multi-file eviction.

### How it fails

An off-by-one that under-evicts lets the cache grow past its bound and fill the SD card; the i+=0 shape is an infinite loop inside CoverArtCache.__init__, i.e. the appliance never finishes booting and shows a black screen nobody is watching.

### Suggested fix

Seed 10 files with distinct mtimes, prune to max_files=3, assert exactly the 3 newest survive by name; repeat for the byte bound; add a case where a victim's unlink raises OSError and assert termination.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-7**._
ISSUE_BODY_EOF_92f1
emit MUT-8 '[MUT-8] preload_content=False and retries=False on the cover stream are unpinned while redirect=False is pinned' code-review,testing,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/cover_cache.py:213`

### What is wrong

Of the three streaming kwargs to pool.urlopen, redirect=False -> True is killed but retries=False -> True and preload_content=False -> True both survive, so the existing test pins redirect specifically rather than the call as a whole.

### How it fails

With preload_content=True urllib3 reads the entire response body into memory before download() sees it, so the _MAX_COVER_BYTES chunk counter can only truncate what is already resident. An attacker-influenced or merely broken cover URL returning a few hundred MB is then a RAM exhaustion on a 2 GB Pi even though the on-disk file stays capped.

### Suggested fix

Assert the full kwarg set of the pool.urlopen call (redirect, retries, preload_content, decode_content) in the existing _open_cover_stream test.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-8**._
ISSUE_BODY_EOF_92f1
emit SEC-3 '[SEC-3] A wrong-typed credential in config.yaml is echoed verbatim into the ConfigError that main.py logs' code-review,security,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** security · **Reproduced:** yes · **Source:** lens

**Location:** `src/config.py:97`

### What is wrong

_field interpolates the raw value with !r into the aggregated type-mismatch error for every field, including discogs.user_token and lastfm.api_key/api_secret/session_key. main.py:118-120 logs that ConfigError in full. The brief requires a bad config to fail loudly and never leak credentials into logs or exception text; it does the first and violates the second.

### How it fails

Operator pastes a credential that YAML does not read as a string (all-digit key, 1e5-shaped value, yes/no/on/off, or a mis-pasted list/mapping). Startup fails and the secret is written in full to the systemd journal, where it persists across reboots.

### Suggested fix

Keep a set of secret field names and emit <redacted> instead of {data[key]!r} for those, or report only the observed type — the type alone is what the operator needs.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SEC-3**._
ISSUE_BODY_EOF_92f1
emit TQ-3 '[TQ-3] src/metadata/coverart.py has no test file at all; 100% of its untrusted-input parsing is uncovered' code-review,testing,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/coverart.py:27`

### What is wrong

Lines 27-60 are the entire body of get_cover_art_url and are 0% covered; the 27% figure is imports and set_useragent. There is no tests/test_coverart.py. This function parses MusicBrainz response bodies (untrusted per the brief) and returns a string that src/display/cover_cache.py then fetches, so it sits directly on a security boundary with zero negative tests.

### How it fails

MusicBrainz returns images as a list of strings rather than dicts. coverart.py:43-45 calls img.get('front') and raises AttributeError, which is NOT caught by the inner 'except musicbrainzngs.ResponseError' at line 51 — it escapes to the outer handler at 56 and aborts the whole candidate loop instead of trying the next release, so cover art is lost for a release that had it. Nothing asserts this degradation. There is also no test that the returned URL is a str, is https, or is validated before being handed to the fetcher.

### Suggested fix

Add tests/test_coverart.py with musicbrainzngs patched: happy path, empty release-list, ResponseError on first release then success on second, and at least three malformed-payload cases including non-dict images and a file:// URL.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-3**._
ISSUE_BODY_EOF_92f1
emit TQ-4 '[TQ-4] urllib3 floor-only pin guards the TLS/IP-pinning control, and the test mocks the pool so a breaking upgrade is undetectable' code-review,security,severity:medium 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** security · **Reproduced:** no · **Source:** lens

**Location:** `requirements.txt:24`

### What is wrong

requirements.txt is floors-only with no lockfile or hashes. That is proportionate for most entries but not for urllib3>=2.0.0, because src/display/cover_cache.py:198-205 builds the S-7 SSRF control on server_hostname and assert_hostname kwargs (the latter deprecated in urllib3 2.x). tests/test_cover_cache.py:257 monkeypatches HTTPSConnectionPool with a fake that accepts any kwargs, so the suite proves only that our code passes them — it can never detect that urllib3 stopped honouring them.

### How it fails

A Pi pip-installs months from now and resolves urllib3 3.x where assert_hostname is removed. Either the kwarg is rejected (TypeError, cover-art fetch throws) or accepted-and-ignored (cert verification falls back to matching the IP literal, fetches fail closed). Both plausible outcomes are loud rather than silent, which is why this is MEDIUM not HIGH — but cover_cache.py sits at 89% coverage and fully green, and that number carries no information about whether the pin still works.

### Suggested fix

Pin urllib3>=2.0.0,<3 (and ceilings on pygame/Pillow/numpy/discogs-client/shazamio; NOT on certifi), ship a hashed requirements.lock for the appliance install, and add one test that constructs a genuine HTTPSConnectionPool with those kwargs so a resolver upgrade fails in CI rather than on the Pi.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-4**._
ISSUE_BODY_EOF_92f1
emit LB-2 '[LB-2] request() coerces every non-GET verb to POST, and a lowercase "get" both POSTs and loses its 429 retry' code-review,bug,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** proved during orchestrator triage

**Location:** `src/metadata/discogs/transport.py:130`

### What is wrong

send = self.session.get if method == "GET" else self.session.post. request("DELETE", ...) and request("PUT", ...) silently issue POSTs. Worse, request("get", ...) fails the == "GET" test twice: it dispatches a POST and retry_on_429 defaults to False.

### How it fails

Latent today (only GET and POST are used), but it is a silent-wrong-verb footgun on the one transport that writes to the real collection.

### Suggested fix

Normalise the method and dispatch via session.request, raising on an unsupported verb.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **LB-2**._
ISSUE_BODY_EOF_92f1
emit MUT-10 '[MUT-10] download()'"'"'s five failure guards are interchangeable: tests pin that it raises, not which guard fired' code-review,testing,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/cover_cache.py:358`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

Deleting the 'no response' raise, disabling 'if resp.status >= 400', deleting the HTTP-status raise, disabling the Content-Type check and deleting its raise all survive individually, because each failure still terminates in some exception downstream (a non-image body dies in validate_image_file, a None response dies on AttributeError). The '>= -> >' survivor also means HTTP 400 exactly is untested.

### How it fails

A refactor drops the Content-Type check entirely; the suite stays green because non-image bodies still fail image validation later - but a body that happens to be a valid image served as text/html, or a 400 response with an image body, now flows into the cache.

### Suggested fix

Use pytest.raises(ValueError, match=...) so each guard is individually pinned, and add a status == 400 boundary case.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-10**._
ISSUE_BODY_EOF_92f1
emit MUT-9 '[MUT-9] Every numeric limit in the fetch and rate-limit paths is unpinned (byte cap, redirect cap, timeouts, wait bounds)' code-review,testing,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/cover_cache.py:71`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

_MAX_COVER_BYTES (6 survivors), _MAX_COVER_REDIRECTS = 5 (both directions), _COVER_CONNECT_READ_TIMEOUT = 15 (both directions), and transport.py's _HTTP_TIMEOUT / _RATE_LIMIT_MAX_WAIT / _RATE_LIMIT_DEFAULT_WAIT all survive. Tests establish that a cap exists, never which. The line 71 comment 'cap redirect chains' is unbacked.

### How it fails

The redirect cap is raised to 500 in a refactor; the suite stays green; a redirect loop through allow-listed hosts ties up the executor thread for the full chain. Or the 429 wait floor max(1, ...) becomes max(0, ...) - both mutants survive at transport.py:137 - so a Retry-After: 0 makes the single retry fire instantly against an API that just rate-limited the device.

### Suggested fix

Assert the shipped constants directly in one small test, drive a redirect chain to exactly _MAX_COVER_REDIRECTS + 1 hops, and add a Retry-After: 0 case asserting sleep >= 1.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-9**._
ISSUE_BODY_EOF_92f1
emit SEC-2 '[SEC-2] _redact_url returns the unredacted URL, query string included, whenever the path is empty' code-review,security,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** security · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/transport.py:79`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

The `return "/".join(segments) or url` fallback returns the original URL verbatim when parts.path is empty, because "/".join([""]) is falsy. The docstring promises "the query string dropped" and warns that "any future query-string credential would otherwise land in the logs verbatim" — which is exactly what this branch does.

### How it fails

Any caller logging an origin-only or query-bearing Discogs URL through the 429 warning at transport.py:138-141 writes the query string to the systemd journal in plaintext. Latent rather than live today because the token rides in a header.

### Suggested fix

Return `parts.path or "/"` after masking and never fall back to the raw url; add a unit case for a query-bearing empty-path URL.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SEC-2**._
ISSUE_BODY_EOF_92f1
emit SEC-4 '[SEC-4] HYPOTHESIS: cover download has per-read timeouts but no overall deadline, so a slow-drip response parks a shared executor worker indefinitely' code-review,security,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** security · **Reproduced:** no · **Source:** lens

**Location:** `src/display/cover_cache.py:205`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

urllib3.Timeout(connect=15, read=15) bounds each socket operation, not the transfer. The streaming loop at cover_cache.py:375-383 reads until the 10 MB cap, so a peer emitting one byte every 14 seconds keeps every read inside the timeout while the download runs effectively forever. No wall-clock deadline, no throughput floor, no early Content-Length reject, and each of up to six redirect hops restarts the clock.

### How it fails

A flaky CDN or captive portal on an allow-listed host (discogs.com, coverartarchive.org, archive.org, mzstatic.com) drips the response body. renderer.py:1392 dispatches download() on the default executor, which transport.py:34-39 documents as shared with Discogs requests and Last.fm scrobbles, so stalled downloads starve that pool and Discogs/Last.fm work stops.

### Suggested fix

Track a monotonic deadline across the whole download() call including redirects and abort past it; reject early on an oversized Content-Length.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SEC-4**._
ISSUE_BODY_EOF_92f1
emit SEC-5 '[SEC-5] HYPOTHESIS: private-address classifier misses NAT64 (64:ff9b::/96) and 6to4 (2002::/16) encodings of internal IPv4' code-review,security,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** security · **Reproduced:** no · **Source:** lens

**Location:** `src/display/cover_cache.py:128`

### What is wrong

The classifier at cover_cache.py:128-137 correctly rejects IPv6 unique-local (fc00::/7), link-local (fe80::/10) and Teredo (covered by CPython's 2001::/23 private block) — I checked these specifically. It does not cover NAT64 64:ff9b::/96 or 6to4 2002::/16, neither of which is in CPython's IPv6 private-network table, so 64:ff9b::a9fe:a9fe and 2002:a9fe:a9fe:: both classify as global and would be pinned.

### How it fails

On a network that routes NAT64 or 6to4, a poisoned or MITM'd DNS answer for an allow-listed cover host returns 64:ff9b::a9fe:a9fe and the fetch reaches the 169.254.169.254 metadata address. Requires the prefix to actually be routed, which a default home LAN does not do — hence LOW.

### Suggested fix

Reject 64:ff9b::/96 explicitly, and for 2002::/16 decode the embedded IPv4 address and re-run the classifier on it.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SEC-5**._
ISSUE_BODY_EOF_92f1
emit STAB-3 '[STAB-3] A fresh urllib3.HTTPSConnectionPool is built per request hop and never closed' code-review,bug,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/display/cover_cache.py:198`

### What is wrong

_open_cover_stream constructs a new HTTPSConnectionPool per hop as a local that is never .close()d; download's finally calls resp.release_conn(), returning the socket to a pool nobody will reuse. Every cover pays a full TCP+TLS handshake and a CAA fetch that 307-redirects to archive.org pays two. I did NOT measure an fd leak and do not claim one — refcounting should reclaim the pool->queue->connection->socket chain promptly.

### How it fails

Steady state this is only handshake churn; combined with STAB-1 it becomes ~31k TLS handshakes/hour and 31k sockets cycled through TIME_WAIT on a Pi.

### Suggested fix

Use the pool as a context manager (or try/finally pool.close()), and consider a small keyed pool cache so consecutive covers from the same pinned IP reuse a connection.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-3**._
ISSUE_BODY_EOF_92f1
emit TQ-5 '[TQ-5] CI: VERSION file content interpolated into three run: shells, actions on floating tags, contents:write token' code-review,security,severity:low 'Wave 3 — Untrusted input & credential hardening' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** security · **Reproduced:** no · **Source:** lens

**Location:** `.github/workflows/sync-version-badge.yml:50`

### What is wrong

${{ steps.ver.outputs.version }} is textually interpolated into run: blocks at lines 39, 50 and 59. The value is repo content read by 'cat VERSION' at line 32. The tr -d '[:space:]' there blocks the naive payload but is not a sanitiser — $IFS, backticks and $() survive it. actions/checkout is on the floating tag @v4 (line 25) in a job holding contents: write with push rights to main. Line 50 also interpolates into a sed replacement using | as the delimiter, so | & or \ in a version corrupts the expression.

### How it fails

A VERSION file containing 1.5.2$(curl$IFS-sfL$IFSevil.sh|sh) executes arbitrary commands in a job whose GITHUB_TOKEN can push to main. The trigger is push to main so the actor already needs write access — this is not an external path today, which caps severity at LOW/MEDIUM. It becomes HIGH the moment anyone adds pull_request_target or a workflow_dispatch input.

### Suggested fix

Pass the version via env: and reference "$VERSION" in the shell, validate it against ^[0-9]+\.[0-9]+\.[0-9]+ before use, and pin actions/checkout to a full commit SHA. Credit where due: permissions is already explicitly job-scoped rather than default write-all.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-5**._
ISSUE_BODY_EOF_92f1

echo "== Wave 4 — Display correctness & contrast =="
emit DISP-1 '[DISP-1] accent is a text colour (album title) with no WCAG clamp — measured 1.05:1, 34/62 covers below 4.5:1' code-review,bug,severity:medium 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:676`

**Severity moved during adversarial review:** HIGH → MEDIUM

### What is wrong

The album title is drawn in p.accent (renderer.py:676), but extract_palette only passes accent through clamp_luminance (palette.py:166), a perceived-brightness clamp that cannot brighten already-saturated or pure-black inputs, while the real ensure_contrast is applied to muted alone (palette.py:189). CLAUDE.md and DESIGN.md both assert 'WCAG AA: 4.5:1 minimum contrast on all text'.

### How it fails

A near-black sleeve (Metallica Black Album, most matte-black Blue Note reissue scans) yields accent=(0,0,0) against bg=(8,8,8) => 1.05:1, so the album title, the accent divider and the genre-chip borders are all invisible. A saturated blue sleeve yields accent=(0,0,255) vs bg=(8,8,56) => 2.22:1, unreadable at room distance on a 1024x600 shelf panel.

### Suggested fix

Route accent through the existing ensure_contrast(accent, bg, 4.5) in extract_palette, or split the role into raw `accent` for graphics and a clamped `accent_text` for the album title, and correct the docs about which roles the 4.5:1 promise covers.

### Decision (Lane, 2026-07-30)

FIX, do not keep as a design choice (Lane, 2026-07-30). Clamp `accent` to the WCAG floor against the surface/gradient actually behind the album title, moving the extracted colour the SMALLEST distance that reaches 4.5:1 so it stays as faithful to the cover as possible; then update DESIGN.md:123 to match, which resolves the CLAUDE.md/DESIGN.md contradiction. Ties into DISP-2 (clamp against the real background, not flat `bg`).

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-1**._
ISSUE_BODY_EOF_92f1
emit DISP-2 '[DISP-2] All contrast clamping is computed against flat bg, but text is drawn on a radial gradient blended toward surface — meta footer measured at 3.99:1' code-review,bug,severity:medium 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:1234`

### What is wrong

_draw_gradient_bg fills the frame with _lerp_color(bg, surface, t*0.55), not flat bg, and all text is blitted on top. But both enforcement points — ensure_contrast(muted, bg, 4.5) at palette.py:189 and the re-assertion at renderer.py:330 — use flat p.bg. Since surface = bg * 1.6, the pixels behind the text are brighter than the value the guarantee is computed against, so the guarantee is systematically optimistic.

### How it fails

A saturated yellow sleeve (hue 60) gives muted=(163,159,143) dutifully clamped to 4.51:1 against bg, but the Year·Label·Catalog footer is actually painted on gradient pixel (65,65,9) and measures 3.99:1 — below the documented AA floor. Header label 4.17:1 and genre chips 4.11:1 sit in the same gradient band.

### Suggested fix

Clamp text roles against the brightest colour the gradient can put under text — _lerp_color(bg, surface, 0.55) — instead of against bg; a one-argument change in palette.py:189 and renderer.py:330 plus a doc correction.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-2**._
ISSUE_BODY_EOF_92f1
emit MUT-3 '[MUT-3] The WCAG 4.5:1 Full-Opacity Rule is asserted nowhere that would notice the threshold changing' code-review,testing,severity:medium 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/palette.py:189`

**Severity moved during adversarial review:** HIGH → MEDIUM

### What is wrong

min_ratio=4.5 can be halved to 2.25 at both the default (L88) and the extract_palette call site (L189) with the suite green; the lightening loop's success branch (L104 condition->False, L105 delete return) also survives, and L106 never executes. The suite exercises the guarantee only on covers that satisfy it for free.

### How it fails

Someone tunes scale_bg (0.18) or the muted base (120/118/115) for aesthetics; contrast on cool-dark covers falls to ~3:1; 632 tests stay green; secondary text on the 1024x600 panel becomes unreadable, which is precisely what DESIGN.md 2 and the palette.py docstring promise cannot happen.

### Suggested fix

Assert contrast_ratio(p.muted, p.bg) >= 4.5 on the OUTPUT of extract_palette over several synthetic covers including a deliberately low-contrast dark-blue one, and unit-test that ensure_contrast's input was below 4.5 while its output is above.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-3**._
ISSUE_BODY_EOF_92f1
emit TQ-1 '[TQ-1] Every renderer test bypasses __init__ via __new__; DisplayRenderer is never actually constructed' code-review,testing,severity:medium 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:340`

### What is wrong

All six renderer test files build their subject with DisplayRenderer.__new__(DisplayRenderer) and hand-assign attributes (tests/test_error_state.py:67, test_renderer_palette.py:52, test_renderer_perf.py:35/44/115/134, test_renderer_robustness.py:35/166/198/236). grep shows zero call sites of DisplayRenderer(config, state). Coverage confirms renderer.py:341-404 (all of __init__), 419-425 (start), 455-474 (_on_state_change), 482-504 (run) and 512-521 (_render dispatch) are 0% executed. The 83% figure is misleading: the tests assert against an object they built themselves.

### How it fails

An __init__ refactor drops or renames self.state.on_change(self._on_state_change) at renderer.py:404. Suite stays fully green. On the Pi the display initialises, shows the boot card, then never updates again — a permanently frozen screen on hardware that has never been run.

### Suggested fix

Add one test that calls the real DisplayRenderer(config, state) under SDL_VIDEODRIVER=dummy and asserts the state subscription fired, plus table-driven tests for _render() dispatch across every PlayerStatus. __init__, _on_state_change and _render need no pygame surface and are headless-testable today.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-1**._
ISSUE_BODY_EOF_92f1
emit DISP-5 '[DISP-5] _render_tracked renders one codepoint at a time, destroying shaping for every non-Latin script it draws' code-review,bug,severity:low 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:1133`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

_render_tracked builds labels as [font.render(ch) for ch in text] with a manual advance. That defeats all text shaping: Arabic renders in isolated forms and LTR order, Devanagari conjuncts decompose, combining marks float free at the wrong advance, and emoji ZWJ clusters split. It draws the meta footer (track.year/label/catalog_number) and the genre chips, not just ASCII constants.

### How it fails

A Discogs release with a Japanese, Arabic or Cyrillic label name — routine data, and the BRIEF notes Discogs fields are user-editable by strangers — renders that label as unshaped, mis-spaced, possibly reversed glyphs in the footer. None of the four bundled fonts covers CJK/Arabic/Devanagari, and the SysFont fallback only fires when a bundled file is missing, never per glyph, so tofu boxes are the likely outcome.

### Suggested fix

Render each label as a single shaped string and apply tracking by measuring cluster boundaries, or drop tracking for non-ASCII strings and fall back to a plain font.render.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-5**._
ISSUE_BODY_EOF_92f1
emit DISP-6 '[DISP-6] layouts.py font floors contradict the documented '"'"'scales every constant proportionally'"'"' resolution independence' code-review,bug,severity:low 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/layouts.py:161`

### What is wrong

CLAUDE.md asserts the renderer 'scales every constant proportionally (s = min(width/1024, height/600)); there are no hard-coded breakpoints'. layouts.py:161-167 contains seven max(floor, int(ref*s)) clamps, so below s ~ 0.33 fonts stop shrinking while rects keep shrinking. Separately layouts.py:169 scales divider_width by sx while every font uses s.

### How it fails

At 320x240 the header strip is 12px tall while font_size_header is floored at 9px (~12px glyph box), so the label fills or exceeds its strip; at 1x1 and 2x600 the cover rect is 0x0. On a 3440x1440 ultrawide the divider — documented as 'a punctuation mark, not a full-width divider' — renders 215px, ~3.4x its proportional size.

### Suggested fix

Either document the minimum supported resolution and the floor behaviour in CLAUDE.md, or drop the floors and let the layout degrade proportionally; use `s` rather than `sx` for divider_width.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-6**._
ISSUE_BODY_EOF_92f1
emit DISP-7 '[DISP-7] Long unbroken words overflow their rect horizontally — _draw_wrapped_text blits with no area clip and _fit_wrapped cannot shrink them' code-review,bug,severity:low 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/display/renderer.py:1294`

### What is wrong

_wrap_lines emits any token wider than max_width as its own unchanged line, and _draw_wrapped_text blits it with no area= clip — unlike the meta footer at renderer.py:690-691, which does clip. _fit_wrapped cannot help because a single token is exactly one line at every candidate size, so the shrink loop returns base_size on its first iteration.

### How it fails

A 120-character unbroken title or artist name — Discogs titles are free text and regularly carry run-ons — renders straight off the right edge of the text column and off the screen, silently truncated by the display rather than by the layout.

### Suggested fix

Pass area=(0, 0, rect.w, rect.h) in _draw_wrapped_text's blit, and add character-level breaking in _wrap_lines for tokens wider than max_width.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **DISP-7**._
ISSUE_BODY_EOF_92f1
emit STAB-5 '[STAB-5] Blocking SD read + image scale run on the asyncio event loop in the render path' code-review,bug,severity:low 'Wave 4 — Display correctness & contrast' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `src/display/renderer.py:1461`

### What is wrong

_load_cover performs pygame.image.load (SD read), .convert() and smoothscale synchronously on the single event loop, up to 10x/second on a cache miss. The palette extraction next door was already moved to an executor for exactly this reason (renderer.py:1418-1424), but the cover load was not.

### How it fails

A worn SD card that stalls a read for several seconds (normal wear-levelling/ECC-retry behaviour) blocks the whole event loop, including the audio-block drain and the silence ticker, so session-end detection and recognition stall with it.

### Suggested fix

Move the load+scale into run_in_executor like the palette extraction, blitting the placeholder until the scaled Surface lands.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-5**._
ISSUE_BODY_EOF_92f1

echo "== Wave 5 — Architecture, docs & test debt =="
emit CRIT-6 '[CRIT-6] test_discogs_live.py is the only executor of the Discogs field-ID map and no lens opened it' code-review,bug,severity:medium 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** no · **Source:** completeness critic

**Location:** `test_discogs_live.py:113`

### What is wrong

226 lines at the repo root, opened by zero lenses. pytest.ini sets testpaths=tests, so it contributes nothing to the 632 tests or the 87% coverage. Its check_collection_fields calls writer._get_collection_fields() -- the exact private method MUT-1 proves the unit suite never executes, and the map that decides WHICH Discogs custom field gets written. The audit's combined position is therefore: the field-selection map is unexecuted by the suite (MUT-1, proved) and unexamined in the only script that would execute it. Run with --test-write it issues a real increment_play_count against the operator's live collection, and docs/first-boot-checklist.md and docs/testing-guide.md:536 point the operator at it -- both also unread. It also reaches into a private method from outside the package, so any writer refactor silently breaks the operator's first-boot smoke test.

### Suggested fix

Read it; give _get_collection_fields a real unit test against a stubbed transport (closing MUT-1); and either rename the script so it cannot be mistaken for suite content or promote it to a marked, opt-in integration test.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-6**._
ISSUE_BODY_EOF_92f1
emit CRIT-7 '[CRIT-7] About 1,900 lines of stated-intent documentation were opened by zero lenses while every lens was required to return a SPEC verdict' code-review,bug,severity:medium 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** bug · **Reproduced:** no · **Source:** completeness critic

**Location:** `DESIGN.md:1`

### What is wrong

The brief names CLAUDE.md, docs/architecture.md, DESIGN.md, PRODUCT.md, docs/testing-guide.md, docs/first-boot-checklist.md, README.md and config.example.yaml as the sources of stated intent, and rule 5 makes SPEC a mandatory verdict. Of those, DESIGN.md (280), PRODUCT.md (44), docs/testing-guide.md (662) and docs/first-boot-checklist.md (130) were opened by nobody, as were docs/roadmap.md (440) and docs/hardware-guide.md (202). Concretely: DISP-1, DISP-2 and MUT-3 all adjudicate the WCAG / Full-Opacity rule, and DISP states in its own notes that it worked from CLAUDE.md because DESIGN.md was not opened -- so the audit's most-contested SPEC judgement was made against a paraphrase. Both test-focused lenses assessed the suite without reading testing-guide.md, and every lens applied the 'never run on hardware' severity multiplier without reading hardware-guide.md or first-boot-checklist.md.

### Suggested fix

Before filing any SPEC-flavoured issue (DISP-1/2, MUT-3/16, SIL-3, REC-4, ARCH-4/5), read DESIGN.md and docs/testing-guide.md and re-check the exact wording each finding claims is contradicted.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-7**._
ISSUE_BODY_EOF_92f1
emit MUT-12 '[MUT-12] The Discogs User-Agent header is unpinned on an appliance that has never run on real hardware' code-review,testing,severity:medium 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/transport.py:98`

### What is wrong

'User-Agent', 'vinyl-now-playing/1.0', 'Content-Type' and 'application/json' all survive string mutation; only the Authorization header is pinned. Nothing asserts the identifying UA that Discogs requires.

### How it fails

A header refactor drops or renames the User-Agent. 632 tests stay green. The first time the Pi ever touches the real Discogs API, every call returns HTTP 403 and the owner gets a display with no pressing details and no play counts - a hardware-only failure, which this project weights higher.

### Suggested fix

Assert the exact default header dict on DiscogsHttp construction, including a non-empty User-Agent.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-12**._
ISSUE_BODY_EOF_92f1
emit MUT-16 '[MUT-16] SPEC: the B-5 side-filter block in SideIndex.from_tracklist is provably redundant with its own fallback, so the docstring credits an inert mechanism' code-review,testing,severity:medium 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/models.py:156`

### What is wrong

The mutant making line 156 never match survives, and it must: current (L124) is the first tracklist entry matching title_key, side_entries (L139) is the subset sharing current's own side letter in tracklist order, so the first title match inside side_entries is always current itself. Lines 154-158 can never yield a different target_position than the L159-160 fallback.

### How it fails

The docstring states the B-5 fix is that 'the side filter disambiguates the occurrence' for a title repeated across sides. It does not: disambiguation would have to happen where current is chosen on line 124, which is an unconditional first-match by title. A maintainer reading this believes reprise handling is solved by a mechanism that is inert.

### Suggested fix

Either delete lines 154-158 in favour of target_position = current.position if current else None, or correct the docstring; in both cases add the reprise test (['A1 Reprise','A2 X','B1 Reprise'], title='Reprise').

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **MUT-16**._
ISSUE_BODY_EOF_92f1
emit TQ-2 '[TQ-2] main() entirely uncovered including SIGTERM/SIGINT shutdown; test_main_wiring docstring overclaims coverage' code-review,testing,severity:medium 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `main.py:116`

### What is wrong

Coverage reports main.py missing 47-50, 97, 116-166, 170 — 116-166 is the entire body of async def main(). Uncovered: the ConfigError to sys.exit(1) startup guard (116-120), _cancel_all (155-158) and loop.add_signal_handler for SIGINT/SIGTERM (160-162). tests/test_main_wiring.py:1 opens 'Regression tests for T-1 — main.py wiring + shutdown had zero coverage', but reading it shows it covers only the two helpers extracted out of main(), not main() itself.

### How it fails

A config-loading change makes load_config() raise something other than ConfigError. Suite green; on the Pi main() dies with an unhandled traceback before display.start(), so the operator gets a black screen with no on-screen error card. Equally, the comment at main.py:152-154 claiming signal-handler safety for Task.cancel is never exercised — this is a systemd appliance where SIGTERM is the shutdown path.

### Suggested fix

Test main() with load_config patched to raise ConfigError and assert SystemExit(1); separately patch the loop and assert add_signal_handler is registered for both signals and that _cancel_all cancels every task.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-2**._
ISSUE_BODY_EOF_92f1
emit TQ-7 '[TQ-7] AudioCapture.run() and _silence_ticker() fully uncovered, including the docstring-claimed Play Count safety net' code-review,testing,severity:medium 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** MEDIUM · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/capture.py:160`

**Severity moved during adversarial review:** LOW → MEDIUM

### What is wrong

Coverage reports capture.py missing 143, 160-169, 173-233. 160-169 is the whole _silence_ticker loop, whose docstring at capture.py:150-158 promises it prevents 'a completed album's SESSION_ENDED unfired and its Play Count never credited'. That promise has zero tests. 173-233 is all of run(), including the retry-with-fresh-stream error path at 221-225 and the ticker teardown at 226-233.

### How it fails

The USB interface disconnects mid-side (explicitly in the brief's threat model). run() hits the except at 221, logs, sleeps 1s and retries with a fresh stream; _silence_ticker is supposed to keep firing so SESSION_ENDED still lands and the Discogs Play Count is credited. No test exercises either path, on hardware that has never been run. test_capture.py:22-25 defends this as hardware-bound, which is fair for sd.InputStream but not for _silence_ticker (pure asyncio plus a mock detector) or the retry branch.

### Suggested fix

Two headless tests: patch asyncio.sleep and assert silence.tick() is called repeatedly and that a raising listener does not kill the ticker; and patch capture_module.sd.InputStream to raise once then succeed, asserting a retry happened and the ticker was cancelled and awaited.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-7**._
ISSUE_BODY_EOF_92f1
emit ARCH-3 '[ARCH-3] renderer.py is still a God object: 1486 lines, 47 defs, five disjoint state clusters' code-review,architecture,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:337`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

DisplayRenderer sets 33 attributes including 7 caches and owns the render loop, pygame window, font book, text engine, palette state machine, cover-download orchestration and every drawing primitive. An AST self-attribute usage matrix shows five clusters with no shared state; the A-15 split extracted only ~120 lines and stopped.

### How it fails

Not a runtime failure. Any restyle or empty-state change requires reading a 1486-line file; the typography and palette-transition logic (which share zero state with the renderer) can only be tested through a pygame-initialised renderer, as my own ARCH-1 repro had to do.

### Suggested fix

Extract TextRenderer (typography.py, ~190 lines), PaletteTransition (~110), CoverPipeline (~120) and FramePainter (frames.py, ~600); DisplayRenderer keeps only window lifecycle, the async loop, _render dispatch and _on_state_change. Full public surfaces in the report.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-3**._
ISSUE_BODY_EOF_92f1
emit ARCH-4 '[ARCH-4] _font docstring says fonts are '"'"'held forever'"'"'; the cache has been a bounded LRU since P-8' code-review,architecture,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:432`

### What is wrong

The _font docstring states a TTF is 'opened once per (role, size) and held forever (fonts are a small, fixed set of sizes)'. renderer.py:132-136 and :356 make it a _BoundedCache capped at 64 with LRU eviction, and the constant's own comment explicitly says it was bounded to stop it being 'the one unbounded dict (P-8)'.

### How it fails

No runtime failure. A maintainer reading the docstring concludes font loading is a one-time cost and writes code that probes many sizes per frame; _fit_wrapped's stepping loop already grows the working set past what 'small, fixed set' implies.

### Suggested fix

Replace the sentence with 'cached per (role, size) in a bounded LRU (_FONT_CACHE_MAX); eviction is rare because the working set is small.'

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-4**._
ISSUE_BODY_EOF_92f1
emit ARCH-5 '[ARCH-5] docs/architecture.md configuration reference omits display.reduced_motion' code-review,architecture,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `docs/architecture.md:828`

### What is wrong

The 'Configuration reference (config.yaml)' table lists display.width/height, fullscreen, dynamic_theming and cover_art_cache_dir but not reduced_motion, which src/config.py:156/:167 accepts, config.example.yaml:39 ships, and which materially changes render behaviour. It is the only field across all five section dataclasses missing from the table.

### How it fails

An owner reading the canonical config reference never learns the flag exists, so the documented way to quiet the display's animations on a struggling Pi is invisible unless they happen to read the prose at line 571 or the example file.

### Suggested fix

Add a row: display.reduced_motion | false | Freezes the status-dot pulse and boot-arc rotation; the render loop goes fully quiet at steady state.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-5**._
ISSUE_BODY_EOF_92f1
emit ARCH-6 '[ARCH-6] mypy reports 16 errors; four renderer surface attributes lack Optional annotations' code-review,architecture,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:355`

### What is wrong

Out-of-the-box mypy --ignore-missing-imports finds 16 errors in 5 files. Most are Optional-narrowing false positives, but _screen, _arc_segment, _gradient_surface and _shadow_surface are initialised to None with no annotation, so mypy infers type None and every real assignment is an error. Their paired _key attributes at renderer.py:377/:385 ARE annotated Optional, so the omission is an oversight not a policy.

### How it fails

No runtime failure. mypy cannot be adopted in CI without a baseline, and the missing annotations remove type checking from exactly the four pygame Surface handles that are None before start().

### Suggested fix

Annotate the four attributes Optional and add a mypy.ini pinning the current error count as a ratchet.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-6**._
ISSUE_BODY_EOF_92f1
emit ARCH-7 '[ARCH-7] DisplayPalette, a pure display type, lives in src/metadata/models.py' code-review,architecture,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/models.py:23`

### What is wrong

DisplayPalette and FALLBACK_PALETTE are five RGB tuples consumed only by src/display; nothing in src/metadata uses them. src/display/palette.py:15 and src/display/renderer.py:96 therefore import up into the metadata layer for a display-owned value object, and docs/architecture.md:333 enshrines the placement.

### How it fails

No runtime failure. It makes the stated 'dependencies point inward' rule unfalsifiable: a display -> metadata import no longer tells you whether the dependency is legitimate.

### Suggested fix

Move both into src/display/palette.py alongside extract_palette/ensure_contrast; re-export from models only if a non-display consumer appears.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-7**._
ISSUE_BODY_EOF_92f1
emit ARCH-8 '[ARCH-8] Three concretes are constructed inside components rather than the documented composition root' code-review,architecture,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:353`

### What is wrong

docs/architecture.md:57 states main.py 'is the composition root'. It wires most collaborators, but CoverArtCache (renderer.py:353), CoverArtFallback (resolver.py:65) and ShazamIOBackend (recognizer.py:183) are each constructed inside the component that uses them, with no injection point.

### How it fails

Tests must monkeypatch private attributes after construction to substitute these (my ARCH-1 repro had to overwrite r._cover_store). The ShazamIOBackend case is also why ARCH-2 is a constructor crash rather than a config error.

### Suggested fix

Give each an optional constructor parameter defaulting to the current concrete, and move backend selection to main.py or src/config.py.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-8**._
ISSUE_BODY_EOF_92f1
emit CRIT-10 '[CRIT-10] One LastFmClient is driven concurrently from two executor threads by two different callers, traced by no lens' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** completeness critic

**Location:** `main.py:128`

### What is wrong

main.py:128-132 constructs one LastFmClient and hands the same instance to both ListenTracker (:129) and TrackCommitService (:132). track_commit_service.py:101 calls run_in_executor(None, self.lastfm.scrobble, ...) while listen_tracker.py:232 calls run_in_executor(None, self.lastfm.love, ...). These are separate coroutines with no shared lock, so both can be in flight on default-executor threads simultaneously against a single pylast Network object, whose thread-safety is not documented as guaranteed. META examined lastfm_client.py for failure isolation and CONC examined the thread boundary, but neither traced the two-caller topology from the composition root. LOW because the realistic overlap window (a session end loving a track while a fresh track scrobbles) is narrow and the worst case is a lost scrobble, not a Discogs write.

### Suggested fix

Either serialise LastFmClient calls behind an asyncio.Lock in the client, or confirm from pylast's documentation that a Network is thread-safe and record that in a comment.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-10**._
ISSUE_BODY_EOF_92f1
emit CRIT-11 '[CRIT-11] A prior independent concurrency pass (CONC.prev-run.md, 35 KB) was preserved but never reconciled with the current union' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** completeness critic

**Location:** `src/tracking/listen_tracker.py:1`

### What is wrong

CONC.md's own notes record that a pre-existing CONC.md from 13:20 was renamed to CONC.prev-run.md rather than deleted, that the current pass is independent of it, and that /tmp/conc held another auditor's repro scripts with an explicit instruction to cross-check before deduping. Nobody did. The adversarial verifier's header reads '77 findings, 9 lenses' and never mentions CONC.prev-run.md. So any finding unique to the earlier concurrency pass is in neither the union nor the verification, and any claim the earlier pass REFUTED may have been silently re-asserted by the later one.

### Suggested fix

Diff CONC.prev-run.md against CONC.md before closing triage; ten minutes, and it either adds findings or retires them.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-11**._
ISSUE_BODY_EOF_92f1
emit CRIT-8 '[CRIT-8] The test-design verdict generalises from 5 of 30 test files; 24 files and about 5,500 lines were never read' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** no · **Source:** completeness critic

**Location:** `tests/test_listen_tracker.py:1`

### What is wrong

tests/ is roughly 7,000 lines across 30 files. TQ opened 5 (test_main_wiring, test_discogs_security, test_capture, test_error_state, test_cover_cache) and ARCH opened 1 (test_renderer_robustness). MUT exercised 9 source files against the suite as a black box, which is valuable but says nothing about test design. So TQ-1 ('every renderer test bypasses __init__ via __new__') is a claim about roughly five renderer test files of which one was read, and the four largest test files in the repo were read by nobody. TQ's own deferred items -- the eight time.time() uses at test_cover_cache.py:470-574, no never-awaited-coroutine sweep under -W error, import-order coupling not ruled out -- all live in that unread majority.

### Suggested fix

Spot-read the four largest test files before acting on TQ-1 or on MUT's per-file survival rates; one sabotage-and-rerun pass over test_listen_tracker.py and test_models.py would settle whether the pattern TQ found in renderer tests generalises.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-8**._
ISSUE_BODY_EOF_92f1
emit CRIT-9 '[CRIT-9] META-5 cites writer.py:133 for the scrobble-timestamp half of its claim; the scrobble timestamp is taken at track_commit_service.py:72' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** completeness critic

**Location:** `src/app/track_commit_service.py:72`

### What is wrong

META-5 is titled 'An unset clock at boot writes a bogus Last Played date and bogus scrobble timestamps (src/metadata/discogs/writer.py:133)'. writer.py:133 is date.today() -- the Last Played half only. The scrobble timestamp is int(time.time()) at track_commit_service.py:72, captured before the resolve await and passed to lastfm.scrobble at :102. The verifier collapsed META-5 into STAB-2 as a duplicate, so the miscitation was never corrected and a triager following the cite will not find the scrobble path. Rule 3 warns about exactly this. Separately I verified STAB's exhaustiveness claim and it holds: exactly two wall-clock reads exist in src/ plus main.py, so DISP's 'every clock in the renderer is monotonic' is also correct.

### Suggested fix

Add track_commit_service.py:72 to the STAB-2 issue so any NTP gate covers both writes, not just Last Played.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **CRIT-9**._
ISSUE_BODY_EOF_92f1
emit META-6 '[META-6] The transient/permanent taxonomy omits discogs_client.exceptions.HTTPError, the library'"'"'s own 429/5xx type' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/errors.py:38`

**Severity moved during adversarial review:** MEDIUM → LOW

### What is wrong

TRANSIENT_EXTERNAL_ERRORS lists only requests exceptions and the two builtins, but the reader's search/release/master calls go through python3-discogs-client, which converts non-2xx statuses into its own HTTPError that does not inherit from requests.exceptions.RequestException. The errors.py:13-14 docstring explicitly (and incorrectly) asserts the Discogs client is requests-based, which is why the tuple is incomplete. Caching is unaffected because discogs_completed=False is set before the classification.

### How it fails

A routine Discogs 429 or 502 during search_collection raises discogs_client HTTPError; is_transient returns False; resolver.py:138 logs 'Unexpected error in Discogs collection search'. An operator reading the journal after an unattended overnight run cannot distinguish routine rate limiting from a genuine defect — the exact discrimination the taxonomy exists to provide (resolver.py:122-124).

### Suggested fix

Add discogs_client.exceptions.HTTPError to TRANSIENT_EXTERNAL_ERRORS and correct the module docstring's claim that the client is requests-based.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-6**._
ISSUE_BODY_EOF_92f1
emit META-8 '[META-8] side_position reports tracklist order, not the position number printed beside it' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/models.py:146`

### What is wrong

The within-side ordinal is the index in side_entries, in tracklist order; the digits captured by _SIDE_RE group 2 (models.py:19) are never used anywhere. An out-of-order tracklist therefore renders a self-contradicting caption.

### How it fails

Tracklist rows [A2 Two, A1 One, B1 Three]. Track 'One' at position A1 renders as 'Side A · 2 of 2'. Display-only; no write consequence.

### Suggested fix

Derive side_position from the parsed group(2) digits, or sort side_entries numerically before indexing.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-8**._
ISSUE_BODY_EOF_92f1
emit META-9 '[META-9] Hyphenated, CD-style and whitespace-padded positions lose all side awareness' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/models.py:19`

### What is wrong

_SIDE_RE only matches ^letters+digits$, so the common Discogs styles 'A-1', '1-01', 'A'/'AA' and any trailing-whitespace position yield side_letter/side_position/side_total = None. The renderer degrades gracefully (renderer.py:790-796 falls back to the raw position), and is_last_track is unaffected, so impact is cosmetic. Trailing whitespace is never stripped and is rendered verbatim.

### How it fails

A release submitted with positions 'A-1'..'B-3' shows 'A-2' where the design calls for 'Side A · 2 of 3'; a position entered as 'A1 ' renders with its trailing space.

### Suggested fix

Strip the position and relax the regex, e.g. ^\s*([A-Za-z]+)[\s.\-]?(\d+)\s*$.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **META-9**._
ISSUE_BODY_EOF_92f1
emit PCONC-3 '[PCONC-3] Confirmation state survives a session end, so confirmation_required is not honoured across the boundary' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** CONC.prev-run.md (reconciled)

**Location:** `src/audio/recognizer.py:174`

### What is wrong

_pending_result / _pending_count / _miss_count are not reset when the session ends, so the second confirmation of a new session can be inherited from before the needle lift.

### How it fails

Halves the number of stale chunks needed to trigger PCONC-1.

### Suggested fix

Reset the confirmation counters when the session epoch changes.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **PCONC-3**._
ISSUE_BODY_EOF_92f1
emit PCONC-4 '[PCONC-4] _BLOCK_QUEUE_MAX does not bound buffering when the loop thread is blocked, and the drop warning is unthrottled' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** CONC.prev-run.md (reconciled)

**Location:** `src/audio/capture.py:196`

### What is wrong

The drop-oldest bound applies to the queue, but when the event-loop thread itself is blocked the backlog accrues in loop._ready instead. The drop warning is emitted per drop — 53 WARNING records in one loop turn was measured.

### How it fails

A blocked loop (see STAB-5, blocking IO in the render path) produces an unbounded backlog plus a log flood onto the SD card.

### Suggested fix

Throttle the drop warning and treat sustained drops as a health signal rather than a per-event log.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **PCONC-4**._
ISSUE_BODY_EOF_92f1
emit REC-5 '[REC-5] A null in the optional album metadata throws away an otherwise-valid title/artist match' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/recognizer.py:111`

### What is wrong

The album lookup iterates track.get("sections", []) and calls meta.get("title", "").lower() with no null guard, so sections: null or a metadata entry with title: null raises inside _parse_shazam. recognize()'s broad except then downgrades the whole response to None.

### How it fails

Shazam returns a correct title and subtitle alongside {"sections": null}: TypeError is raised, the match is discarded as a miss, and six such chunks show "NO MATCH FOUND" for a track that was actually identified every time.

### Suggested fix

Use `track.get("sections") or []`, `section.get("metadata") or []`, `(meta.get("title") or "").lower()`, and wrap the album lookup in its own try/except so it can never sink the whole result.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **REC-5**._
ISSUE_BODY_EOF_92f1
emit SIL-3 '[SIL-3] MUSIC_STOPPED is documented as "inter-track gap" but needs >99% of the 15s window silent, and has no consumer at all' code-review,bug,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/silence.py:24`

### What is wrong

RMS is computed over the whole 15s chunk, so a short gap is diluted by the music either side. With threshold 0.01 against music RMS 0.1 the window only drops below threshold above ~99% silent fraction (>=14.85s of 15s). Separately, grep shows MUSIC_STOPPED has no consumer outside silence.py — main.py handles only MUSIC_STARTED and SESSION_ENDED.

### How it fails

A real 2-6s inter-track gap yields window RMS 0.093 / 0.078 — nowhere near the 0.01 threshold — so the event the comment describes can never fire for the situation it names. Anyone tuning silence_threshold_rms to catch inter-track gaps is chasing an unreachable behaviour.

### Suggested fix

Correct the comment to describe what it means (the whole window went quiet = start of the end-of-session timer), or delete the member; compute sub-window RMS if genuine inter-track detection is wanted.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **SIL-3**._
ISSUE_BODY_EOF_92f1
emit TQ-6 '[TQ-6] sys.modules['"'"'sounddevice'"'"'] stubbed at import time in two test modules and never restored' code-review,testing,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** testing · **Reproduced:** no · **Source:** lens

**Location:** `tests/test_capture.py:37`

### What is wrong

sys.modules.setdefault('sounddevice', MagicMock()) runs at module scope in tests/test_capture.py:37 and tests/test_main_wiring.py:19 and is never undone, so a MagicMock sits in sys.modules for the rest of the process, visible to every other test module. Other files in the same suite do this correctly with patch.dict (test_lastfm_client.py:88 and 8 more sites) or monkeypatch.setitem (test_recognizer_encode.py:34), both of which restore.

### How it fails

Not currently causing failures — the reversed-order run was fully green and nothing else touches sounddevice. The latent risk is a future test that imports or asserts on sounddevice and silently gets a MagicMock. Compounding it, test_capture.py:7-10 admits setdefault means the real module loads on machines with PortAudio, so the suite's module graph differs between CI and a dev Mac.

### Suggested fix

Move the stub into conftest.py behind a session-scoped fixture with explicit teardown, or require PortAudio in the dev environment and import the real module unconditionally.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-6**._
ISSUE_BODY_EOF_92f1
emit TQ-9 '[TQ-9] aiohttp declared but never imported; pytest-cov undeclared; no Python version declared and no CI runs the tests' code-review,testing,severity:low 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** LOW · **Type:** testing · **Reproduced:** yes · **Source:** lens

**Location:** `requirements.txt:31`

**Severity moved during adversarial review:** NIT → LOW

### What is wrong

aiohttp>=3.9.0 at requirements.txt:31 appears in no import anywhere in src/ or main.py (it is transitive via shazamio) and carries no justifying comment, unlike requests and urllib3 which both do (lines 13, 20-23). pytest-cov is used in the project's documented coverage command but is absent from the declared test deps. There is no pyproject.toml, setup.py or python_requires, and the only GitHub workflow is the badge sync — so 632 tests exist and nothing runs them automatically on push.

### How it fails

A fresh Pi install follows requirements.txt, then the documented 'pytest --cov' command fails with 'unrecognized arguments: --cov'. Separately, a contributor on Python 3.9 hits a syntax or stdlib incompatibility with nothing in the repo declaring the constraint and no CI to catch it before it reaches the appliance.

### Suggested fix

Drop aiohttp or add the same one-line justification the other direct-import entries carry, add pytest-cov, and add a minimal CI job that runs pytest on a pinned Python 3.11.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-9**._
ISSUE_BODY_EOF_92f1
emit ARCH-9 '[ARCH-9] Dead fallback branch and vestigial default in _draw_genre_chips' code-review,architecture,severity:nit 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** architecture · **Reproduced:** yes · **Source:** lens

**Location:** `src/display/renderer.py:816`

### What is wrong

chips_rect defaults to None and line 816 falls back to layout.genre_chips, but the sole production caller (renderer.py:682) and both tests (test_renderer_robustness.py:81, :98) always pass a rect. The fallback is a leftover from the pre-push-down fixed-rect geometry.

### How it fails

No runtime failure; it advertises a chip-positioning mode that nothing uses and that the push-down layout would place wrong if anyone did use it.

### Suggested fix

Make chips_rect a required positional parameter and delete the branch.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **ARCH-9**._
ISSUE_BODY_EOF_92f1
emit REC-4 '[REC-4] _same_track docstring claims "whitespace-insensitively" but .strip() only normalises the ends' code-review,bug,severity:nit 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/audio/recognizer.py:230`

### What is wrong

The docstring's stated motivation is that Shazam returns subtly different formatting for the same track, but the implementation normalises only leading/trailing whitespace and case. Internal whitespace differences compare unequal.

### How it fails

"My  Song" vs "My Song" returns False, so the two are treated as different tracks — an unnecessary re-resolve/re-scrobble, or churn that never confirms, exactly the outcome the docstring says the normalisation prevents.

### Suggested fix

Fix the wording, or normalise with " ".join(s.split()).casefold() (plus unicodedata.normalize("NFKC", s) if unicode robustness is wanted).

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **REC-4**._
ISSUE_BODY_EOF_92f1
emit STAB-6 '[STAB-6] No logging policy for an always-on process' code-review,bug,severity:nit 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** bug · **Reproduced:** no · **Source:** lens

**Location:** `main.py:38`

### What is wrong

logging.basicConfig writes to stderr with no rotation and no rate limiting. Under systemd this lands in journald, whose default RateLimitBurst (10,000/30s) sits above STAB-1's 8.7 lines/s, so that storm is not rate-limited away — it just accumulates.

### How it fails

Not a defect standalone; it is what turns STAB-1 from noisy into disk-consuming on a small SD card.

### Suggested fix

Document journald limits (SystemMaxUse/RuntimeMaxUse) in the Pi setup guide, or add a rate-limiting filter on the repeated cover-load warning.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-6**._
ISSUE_BODY_EOF_92f1
emit STAB-7 '[STAB-7] Stale hardcoded sample date in the Last Played comment' code-review,bug,severity:nit 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** bug · **Reproduced:** yes · **Source:** lens

**Location:** `src/metadata/discogs/writer.py:133`

### What is wrong

The inline comment reads `# e.g. "2026-05-24"`, a hardcoded past date presented as the current example alongside date.today().

### How it fails

Cosmetic only; flagged because the project's own review standard makes a stale comment a defect class.

### Suggested fix

Drop the sample date or make it generic (YYYY-MM-DD).

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **STAB-7**._
ISSUE_BODY_EOF_92f1
emit TQ-8 '[TQ-8] Tautological assertion: assert PlayerStatus.ERROR is not None can never be false' code-review,testing,severity:nit 'Wave 5 — Architecture, docs & test debt' <<'ISSUE_BODY_EOF_92f1'
**Severity:** NIT · **Type:** testing · **Reproduced:** no · **Source:** lens

**Location:** `tests/test_error_state.py:91`

### What is wrong

test_error_status_exists asserts 'PlayerStatus.ERROR is not None'. An enum member reached by attribute access is never None, so the assertion itself is unfalsifiable; the only thing that can fail is the attribute lookup. This is the only genuinely unfalsifiable assertion I found in 8553 lines of test code.

### How it fails

Someone renames PlayerStatus.ERROR to PlayerStatus.FAULT and adds a backwards-compat alias; the test passes while asserting nothing about the enum's actual contract. More generally the test gives false assurance that the ERROR status is meaningfully validated.

### Suggested fix

Replace with an assertion on the full expected PlayerStatus member set so a renamed or removed member is also caught.

---
_Filed from `CODE_REVIEW_2026-07-30.md` (Round 3 adversarial cold audit). Finding ID: **TQ-8**._
ISSUE_BODY_EOF_92f1

# ---- re-rate issue #61 (LOW -> MEDIUM, un-defer, pull onto Wave 1) ----------
echo "== re-rating #61 =="
gh issue edit 61 --repo "$REPO" --add-label "severity:medium,data-integrity" --milestone 'Wave 1 — Collection data integrity' >/dev/null 2>&1 && echo "  #61 relabelled + milestoned" || echo "  !! #61 edit failed"
gh issue comment 61 --repo "$REPO" --body-file - <<'ISSUE_BODY_EOF_92f1'
Re-rated **LOW → MEDIUM** and **un-deferred** by the Round-3 adversarial cold audit (2026-07-30).

This issue was parked as "gated on real-world rate-limit evidence, no Pi running." That gate is now moot: the 429 path was **reproduced** to have data-integrity consequences, not just a parked executor worker.

- **META-1**: when the pre-write READ of the Play Count field hits a persistent 429, the absolute write resets the accumulated count to `1` and logs success.
- **META-10**: a persistent 429 silently loses the play, and `Retry-After` is clamped below what Discogs asks.

Pulled onto the **Wave 1 (collection data integrity)** milestone and coupled to the transport/429 hardening rather than left as standalone tech-debt.
ISSUE_BODY_EOF_92f1

echo
echo "Done. Expected 88 new issues; map written to $MAP:"
wc -l "$MAP"
echo "(If the count is not 88, some create calls failed — see stderr above.)"
