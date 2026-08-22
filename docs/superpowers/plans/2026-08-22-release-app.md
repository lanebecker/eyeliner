# Dedicated Release App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the protected `release` environment and a repository-scoped GitHub App the only path that can create immutable `v*` tags and GitHub Releases.

**Architecture:** Split the controlled release into a read-only `validate` job and an environment-gated `publish` job. The publish job mints a one-hour, current-repository app token immediately before one `gh release create` call. Active ruleset `Protect release tags` (ID `21211977`) makes the installed app the sole release-tag-ruleset bypass actor; it was created only after post-merge Task 7 checks and API readback succeeded.

**Tech Stack:** GitHub Apps, GitHub Actions, repository environments, repository rulesets, `actions/create-github-app-token` v3.2.0, GitHub CLI, Python/pytest, PyYAML, actionlint.

**Spec:** `docs/superpowers/specs/2026-08-22-release-app-design.md`

**Completion readback (2026-08-22):** PR #433 merged at `05a1c7b55cccde92786918ab18ee85a6de2aa5cc`. `Protect main` (ID `21204190`) and `Protect release tags` (ID `21211977`) are active with the verified policies below. Controlled run `32600568406` published immutable Latest Release `v1.5.35` from exact tested SHA `4b79513b6811c1884be42dadbb1d45f2354d70a6` after Lane approved the protected environment; post-tag consistency run `32600637687` passed. Issues #402, #415, and #416 are closed.

## Global Constraints

- The app name is `vinyl-now-playing-release-lbecker` and it is installed only on `lanebecker/vinyl-now-playing`. The approved longer name was shortened because GitHub App names are limited to 34 characters.
- The app's only explicit permission is repository `Contents: Read and write`, plus GitHub-mandatory Metadata read access; it has no webhook or subscribed events.
- The private key exists only as the `release` environment secret `RELEASE_APP_PRIVATE_KEY`; never print, read into task output, copy to the workspace, or store as a repository secret.
- The Client ID is the `release` environment variable `RELEASE_APP_CLIENT_ID`.
- The `release` environment accepts only `main`, requires reviewer `lanebecker` (user ID `3211673`), and permits self-review.
- The ordinary `GITHUB_TOKEN` remains `actions: read`, `checks: read`, `contents: read` and never publishes.
- The app token requests only `contents: write`, targets only the current repository, and is automatically revoked.
- The exact token action pin is `actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1` (`v3.2.0`).
- `Protect main` remains active with no bypass actors and requires `test (3.11)`, `test (3.12)`, and `test (3.13)` from GitHub Actions integration ID `15368`.
- Verified Task 7 state: active `Protect release tags` ruleset ID `21211977` targets `refs/tags/v*`; its sole bypass actor is installed release App ID `4684884`; it restricts creation and deletion and blocks non-fast-forward updates.
- Published releases remain immutable. Never overwrite, delete, move, or blindly retry an ambiguous tag/Release creation.
- Local git is read-only for Codex. Codex performs every other available operation; the sole unavoidable handoff is explicit-path branch/commit/push.
- Every unavoidable handoff begins with `cd` to the absolute path of the appropriate active checkout or worktree and verifies its expected branch before mutation. The canonical clone is only the default when it is the active checkout. Handoffs never use `git add -A`.
- Bundle-local Python commands below execute from `/private/tmp/vnp-release-app-identity` after branch verification. They reuse the canonical clone's `.venv` only as an absolute interpreter path because the isolated worktree has no `.venv`; all relative project files and test targets resolve from the isolated worktree.

## File structure

