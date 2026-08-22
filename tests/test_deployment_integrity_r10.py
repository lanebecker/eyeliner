"""Deployment-facing CI contracts introduced in Wave 1."""
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parent.parent
TESTS_WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
PORTAUDIO_RUN = "sudo apt-get update\nsudo apt-get install --yes libportaudio2\n"
EXPECTED_STRATEGY = {
    "fail-fast": False,
    "matrix": {"python-version": ["3.11", "3.12", "3.13"]},
}
EXPECTED_CRITICAL_STEPS = {
    "Install PortAudio": {"name": "Install PortAudio", "run": PORTAUDIO_RUN},
    "Smoke-test audio backend": {
        "name": "Smoke-test audio backend",
        "run": "python -I scripts/check_audio_backend.py",
    },
    "Run tests": {"name": "Run tests", "run": "pytest -q"},
}
EXPECTED_SERVICE_STEP = {
    "name": "Render and verify system service",
    "run": """set -euo pipefail
tmp_dir="$(mktemp -d)"
app_dir="$tmp_dir/app"
mkdir -p "$app_dir/venv/bin"
touch "$app_dir/config.yaml" "$app_dir/main.py" "$tmp_dir/Xauthority"
chmod 600 "$app_dir/config.yaml"
ln -s "$(command -v python)" "$app_dir/venv/bin/python3"
python -I scripts/render_system_service.py --user root --app-dir "$app_dir" --display :0 --xauthority "$tmp_dir/Xauthority" --output "$tmp_dir/vinyl-now-playing.service"
/usr/bin/systemd-analyze verify "$tmp_dir/vinyl-now-playing.service"
""",
}


def _workflow(workflow_path=TESTS_WORKFLOW):
    return yaml.safe_load(workflow_path.read_text())


def _tests_job(workflow_path=TESTS_WORKFLOW):
    return _workflow(workflow_path)["jobs"]["test"]


def _step_index(steps, name):
    return next(index for index, step in enumerate(steps) if step.get("name") == name)


def _named_steps(steps, name):
    return [step for step in steps if step.get("name") == name]


def _assert_audio_boundary_contract(workflow):
    job = workflow["jobs"]["test"]
    steps = job["steps"]
    portaudio_index = _step_index(steps, "Install PortAudio")
    dependencies_index = _step_index(steps, "Install dependencies")
    smoke_index = _step_index(steps, "Smoke-test audio backend")
    pytest_index = _step_index(steps, "Run tests")

    assert "defaults" not in workflow
    assert job["strategy"] == EXPECTED_STRATEGY
    assert "defaults" not in job
    assert "if" not in job
    assert "continue-on-error" not in job
    for name, expected_step in EXPECTED_CRITICAL_STEPS.items():
        assert _named_steps(steps, name) == [expected_step]
    assert portaudio_index < dependencies_index < smoke_index < pytest_index


def _assert_system_service_contract(workflow):
    """#419: every supported Linux leg parses a rendered representative unit."""
    job = workflow["jobs"]["test"]
    steps = job["steps"]
    service_index = _step_index(steps, "Render and verify system service")
    smoke_index = _step_index(steps, "Smoke-test audio backend")
    pytest_index = _step_index(steps, "Run tests")

    assert _named_steps(steps, "Render and verify system service") == [
        EXPECTED_SERVICE_STEP
    ]
    assert "if" not in steps[service_index]
    assert "continue-on-error" not in steps[service_index]
    assert smoke_index < service_index < pytest_index


def test_ci_installs_portaudio_and_smokes_the_real_backend_before_pytest():
    """#156: every supported CI leg proves the installed backend before stubbing."""
    _assert_audio_boundary_contract(_workflow())


def test_ci_renders_and_verifies_the_versioned_system_service_before_pytest():
    _assert_system_service_contract(_workflow())


@pytest.mark.parametrize(
    ("field", "value"),
    (("if", "matrix.python-version != '3.13'"), ("continue-on-error", True)),
)
def test_system_service_contract_rejects_ci_bypass_mutations(tmp_path, field, value):
    """Disposable mutations prove service verification cannot go nonblocking."""
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    steps[_step_index(steps, "Render and verify system service")][field] = value
    mutated_workflow = tmp_path / "tests.yml"
    mutated_workflow.write_text(yaml.safe_dump(workflow))

    with pytest.raises(AssertionError):
        _assert_system_service_contract(_workflow(mutated_workflow))


