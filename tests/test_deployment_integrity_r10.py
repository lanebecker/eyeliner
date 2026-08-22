"""Deployment-facing CI contracts introduced in Wave 1."""
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parent.parent
TESTS_WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
PORTAUDIO_RUN = "sudo apt-get update\nsudo apt-get install --yes libportaudio2\n"


def _tests_job(workflow_path=TESTS_WORKFLOW):
    return yaml.safe_load(workflow_path.read_text())["jobs"]["test"]


def _step_index(steps, name):
    return next(index for index, step in enumerate(steps) if step.get("name") == name)


def _assert_audio_boundary_contract(job):
    steps = job["steps"]
    portaudio_index = _step_index(steps, "Install PortAudio")
    dependencies_index = _step_index(steps, "Install dependencies")
    smoke_index = _step_index(steps, "Smoke-test audio backend")
    pytest_index = _step_index(steps, "Run tests")

    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    assert "if" not in job
    assert "continue-on-error" not in job
    assert steps[portaudio_index]["run"] == PORTAUDIO_RUN
    assert steps[smoke_index]["run"] == "python -I scripts/check_audio_backend.py"
    assert steps[pytest_index]["run"] == "pytest -q"
    for index in (portaudio_index, smoke_index, pytest_index):
        assert "if" not in steps[index]
        assert "continue-on-error" not in steps[index]
    assert portaudio_index < dependencies_index < smoke_index < pytest_index


def test_ci_installs_portaudio_and_smokes_the_real_backend_before_pytest():
    """#156: every supported CI leg proves the installed backend before stubbing."""
    _assert_audio_boundary_contract(_tests_job())


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
        _assert_audio_boundary_contract(_tests_job(mutated_workflow))


def test_audio_boundary_contract_rejects_nonblocking_portaudio_install(tmp_path):
    """The exact command blocks a shell-level `|| true` bypass as well."""
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    portaudio_step = steps[_step_index(steps, "Install PortAudio")]
    portaudio_step["run"] += " || true"
    mutated_workflow = tmp_path / "tests.yml"
    mutated_workflow.write_text(yaml.safe_dump(workflow))

    with pytest.raises(AssertionError):
        _assert_audio_boundary_contract(_tests_job(mutated_workflow))
