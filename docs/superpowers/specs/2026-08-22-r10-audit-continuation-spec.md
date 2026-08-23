# R10 Cold-Audit Remediation Continuation Specification

> **For agentic workers:** This is the portable handoff for completing the
> 2026-08-22 pre-launch cold audit. Read this file, `CLAUDE.md`, and
> `docs/decisions/remediation-guardrails.md` before changing code. Verify all
> live GitHub state rather than trusting dates or status text in this snapshot.

**Snapshot date:** 2026-08-22/23 (America/Chicago)  
**Repository:** `lanebecker/vinyl-now-playing`  
**Starting main SHA:** `857876eca334ff1ce47ec3461287d3463f635ebc`  
**Latest controlled release:** `v1.5.35` at
`4b79513b6811c1884be42dadbb1d45f2354d70a6`  
**Supplemental audit source:** local, gitignored project record
`CODE_REVIEW_2026-08-22-R10.md`, when present  
**Purpose:** preserve the complete plan, decisions, completed evidence, open
gates, and working protocol so a new model can resume without reconstructing
the audit from chat history.

## 1. Outcome and current posture

The adversarial audit accepted 21 findings: 4 High, 10 Medium, and 7 Low. It
also carried five pre-existing issues into the wave plan. No Critical code
defect was found. Release governance, dependency scanning, licensing, the
controlled-release path, Wave 1 software, Discogs cache truthfulness, metadata
recovery, and finalizer cancellation hardening have since been implemented.

The repository has made its first controlled release, but the appliance has
not yet been brought up on the target Raspberry Pi. Do not claim first-live
readiness until the Wave 1 hardware and live-integration gates are recorded.

Current work order:

1. Resume **Wave 3** in software while the Pi is unavailable.
2. Complete **Wave 4** after or alongside Wave 3 when it does not obscure the
   higher-risk pipeline work.
3. Complete **Wave 5** only with its explicit visual approval checkpoint.
4. Return to the open **Wave 1** hardware/live gates when the owner has the Pi.
5. Keep the remaining Architecture v1.6 seams deferred unless a point fix
   makes one unavoidable.

## 2. Authority and non-negotiable decisions

Use this precedence when sources disagree:

1. Explicit owner decisions recorded below.
2. Current tested production behavior and ratified regression tests.
3. `docs/decisions/remediation-guardrails.md`, issue history, CHANGELOG, and
   current operational documentation.
4. `DESIGN.md` only as maintained design intent. It is partly outdated and is
   not a canonical executable specification.

Binding owner decisions:

- Preserve the **system-service** deployment architecture. A user service may
  be a documented Wayland fallback, not an implicit architectural migration.
- Never treat divergence from `DESIGN.md` alone as a bug. Discuss material
  divergence before changing production.
- Production keeps global album adjacency across side boundaries.
- Production keeps square genre chips and the muted-blended adjacent-panel
  divider. Remove the dead chip-radius field; do not restyle the output.
- Discogs normal positive cache entries remain immortal. Recovery is allowed
  only after definitive missing-instance proof, conditional invalidation, a
  complete fresh collection view, exactly one safe replacement, and one
  bounded retry. All recovery state is memory-only.
- Ordinary META-7 behavior remains: Last Played may succeed independently when
  an ordinary Play Count write fails. Missing-instance recovery failure must
  suppress Last Played because target identity is unsafe.
- Last.fm is best-effort. Retry only definite failures in bounded memory;
  preserve confirmation-time timestamps, epoch gating, and per-spin dedup;
  never retry ambiguous outcomes; no durable outbox.
- Recognition freshness should remain hard bounded and favor recent audio,
  while preserving drop-oldest correctness, two-hit confirmation, and the
  throttled sustained-drop health signal.
- Runtime/dev dependencies may be split, but Python 3.11 and Pi OS Bookworm
  support must not be raised accidentally. Do not introduce a platform-specific
  lockfile as a universal deployment artifact.
- Keep Impeccable tooling, live configuration, and critique history. Archive
  only the project-specific stale `design/.impeccable/design.json` snapshot.