def test_system_service_contract_keeps_using_the_os_parser_if_path_is_shadowed(tmp_path):
    """A prior PATH mutation cannot replace the absolute Ubuntu parser binary."""
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    service_index = _step_index(steps, "Render and verify system service")
    steps.insert(
        service_index,
        {"name": "Shadow PATH", "run": "echo /tmp/fake-bin >> \"$GITHUB_PATH\""},
    )
    mutated_workflow = tmp_path / "tests.yml"
    mutated_workflow.write_text(yaml.safe_dump(workflow))

    _assert_system_service_contract(_workflow(mutated_workflow))


@pytest.mark.parametrize(
    ("step_name", "field", "value"),
    (
        ("Install PortAudio", "if", "matrix.python-version != '3.13'"),
        ("Smoke-test audio backend", "if", "matrix.python-version != '3.13'"),
        ("Run tests", "if", "matrix.python-version != '3.13'"),
        ("Install PortAudio", "continue-on-error", True),
        ("Smoke-test audio backend", "continue-on-error", True),
        ("Run tests", "continue-on-error", True),
    ),
)
def test_audio_boundary_contract_rejects_per_leg_bypass_mutations(
    tmp_path, step_name, field, value
):
    """Mutate a disposable workflow file to prove every required step fails closed."""
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    steps[_step_index(steps, step_name)][field] = value
    mutated_workflow = tmp_path / "tests.yml"
    mutated_workflow.write_text(yaml.safe_dump(workflow))

    with pytest.raises(AssertionError):
        _assert_audio_boundary_contract(_workflow(mutated_workflow))


def test_audio_boundary_contract_rejects_nonblocking_portaudio_install(tmp_path):
    """The exact command blocks a shell-level `|| true` bypass as well."""
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    portaudio_step = steps[_step_index(steps, "Install PortAudio")]
    portaudio_step["run"] += " || true"
    mutated_workflow = tmp_path / "tests.yml"
    mutated_workflow.write_text(yaml.safe_dump(workflow))

    with pytest.raises(AssertionError):
        _assert_audio_boundary_contract(_workflow(mutated_workflow))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda workflow: workflow.__setitem__(
            "defaults", {"run": {"shell": "bash -c 'source {0} || true'"}}
        ),
        lambda workflow: workflow["jobs"]["test"].__setitem__(
            "defaults", {"run": {"shell": "bash -c 'source {0} || true'"}}
        ),
        lambda workflow: workflow["jobs"]["test"]["strategy"]["matrix"].__setitem__(
            "exclude", [{"python-version": "3.13"}]
        ),
        lambda workflow: workflow["jobs"]["test"]["strategy"]["matrix"].__setitem__(
            "include", [{"python-version": "3.14"}]
        ),
        lambda workflow: workflow["jobs"]["test"]["steps"][_step_index(
            workflow["jobs"]["test"]["steps"], "Install PortAudio"
        )].__setitem__("shell", "bash -c 'source {0} || true'"),
        lambda workflow: workflow["jobs"]["test"]["steps"][_step_index(
            workflow["jobs"]["test"]["steps"], "Smoke-test audio backend"
        )].__setitem__("shell", "bash -c 'source {0} || true'"),
        lambda workflow: workflow["jobs"]["test"]["steps"][_step_index(
            workflow["jobs"]["test"]["steps"], "Run tests"
        )].__setitem__("shell", "bash -c 'source {0} || true'"),
    ),
    ids=(
        "workflow-default-shell",
        "job-default-shell",
        "matrix-exclude",
        "matrix-include",
        "portaudio-step-shell",
        "smoke-step-shell",
        "pytest-step-shell",
    ),
)
def test_audio_boundary_contract_rejects_effective_semantics_bypasses(
    tmp_path, mutation
):
    """Disposable mutations prove defaults and matrix overlays cannot bypass CI."""
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    mutation(workflow)
    mutated_workflow = tmp_path / "tests.yml"
    mutated_workflow.write_text(yaml.safe_dump(workflow))

    with pytest.raises(AssertionError):
        _assert_audio_boundary_contract(_workflow(mutated_workflow))