- Create `docs/superpowers/specs/2026-08-22-release-app-design.md`: approved security design and recovery contract.
- Create `docs/superpowers/plans/2026-08-22-release-app.md`: this task-by-task implementation plan.
- Modify `.github/workflows/release.yml`: read-only validation job plus environment-gated app publication job.
- Modify `tests/test_workflow_integrity_r10.py`: behavior-level workflow contract for permission, environment, token, dependency, and post-approval gates.
- Modify `docs/decisions/remediation-guardrails.md`: durable app/environment identities and external-state status.
- Modify `docs/testing-guide.md`: controlled-release approval/token verification instructions.
- Modify `CHANGELOG.md`: unreleased release-credential hardening note.
- Modify `CLAUDE.md`: durable execution-ownership instruction already approved by Lane.
- Modify `.github/workflows/release-consistency.yml`: explanatory App-token event-semantics comment only.

---

### Task 1: Create the external release identity and protected environment

**External objects:**
- Create: GitHub App `vinyl-now-playing-release-lbecker`
- Create: repository environment `release`
- Create: environment branch policy `main`
- Create: environment variable `RELEASE_APP_CLIENT_ID`
- Create: environment secret `RELEASE_APP_PRIVATE_KEY`

**Interfaces:**
- Produces: numeric App ID for the later ruleset, Client ID for `vars.RELEASE_APP_CLIENT_ID`, and a private key available only as `secrets.RELEASE_APP_PRIVATE_KEY` in the `release` environment.
- Consumes: repository owner account `lanebecker`, repository `vinyl-now-playing`, reviewer user ID `3211673`.

- [x] **Step 1: Re-read external-state baselines**

Verify `Protect main`, immutable releases, MIT detection, and the exact merge SHA before adding another identity:

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
gh api repos/lanebecker/vinyl-now-playing/rulesets && \
gh api repos/lanebecker/vinyl-now-playing/immutable-releases && \
gh api repos/lanebecker/vinyl-now-playing --jq '{license,security_and_analysis}' && \
gh api repos/lanebecker/vinyl-now-playing/branches/main --jq '{sha:.commit.sha,protected:.protected}'
```

Expected: ruleset `21204190` is active, immutable releases reports enabled, license SPDX is MIT, and `main.protected` is true.

- [x] **Step 2: Create the GitHub App through the account UI**

Use `https://github.com/settings/apps/new` with these exact values:

```text
GitHub App name: vinyl-now-playing-release-lbecker
Homepage URL: https://github.com/lanebecker/vinyl-now-playing
Webhook: inactive
Repository permissions → Contents: Read and write
All other repository/account/organization permissions: No access
Subscribe to events: none
Where can this GitHub App be installed?: Only on this account
```

Record the displayed numeric App ID and Client ID without putting either in a secret. Generate one private key. Do not open or print the `.pem` file.

- [x] **Step 3: Install the app on only this repository**

From the app's Install App page, choose `lanebecker`, select **Only select repositories**, and select only `vinyl-now-playing`. Verify the installation page names no other repository.

- [x] **Step 4: Create the environment through the API**

Submit this exact JSON to `PUT /repos/lanebecker/vinyl-now-playing/environments/release`:

```json
{
  "wait_timer": 0,
  "prevent_self_review": false,
  "reviewers": [{"type": "User", "id": 3211673}],
  "deployment_branch_policy": {
    "protected_branches": false,
    "custom_branch_policies": true
  }
}
```

Then submit `{"name":"main"}` to `POST /repos/lanebecker/vinyl-now-playing/environments/release/deployment-branch-policies`.

- [x] **Step 5: Store the environment variable and secret without disclosure**

Set `RELEASE_APP_CLIENT_ID` to the displayed Client ID as an environment variable. Stream the downloaded `.pem` into `gh secret set RELEASE_APP_PRIVATE_KEY --env release`; do not use `--body`, command substitution, clipboard echoing, or any command that prints the file. If an iCloud-backed download cannot be streamed by the CLI, copy it through Finder to `/private/tmp` without opening it, upload from that private temporary path, verify the secret metadata, and move both local copies to Trash. Empty only those key copies from Trash after the environment readback succeeds and Lane confirms the permanent deletion at action time.

