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


def test_controlled_release_has_no_deindented_step_content_at_workflow_scope():
    """Deindenting block-scalar lines must not create invalid workflow keys."""
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "release.yml").read_text()
    )

    unexpected = set(workflow) - WORKFLOW_TOP_LEVEL_KEYS
    assert unexpected == set(), f"unexpected workflow-level keys: {sorted(unexpected)}"
