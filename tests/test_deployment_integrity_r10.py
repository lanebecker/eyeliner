"""Deployment-facing CI contracts introduced in Wave 1."""
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
TESTS_WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _tests_job():
    return yaml.safe_load(TESTS_WORKFLOW.read_text())["jobs"]["test"]


def _step_index(steps, name):
    return next(index for index, step in enumerate(steps) if step.get("name") == name)


def test_ci_installs_portaudio_and_smokes_the_real_backend_before_pytest():
    """#156: every supported CI leg proves the installed backend before stubbing."""
    job = _tests_job()
    steps = job["steps"]
    portaudio_index = _step_index(steps, "Install PortAudio")
    dependencies_index = _step_index(steps, "Install dependencies")
    smoke_index = _step_index(steps, "Smoke-test audio backend")
    pytest_index = _step_index(steps, "Run tests")

    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    assert "sudo apt-get install --yes libportaudio2" in steps[portaudio_index]["run"]
    assert steps[smoke_index]["run"] == "python scripts/check_audio_backend.py"
    assert steps[pytest_index]["run"] == "pytest -q"
    assert portaudio_index < dependencies_index < smoke_index < pytest_index