- [x] **Step 6: Read back the non-secret environment contract**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
gh api repos/lanebecker/vinyl-now-playing/environments/release && \
gh variable list --repo lanebecker/vinyl-now-playing --env release && \
gh secret list --repo lanebecker/vinyl-now-playing --env release
```

Expected: reviewer `lanebecker`, self-review allowed, custom branch policy `main`, variable name `RELEASE_APP_CLIENT_ID`, and secret name `RELEASE_APP_PRIVATE_KEY`. Secret values must not appear.

### Task 2: Add RED-first workflow security contracts

**Files:**
- Modify: `tests/test_workflow_integrity_r10.py`
- Test: `tests/test_workflow_integrity_r10.py`

**Interfaces:**
- Consumes: current `.github/workflows/release.yml` loaded with PyYAML.
- Produces: behavior tests that describe the exact `validate`/`publish` workflow contract.

- [x] **Step 1: Add a workflow loader and step lookup**

Add these helpers after the constants:

```python
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
TOKEN_ACTION = (
    "actions/create-github-app-token@"
    "bcd2ba49218906704ab6c1aa796996da409d3eb1"
)


def _release_workflow():
    return yaml.safe_load(RELEASE_WORKFLOW.read_text())


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)
```

- [x] **Step 2: Write the permission and job-boundary tests**

```python
def test_release_workflow_keeps_builtin_token_read_only():
    workflow = _release_workflow()
    assert workflow["permissions"] == {
        "actions": "read",
        "checks": "read",
        "contents": "read",
    }


def test_release_credentials_exist_only_in_environment_gated_publish_job():
    jobs = _release_workflow()["jobs"]
    validate = jobs["validate"]
    publish = jobs["publish"]

    assert "environment" not in validate
    assert "secrets." not in yaml.safe_dump(validate)
    assert publish["needs"] == "validate"
    assert publish["environment"] == "release"
    assert "secrets.RELEASE_APP_PRIVATE_KEY" in yaml.safe_dump(publish)
```

- [x] **Step 3: Write the token scope and publication-authentication test**

```python
def test_publish_mints_one_repo_token_and_uses_it_for_release_creation():
    publish = _release_workflow()["jobs"]["publish"]
    token_step = next(step for step in publish["steps"] if step.get("id") == "app-token")
    release_step = _step(publish, "Create immutable tag and GitHub Release")

    assert token_step["uses"] == TOKEN_ACTION
    assert token_step["with"] == {
        "client-id": "${{ vars.RELEASE_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.RELEASE_APP_PRIVATE_KEY }}",
        "permission-contents": "write",
    }
    assert "skip-token-revoke" not in token_step["with"]
    assert release_step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "github.token" not in yaml.safe_dump(release_step)
```

- [x] **Step 4: Write the post-approval revalidation test**

```python
def test_publish_rechecks_identity_and_immutability_after_approval():
    publish = _release_workflow()["jobs"]["publish"]
    rendered = yaml.safe_dump(publish)

    assert "scripts/check_version_metadata.py" in rendered
    assert "refs/tags/$actual_tag" in rendered
    assert "releases/tags/$actual_tag" in rendered
    assert 'case "$release_status"' in rendered
    assert "200)" in rendered
    assert "404)" in rendered
    assert "git fetch --no-tags origin main" in rendered
    assert "git rev-parse origin/main" in rendered
    assert "needs.validate.outputs.release_sha" in rendered
```

- [x] **Step 5: Run the tests and observe RED**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' \
  -m pytest tests/test_workflow_integrity_r10.py -q
```

Expected: the new tests fail because the current workflow has one `release` job, grants `contents: write`, has no environment, and uses `github.token` to publish.

### Task 3: Split validation from app-authenticated publication

**Files:**
- Modify: `.github/workflows/release.yml`
- Test: `tests/test_workflow_integrity_r10.py`

**Interfaces:**
- Consumes: `scripts/check_version_metadata.py`, `scripts/release_gate.py`, `vars.RELEASE_APP_CLIENT_ID`, `secrets.RELEASE_APP_PRIVATE_KEY`.
- Produces: `validate` outputs `release_sha`, `version`, and `tag`; `publish` consumes those outputs after environment approval.

