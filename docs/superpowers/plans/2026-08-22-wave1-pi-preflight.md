# Wave 1 Pi Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Apply superpowers:test-driven-development to every code or behavior change and superpowers:verification-before-completion before any pass claim. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make first deployment fail closed on exposed credentials, prove the real audio package in CI, version the supported system-service deployment, and make the deferred Discogs live write safe to execute.

**Architecture:** Four narrow boundaries remain independent: open-file config security, a pre-pytest installed-package smoke, a validated system-unit renderer, and an explicitly confirmed live-write probe. CI/software tests establish deployability; the target Pi establishes hardware, session, and external-write truth.

**Tech Stack:** Python 3.11–3.13, pytest, PyYAML, GitHub Actions, Ubuntu/apt, sounddevice/PortAudio, systemd, Discogs API.

**Spec:** `docs/superpowers/specs/2026-08-22-wave1-pi-preflight-design.md`

## Global Constraints

- Work only in `/private/tmp/vnp-release-app-identity` on branch `codex/r10-wave1-pi-preflight`, based on `origin/main` SHA `3d92432396473aafbe6ee9db38c98109f5d36449`.
- `DESIGN.md` is noncanonical. Preserve the owner-ratified system-service model and the historical contracts from #83 and #201.
- Never read, print, stage, copy, or rewrite the ignored live `config.yaml`. It has already been contained at mode `0600` in the canonical clone.
- Never use `git add -A`; stage only the explicit files owned by the current task.
- Every test begins RED for the intended reason before production implementation. Record RED and GREEN commands/results in the SDD task report.
- Use the canonical clone's venv only as this absolute interpreter:
  `/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python`.
- The local interpreter is unsupported Python 3.9.6 and the sandbox denies socket binds/fonts. The full local baseline is 16 failed, 1522 passed, 3 warnings. Do not normalize those known failures into product defects; require exact supported GitHub CI before merge.
- No task may claim real UCA222, Wayland/Xwayland, cold-boot, time-sync, display, or Discogs semantics from mocks or CI.
- Do not execute the Discogs write until Lane supplies the exact custom-folder artist/album and explicitly approves that one external write.

## File and interface map

| Task | Owns | Interface consumed by later tasks |
|---|---|---|
| 1 | `src/config.py`, `tests/test_config.py` | `load_config` rejects unsafe POSIX modes before YAML parsing. |
| 2 | `scripts/check_audio_backend.py`, `tests/test_audio_backend_smoke.py`, `conftest.py`, `.github/workflows/tests.yml`, focused CI-contract tests | CLI returns nonzero unless the real installed module exposes every production-used API. |
| 3 | `deploy/vinyl-now-playing.service.in`, `scripts/render_system_service.py`, `tests/test_system_service.py`, `.github/workflows/tests.yml` | Renderer validates inputs/config mode and atomically emits the sole supported system unit. |
| 4 | `scripts/discogs_live_check.py`, `tests/test_discogs_live_check.py` | Write is impossible without explicit record selection plus confirmation/`--yes`. |
| 5 | `docs/pi-setup-guide.md`, `docs/first-boot-checklist.md`, `docs/testing-guide.md`, `docs/decisions/remediation-guardrails.md`, `CHANGELOG.md` | One durable procedure and hardware evidence form for Tasks 1–4. |
| 6 | all Wave 1 files | Fresh integrated verification and adversarial review; no implementation. |
| 7 | GitHub PR/issues/milestone and target Pi | Exact-SHA CI, merge, issue evidence, then hardware-only closeout. |

---

### Task 1: Enforce `config.yaml` POSIX permissions before parsing (#418)

**Files:**
- Modify: `src/config.py`
- Modify: `tests/test_config.py`

- [x] **Step 1: Write focused RED tests**

Add a helper that writes valid YAML and explicitly chmods it `0600`. Add tests proving:

1. `0600` parses normally.
2. Each of `0640`, `0604`, and `0644` raises `ConfigError` naming the path and `chmod 600 <path>` without the sentinel token value.
3. The permission decision uses `os.fstat` on the opened descriptor before `yaml.safe_load`; a mocked parser must not be called for an unsafe file.
4. An injected unsupported-mode result logs one warning and continues.
5. Existing empty/UTF-8/unreadable/directory tests retain their original error class/meaning; valid file fixtures use `0600`.

Run:

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight' && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_config.py
```

Expected RED: only the new permission-contract tests fail because no open-file mode check exists.

- [x] **Step 2: Implement the minimum open-file guard**

Inside the existing `with open(...) as f`, call a small private helper with
`os.fstat(f.fileno())` before reading. On POSIX, evaluate `stat.S_IMODE` and
raise `ConfigError` when `mode & 0o077`. When mode semantics are unavailable,
log one warning and continue. Preserve all existing `ConfigError` normalization;
do not chmod or inspect ownership in this change.

- [x] **Step 3: Verify GREEN and regression scope**

Run the focused command above, then:

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight' && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_config.py tests/test_ops_polish_r9.py && git diff --check
```

- [x] **Step 4: Independent spec and quality review**