- The prototype may intentionally have richer empty states than production.
- Prototype palette changes require a rendered before/after visualization and
  owner approval **before implementation**. Retained guarantees are at least
  4.5:1 contrast on every rendered background and at least 60 degrees of hue
  separation.
- Live Discogs writes, credential use, Pi service installation, reboot,
  hot-plug, and power-loss tests are explicit external-action gates. Never
  infer authorization from this document.

## 3. Regression contract for every task

### Workspace and Git safety

- Inspect the canonical checkout before work. It may contain owner changes and
  unrelated untracked artifacts. Do not clean, reset, stash, or absorb them.
- Use an isolated worktree from current `origin/main`.
- Every human handoff command must begin with an absolute `cd` to the intended
  directory. Run commands yourself when permissions allow.
- Stage explicit paths only. Never use `git add -A` or a broad staging glob.
- Use a `codex/` branch, focused commits, and `Closes #N` only when the PR truly
  satisfies that issue's acceptance criteria.

Safe starting pattern (choose a unique absolute temporary path):

```bash
cd '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing'
git status --short --branch
git worktree list
git fetch origin main
git rev-parse --verify origin/main
test ! -e '/private/tmp/vnp-r10-next-wave'
git worktree add '/private/tmp/vnp-r10-next-wave' -b 'codex/r10-next-wave' origin/main
cd '/private/tmp/vnp-r10-next-wave'
```

Read the first two commands before proceeding: they are the owner-change and
path-collision check. If the sample path or branch already exists, choose a new
task-specific absolute name; do not delete or repurpose the existing target.

### Development and review

- Read the complete issue body, comments, linked historical issues, tests, and
  current implementation before writing a spec.
- For multi-step work, write/review a concrete implementation plan first.
- Use test-driven development: demonstrate RED for the intended failure,
  implement the smallest safe change, then run focused and integrated GREEN.
- Use an independent adversarial reviewer for each material task and again for
  the whole wave. Required verdict is `SPEC PASS / QUALITY PASS`, with zero open
  material findings. Fix and re-review until that boundary is met.
- Attack cancellation, concurrency, retry, idempotency, cache truth, partial
  failure, malformed input, long uptime, and shutdown—not only happy paths.
- Do not weaken a regression test to fit new code. A deliberate contract change
  must be explicit in the spec and historical guardrails.

### Verification and merge

- Run `git diff --check`, targeted tests, the wave's integrated suite, and
  compile/lint checks appropriate to changed files.
- The old local `.venv` uses unsupported Python 3.9 and has known dependency and
  sandbox limitations. It is useful for focused tests but is not the release
  authority.
- A PR must pass the exact supported Python 3.11/3.12/3.13 test matrix.
- If dependency audit path filters do not select the branch, manually dispatch
  `security.yml` for the exact branch SHA and require all three versions green.
- Merge only after live checks are green. Verify the resulting main SHA and its
  post-merge tests. Close issues with links to the PR, merge SHA, tests, and any
  live evidence. Never close an external/hardware acceptance item on software
  evidence alone.
- Update durable docs and, when present in the working environment, the local
  audit, `log.md`, and shared memory when a decision or reusable invariant
  changes. Keep present-tense status statements reconciled.

## 4. Completed work and evidence

### Wave 0 — Release trust (complete)

Milestone `R10 Wave 0 — Release trust` is closed.

- PR #433 established protected-main and controlled-release governance.
- PR #436 published the first controlled release, `v1.5.35`, from exact SHA
  `4b79513b6811c1884be42dadbb1d45f2354d70a6`.
- PR #437 reconciled closeout documentation.
- Issues #295, #402, #415, #416, and #417 are closed.
- Main requires PRs and Python 3.11/3.12/3.13 checks; force push/deletion are
  blocked; tag/release publication is mediated by a narrowly scoped GitHub App.
- Releases are immutable; the release workflow validates exact-SHA test
  provenance, VERSION/CHANGELOG/badge consistency, and publication state.
- Dependency graph/alerts/security updates and a three-version `pip-audit`
  workflow are active.
