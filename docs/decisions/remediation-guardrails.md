# Remediation Decision Guardrails

This is the tracked ledger of decisions that remediation work must preserve. It exists because the detailed audit reports and `log.md` are intentionally local and gitignored, while closed issues, comments, and source-level ratifications can otherwise be easy to miss.

Update this file whenever the owner changes a listed decision, a remediation establishes a new cross-cutting invariant, or a closed/deferred issue becomes relevant again. Do not duplicate detailed implementation history here; link to its durable source.

**Last reconciled:** 2026-08-22, before Round 10 remediation.

## Authority and conflict order

When sources disagree, stop and resolve the conflict in this order:

1. The owner's latest explicit decision.
2. Tested, currently shipped production behavior for already-ratified behavior.
3. GitHub issue bodies, owner comments, and closure rationale.
4. `PRODUCT.md`, `docs/architecture.md`, `docs/roadmap.md`, and operational checklists.
5. `DESIGN.md` and the prototype for design intent only. Tested production wins while an outdated section awaits reconciliation.
6. Local audit reports and `log.md` as historical evidence, not as the only durable authority.

Never treat a test as proof that its behavior is intentional without checking the linked decision. Once intent is confirmed, keep or add a behavior-level regression test.

## Required contract for every remediation

Before changing code, configuration, workflows, or behavior:

1. Read the current issue and every linked predecessor or conflict.
2. Search this ledger, `CHANGELOG.md`, local `CODE_REVIEW_*.md`, and `log.md` for the issue number and touched symbols.
3. Identify the tests and source comments that encode behavior the change must preserve.
4. Check product, architecture, roadmap, setup, testing, and first-boot documentation relevant to the change.
5. Write an issue-specific contract naming: intended change, preserved invariants, prohibited regressions, verification commands, and any hardware/external-state proof that remains necessary.
6. Use RED-first tests for behavior changes. Run the targeted preserved-behavior tests before and after, then the supported CI matrix.
7. Update this ledger if the remediation creates or changes a cross-cutting decision.

Workspace safety is part of the contract: every handoff command block begins by changing to `/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing` with an absolute path; preserve user-owned ignored/untracked files, never stage `config.yaml`, and stage explicit paths rather than `git add -A`.

## Global invariants

### Data integrity

- Prefer a missed Discogs credit to a phantom or wrong-record credit.
- Ambiguous external-write outcomes are not retried automatically when a retry could double-apply.
- Discogs write targets require safe release and instance identity; never guess across ambiguous owned releases.
- Confirmation, session epoch, side coverage, and per-spin deduplication gates remain load-bearing unless the owner explicitly changes the product behavior.

### Runtime and deployment

- Supported Python is 3.11, 3.12, and 3.13; Raspberry Pi OS compatibility, not the local Python 3.9 `.venv`, controls dependency decisions.
- The application remains a system service. Preserve the network/time ordering and retry semantics established by #83 and #201.
- `config.yaml` contains write-capable secrets, remains untracked, and must be mode `0600` on the appliance.
- Hardware-only claims stay hypotheses until the first-boot checklist records real-device evidence.

### Release and repository governance

- A release is the exact current `main` SHA after all three supported-Python checks succeed for that SHA.
- Every public version receives one immutable tag and one GitHub Release created together by the controlled workflow. Tags alone are not the supported release surface.
- GitHub immutable releases remains enabled so a published release's tag and assets cannot be modified or deleted.
- VERSION, CHANGELOG, README badge, tag, GitHub Release, and tested SHA must agree.
- Protected `main` must require the three matrix checks; direct automation writes need no broad bypass.
- Third-party Actions remain pinned to full commit SHAs and job permissions remain least-privilege.

### Recognition and optional services

- Recognition freshness is bounded; do not replace drop-oldest/coalescing behavior with an unbounded queue.
- Last.fm remains best-effort: preserve confirmation-time timestamps and per-spin deduplication; retry only definite failures in memory, never ambiguous outcomes, and do not add a durable outbox without a new owner decision.

### Display and design

- Production targets the 1024×600 Waveshare display and preserves the existing legibility floors and font fallback behavior.
- All rendered text roles maintain at least 4.5:1 contrast on their actual backgrounds.
- Production palette extraction remains authentic and order-independent; cross-album Hue Diversity is prototype-only.
- Cross-side PREV/NEXT adjacency, square chips, and the muted-blended divider are ratified production behavior.
- Prototype palette changes require rendered 1024×600 current-versus-candidate views for all five albums and owner approval before implementation.
- Impeccable remains part of the design workflow. Only the project-specific stale `design/.impeccable/design.json` snapshot is scheduled for archival; live configuration and critique history stay.

## Deliberately deferred or rejected alternatives