Reviewer must attempt secret-bearing `0644`, parse-before-check, directory,
missing-file, and unsupported-platform mutations. Resolve every confirmed
finding and rerun the focused suite before explicit-path staging.

---

### Task 2: Prove the installed audio backend before pytest stubbing (#156)

**Files:**
- Create: `scripts/check_audio_backend.py`
- Create: `tests/test_audio_backend_smoke.py`
- Modify: `conftest.py`
- Modify: `.github/workflows/tests.yml`
- Modify: a focused workflow-integrity test file selected after inspecting current ownership

- [x] **Step 1: Write RED behavior and workflow tests**

Test a pure `validate_backend(module)` function with a complete fake and one
missing/non-callable case for each of `query_devices`, `InputStream`,
`_terminate`, and `_initialize`. Test CLI import failure redaction/actionability.
Add workflow assertions requiring `libportaudio2` installation and the smoke
command before `pytest`. Add a conftest helper test proving a successful real
import is retained while import failure receives a centrally-owned stub.

Run:

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight' && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_audio_backend_smoke.py tests/test_workflow_integrity_r10.py
```

Expected RED: new script/API and workflow steps do not exist and conftest still
tests only `sys.modules` membership.

- [x] **Step 2: Implement the isolated smoke and scoped fallback**

Create a dependency-free CLI that imports `sounddevice`, validates the exact
four callables, prints package version only as non-secret diagnostics, and exits
nonzero with an actionable message on failure. In CI install `libportaudio2`
before pip dependencies and run `python scripts/check_audio_backend.py` before
pytest on every matrix leg. Refactor conftest to attempt the real import and
install/remove only its own fallback stub if that import fails.

- [x] **Step 3: Verify GREEN**

Run the RED command, `tests/test_capture.py`, `tests/test_main_wiring.py`, and
`tests/test_ops_polish_r9.py`, then `git diff --check`. Do not claim live device
coverage.

- [x] **Step 4: Independent adversarial review**

Reviewer must mutate every required API away, move the smoke after pytest,
remove PortAudio, simulate a pre-existing third-party module, and test teardown
ownership. Resolve confirmed findings before explicit-path staging.

---

### Task 3: Version and validate the system service (#419)

**Files:**
- Create: `deploy/vinyl-now-playing.service.in`
- Create: `scripts/render_system_service.py`
- Create: `tests/test_system_service.py`
- Modify: `.github/workflows/tests.yml`
- Modify: the same focused workflow-integrity test file selected in Task 2

- [x] **Step 1: Write RED renderer and invariant tests**

Pin exact assertions for all preserved directives, `Type=simple`, service user,
working directory, `DISPLAY`, `XAUTHORITY`, absolute venv Python/main paths, and
`WantedBy=graphical.target`. Test safe substitution, rejection of relative or
newline/whitespace-injection values, missing app/config, non-`0600` config,
atomic output, byte-identical rerender, and unchanged config mode. Add workflow
assertions requiring representative render plus `systemd-analyze verify` on
Ubuntu.

Run:

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight' && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_system_service.py tests/test_workflow_integrity_r10.py
```

Expected RED: template, renderer, and CI verification do not exist.

- [x] **Step 2: Implement template and minimal renderer**

Use explicit placeholders for service user, absolute app directory, display,
and absolute Xauthority. The CLI requires all four plus `--output`; it validates
`<app-dir>/config.yaml` as `0600`, renders to a same-directory temporary file,
fsyncs, then atomically replaces output. It never invokes sudo/systemctl and
never changes config mode. CI creates a temporary representative app/config,
renders, then runs `systemd-analyze verify`.

- [x] **Step 3: Verify GREEN and static unit semantics**

Run the RED command and `git diff --check`. On Linux CI, require the real
`systemd-analyze verify`; locally, unit tests must skip only the external binary
check, never the directive contract.

- [x] **Step 4: Independent adversarial review**

Reviewer must try template injection, relative paths, symlink/output races,
unsafe config mode, partial writes, changed historical restart/time directives,
and a renderer that mutates config. Resolve all confirmed findings.

---

### Task 4: Put an explicit safety gate around the Discogs write (#366)

**Files:**
- Modify: `scripts/discogs_live_check.py`
- Modify: `tests/test_discogs_live_check.py`

- [ ] **Step 1: Write RED tests**

Test `--artist` and `--album` flow through every displayed/search operation.
Test that `--test-write` aborts without calling `increment_play_count` on `n`,
empty input, or EOF; accepts only an exact affirmative response; and that
`--yes` bypasses the prompt only when both record arguments were explicitly
provided. Test the prompt contains artist, album, release/instance IDs, and
field name but no token. Preserve existing redaction and root-relative config
tests.