- The MIT license is tracked and detected.

Do not regress the release security-contract tests, GitHub App identity and
least privilege, environment approval, exact-SHA gating, or immutable-release
policy.

### Wave 1 — Pi preflight and deployability (software complete; live gates open)

PR #438 merged at main SHA
`7915dcacea00dea7846b3d0dfa4b915ec6f74dbe`. Issue #156 is closed. Software now
includes the config permission guard, installed-sounddevice CI smoke, versioned
service renderer/verifier, fail-closed preflight/deploy/rollback instructions,
and an ID-bound Discogs live-write checker. Supported CI was green.

The milestone remains open because the owner has not brought the app up on the
Pi. Open issues:

- #418 — R10-04 real credential permissions. Verify the real Pi config is
  `0600` and the runtime guard redacts every secret value.
- #419 — R10-15 production service artifact. Render/install/verify it on the
  chosen Pi OS image without changing the system-service architecture.
- #366 — custom Discogs folder live-write hypothesis. Requires an
  owner-authorized sacrificial record in the exact configured folder and
  before/after evidence. `--yes` must be bound to explicit positive release and
  instance IDs; same-name ambiguity must fail closed.

Required real-hardware evidence before closing Wave 1:

- Pi OS/Python versions, timezone and NTP synchronized state.
- UCA222 enumeration, selected input stream, real `sounddevice` API, and USB
  hot-plug while the service is initially unarmed/idle.
- Wayland/Xwayland display discovery and readable `XAUTHORITY` for the service
  user, or the documented fallback decision.
- `systemd-analyze verify`, cold boot/autostart, unchanged MainPID where
  required, bounded restart behavior, and clean SIGTERM during credit.
- Shazam, Discogs read, cover CDN TLS, full-side credit, offline/time-sync boot,
  power-loss recovery, and exact custom-folder write evidence.
- Known-good rollback using the documented detached worktree/relocated venv
  procedure.

### Wave 2 — Metadata recovery and cache coherence (complete)

PR #440 merged at main SHA
`817d5fcb6fd57c03026ccfc8d3243fa257ef13e8`. Issues #420 (R10-05) and #421
(R10-12) are closed; milestone #53 is closed.

The implementation distinguishes clean, unknown, failed, cooldown-skipped, and
incomplete collection outcomes; never promotes a partial rebuild; retains the
last complete snapshot; and uses bounded failure backoff. Missing-instance
recovery is serialized, preserves instance multiplicity, proves a strict
single-page complete response, requires exactly one eligible replacement,
revalidates before write, conditionally invalidates cache state, and spends one
recovery budget. Duplicate copies, pagination ambiguity, identity drift, and
refresh failure refuse credit. The integrated Wave 2 suite passed 421 tests,
and supported CI/dependency audit passed.

PR #441 then closed #439 at main SHA
`857876eca334ff1ce47ec3461287d3463f635ebc`. Executor-backed Discogs operations
now keep the finalizer serialized until an already-submitted worker exits,
retrieve late success/failure, and re-propagate cancellation, including repeated
cancellation. The focused suite passed 100 tests before the final narrow
hardening; adversarial rereview passed; exact PR CI and dependency audit passed
on Python 3.11/3.12/3.13.

## 5. Remaining implementation waves

### Wave 3 — Optional-side-effect isolation