- [x] **Step 1: Make the built-in token read-only**

Replace the permission block with:

```yaml
permissions:
  actions: read
  checks: read
  contents: read
```

- [x] **Step 2: Rename the existing job and expose validated outputs**

Start the validation job with:

```yaml
jobs:
  validate:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    outputs:
      release_sha: ${{ steps.identity.outputs.release_sha }}
      version: ${{ steps.version.outputs.version }}
      tag: ${{ steps.version.outputs.tag }}
```

Give the first SHA verification step `id: identity` and append this only after the equality check succeeds:

```bash
echo "release_sha=$RELEASE_SHA" >> "$GITHUB_OUTPUT"
```

Keep metadata validation, existing tag/Release refusal, exact workflow/check fetch, and `scripts/release_gate.py` in `validate`. Remove the existing publication step from this job.

- [x] **Step 3: Add the protected publish job**

Add this job after `validate`:

```yaml
  publish:
    needs: validate
    environment: release
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
        with:
          fetch-depth: 0
          ref: ${{ needs.validate.outputs.release_sha }}

      - name: Revalidate release candidate after approval
        env:
          GH_TOKEN: ${{ github.token }}
          EXPECTED_SHA: ${{ needs.validate.outputs.release_sha }}
          EXPECTED_TAG: ${{ needs.validate.outputs.tag }}
        run: |
          set -euo pipefail
          python scripts/check_version_metadata.py \
            --version-file VERSION \
            --changelog-file CHANGELOG.md \
            --readme-file README.md
          actual_version=$(tr -d '[:space:]' < VERSION)
          actual_tag="v$actual_version"
          if [ "$actual_tag" != "$EXPECTED_TAG" ]; then
            echo "::error::validated tag $EXPECTED_TAG changed to $actual_tag"
            exit 1
          fi
          if [ "$(git rev-parse HEAD)" != "$EXPECTED_SHA" ]; then
            echo "::error::checkout does not match validated SHA $EXPECTED_SHA"
            exit 1
          fi
          if git rev-parse --verify --quiet "refs/tags/$actual_tag"; then
            echo "::error::tag $actual_tag already exists; releases are immutable"
            exit 1
          fi
          release_headers="$RUNNER_TEMP/release-probe.headers"
          release_error="$RUNNER_TEMP/release-probe.error"
          probe_exit=0
          gh api \
            --include \
            --silent \
            -H 'Accept: application/vnd.github+json' \
            "repos/$GITHUB_REPOSITORY/releases/tags/$actual_tag" \
            > "$release_headers" 2> "$release_error" || probe_exit=$?
          release_status=$(awk '/^HTTP\/[0-9.]+ [0-9][0-9][0-9]/ { status = $2 } END { print status }' "$release_headers")
          case "$release_status" in
            200)
              echo "::error::GitHub Release $actual_tag already exists"
              exit 1
              ;;
            404)
              ;;
            *)
              echo "::error::could not verify absence of GitHub Release $actual_tag (HTTP ${release_status:-unavailable}, gh exit $probe_exit)"
              if [ -s "$release_error" ]; then
                cat "$release_error" >&2
              fi
              exit 1
              ;;
          esac

      - name: Mint repository-scoped release token
        id: app-token
        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1  # v3.2.0
        with:
          client-id: ${{ vars.RELEASE_APP_CLIENT_ID }}
          private-key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
          permission-contents: write

      - name: Create immutable tag and GitHub Release
        env:
          GH_TOKEN: ${{ steps.app-token.outputs.token }}
          RELEASE_SHA: ${{ needs.validate.outputs.release_sha }}
          TAG: ${{ needs.validate.outputs.tag }}
        run: |
          set -euo pipefail
          prerelease_arg=()
          if [[ "$TAG" == *-* ]]; then
            prerelease_arg=(--prerelease)
          fi
          git fetch --no-tags origin main
          current_main=$(git rev-parse origin/main)
          if [ "$RELEASE_SHA" != "$current_main" ]; then
            echo "::error::main advanced to $current_main during approval; $RELEASE_SHA will not be released"
            exit 1
          fi
          gh release create "$TAG" \
            --repo "$GITHUB_REPOSITORY" \
            --target "$RELEASE_SHA" \
            --title "$TAG" \
            --generate-notes \
            "${prerelease_arg[@]}" \
            --notes "Tested SHA: [$RELEASE_SHA](https://github.com/$GITHUB_REPOSITORY/commit/$RELEASE_SHA)

          Supported Python: 3.11, 3.12, 3.13.

          Supported Raspberry Pi OS: Bookworm/Legacy (Python 3.11) and Trixie (Python 3.13). See docs/pi-setup-guide.md."
```