| Decision | Durable source | Revisit trigger |
|---|---|---|
| No production cross-album Hue Diversity registry | [#73](https://github.com/lanebecker/vinyl-now-playing/issues/73) | Similar back-to-back physical-display palettes are genuinely distracting. |
| Do not persist the Discogs collection index | [#169](https://github.com/lanebecker/vinyl-now-playing/issues/169) | A persistence design safely handles remove/re-add instance changes. |
| Do not guess through the reprise/bookend ambiguity | [#227](https://github.com/lanebecker/vinyl-now-playing/issues/227) | A signal distinguishes a genuine same-release replay without phantom credit. |
| Do not wildcard `Various` compilation matching | [#265](https://github.com/lanebecker/vinyl-now-playing/issues/265) | Real evidence justifies a rule that cannot over-credit the wrong release. |
| Keep `ChunkAssembler`'s bounded `np.concatenate` copy | [#259](https://github.com/lanebecker/vinyl-now-playing/issues/259) | Measured Pi performance makes the copy material. |
| Keep `np.mean(audio ** 2)` for silence RMS | [#404](https://github.com/lanebecker/vinyl-now-playing/issues/404) | An alternative preserves the exact documented threshold boundary. |
| Keep the bounded 128-entry label cache | [#372](https://github.com/lanebecker/vinyl-now-playing/issues/372) | Measured churn becomes visible or operationally material. |
| Accept the cover-header slow-drip residual | [#176](https://github.com/lanebecker/vinyl-now-playing/issues/176) | Executed evidence shows a practical hang or denial-of-service path. |
| Verify custom Discogs-folder writes on hardware | [#366](https://github.com/lanebecker/vinyl-now-playing/issues/366) | Wave 1 first-boot probe determines whether code changes are needed. |

## Round 10 wave contracts

### Wave 0 — release trust and security

- **#402 / R10-01:** authenticate the exact-SHA `push` run of `.github/workflows/tests.yml` and require its successful `test (3.11)`, `test (3.12)`, and `test (3.13)` jobs; keep the tag consistency workflow for out-of-band tag pushes; fail closed on missing, pending, failed, wrong-SHA, wrong-workflow, or wrong-run checks; recheck `main` immediately before publication.
- **#415 / R10-02:** protect `main`, require all three checks, block force-push/deletion, and remove the badge bot's direct write instead of granting a broad Actions bypass.
- **#295 / R10-03:** keep Dependabot and repository security features enabled; retain a failing dependency-advisory CI scan; preserve Python 3.11/Pi compatibility and document any security-update waiver.
- **#416 / R10-07:** the controlled workflow creates the GitHub Release together with the tag; the next public release, rather than rewritten historical tags, restores the Latest Release surface.
- **#417 / R10-08:** ship the full MIT grant as `LICENSE`, copyright 2026 Lane Becker, and link the README claim to it.

**Operational checkpoint (2026-08-22):** the first Wave 0 bundle is implemented locally and independently break-reviewed. It is not operationally complete until the following ordered gates succeed:

1. Merge the bundle through a `codex/` branch and pull request; do not stage ignored audit artifacts, issue scripts/maps, release-note snapshots, or `config.yaml`.
2. Observe the tests, version-metadata, and Python 3.11–3.13 dependency-audit workflows on GitHub. Address failures before making any new check required.
3. Activate the `main` ruleset only after the read-only metadata workflow is present on `main`: require pull requests and `test (3.11)`, `test (3.12)`, and `test (3.13)`; block force-push and deletion; include administrators and grant no broad bypass.
4. Activate a `v*` tag-creation ruleset that permits the controlled release workflow while preventing ad-hoc release-tag creation. Do this before the first controlled release dispatch.
5. Verify GitHub detects `LICENSE` as MIT before closing #417. Keep #416 open until a controlled release is Latest and its version, newest tag, and linked tested SHA agree.

GitHub immutable releases is already enabled. The pre-merge live-state check still reports unprotected `main` and no rulesets; that is an intentional sequencing gate, not approval to dispatch a release.

### Later waves

- Wave 1 preserves the system-service decision and treats #366 as a real-hardware gate.
- Wave 2 preserves immortal positive collection reads during normal operation; only a definitive missing-instance write may invalidate, refresh, and permit one identity-safe retry.
- Wave 3 preserves recognition freshness, confirmation timestamps, epoch gates, and spin deduplication while isolating Last.fm latency.
- Wave 4 keeps floor-based dependency manifests and Python 3.11 compatibility; it does not introduce a platform-specific lockfile implicitly.
- Wave 5 begins with design-source reconciliation and owner-visible render approval; it cannot use outdated `DESIGN.md` alone as evidence that production is defective.
- Wave 6 remains post-launch architecture work unless an earlier point fix makes the same refactor unavoidable.
