"""Behavior checks for the Wave 0 GitHub Actions workflow definitions."""
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
WORKFLOW_TOP_LEVEL_KEYS = {
    "concurrency",
    "defaults",
    "env",
    "jobs",
    "name",
    "on",
    True,  # PyYAML 5 follows YAML 1.1 and decodes the unquoted key `on` as true.
    "permissions",
    "run-name",
}
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
TOKEN_ACTION = (
    "actions/create-github-app-token@"
    "bcd2ba49218906704ab6c1aa796996da409d3eb1"
)
RELEASE_STEP_ENV = {
    "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
    "RELEASE_SHA": "${{ needs.validate.outputs.release_sha }}",
    "TAG": "${{ needs.validate.outputs.tag }}",
}
RELEASE_STEP_RUN = r'''set -euo pipefail
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
'''
REVALIDATE_STEP_ENV = {
    "GH_TOKEN": "${{ github.token }}",
    "EXPECTED_SHA": "${{ needs.validate.outputs.release_sha }}",
    "EXPECTED_TAG": "${{ needs.validate.outputs.tag }}",
}
REVALIDATE_STEP_RUN = r'''set -euo pipefail
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
'''
EXPECTED_PUBLISH_JOB = {
    "needs": "validate",
    "environment": "release",
    "runs-on": "ubuntu-latest",
    "steps": [
        {
            "uses": CHECKOUT_ACTION,
            "with": {
                "fetch-depth": 0,
                "ref": "${{ needs.validate.outputs.release_sha }}",
            },
        },
        {
            "name": "Revalidate release candidate after approval",
            "env": REVALIDATE_STEP_ENV,
            "run": REVALIDATE_STEP_RUN,
        },
        {
            "name": "Mint repository-scoped release token",
            "id": "app-token",
            "uses": TOKEN_ACTION,
            "with": {
                "client-id": "${{ vars.RELEASE_APP_CLIENT_ID }}",
                "private-key": "${{ secrets.RELEASE_APP_PRIVATE_KEY }}",
                "permission-contents": "write",
            },
        },
        {
            "name": "Create immutable tag and GitHub Release",
            "env": RELEASE_STEP_ENV,
            "run": RELEASE_STEP_RUN,
        },
    ],
}


def _release_workflow():
    return yaml.safe_load(RELEASE_WORKFLOW.read_text())


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def _job_steps(workflow):
    return (
        (job_name, step)
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
    )


def _step_identity(step):
    if "uses" in step:
        return ("uses", step["uses"], step.get("id"))
    return ("name", step.get("name"), step.get("id"))


def _assert_fail_closed_release_probe(run, tag):
    api_call = f'''gh api \\
  --include \\
  --silent \\
  -H 'Accept: application/vnd.github+json' \\
  "repos/$GITHUB_REPOSITORY/releases/tags/{tag}" \\
  > "$release_headers" 2> "$release_error" || probe_exit=$?'''
    status_assignment = (
        "release_status=$(awk '/^HTTP\\/[0-9.]+ [0-9][0-9][0-9]/ "
        "{ status = $2 } END { print status }' \"$release_headers\")"
    )
    connected_probe = (
        f'{api_call}\n{status_assignment}\ncase "$release_status" in'
    )

    assert connected_probe in run
    assert run.count("release_status=") == 1

    case_start = run.index('case "$release_status" in')
    existing_start = run.index("200)", case_start)
    absent_start = run.index("404)", existing_start)
    ambiguous_start = run.index("*)", absent_start)
    case_end = run.index("esac", ambiguous_start)
    assert "exit 1" in run[existing_start:absent_start]
    assert "exit 1" not in run[absent_start:ambiguous_start]
    assert "exit 1" in run[ambiguous_start:case_end]


def _assert_narrow_release_publication_window(run):
    prerelease_setup = '''prerelease_arg=()
if [[ "$TAG" == *-* ]]; then
  prerelease_arg=(--prerelease)
fi
git fetch --no-tags origin main'''
    assert prerelease_setup in run
    assert run.count("prerelease_arg=") == 2

    fetch = "git fetch --no-tags origin main"
    resolve = "current_main=$(git rev-parse origin/main)"
    main_guard = 'if [ "$RELEASE_SHA" != "$current_main" ]; then'
    release_create = 'gh release create "$TAG"'
    assert run.index(fetch) < run.index(resolve) < run.index(main_guard) < run.index(
        "exit 1", run.index(main_guard)
    ) < run.index(release_create)
    guard_end = run.index("\nfi", run.index(main_guard)) + len("\nfi")
    next_executable = next(
        line.strip()
        for line in run[guard_end:].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert next_executable == 'gh release create "$TAG" \\'


def test_controlled_release_has_no_deindented_step_content_at_workflow_scope():
    """Deindenting block-scalar lines must not create invalid workflow keys."""
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "release.yml").read_text()
    )

    unexpected = set(workflow) - WORKFLOW_TOP_LEVEL_KEYS
    assert unexpected == set(), f"unexpected workflow-level keys: {sorted(unexpected)}"