- [x] **Step 4: Run the workflow tests and observe GREEN**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' \
  -m pytest tests/test_workflow_integrity_r10.py -q
```

Expected: all workflow-integrity tests pass.

- [x] **Step 5: Run the complete focused regression slice**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest \
  tests/test_workflow_integrity_r10.py \
  tests/test_release_gate_r10.py \
  tests/test_version_metadata_r10.py \
  tests/test_ci_integrity_r8.py -q
```

Expected: all tests pass; local Python 3.9 dependency warnings remain non-blocking.

### Task 4: Reconcile durable documentation

**Files:**
- Modify: `docs/decisions/remediation-guardrails.md`
- Modify: `docs/testing-guide.md`
- Modify: `CHANGELOG.md`
- Modify: `CLAUDE.md`
- Modify: `.github/workflows/release-consistency.yml` (explanatory comment only)
- Modify: `docs/superpowers/plans/2026-08-22-release-app.md` (reconcile the implemented fail-closed/ordering contract)
- Test: `scripts/check_version_metadata.py`

**Interfaces:**
- Consumes: actual app ID/Client ID, environment readback, final workflow shape.
- Produces: durable operator and future-agent instructions matching live GitHub state.

- [x] **Step 1: Update the decision ledger**

Under release governance, record:

```markdown
- Release publication uses the repository-scoped `vinyl-now-playing-release-lbecker` GitHub App. Its only explicit permission is repository Contents read/write, plus GitHub-mandatory Metadata read access; it is installed only on this repository.
- The app private key exists only as `RELEASE_APP_PRIVATE_KEY` in the protected `release` environment; its Client ID is `RELEASE_APP_CLIENT_ID`. Lane approves the environment after validation succeeds.
- After the bundle merged and its checks passed, Task 7 created active `Protect release tags` ruleset ID `21211977`, permitting only App ID `4684884` to bypass creation, deletion, or non-fast-forward-update restrictions for `v*` tags. The built-in GitHub Actions integration is not an eligible bypass actor for this personal repository.
```

The Wave 0 operational checkpoint now records the API-verified live state, exact merge and release SHAs, both ruleset IDs, issue closures, and the successful first controlled publication.

- [x] **Step 2: Update the testing guide**

Document this operator sequence:

```markdown
1. Dispatch `controlled release` from `main`.
2. Wait for the read-only `validate` job to succeed.
3. Review the validated SHA/version/tag in the run, then approve the `release` environment.
4. Confirm the `publish` job mints the app token and creates one tag plus one GitHub Release.
5. If the release API fails or times out, inspect tag and Release state before rerunning; never retry blindly.
```

- [x] **Step 3: Update the changelog and shared instructions**

Add an Unreleased bullet stating that release publication now uses an environment-approved, repository-scoped GitHub App rather than a write-capable built-in token. Keep the execution-ownership rule in `CLAUDE.md` and ensure no older repository-specific instruction recommends manual tags or `git add -A`.