**Milestone:** `R10 Wave 3 — Optional-side-effect isolation` (#54)  
**Open issues:** #422 (R10-09), #423 (R10-10), #424 (R10-11)  
**Priority:** next software wave; P2 Medium findings.

#### Task 3.1 — Specify one end-to-end backpressure model

Before code, write a reviewed state-machine/lifecycle spec covering:

- a small, bounded, lifecycle-owned, single-consumer `ScrobbleDispatcher`;
- the point at which epoch validation and an in-flight reservation occur;
- immutable confirmation-time timestamp capture at enqueue;
- distinct `in_flight`, `delivered`, definite-failure, ambiguous, retryable, and
  dropped-at-shutdown outcomes;
- bounded in-memory retry count/backoff for definite failures only;
- serialization of pylast access, default-executor use, bounded shutdown drain,
  overflow/loss policy, and no untracked bare tasks;
- recognition queue age, overflow/coalescing, and relationship to the dispatcher.

Conflict checks: preserve #61 executor serialization, #383 confirmation-time
timestamp, #48 drop-oldest freshness, #153 throttled drop-health logging, epoch
guards, per-spin dedup, and two-hit confirmation.

#### Task 3.2 — Implement #422 and #423 together

Move optional Last.fm network latency out of the sole recognition consumer. A
slow Last.fm call must not delay the next recognition dequeue/commit. Commit the
delivered latch only after success. Definite failure becomes retry-eligible
without simultaneous duplicates; ambiguous outcome is no-retry. Document the
best-effort guarantee in product/user docs.

Required adversarial tests include slow executor work, full dispatcher queue,
duplicate same-spin commits, epoch change, cancellation before/after enqueue,
late success, definite and ambiguous failure, retry exhaustion, shutdown during
work/backoff, executor exception, and task-leak detection.

#### Task 3.3 — Implement and hardware-tune #424

Add queue-age telemetry first. Implement a bounded latest-value mailbox, queue
size 1–2, or drain-to-newest policy only after the contract is reviewed. A
30-second backend stall must resume on bounded-fresh audio instead of consuming
roughly 50 seconds of stale FIFO history. Preserve memory bounds, health logs,
session/epoch correctness, and two-hit confirmation.

Software can establish deterministic queue semantics now, but do not close #424
until a physical record test confirms recognition reliability on the Pi if the
issue acceptance criteria require it.

**Wave 3 exit gate:** Last.fm delay/failure cannot stall recognition; retry and
loss semantics match owner decisions; a backend stall resumes on fresh-enough
audio; shutdown is bounded and leak-free; supported CI/audit and independent
whole-wave adversarial review pass.

### Wave 4 — Documentation and dependency hygiene

**Milestone:** `R10 Wave 4 — Docs & dependency hygiene` (#55)  
**Open issues:** #425 (R10-13), #426 (R10-14)  
**Priority:** low-risk P3 cleanup.

#### #425 — Reconcile CI and cover-cache boundary docs

Audit current workflow names/triggers and Python matrix legs rather than copying
the original finding. Ensure README/testing/deployment docs identify tests,
version metadata, dependency audit, and their path/dispatch behavior. State the
actual cover-cache boundary: downloaded bytes and derived image/render caches
must not be conflated. Remove present-tense claims superseded by Waves 0–2.

#### #426 — Split runtime and development dependencies

Produce a minimal runtime install that excludes pytest and other test-only
tools, plus an explicit development/test manifest. Preserve environment markers
such as the Python 3.13 `audioop-lts` path and Python 3.11/Bookworm support.
Update Dependabot, CI, Pi setup, rollback, and dependency audit so both runtime
and supported development environments remain covered. Test a clean runtime
install with imports/startup probes; do not rely on packages leaked from a
developer environment.

**Wave 4 exit gate:** all durable docs match live workflows/cache boundaries;
clean runtime install excludes test packages and passes startup probes on every
supported Python/Pi target; CI and three-version audit remain green.

### Wave 5 — Design reconciliation and tooling

**Milestone:** `R10 Wave 5 — Design reconciliation & tooling` (#56)  
**Open issues:** #58, #427 (R10-16), #428 (R10-18), #429 (R10-19), #430
(R10-20), #431 (R10-21). R10-17's palette contract is carried by reopened #58.

Execute in this order:

1. Add/verify a prominent scope/status header in `DESIGN.md`; reconcile global
   cross-side adjacency, square chips, and muted-blended divider. Remove the
   dead `chip_radius` model field while proving identical render output.
2. Inventory prototype states/variants. Retain only shipped behavior and
   explicitly planned explorations; make every retained path reachable without
   DevTools. Add fixtures for first/last track, 0/null/4+ genres, missing cover,
   long text, and retained empty/paused/error paths. Preserve the richer
   prototype empty-state review mode.
3. Prepare at least two palette candidates. Render all five albums at 1024x600
   in a labeled current-versus-candidate contact sheet. Include contrast against
   every actual background and minimum pairwise hue distance. **Stop and obtain
   owner visual approval. Do not modify palette data before approval.**
4. Commit only the approved palette and dependency-free contract tests. Make
   standalone controls visible when not hosted, keyboard operable, and give
   every switch an accessible name/state.
5. Move `design/.impeccable/design.json` to
   `design/archive/impeccable-design-2026-06-10.json`, preserving history.
   Keep Impeccable tooling, live config, and critique artifacts.

Production Hue Diversity remains deferred. A prototype change does not
authorize a production palette registry or order-dependent global state.

**Wave 5 exit gate:** maintained sources clearly state authority; every retained
prototype path is reachable/tested; owner-approved palette metrics pass; current
production render decisions are preserved; stale project snapshot is archived
without disabling Impeccable.

### Architecture v1.6 seams — deferred

Milestone #57 remains open with #394 (CoverPipeline consolidation), #405
(mutated Pillow global), #218 (persistence/latch seam), and #219 (idle-screen
seams). #439 is closed. Do not pull these ahead of Waves 3–5 or first Pi bring-up
unless a point fix necessarily touches the same seam. When planned, combine
#394/#405 if they share the cover pipeline; align #218/#219 with an approved
v1.6 feature spec.

## 6. Live issue/milestone snapshot

| Wave | Milestone | State at handoff | Issues |
|---|---|---|---|
| 0 | #51 Release trust | Closed | completed #295, #402, #415, #416, #417 |
| 1 | #52 Pi preflight | Open | #418, #419, #366 open; #156 closed |
| 2 | #53 Metadata recovery | Closed | #420, #421 closed |
| 3 | #54 Optional-side-effect isolation | Open | #422, #423, #424 |
| 4 | #55 Docs & dependency hygiene | Open | #425, #426 |
| 5 | #56 Design reconciliation | Open | #58, #427–#431 |
| v1.6 | #57 Architecture seams | Open | #394, #405, #218, #219; #439 closed |

Always query GitHub again before acting. Do not use issue number #425 for the
old R10-15 service finding; R10-15 is #419. That mapping was previously confused
and explicitly corrected.

## 7. Resume checklist for the next model

1. Verify `origin/main`, PRs, issues, milestones, latest release, protection,
   and current Actions state. Confirm whether this spec's docs-only PR has
   already merged.
2. Read the durable guardrail documents and live issue history. If the local
   gitignored `CODE_REVIEW_2026-08-22-R10.md` exists, use it as supplemental
   provenance; its absence is not a blocker. This committed spec, current
   source/tests, `remediation-guardrails.md`, and live GitHub history are the
   portable authority.
3. Inspect the canonical checkout without modifying it; create an isolated
   worktree from current main.
4. Start Wave 3 with a no-code history/source preflight and a reviewed delivery,
   cancellation, retry, and backpressure state-machine spec.
5. Map exact RED tests and integration commands before implementation.
6. Execute in small reviewed tasks. Keep issue comments current after each task,
   especially when software is complete but a hardware gate remains.
7. At every wave boundary, run a cold whole-branch adversarial pass, merge only
   after supported CI/audit, close only satisfied issues, reconcile docs, and
   append evidence to the audit/log/memory.

## 8. Definition of project-audit completion

This audit is complete only when:

- Waves 0–5 have passed their exit gates or have an explicit owner waiver with
  rationale and residual risk;
- the target Pi first-boot/service/audio/display/network/Discogs checks are
  recorded green, including #366's exact folder behavior;
- all audit issues have evidence-backed final states and milestones accurately
  reflect open work;
- visual changes were approved from rendered evidence before implementation;
- supported CI and dependency audit are green on the final main SHA;
- durable documentation and all locally present audit/log/memory records agree;
  and
- deferred v1.6 seams remain explicitly scheduled rather than accidentally
  represented as pre-launch completion requirements.

Until then, report the application as software-remediated through Wave 2 with a
successful controlled release, **not** as fully proven on its production Pi.