def test_release_workflow_keeps_builtin_token_read_only():
    """Catch a workflow-wide permission escalation of the built-in token."""
    workflow = _release_workflow()
    assert workflow["permissions"] == {
        "actions": "read",
        "checks": "read",
        "contents": "read",
    }


def test_release_credentials_exist_only_in_environment_gated_publish_job():
    """Catch release credentials leaking into validation or bypassing approval."""
    jobs = _release_workflow()["jobs"]
    assert set(jobs) == {"validate", "publish"}
    assert all("permissions" not in job for job in jobs.values())

    validate = jobs["validate"]
    publish = jobs["publish"]

    assert publish == EXPECTED_PUBLISH_JOB
    assert [_step_identity(step) for step in validate["steps"]] == [
        ("uses", CHECKOUT_ACTION, None),
        ("name", "Verify selected SHA is current main", "identity"),
        ("name", "Read and validate release version", "version"),
        ("name", "Refuse an existing tag or GitHub Release", None),
        ("name", "Fetch tests workflow and check runs for the exact SHA", None),
        ("name", "Require the complete supported-Python matrix", None),
    ]
    assert [_step_identity(step) for step in publish["steps"]] == [
        ("uses", CHECKOUT_ACTION, None),
        ("name", "Revalidate release candidate after approval", None),
        ("uses", TOKEN_ACTION, "app-token"),
        ("name", "Create immutable tag and GitHub Release", None),
    ]
    assert "environment" not in validate
    assert "secrets." not in yaml.safe_dump(validate)
    assert publish["needs"] == "validate"
    assert publish["environment"] == "release"
    assert "secrets.RELEASE_APP_PRIVATE_KEY" in yaml.safe_dump(publish)


def test_publish_mints_one_repo_token_and_uses_it_for_release_creation():
    """Catch publication falling back to the built-in token or broad App scope."""
    workflow = _release_workflow()
    publish = workflow["jobs"]["publish"]
    token_step = next(step for step in publish["steps"] if step.get("id") == "app-token")
    release_step = _step(publish, "Create immutable tag and GitHub Release")

    token_steps = [
        (job_name, step)
        for job_name, step in _job_steps(workflow)
        if str(step.get("uses", "")).startswith("actions/create-github-app-token@")
    ]
    release_steps = [
        (job_name, step)
        for job_name, step in _job_steps(workflow)
        if "gh release create" in step.get("run", "")
    ]
    serialized_workflow = yaml.safe_dump(workflow)

    assert token_steps == [("publish", token_step)]
    assert token_step["uses"] == TOKEN_ACTION
    assert token_step["with"] == {
        "client-id": "${{ vars.RELEASE_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.RELEASE_APP_PRIVATE_KEY }}",
        "permission-contents": "write",
    }
    assert "skip-token-revoke" not in token_step["with"]
    assert release_steps == [("publish", release_step)]
    assert release_step["env"] == RELEASE_STEP_ENV
    assert release_step["run"] == RELEASE_STEP_RUN
    assert release_step["run"].count("gh release create") == 1
    assert publish["needs"] == "validate"
    assert publish["environment"] == "release"
    assert publish["steps"].index(token_step) + 1 == publish["steps"].index(
        release_step
    )
    assert release_step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert serialized_workflow.count("steps.app-token.outputs.token") == 1
    assert serialized_workflow.count("secrets.RELEASE_APP_PRIVATE_KEY") == 1
    assert "github.token" not in yaml.safe_dump(release_step)


def test_publish_rechecks_identity_and_immutability_after_approval():
    """Catch approval-time drift or an existing tag/release being published."""
    jobs = _release_workflow()["jobs"]
    validate = jobs["validate"]
    publish = jobs["publish"]
    steps = publish["steps"]
    revalidate_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Revalidate release candidate after approval"
    )
    token_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "app-token"
    )
    release_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Create immutable tag and GitHub Release"
    )
    revalidate_step = steps[revalidate_index]
    release_step = steps[release_index]
    refuse_existing_step = _step(
        validate, "Refuse an existing tag or GitHub Release"
    )
    revalidate_run = revalidate_step["run"]
    release_run = release_step["run"]

    assert revalidate_index < token_index < release_index
    assert revalidate_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "EXPECTED_SHA": "${{ needs.validate.outputs.release_sha }}",
        "EXPECTED_TAG": "${{ needs.validate.outputs.tag }}",
    }
    assert "scripts/check_version_metadata.py" in revalidate_run
    assert "actual_version=$(tr -d '[:space:]' < VERSION)" in revalidate_run
    assert 'actual_tag="v$actual_version"' in revalidate_run
    assert 'if [ "$actual_tag" != "$EXPECTED_TAG" ]; then' in revalidate_run
    assert 'if [ "$(git rev-parse HEAD)" != "$EXPECTED_SHA" ]; then' in revalidate_run
    assert 'refs/tags/$actual_tag' in revalidate_run
    _assert_fail_closed_release_probe(refuse_existing_step["run"], "$TAG")
    _assert_fail_closed_release_probe(revalidate_run, "$actual_tag")
    assert release_step["env"]["RELEASE_SHA"] == (
        "${{ needs.validate.outputs.release_sha }}"
    )
    _assert_narrow_release_publication_window(release_run)