Run:

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight' && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_discogs_live_check.py
```

Expected RED: fixed constants and unconditional `--test-write` do not satisfy
the selection/confirmation contract.

- [ ] **Step 2: Implement selection and confirmation**

Make artist/album explicit CLI inputs with current constants only for read-only
compatibility. A write requires both inputs to have been supplied explicitly.
Prompt immediately before the one writer call; accept only `yes`. `--yes` is
the noninteractive equivalent and must remain visually loud in the summary.

- [ ] **Step 3: Verify GREEN and no-write mutations**

Run the focused suite and `git diff --check`. Reviewer must attempt absent
arguments, EOF, whitespace/case variations, failed search, exception, and
ambiguous response paths. No reviewer executes a live network call.

---

### Task 5: Reconcile deployment docs and the regression contract

**Files:**
- Modify: `docs/pi-setup-guide.md`
- Modify: `docs/first-boot-checklist.md`
- Modify: `docs/testing-guide.md`
- Modify: `docs/decisions/remediation-guardrails.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Replace copy/paste drift with the versioned path**

Document exact renderer usage for `/etc/systemd/system/vinyl-now-playing.service`,
then `systemd-analyze verify`, `daemon-reload`, enable/start, and status. Preserve
the system-service decision, #83/#201 rationale, time-sync waiter, and the
image-specific Wayland/Xwayland warning. Remove any instruction suggesting a
user-service migration as the Wave 1 fallback.

- [ ] **Step 2: Add rollback and evidence forms**

Document backup/restore of the last known-good rendered unit and venv/tag,
`config.yaml` `0600` readback, UCA222 import/enumeration/hot-plug results,
cold-boot/display/session/time-sync outcomes, and a redacted Discogs record
before/after/HTTP result. Explicitly say CI does not close hardware gates.

- [ ] **Step 3: Update durable status without premature closure**

Update the guardrail and changelog with the implemented software controls.
Leave #366 and Wave 1 hardware state pending until observed. Correct the stale
`tests/conftest.py` reference to root `conftest.py`.

- [ ] **Step 4: Independent documentation review**

Review against live issues #418/#156/#419/#366 and closed #83/#201/#301.
Search for contradictory service type, retry values, permission advice,
hardware-success claims, unsafe write instructions, and stale file paths.

---

### Task 6: Integrated verification and adversarial release review

**Files:** all intended Wave 1 files; no production edits by reviewer.

- [ ] **Step 1: Run focused local verification**

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight' && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m py_compile src/config.py scripts/check_audio_backend.py scripts/render_system_service.py scripts/discogs_live_check.py && '/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest -q tests/test_config.py tests/test_audio_backend_smoke.py tests/test_capture.py tests/test_main_wiring.py tests/test_ops_polish_r9.py tests/test_system_service.py tests/test_discogs_live_check.py tests/test_workflow_integrity_r10.py && git diff --check
```

- [ ] **Step 2: Run repository security/scope checks**

Verify no secret/private-key material, no tracked `config.yaml`, no user-service
artifact, no unintended files, no staged files, and an explicit intended diff.
Run workflow lint using the repository's pinned/cached actionlint procedure.

- [ ] **Step 3: Independent broad adversarial review**

Review config TOCTOU/redaction, CI fake-package bypass, exact production API
coverage, system-unit injection/partial writes/semantic drift, Discogs write
bypass/ambiguous retries, docs/history conflicts, and test gaming. Fix through
the owning task and repeat review until SPEC PASS / QUALITY PASS with zero open
High/Critical or materially distinct non-gameable findings.

---

### Task 7: Merge software evidence, then execute the hardware gate

**External objects:** GitHub pull request, issues #418/#156/#419/#366, milestone #52, target Raspberry Pi, live Discogs record.

- [ ] **Step 1: Create the PR with explicit-path Git operations**

Only if a Git mutation cannot be performed directly, the handoff begins with:

```bash
cd '/private/tmp/vnp-release-app-identity' && test "$(git branch --show-current)" = 'codex/r10-wave1-pi-preflight'
```

Then stage only named Wave 1 files, commit, and push. Never use `git add -A`.

- [ ] **Step 2: Require exact-SHA supported CI**

Require Python 3.11/3.12/3.13 tests, real sounddevice smoke on each leg,
systemd verification, dependency audit, actionlint, and review. Merge only the
reviewed exact SHA.

- [ ] **Step 3: Close only software-complete issue contracts**

Read back main and exact-SHA checks. #156 may close after its CI acceptance is
proven. #418 and #419 remain open if their issue contracts still require Pi
mode/cold-boot evidence. #366 remains open until the authorized write result.

- [ ] **Step 4: Run the real Pi checklist**

Record OS/Python versions, `stat` mode without content, real sounddevice import
and required APIs, UCA222 enumeration/stream/hot-plug, selected DISPLAY/session
auth, system-unit verification, offline/time-sync behavior, SIGTERM, and cold
boot. Do not include credentials in evidence.

- [ ] **Step 5: Obtain record-specific authorization and run #366 once**

Lane supplies the exact artist/album in a non-default folder and approves that
write. Record before/after value and outcome. On success, close #366; on 404,
open/activate the folder-ID propagation task; on ambiguity, inspect before any
retry.

- [ ] **Step 6: Close Wave 1**

Update the audit, guardrail, SDD record, issue comments, and milestone with
exact merge SHA, run IDs, Pi image/Python, service evidence, config mode, and
Discogs outcome. Close milestone #52 only when every exit gate is observed.
