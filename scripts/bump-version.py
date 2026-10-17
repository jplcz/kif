#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: BSD-2-Clause

"""
Bumps the project's semantic version across every version-bearing file in
lock-step, so a single invocation keeps them all consistent:

  - CMakeLists.txt's `project(... VERSION X.Y.Z ...)` declaration (the
    SSOT every other file below is derived from).
  - conanfile.py's `version = "X.Y.Z"` class attribute, if present.
  - pyproject.toml's `version = "X.Y.Z"` (pip package).
  - packaging/vcpkg/ports/*/vcpkg.json's "version" field, if present.

Usage:
  scripts/bump-version.py --bump patch|minor|major
  scripts/bump-version.py --set X.Y.Z

Prints the old and new version to stdout; when run inside a GitHub Actions
job (i.e. $GITHUB_OUTPUT is set), also appends `old_version=X.Y.Z` and
`new_version=X.Y.Z` to it for use by subsequent workflow steps.
"""

import argparse
import glob
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CMAKELISTS = ROOT / "CMakeLists.txt"
CONANFILE = ROOT / "conanfile.py"
PYPROJECT = ROOT / "pyproject.toml"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_current_version() -> tuple:
    text = CMAKELISTS.read_text(encoding="utf-8")
    match = re.search(r"project\([^)]*\bVERSION\s+(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        sys.exit("error: could not find `project(... VERSION X.Y.Z ...)` in CMakeLists.txt")
    return tuple(int(part) for part in match.groups())


def bump(version: tuple, part: str) -> tuple:
    major, minor, patch = version
    if part == "major":
        return (major + 1, 0, 0)
    if part == "minor":
        return (major, minor + 1, 0)
    if part == "patch":
        return (major, minor, patch + 1)
    raise ValueError(f"unknown bump part: {part}")


def write_cmakelists(new_version: str) -> None:
    text = CMAKELISTS.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"(project\([^)]*\bVERSION\s+)\d+\.\d+\.\d+",
        lambda m: m.group(1) + new_version,
        text,
        count=1,
    )
    if count != 1:
        sys.exit("error: failed to update CMakeLists.txt version")
    CMAKELISTS.write_text(new_text, encoding="utf-8")


def write_conanfile(new_version: str) -> None:
    if not CONANFILE.exists():
        return
    text = CONANFILE.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(^\s*version\s*=\s*)"\d+\.\d+\.\d+"',
        lambda m: m.group(1) + f'"{new_version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        sys.exit("error: failed to update conanfile.py version")
    CONANFILE.write_text(new_text, encoding="utf-8")


def write_pyproject(new_version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(^version\s*=\s*)"\d+\.\d+\.\d+"', lambda m: m.group(1) + f'"{new_version}"', text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        sys.exit("error: failed to update pyproject.toml version")
    PYPROJECT.write_text(new_text, encoding="utf-8")


def write_vcpkg_manifests(new_version: str) -> None:
    for manifest in sorted(glob.glob(str(ROOT / "packaging" / "vcpkg" / "ports" / "*" / "vcpkg.json"))):
        path = pathlib.Path(manifest)
        text = path.read_text(encoding="utf-8")
        new_text, count = re.subn(
            r'("version"\s*:\s*)"\d+\.\d+\.\d+"',
            lambda m: m.group(1) + f'"{new_version}"',
            text,
            count=1,
        )
        if count != 1:
            sys.exit(f"error: failed to update {path} version")
        path.write_text(new_text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bump", choices=["major", "minor", "patch"], help="increment the given version part")
    group.add_argument("--set", metavar="X.Y.Z", help="set an explicit version instead of incrementing")
    args = parser.parse_args()

    current = read_current_version()
    if args.set:
        if not VERSION_RE.match(args.set):
            sys.exit(f"error: --set version must be X.Y.Z, got {args.set!r}")
        new_version = args.set
    else:
        new_version = ".".join(str(part) for part in bump(current, args.bump))

    write_cmakelists(new_version)
    write_conanfile(new_version)
    write_pyproject(new_version)
    write_vcpkg_manifests(new_version)

    old_version = ".".join(str(part) for part in current)
    print(f"Bumped version: {old_version} -> {new_version}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"old_version={old_version}\n")
            handle.write(f"new_version={new_version}\n")


if __name__ == "__main__":
    main()