- [x] **Step 4: Verify metadata and documentation whitespace**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' \
  scripts/check_version_metadata.py \
  --version-file VERSION \
  --changelog-file CHANGELOG.md \
  --readme-file README.md && \
git diff --check
```

Expected: metadata passes for the current version and the diff check exits zero.

### Task 5: GitHub-aware verification and adversarial review

**Files:**
- Review: `.github/workflows/release.yml`
- Review: `tests/test_workflow_integrity_r10.py`
- Review: `docs/decisions/remediation-guardrails.md`
- Review: `docs/testing-guide.md`

**Interfaces:**
- Consumes: completed local diff and live external app/environment state.
- Produces: independent SPEC/QUALITY verdict and any required narrow rework.

- [x] **Step 1: Resolve and checksum-verify actionlint**

Use the already checksum-verified `/tmp/vnp-actionlint-1.7.12/actionlint` if present. Otherwise download official v1.7.12 Darwin arm64 assets and verify `actionlint_1.7.12_checksums.txt` before extraction.

- [x] **Step 2: Validate every workflow**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
/tmp/vnp-actionlint-1.7.12/actionlint .github/workflows/*.yml
```

Expected: exit zero and no output.

- [x] **Step 3: Run fresh local gates**

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m py_compile \
  scripts/release_gate.py \
  scripts/check_version_metadata.py \
  tests/test_workflow_integrity_r10.py && \
'/Users/lanebecker-wmf/Documents/Claude.nosync/Projects/Vinyl Now Playing/.venv/bin/python' -m pytest \
  tests/test_workflow_integrity_r10.py \
  tests/test_release_gate_r10.py \
  tests/test_version_metadata_r10.py \
  tests/test_ci_integrity_r8.py -q && \
git diff --check
```

Expected: compilation, focused tests, and diff check all pass.

- [x] **Step 4: Dispatch an independent break-this review**

Give a fresh subagent the spec, plan, current diff, live ruleset/environment readback, and this attack surface:

```text
Try to make a human, ordinary GITHUB_TOKEN job, wrong app, wrong repository,
unapproved job, stale main SHA, older green CI run, existing tag, ambiguous API
retry, or leaked private key create or corrupt a v* release. Execute at most two
safe reproductions. Report SPEC and QUALITY separately; do not edit files.
```

Require explicit classification of every finding as introduced/pre-existing and severity CRITICAL/HIGH/MEDIUM/LOW/NIT.

- [x] **Step 5: Rework findings and run the narrow second pass**

For each accepted finding: add or strengthen a RED-first test, observe RED, make the smallest fix, observe GREEN, then send only the reworked hunks to the same reviewer. Stop after approximately 20 review tool calls and two reproductions; unresolved items may remain labeled HYPOTHESIS.

### Task 6: Land the workflow through protected main

**Files:**
- Stage only the files listed in this plan's File structure section.

**Interfaces:**
- Consumes: locally verified diff and independent PASS/conditional-PASS verdict.
- Produces: the already-existing `codex/release-app-identity` branch pushed for a pull request; post-merge current-main SHA with green workflows.

- [x] **Step 1: Perform the sole unavoidable git handoff**

The branch already exists and is checked out in the isolated worktree; do not create it again. The handoff must use this one failure-stopping chain, verify the branch before mutation, stage only the named files, run the cached diff check, commit with the issue references, and push:

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
test -z "$(git diff --cached --name-only)" && \
git add -- \
  .github/workflows/release-consistency.yml \
  .github/workflows/release.yml \
  CHANGELOG.md \
  CLAUDE.md \
  docs/decisions/remediation-guardrails.md \
  docs/superpowers/plans/2026-08-22-release-app.md \
  docs/superpowers/specs/2026-08-22-release-app-design.md \
  docs/testing-guide.md \
  tests/test_workflow_integrity_r10.py && \
git diff --cached --check && \
git diff --cached --name-only && \
git commit -m 'Use dedicated GitHub App for controlled releases' -m 'Refs #402, #415, #416' && \
git push -u origin codex/release-app-identity
```

It must not stage audit reports, issue scripts/maps, release-note snapshots, `config.yaml`, any private key, or any file not listed above. It must never use `git add -A` or create/push a release tag.

- [x] **Step 2: Create the PR directly with GitHub CLI**

