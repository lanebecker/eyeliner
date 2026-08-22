#!/usr/bin/env python3
"""Fail closed unless required GitHub checks passed for one exact release SHA."""
import argparse
import json
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--runs-file", required=True, type=Path)
    parser.add_argument("--checks-file", required=True, type=Path)
    parser.add_argument("--require", action="append", required=True)
    args = parser.parse_args()

    if re.fullmatch(r"[0-9a-f]{40}", args.sha) is None:
        parser.error("sha must be exactly 40 lowercase hexadecimal characters")

    runs_payload = json.loads(args.runs_file.read_text())
    matching_runs = [
        run
        for run in runs_payload["workflow_runs"]
        if run.get("head_sha") == args.sha
        and run.get("path") == ".github/workflows/tests.yml"
        and run.get("event") == "push"
    ]
    if not matching_runs:
        parser.error("no successful tests.yml push run for exact SHA")
    selected_run = max(matching_runs, key=lambda run: run.get("run_number", -1))
    if (
        selected_run.get("status") != "completed"
        or selected_run.get("conclusion") != "success"
    ):
        parser.error("newest tests.yml push run for exact SHA is not successful")
    run_id = selected_run.get("id")
    if not isinstance(run_id, int):
        parser.error("selected tests.yml run has no numeric id")
    selected_suite_id = selected_run.get("check_suite_id")
    if not isinstance(selected_suite_id, int):
        parser.error("selected tests.yml run has no numeric check suite id")

    payload = json.loads(args.checks_file.read_text())
    run_marker = f"/actions/runs/{run_id}/job/"
    eligible = [
        check
        for check in payload["check_runs"]
        if check.get("head_sha") == args.sha
        and check.get("app", {}).get("slug") == "github-actions"
        and run_marker in check.get("details_url", "")
    ]
    passed_checks = []
    missing = []
    for name in args.require:
        matches = [check for check in eligible if check.get("name") == name]
        if (
            len(matches) != 1
            or matches[0].get("status") != "completed"
            or matches[0].get("conclusion") != "success"
        ):
            missing.append(name)
        else:
            passed_checks.append(matches[0])
    if missing:
        parser.error("required checks not successful: " + ", ".join(missing))

    suite_ids = {
        check.get("check_suite", {}).get("id") for check in passed_checks
    }
    if len(suite_ids) != 1 or None in suite_ids:
        parser.error("required checks do not belong to one GitHub Actions suite")
    if suite_ids != {selected_suite_id}:
        parser.error("required checks do not match selected tests.yml suite")

    print(f"release gate passed for {args.sha} via tests run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
