"""Behavior tests for the workflow-authenticated release gate in #402."""
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "release_gate.py"
SHA = "a" * 40
RUN_ID = 98765
SUITE_ID = 12345
REQUIRED = ("test (3.11)", "test (3.12)", "test (3.13)")


def _run_record(
    *,
    run_id=RUN_ID,
    head_sha=SHA,
    path=".github/workflows/tests.yml",
    event="push",
    status="completed",
    conclusion="success",
    run_number=100,
    suite_id=SUITE_ID,
):
    return {
        "id": run_id,
        "head_sha": head_sha,
        "path": path,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "run_number": run_number,
        "check_suite_id": suite_id,
    }


def _check(
    name,
    *,
    run_id=RUN_ID,
    head_sha=SHA,
    status="completed",
    conclusion="success",
    suite_id=SUITE_ID,
    app_slug="github-actions",
):
    return {
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": app_slug},
        "check_suite": {"id": suite_id},
        "details_url": f"https://github.com/example/repo/actions/runs/{run_id}/job/1",
    }


def _invoke(tmp_path, checks, *, runs=None, sha=SHA):
    checks_file = tmp_path / "checks.json"
    runs_file = tmp_path / "runs.json"
    checks_file.write_text(json.dumps({"check_runs": checks}))
    runs_file.write_text(json.dumps({"workflow_runs": runs or [_run_record()]}))
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--sha",
            sha,
            "--runs-file",
            str(runs_file),
            "--checks-file",
            str(checks_file),
            *[part for name in REQUIRED for part in ("--require", name)],
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )


def test_release_gate_accepts_exact_sha_only_after_authenticated_matrix_passes(tmp_path):
    """Removing workflow/run authentication must make this guarantee disappear."""
    result = _invoke(tmp_path, [_check(name) for name in REQUIRED])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"release gate passed for {SHA} via tests run {RUN_ID}"


def test_release_gate_rejects_a_non_commit_sha_before_considering_checks(tmp_path):
    """Removing full-hex SHA validation must make symbolic refs releasable."""
    result = _invoke(tmp_path, [_check(name, head_sha="main") for name in REQUIRED], sha="main")

    assert result.returncode != 0
    assert "sha must be exactly 40 lowercase hexadecimal characters" in result.stderr


@pytest.mark.parametrize(
    ("broken_name", "head_sha", "status", "conclusion"),
    [
        ("test (3.11)", SHA, "completed", "failure"),
        ("test (3.12)", SHA, "in_progress", None),
        ("test (3.13)", "b" * 40, "completed", "success"),
    ],
)
def test_release_gate_fails_closed_on_failed_pending_or_wrong_sha_job(
    tmp_path, broken_name, head_sha, status, conclusion
):
    checks = [
        _check(
            name,
            head_sha=head_sha if name == broken_name else SHA,
            status=status if name == broken_name else "completed",
            conclusion=conclusion if name == broken_name else "success",
        )
        for name in REQUIRED
    ]
    result = _invoke(tmp_path, checks)

    assert result.returncode != 0
    assert f"required checks not successful: {broken_name}" in result.stderr


def test_release_gate_rejects_same_name_success_from_an_untrusted_app(tmp_path):
    """Ignoring app provenance must let a foreign app mask a failed CI leg."""
    checks = [_check(name) for name in REQUIRED]
    checks[0]["conclusion"] = "failure"
    checks.append(_check(REQUIRED[0], app_slug="not-github-actions"))

    result = _invoke(tmp_path, checks)

    assert result.returncode != 0
    assert f"required checks not successful: {REQUIRED[0]}" in result.stderr


def test_release_gate_rejects_a_successful_impostor_workflow(tmp_path):
    """Trusting names/suite alone must let another workflow impersonate tests.yml."""
    result = _invoke(
        tmp_path,
        [_check(name) for name in REQUIRED],
        runs=[_run_record(path=".github/workflows/impostor.yml")],
    )

    assert result.returncode != 0
    assert "no successful tests.yml push run for exact SHA" in result.stderr


def test_release_gate_ignores_duplicate_names_from_a_different_run(tmp_path):
    """Filtering names globally must block a safe SHA after a scheduled run."""
    checks = [_check(name) for name in REQUIRED]
    checks.extend(
        _check(name, run_id=RUN_ID + 1, suite_id=SUITE_ID + 1)
        for name in REQUIRED
    )
    runs = [
        _run_record(),
        _run_record(run_id=RUN_ID + 1, event="schedule", run_number=101),
    ]

    result = _invoke(tmp_path, checks, runs=runs)

    assert result.returncode == 0, result.stderr


def test_release_gate_rejects_required_jobs_split_across_suites(tmp_path):
    """Dropping the shared-suite rule must combine unrelated job sets."""
    checks = [_check(name) for name in REQUIRED]
    checks[-1]["check_suite"] = {"id": SUITE_ID + 1}

    result = _invoke(tmp_path, checks)

    assert result.returncode != 0
    assert "required checks do not belong to one GitHub Actions suite" in result.stderr


def test_release_gate_rejects_newest_tests_run_when_it_failed(tmp_path):
    """Filtering for success before selecting newest must revive an older green run."""
    runs = [
        _run_record(),
        _run_record(
            run_id=RUN_ID + 1,
            run_number=101,
            suite_id=SUITE_ID + 1,
            conclusion="failure",
        ),
    ]

    result = _invoke(tmp_path, [_check(name) for name in REQUIRED], runs=runs)

    assert result.returncode != 0
    assert "newest tests.yml push run for exact SHA is not successful" in result.stderr


def test_release_gate_rejects_jobs_not_in_selected_runs_declared_suite(tmp_path):
    """Checking only that jobs agree must accept a suite the selected run does not own."""
    checks = [_check(name, suite_id=SUITE_ID + 1) for name in REQUIRED]

    result = _invoke(tmp_path, checks)

    assert result.returncode != 0
    assert "required checks do not match selected tests.yml suite" in result.stderr