Codex creates the PR, listing local verification, external app/environment scope, independent review verdict, and the remaining post-merge tag-ruleset gate.

- [x] **Step 3: Monitor every PR check**

Require version metadata; dependency audit on 3.11/3.12/3.13; and tests on 3.11/3.12/3.13. Verify the PR file list contains only intended files and GitHub reports `MERGEABLE/CLEAN` before asking Lane to merge.

- [x] **Step 4: Verify post-merge current main**

Wait for the merge-SHA metadata, dependency-audit matrix, and test matrix. Do not create tag protection while any post-merge check is pending or failing.

### Task 7: Activate and verify release-tag protection

**External objects:**
- Create: active repository ruleset `Protect release tags`
- Verify: existing `Protect main`
- Verify: immutable releases, MIT license, app installation, environment

**Interfaces:**
- Consumes: installed release App ID and merged app-authenticated workflow.
- Produces: only the release app can create/update/delete matching `v*` tags.

- [x] **Step 1: Create the ruleset payload with the Task 1 App ID**

Store the numeric App ID read in Task 1 in the shell variable `release_app_id`, then generate the payload without stringifying the ID:

```bash
cd '/private/tmp/vnp-release-app-identity' && \
test "$(git branch --show-current)" = 'codex/release-app-identity' && \
jq -n --argjson app_id "$release_app_id" '{
  name: "Protect release tags",
  target: "tag",
  enforcement: "active",
  bypass_actors: [{
    actor_id: $app_id,
    actor_type: "Integration",
    bypass_mode: "always"
  }],
  conditions: {
    ref_name: {include: ["refs/tags/v*"], exclude: []}
  },
  rules: [
    {type: "creation"},
    {type: "deletion"},
    {type: "non_fast_forward"}
  ]
}' > /tmp/vnp-release-tag-ruleset.json
```

Run `jq -e '.bypass_actors[0].actor_id | type == "number"' /tmp/vnp-release-tag-ruleset.json` before POSTing.

- [x] **Step 2: Create and read back the ruleset**

POST the payload to `repos/lanebecker/vinyl-now-playing/rulesets`, capture the returned ID, then GET that exact ID. Expected: active tag target, `refs/tags/v*`, exactly one bypass actor matching the custom app, and exactly the three specified rules.

- [x] **Step 3: Verify the complete live release trust boundary**

Read back:

```text
Protect main: active, no bypass, PR + exact three GitHub Actions checks,
              deletion and non-fast-forward protection
Protect release tags: active, one custom-app bypass, v* creation/deletion/
                      non-fast-forward protection
Immutable releases: enabled
License: MIT
Environment: release, reviewer Lane, self-review allowed, main only,
             expected variable/secret names
App: installed only on vinyl-now-playing, Contents read/write only
release.yml: ordinary token read-only, publish job environment-gated,
             final GH_TOKEN comes only from app-token output
```

- [x] **Step 4: Update status records**

Append the verified ruleset/environment/app IDs and post-merge SHA to the ignored audit report and `log.md`. Mark #415 complete only after main and tag protection readbacks both match. Keep #416 open until the first real app-backed release becomes Latest and its VERSION/tag/tested SHA agree.

Result: #416 remained open through validation and protected-environment approval, then closed only after API readback proved immutable Latest Release `v1.5.35`, VERSION, tag target, Release target, and tested SHA `4b79513b6811c1884be42dadbb1d45f2354d70a6` agree and post-tag consistency passed.

- [x] **Step 5: Prepare the first controlled-release checklist**

Before the next version dispatch, confirm the version bump updates VERSION, CHANGELOG, and the rendered README badge in one PR. After the merge SHA's three tests pass, dispatch from `main`, wait for `validate`, approve `release`, observe `publish`, and inspect the resulting immutable Release before any retry or issue closure.

Result: PR #436 updated exactly those three metadata files; exact-SHA tests and dependency audits passed; controlled run `32600568406` paused for and received Lane's approval; publication succeeded without retry; and independent API readback plus post-tag run `32600637687` verified the result before #416 closed.
