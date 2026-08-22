#!/usr/bin/env python3
"""Fail closed unless VERSION, CHANGELOG, and README badge agree."""
import argparse
import re
from pathlib import Path


SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", required=True, type=Path)
    parser.add_argument("--changelog-file", required=True, type=Path)
    parser.add_argument("--readme-file", required=True, type=Path)
    args = parser.parse_args()

    version = "".join(args.version_file.read_text().split())
    if SEMVER.fullmatch(version) is None:
        parser.error("VERSION is not valid release semver")

    changelog = args.changelog_file.read_text()
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE) is None:
        parser.error(f"CHANGELOG has no ## [{version}] heading")

    readme = args.readme_file.read_text()
    badge_versions = re.findall(
        r"\[!\[version\]\(https://img\.shields\.io/badge/version-(.+?)-blueviolet\)\]"
        r"\(VERSION\)",
        readme,
    )
    if badge_versions != [version]:
        parser.error(f"README version badge does not match VERSION {version}")

    print(f"version metadata passed for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
