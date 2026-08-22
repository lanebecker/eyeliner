"""Behavior tests for the read-only version metadata gate in #415 / R10-02."""
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "scripts" / "check_version_metadata.py"


def _run_check(tmp_path, *, version="1.5.35", heading="1.5.35", badge="1.5.35"):
    version_file = tmp_path / "VERSION"
    changelog_file = tmp_path / "CHANGELOG.md"
    readme_file = tmp_path / "README.md"
    version_file.write_text(version + "\n")
    changelog_file.write_text(f"# Changelog\n\n## [{heading}] - 2026-08-22\n")
    readme_file.write_text(
        f"[![version](https://img.shields.io/badge/version-{badge}-blueviolet)](VERSION)\n"
    )
    return subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--version-file",
            str(version_file),
            "--changelog-file",
            str(changelog_file),
            "--readme-file",
            str(readme_file),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )


def test_version_metadata_gate_accepts_one_consistent_release_candidate(tmp_path):
    """A valid candidate gives contributors one deterministic green check."""
    result = _run_check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "version metadata passed for 1.5.35"


def test_version_metadata_gate_rejects_a_stale_readme_badge(tmp_path):
    """Removing the badge comparison must let this stale public version pass."""
    result = _run_check(tmp_path, badge="1.5.34")

    assert result.returncode != 0
    assert "README version badge does not match VERSION 1.5.35" in result.stderr


def test_version_metadata_gate_rejects_stale_badge_even_if_expected_url_is_elsewhere(
    tmp_path,
):
    """A substring search must not let a comment hide a stale rendered badge."""
    version_file = tmp_path / "VERSION"
    changelog_file = tmp_path / "CHANGELOG.md"
    readme_file = tmp_path / "README.md"
    version_file.write_text("1.5.35\n")
    changelog_file.write_text("## [1.5.35] - 2026-08-22\n")
    readme_file.write_text(
        "<!-- https://img.shields.io/badge/version-1.5.35-blueviolet -->\n"
        "[![version](https://img.shields.io/badge/version-1.5.34-blueviolet)](VERSION)\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--version-file",
            str(version_file),
            "--changelog-file",
            str(changelog_file),
            "--readme-file",
            str(readme_file),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "README version badge does not match VERSION 1.5.35" in result.stderr


def test_version_metadata_gate_rejects_a_missing_changelog_heading(tmp_path):
    """Removing the changelog comparison must let an undocumented version pass."""
    result = _run_check(tmp_path, heading="1.5.34")

    assert result.returncode != 0
    assert "CHANGELOG has no ## [1.5.35] heading" in result.stderr


def test_version_metadata_gate_rejects_untrusted_version_bytes(tmp_path):
    """Removing strict semver validation must accept shell/sed-hostile input."""
    result = _run_check(tmp_path, version="1.5.35$(id)", heading="1.5.35$(id)", badge="1.5.35$(id)")

    assert result.returncode != 0
    assert "VERSION is not valid release semver" in result.stderr


@pytest.mark.parametrize("version", ["1.5.35", "10.0.1", "1.6.0-rc.1", "2.0.0-beta2"])
def test_version_metadata_gate_preserves_supported_release_forms(tmp_path, version):
    """Narrowing the historical release grammar must reject one of these forms."""
    result = _run_check(tmp_path, version=version, heading=version, badge=version)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("version", ["1x5x35", "v1.5.35", "1.5", ""])
def test_version_metadata_gate_preserves_known_rejections(tmp_path, version):
    """Loosening the historical release grammar must accept one of these values."""
    result = _run_check(tmp_path, version=version, heading=version, badge=version)

    assert result.returncode != 0
    assert "VERSION is not valid release semver" in result.stderr


def test_repository_version_metadata_is_currently_consistent():
    """A release-prep omission in any of the three tracked files must fail CI."""
    result = subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--version-file",
            str(REPO / "VERSION"),
            "--changelog-file",
            str(REPO / "CHANGELOG.md"),
            "--readme-file",
            str(REPO / "README.md"),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
