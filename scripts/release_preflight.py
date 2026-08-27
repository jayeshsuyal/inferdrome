#!/usr/bin/env python3
"""Run the offline, fail-closed v0.1 release preflight.

This command checks repository-owned release inputs and can delegate to the
existing engineering and dashboard gates. It never contacts a provider,
launches a GPU, publishes an artifact, or treats a checked checklist item as
machine-verifiable proof. Normal CI can resolve its phase from the exact
package version; final release work must explicitly select final-pre-tag or
post-tag.
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

Phase = Literal["candidate", "final-pre-tag", "post-tag"]
RequestedPhase = Literal["auto", "candidate", "final-pre-tag", "post-tag"]
TagState = Literal["absent", "head", "elsewhere", "error"]
DEVELOPMENT_VERSION = "0.1.0.dev0"
FINAL_VERSION = "0.1.0"
FINAL_TAG = "v0.1.0"
CAPTURE_PRODUCER_COMMIT = "c08b46d9fbd87477f45d130aa3c63615937c4dc3"
CheckStatus = Literal["PASS", "FAIL", "PENDING", "MANUAL", "SKIPPED"]

LICENSE_FILENAMES: tuple[str, ...] = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "COPYING",
    "COPYING.md",
    "COPYING.txt",
)


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


def _read_package_versions(repository_root: Path) -> tuple[str | None, str | None]:
    with (repository_root / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source).get("project", {})
    project_version = project.get("version")
    package_text = (repository_root / "src/inferdrome/__init__.py").read_text(
        encoding="utf-8"
    )
    package_match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        package_text,
        flags=re.MULTILINE,
    )
    package_version = package_match.group(1) if package_match else None
    return (
        project_version if isinstance(project_version, str) else None,
        package_version,
    )


def _check_version(repository_root: Path, *, phase: Phase) -> Check:
    expected_version = (
        DEVELOPMENT_VERSION if phase == "candidate" else FINAL_VERSION
    )
    try:
        versions = _read_package_versions(repository_root)
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        TypeError,
        AttributeError,
    ) as error:
        return Check("package-version", "FAIL", f"could not read version: {error}")
    if versions != (expected_version, expected_version):
        return Check(
            "package-version",
            "FAIL",
            f"pyproject/package versions must both be {expected_version!r} "
            f"for phase {phase!r}; found {versions!r}",
        )
    return Check(
        "package-version",
        "PASS",
        f"package and metadata are {expected_version} for phase {phase}",
    )


def _tag_state(repository_root: Path, *, tag: str) -> tuple[TagState, str]:
    try:
        tag_ref = f"refs/tags/{tag}"
        show_ref = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", tag_ref],
            cwd=repository_root,
            check=False,
        )
        if show_ref.returncode == 1:
            return "absent", f"{tag} does not exist"
        if show_ref.returncode != 0:
            return "error", f"could not inspect {tag_ref}"

        tag_result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{tag_ref}^{{commit}}"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        head_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return "error", f"tag inspection failed: {error}"
    if tag_result.returncode != 0 or head_result.returncode != 0:
        return (
            "error",
            f"{tag} and HEAD must both resolve to commits "
            f"(tag exit {tag_result.returncode}, HEAD exit {head_result.returncode})",
        )
    tag_commit = (tag_result.stdout or "").strip()
    head_commit = (head_result.stdout or "").strip()
    if not tag_commit or tag_commit != head_commit:
        return "elsewhere", f"{tag} does not point to HEAD"
    return "head", f"{tag} points to HEAD ({head_commit})"


def _resolve_phase(
    repository_root: Path, requested_phase: RequestedPhase
) -> tuple[Phase, Check | None]:
    if requested_phase != "auto":
        return requested_phase, None
    try:
        versions = _read_package_versions(repository_root)
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        TypeError,
        AttributeError,
    ) as error:
        return "candidate", Check(
            "phase-selection",
            "FAIL",
            f"auto phase could not read package versions: {error}",
        )
    if versions == (DEVELOPMENT_VERSION, DEVELOPMENT_VERSION):
        return "candidate", Check(
            "phase-selection",
            "PASS",
            "auto phase selected candidate for exact development versions",
        )
    if versions == (FINAL_VERSION, FINAL_VERSION):
        tag_state, tag_detail = _tag_state(repository_root, tag=FINAL_TAG)
        if tag_state == "head":
            return "post-tag", Check(
                "phase-selection",
                "PASS",
                "auto phase selected post-tag for final versions and a tag "
                "that resolves to HEAD",
            )
        if tag_state == "absent":
            return "final-pre-tag", Check(
                "phase-selection",
                "PASS",
                "auto phase selected final-pre-tag for exact final versions "
                "without a release tag",
            )
        if tag_state == "elsewhere":
            return "final-pre-tag", Check(
                "phase-selection",
                "FAIL",
                f"exact final versions are already tagged elsewhere ({tag_detail}); "
                "update to the next development-cycle version/configuration",
            )
        return "final-pre-tag", Check(
            "phase-selection",
            "FAIL",
            f"could not safely select a final phase: {tag_detail}",
        )
    return "candidate", Check(
        "phase-selection",
        "FAIL",
        "auto phase requires both version locations to be exactly "
        f"{DEVELOPMENT_VERSION!r} or exactly {FINAL_VERSION!r}; found {versions!r}",
    )


def _check_license_artifact(repository_root: Path, *, phase: Phase) -> Check:
    if phase == "candidate":
        return Check(
            "license-artifact",
            "SKIPPED",
            "not required for development candidates; owner decides the license",
        )
    present: list[str] = []
    for filename in LICENSE_FILENAMES:
        path = repository_root / filename
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
                present.append(filename)
        except OSError:
            continue
    if not present:
        return Check(
            "license-artifact",
            "FAIL",
            "final phases require one non-empty regular license artifact "
            f"({', '.join(LICENSE_FILENAMES)})",
        )
    return Check(
        "license-artifact",
        "PASS",
        f"final phase license artifact present: {present[0]}",
    )


def _check_tag(repository_root: Path, *, phase: Phase, tag: str) -> Check:
    tag_state, tag_detail = _tag_state(repository_root, tag=tag)
    if phase == "final-pre-tag":
        if tag_state == "absent":
            return Check(
                "release-tag",
                "PASS",
                f"{tag} does not exist; pre-tag phase may proceed",
            )
        if tag_state in ("head", "elsewhere"):
            return Check(
                "release-tag",
                "FAIL",
                f"{tag} already exists; use post-tag verification",
            )
        return Check("release-tag", "FAIL", tag_detail)
    if tag_state == "head":
        return Check("release-tag", "PASS", tag_detail)
    if tag_state == "elsewhere":
        return Check(
            "release-tag",
            "FAIL",
            f"{tag} does not point to the checked commit",
        )
    return Check("release-tag", "FAIL", tag_detail)


def _check_capture_ancestor(repository_root: Path) -> Check:
    try:
        result = subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                CAPTURE_PRODUCER_COMMIT,
                "HEAD",
            ],
            cwd=repository_root,
            check=False,
        )
    except OSError as error:
        return Check(
            "capture-producer-ancestor",
            "FAIL",
            f"could not verify frozen capture producer ancestry: {error}",
        )
    if result.returncode == 0:
        return Check(
            "capture-producer-ancestor",
            "PASS",
            f"frozen capture producer {CAPTURE_PRODUCER_COMMIT} is an ancestor of HEAD",
        )
    if result.returncode == 1:
        return Check(
            "capture-producer-ancestor",
            "FAIL",
            f"frozen capture producer {CAPTURE_PRODUCER_COMMIT} is not an "
            "ancestor of HEAD",
        )
    return Check(
        "capture-producer-ancestor",
        "FAIL",
        "git merge-base could not verify the frozen capture producer ancestry "
        f"(exit code {result.returncode})",
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


def _manual_checks(repository_root: Path) -> tuple[Check, ...]:
    return tuple(_manual_check(repository_root, item) for item in MANUAL_RELEASE_ITEMS)


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
    phase: RequestedPhase,
    repository_only: bool,
    require_clean: bool,
    run_gates: bool,
) -> tuple[Check, ...]:
    """Return deterministic checks for one release-preflight invocation."""

    resolved_phase, phase_selection = _resolve_phase(repository_root, phase)
    checks = [
        _check_required_files(repository_root),
    ]
    if phase_selection is not None:
        checks.append(phase_selection)
    checks.extend(
        (
            _check_version(repository_root, phase=resolved_phase),
            _check_license_artifact(repository_root, phase=resolved_phase),
        )
    )
    checks.extend(
        (
            _check_document_markers(repository_root),
            _check_ci_gate_inventory(repository_root),
            _check_working_tree(repository_root, require_clean=require_clean),
        )
    )
    if resolved_phase != "candidate":
        checks.append(
            _check_capture_ancestor(repository_root)
        )
        checks.append(
            _check_tag(repository_root, phase=resolved_phase, tag=FINAL_TAG)
        )
        checks.append(
            Check(
                "aggregate-ci",
                "MANUAL",
                "GitHub records all three required jobs; this preflight does not "
                "self-verify their aggregate result",
            )
        )
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
    checks.extend(_manual_checks(repository_root))
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
    phase: Phase,
    repository_only: bool,
    run_gates: bool,
) -> None:
    mode = "repository-only" if repository_only else "release-closure"
    print("Inferdrome v0.1 release preflight")
    print(f"phase: {phase}")
    print(f"mode: {mode}")
    print(f"existing gates delegated: {'yes' if run_gates else 'no'}")
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
    if repository_only:
        if _has_failure(checks):
            print("result: FAIL (repository-owned checks are not ready)")
        else:
            result = {
                "candidate": "REPOSITORY_READY",
                "final-pre-tag": "FINAL_PRE_TAG_REPOSITORY_READY",
                "post-tag": "POST_TAG_REPOSITORY_READY",
            }[phase]
            print(
                f"result: {result} (manual inputs are reported only; existing "
                "CI jobs and owner review supply remaining release evidence)"
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
        result = {
            "candidate": (
                "CANDIDATE_READY (manual entries are recorded, but automation "
                "does not verify them)"
            ),
            "final-pre-tag": (
                "PRE_TAG_READY (machine checks passed; the release owner may "
                "perform the separately authorized tag action)"
            ),
            "post-tag": (
                "POST_TAG_VERIFIED (the tag points to HEAD; final record and "
                "approval remain owner-controlled)"
            ),
        }[phase]
        print(f"result: {result}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the offline v0.1 release preflight. The default release-closure "
            "mode fails closed on open manual inputs."
        )
    )
    parser.add_argument(
        "--phase",
        choices=("auto", "candidate", "final-pre-tag", "post-tag"),
        default="auto",
        help=(
            "auto selects candidate for exact development versions; for exact "
            "final versions it selects final-pre-tag without the tag, post-tag "
            "at tag HEAD, and fails if the tag is elsewhere; explicit phases "
            "override it"
        ),
    )
    parser.add_argument(
        "--repository-only",
        action="store_true",
        help="report manual inputs without blocking on them",
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
    requested_phase = arguments.phase
    phase, _ = _resolve_phase(repository_root, requested_phase)
    repository_only = bool(arguments.repository_only)
    require_clean = not bool(arguments.allow_dirty)
    if repository_only and not arguments.require_clean and not arguments.allow_dirty:
        require_clean = False
    checks = run_preflight(
        repository_root,
        phase=requested_phase,
        repository_only=repository_only,
        require_clean=require_clean,
        run_gates=bool(arguments.run_gates),
    )
    _print_report(
        checks,
        phase=phase,
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
