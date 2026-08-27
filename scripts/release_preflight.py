#!/usr/bin/env python3
"""Run the offline, fail-closed v0.1 release preflight.

This command checks repository-owned release inputs and can delegate to the
existing engineering and dashboard gates. It never contacts a provider,
launches a GPU, publishes an artifact, or treats a checked checklist item as
machine-verifiable proof.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EXPECTED_DEVELOPMENT_VERSION = "0.1.0.dev0"
CheckStatus = Literal["PASS", "FAIL", "PENDING", "MANUAL", "SKIPPED"]


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class ManualItem:
    name: str
    marker: str
    owner: str


MANUAL_RELEASE_ITEMS: tuple[ManualItem, ...] = (
    ManualItem(
        "capture-ancestor",
        "Record the eventual release commit",
        "release owner",
    ),
    ManualItem(
        "exitspec-outcomes",
        "Independently demonstrate ExitSpec",
        "ExitSpec owner",
    ),
    ManualItem(
        "exitspec-receipt",
        "Retain the ExitSpec ingestion receipt digest",
        "ExitSpec owner",
    ),
    ManualItem(
        "archive-publication",
        "Owner decides whether to approve public delivery",
        "repository owner",
    ),
    ManualItem(
        "security-review",
        "Complete a human review against",
        "security reviewer",
    ),
    ManualItem(
        "license-selection",
        "Select and add the repository license",
        "repository owner",
    ),
    ManualItem(
        "required-checks",
        "Confirm all required GitHub checks pass",
        "release owner",
    ),
    ManualItem(
        "release-tag",
        "Tag that exact commit as",
        "release owner",
    ),
)

REQUIRED_FILES: tuple[str, ...] = (
    "README.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "src/inferdrome/__init__.py",
    "docs/PRODUCT.md",
    "docs/ROADMAP.md",
    "docs/V0_1_RELEASE_CHECKLIST.md",
    "docs/V0_1_DEFINITION_OF_DONE.md",
    ".github/workflows/ci.yml",
    "scripts/engineering_gate.sh",
    "scripts/dashboard_gate.sh",
    "scripts/deployment_qualification_gate.sh",
    "scripts/release_preflight.py",
)

DOCUMENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "README.md",
        (
            "Inferdrome produces measurements. ExitSpec owns customer acceptance.",
            "dry-run/reference",
            "outside the v0.1 release gate",
        ),
    ),
    (
        "docs/PRODUCT.md",
        (
            "Inferdrome produces measurements. ExitSpec owns customer acceptance.",
            "external ExitSpec handoff boundary",
            "post-v0.1",
        ),
    ),
    (
        "docs/ROADMAP.md",
        (
            "No slice grants cloud launch",
            "Still required to close the external acceptance boundary",
            "release work remain open",
        ),
    ),
    (
        "docs/V0_1_RELEASE_CHECKLIST.md",
        (
            "Deployment qualification gate",
            "External release blocker",
            "EXTERNAL_ONLY",
        ),
    ),
    (
        "docs/V0_1_DEFINITION_OF_DONE.md",
        (
            "separately owned",
            "A repository license is selected and added",
            "release-blocking",
        ),
    ),
)


def _check_required_files(repository_root: Path) -> Check:
    missing = tuple(
        relative_path
        for relative_path in REQUIRED_FILES
        if not (repository_root / relative_path).is_file()
    )
    if missing:
        return Check(
            "required-files",
            "FAIL",
            "missing: " + ", ".join(missing),
        )
    return Check(
        "required-files",
        "PASS",
        f"{len(REQUIRED_FILES)} release inputs are present",
    )


def _check_development_version(repository_root: Path) -> Check:
    try:
        with (repository_root / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source).get("project", {})
        project_version = project.get("version")
        package_text = (repository_root / "src/inferdrome/__init__.py").read_text(
            encoding="utf-8"
        )
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        TypeError,
        AttributeError,
    ) as error:
        return Check("development-version", "FAIL", f"could not read version: {error}")

    package_match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        package_text,
        flags=re.MULTILINE,
    )
    package_version = package_match.group(1) if package_match else None
    versions = (project_version, package_version)
    if versions != (EXPECTED_DEVELOPMENT_VERSION, EXPECTED_DEVELOPMENT_VERSION):
        return Check(
            "development-version",
            "FAIL",
            "pyproject/package versions must both remain "
            f"{EXPECTED_DEVELOPMENT_VERSION!r}; found {versions!r}",
        )
    return Check(
        "development-version",
        "PASS",
        f"package and metadata remain at {EXPECTED_DEVELOPMENT_VERSION}",
    )


def _check_document_markers(repository_root: Path) -> Check:
    missing: list[str] = []
    for relative_path, markers in DOCUMENT_MARKERS:
        try:
            text = (repository_root / relative_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            missing.append(f"{relative_path} ({error})")
            continue
        for marker in markers:
            if marker not in text:
                missing.append(f"{relative_path}: {marker!r}")
    if missing:
        return Check(
            "claim-boundaries",
            "FAIL",
            "missing documented boundary: " + "; ".join(missing),
        )
    return Check(
        "claim-boundaries",
        "PASS",
        "producer proof, local qualification, dry-run/simulation, external work, "
        "and future scope remain labeled",
    )


def _check_ci_gate_inventory(repository_root: Path) -> Check:
    try:
        workflow = (repository_root / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as error:
        return Check("ci-gate-inventory", "FAIL", f"could not read workflow: {error}")

    required_jobs = (
        "  engineering:",
        "  deployment-qualification:",
        "  dashboard:",
    )
    missing = tuple(job for job in required_jobs if job not in workflow)
    if missing:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow is missing jobs: " + ", ".join(missing),
        )
    return Check(
        "ci-gate-inventory",
        "PASS",
        "engineering, deployment qualification, and dashboard jobs are defined",
    )


def _check_working_tree(repository_root: Path, *, require_clean: bool) -> Check:
    if not require_clean:
        return Check(
            "working-tree",
            "SKIPPED",
            "cleanliness not required; use --require-clean for a release commit",
        )
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return Check("working-tree", "FAIL", f"git status failed: {error}")
    if result.returncode != 0:
        return Check(
            "working-tree",
            "FAIL",
            f"git status failed with exit code {result.returncode}",
        )
    changed_entries = sum(
        1 for line in (result.stdout or "").splitlines() if line.strip()
    )
    if changed_entries:
        return Check(
            "working-tree",
            "FAIL",
            f"working tree has {changed_entries} changed or untracked file(s)",
        )
    return Check("working-tree", "PASS", "no changed or untracked files")


def _manual_check(repository_root: Path, item: ManualItem) -> Check:
    checklist_path = repository_root / "docs/V0_1_RELEASE_CHECKLIST.md"
    try:
        lines = checklist_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return Check(
            f"manual-{item.name}",
            "FAIL",
            f"could not read release checklist: {error}",
        )

    candidates = [line for line in lines if item.marker in line]
    if len(candidates) != 1:
        return Check(
            f"manual-{item.name}",
            "FAIL",
            f"expected one checklist item containing {item.marker!r}; "
            f"found {len(candidates)}",
        )
    line = candidates[0].lstrip()
    checkbox = re.match(r"^- \[([ xX])\]", line)
    if checkbox is None:
        return Check(
            f"manual-{item.name}",
            "FAIL",
            "checklist item is not an explicit checkbox",
        )
    if checkbox.group(1).lower() == "x":
        return Check(
            f"manual-{item.name}",
            "MANUAL",
            f"recorded by {item.owner}; automation cannot verify this input",
        )
    return Check(
        f"manual-{item.name}",
        "PENDING",
        f"owner input remains open ({item.owner}); automation cannot prove it",
    )


def _run_gate(repository_root: Path, name: str, script_name: str) -> Check:
    script = repository_root / "scripts" / script_name
    gate_environment = os.environ.copy()
    gate_environment["INFERDROME_PYTHON"] = sys.executable
    source_root = repository_root / "src"
    existing_pythonpath = gate_environment.get("PYTHONPATH")
    gate_environment["PYTHONPATH"] = str(source_root) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    try:
        result = subprocess.run(
            [str(script)],
            cwd=repository_root,
            check=False,
            env=gate_environment,
        )
    except OSError as error:
        return Check(name, "FAIL", f"could not execute {script_name}: {error}")
    if result.returncode != 0:
        return Check(
            name,
            "FAIL",
            f"{script_name} exited with code {result.returncode}",
        )
    return Check(name, "PASS", f"delegated to {script_name}")


def run_preflight(
    repository_root: Path,
    *,
    repository_only: bool,
    require_clean: bool,
    run_gates: bool,
) -> tuple[Check, ...]:
    """Return deterministic checks for one release-preflight invocation."""

    checks = [
        _check_required_files(repository_root),
        _check_development_version(repository_root),
        _check_document_markers(repository_root),
        _check_ci_gate_inventory(repository_root),
        _check_working_tree(repository_root, require_clean=require_clean),
    ]
    if run_gates:
        checks.extend(
            (
                _run_gate(repository_root, "engineering-gate", "engineering_gate.sh"),
                _run_gate(repository_root, "dashboard-gate", "dashboard_gate.sh"),
            )
        )
    else:
        checks.extend(
            (
                Check(
                    "engineering-gate",
                    "SKIPPED",
                    "not run; pass --run-gates to delegate to existing gates",
                ),
                Check(
                    "dashboard-gate",
                    "SKIPPED",
                    "not run; pass --run-gates to delegate to existing gates",
                ),
            )
        )
    checks.extend(_manual_check(repository_root, item) for item in MANUAL_RELEASE_ITEMS)
    return tuple(checks)


def _has_failure(checks: Sequence[Check]) -> bool:
    return any(check.status == "FAIL" for check in checks)


def _has_pending_manual_input(checks: Sequence[Check]) -> bool:
    return any(check.status == "PENDING" for check in checks)


def _has_skipped_check(checks: Sequence[Check]) -> bool:
    return any(check.status == "SKIPPED" for check in checks)


def _print_report(
    checks: Sequence[Check],
    *,
    repository_only: bool,
    run_gates: bool,
) -> None:
    mode = "repository-only" if repository_only else "release-closure"
    print("Inferdrome v0.1 release preflight")
    print(f"mode: {mode}")
    print(f"existing gates delegated: {'yes' if run_gates else 'no'}")
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
    if repository_only:
        if _has_failure(checks):
            print("result: FAIL (repository-owned checks are not ready)")
        else:
            print(
                "result: REPOSITORY_READY (manual release inputs are reported only; "
                "no acceptance or release approval is claimed)"
            )
        return

    if (
        _has_failure(checks)
        or _has_pending_manual_input(checks)
        or _has_skipped_check(checks)
    ):
        print(
            "result: BLOCKED (release closure requires the repository checks plus "
            "the still-open manual/external inputs; skipped checks are not proof)"
        )
    else:
        print(
            "result: READY_FOR_OWNER_RELEASE_REVIEW (manual entries are recorded, "
            "but automation does not verify them)"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the offline v0.1 release preflight. The default release-closure "
            "mode fails closed on open manual inputs."
        )
    )
    parser.add_argument(
        "--repository-only",
        action="store_true",
        help="check repository-owned conditions without blocking on manual inputs",
    )
    parser.add_argument(
        "--run-gates",
        action="store_true",
        help="delegate to the existing engineering and dashboard gates",
    )
    cleanliness = parser.add_mutually_exclusive_group()
    cleanliness.add_argument(
        "--require-clean",
        action="store_true",
        help="fail if git reports changed or untracked files",
    )
    cleanliness.add_argument(
        "--allow-dirty",
        action="store_true",
        help="skip the clean-worktree check for local iteration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parent.parent
    repository_only = bool(arguments.repository_only)
    require_clean = not bool(arguments.allow_dirty)
    if repository_only and not arguments.require_clean and not arguments.allow_dirty:
        require_clean = False
    checks = run_preflight(
        repository_root,
        repository_only=repository_only,
        require_clean=require_clean,
        run_gates=bool(arguments.run_gates),
    )
    _print_report(
        checks,
        repository_only=repository_only,
        run_gates=bool(arguments.run_gates),
    )
    if repository_only:
        return 1 if _has_failure(checks) else 0
    return (
        1
        if (
            _has_failure(checks)
            or _has_pending_manual_input(checks)
            or _has_skipped_check(checks)
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
