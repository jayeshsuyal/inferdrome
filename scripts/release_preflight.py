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
import hashlib
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
TagState = Literal["absent", "head", "elsewhere", "unannotated", "error"]
DEVELOPMENT_VERSION = "0.2.0.dev0"
FINAL_VERSION = "0.2.0"
FINAL_TAG = "v0.2.0"
CAPTURE_PRODUCER_COMMIT = "c08b46d9fbd87477f45d130aa3c63615937c4dc3"
CheckStatus = Literal["PASS", "FAIL", "PENDING", "MANUAL", "SKIPPED"]

APACHE_LICENSE_EXPRESSION = "Apache-2.0"
APACHE_LICENSE_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)
REPOSITORY_URL = "https://github.com/jayeshsuyal/inferdrome"
UV_LOCK_VERSION = 1
UV_LOCK_REVISION = 3
PROJECT_REQUIRES_PYTHON = ">=3.12,<3.13"
LOCK_REQUIRES_PYTHON = "==3.12.*"
PYPI_REGISTRY_URL = "https://pypi.org/simple"
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_NORMALIZED_EXTRA_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_ACTION_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PIP_INSTALL_COMMAND_RE = re.compile(
    r"\bpip(?:3(?:\.\d+)*)?\s+install\b",
    re.IGNORECASE,
)
_DIRECT_PIP_COMMAND_RE = re.compile(
    r"(?:"
    r"^\s*(?:[\"']?\$(?:\{)?inferdrome_python(?:\})?[\"']?|"
    r"(?:[^\s\"']+/)?python(?:3(?:\.\d+)*)?)\s+-m\s+pip\b"
    r"|"
    r"^\s*(?:[^\s\"']+/)?pip(?:3(?:\.\d+)*)?\s+(?:install|wheel)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_UV_SYNC_COMMAND_RE = re.compile(
    r"(?:"
    r"(?:\"\$(?:\{uv_bin\}|uv_bin)\"|\$(?:\{uv_bin\}|uv_bin)|"
    r"\"[^\"\n]+/uv\"|(?:[A-Za-z0-9_.${}/-]+/)?uv)\s+sync\b"
    r"|"
    r"(?:\"[^\"\n]+/python(?:3(?:\.\d+)*)?\"|"
    r"(?:[A-Za-z0-9_.${}/-]+/)?python(?:3(?:\.\d+)*)?)"
    r"\s+-m\s+uv\s+sync\b"
    r")"
)
_UV_LOCK_COMMAND_RE = re.compile(
    r"(?:"
    r"(?:\"\$(?:\{uv_bin\}|uv_bin)\"|\$(?:\{uv_bin\}|uv_bin)|"
    r"\"[^\"\n]+/uv\"|(?:[A-Za-z0-9_.${}/-]+/)?uv)\s+lock\b"
    r"|"
    r"(?:\"[^\"\n]+/python(?:3(?:\.\d+)*)?\"|"
    r"(?:[A-Za-z0-9_.${}/-]+/)?python(?:3(?:\.\d+)*)?)"
    r"\s+-m\s+uv\s+lock\b"
    r")"
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


def _uv_sync_command_lines(text: str) -> tuple[str, ...]:
    """Return non-comment lines that directly invoke ``uv sync``."""

    return tuple(
        line.strip()
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and _UV_SYNC_COMMAND_RE.search(line) is not None
    )


def _uv_lock_command_lines(text: str) -> tuple[str, ...]:
    """Return non-comment lines that directly invoke ``uv lock``."""

    return tuple(
        line.strip()
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and _UV_LOCK_COMMAND_RE.search(line) is not None
    )


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
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "uv.lock",
    "src/inferdrome/__init__.py",
    "docs/PRODUCT.md",
    "docs/ROADMAP.md",
    "docs/V0_1_RELEASE_CHECKLIST.md",
    "docs/V0_1_DEFINITION_OF_DONE.md",
    ".github/workflows/ci.yml",
    "scripts/engineering_gate.sh",
    "scripts/dashboard_gate.sh",
    "scripts/dashboard_package_gate.sh",
    "scripts/deployment_qualification_gate.sh",
    "scripts/bootstrap_ci_uv.sh",
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
            "owner-selected Apache License 2.0 is added",
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
    expected_version = DEVELOPMENT_VERSION if phase == "candidate" else FINAL_VERSION
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

        type_result = subprocess.run(
            ["git", "cat-file", "-t", tag_ref],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if type_result.returncode != 0:
            return "error", f"could not inspect the object type for {tag_ref}"
        object_type = (type_result.stdout or "").strip()
        if object_type != "tag":
            return (
                "unannotated",
                f"{tag} is not an annotated tag (object type: "
                f"{object_type or 'unknown'})",
            )

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
        if tag_state == "unannotated":
            return "final-pre-tag", Check(
                "phase-selection",
                "FAIL",
                f"{tag_detail}; the release tag must be an authorized annotated tag",
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


def _check_license_artifact(repository_root: Path) -> Check:
    license_path = repository_root / "LICENSE"
    pyproject_path = repository_root / "pyproject.toml"
    try:
        if license_path.is_symlink() or not license_path.is_file():
            raise ValueError("LICENSE must be a regular repository file")
        license_bytes = license_path.read_bytes()
        if hashlib.sha256(license_bytes).hexdigest() != APACHE_LICENSE_SHA256:
            raise ValueError("LICENSE is not the canonical Apache License 2.0 file")
        license_text = license_bytes.decode("utf-8")
        if (
            "Apache License\n                           Version 2.0, January 2004"
            not in license_text
            or "http://www.apache.org/licenses/" not in license_text
            or "END OF TERMS AND CONDITIONS" not in license_text
            or "APPENDIX: How to apply the Apache License to your work."
            not in license_text
        ):
            raise ValueError("LICENSE is not the canonical Apache License 2.0 text")
        with pyproject_path.open("rb") as source:
            pyproject = tomllib.load(source)
        project = pyproject.get("project")
        if not isinstance(project, dict):
            raise ValueError("pyproject project metadata is unavailable")
        if project.get("license") != APACHE_LICENSE_EXPRESSION:
            raise ValueError("project license expression must be Apache-2.0")
        license_files = project.get("license-files")
        if not isinstance(license_files, list) or "LICENSE" not in license_files:
            raise ValueError("project license-files must include LICENSE")
        project_urls = project.get("urls")
        if not isinstance(project_urls, dict):
            raise ValueError("project URLs are unavailable")
        if project_urls.get("Repository") != REPOSITORY_URL:
            raise ValueError("project Repository URL is not canonical")
    except (
        OSError,
        UnicodeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as error:
        return Check("license-artifact", "FAIL", str(error))
    return Check(
        "license-artifact",
        "PASS",
        "canonical Apache-2.0 artifact and package metadata are present",
    )


def _normalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _canonical_project_requirement(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ValueError("project dependency metadata is invalid")
    match = _PACKAGE_NAME_RE.match(value)
    if match is None:
        raise ValueError("project dependency metadata is invalid")
    name = _normalize_package_name(match.group())
    suffix = value[match.end() :]
    if any(character.isspace() for character in suffix) or ";" in suffix:
        raise ValueError("project dependency syntax requires lock-check support")
    if suffix and suffix[0] not in "[<>=!~":
        raise ValueError("project dependency metadata is invalid")
    return name + suffix


def _locked_requirement(value: object) -> tuple[str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("locked project dependency metadata is invalid")
    if set(value) - {"name", "specifier", "marker", "extras"}:
        raise ValueError("locked project dependency metadata has unknown fields")
    name = value.get("name")
    if not isinstance(name, str) or _PACKAGE_NAME_RE.fullmatch(name) is None:
        raise ValueError("locked project dependency name is invalid")
    requirement = _normalize_package_name(name)
    extras = value.get("extras", [])
    if not isinstance(extras, list) or any(
        not isinstance(extra, str) or _NORMALIZED_EXTRA_RE.fullmatch(extra) is None
        for extra in extras
    ):
        raise ValueError("locked project dependency extras are invalid")
    if extras:
        requirement += "[" + ",".join(extras) + "]"
    specifier = value.get("specifier", "")
    if not isinstance(specifier, str) or any(
        character.isspace() for character in specifier
    ):
        raise ValueError("locked project dependency specifier is invalid")
    requirement += specifier
    marker = value.get("marker")
    if marker is not None and not isinstance(marker, str):
        raise ValueError("locked project dependency marker is invalid")
    return requirement, marker


def _locked_dependency_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("locked dependency inventory is invalid")
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "marker"}:
            raise ValueError("locked dependency inventory is invalid")
        name = item.get("name")
        if not isinstance(name, str) or _PACKAGE_NAME_RE.fullmatch(name) is None:
            raise ValueError("locked dependency inventory is invalid")
        if "marker" in item and not isinstance(item["marker"], str):
            raise ValueError("locked dependency inventory is invalid")
        names.append(_normalize_package_name(name))
    return tuple(sorted(names))


def _requirement_name(requirement: str) -> str:
    match = _PACKAGE_NAME_RE.match(requirement)
    if match is None:
        raise ValueError("project dependency metadata is invalid")
    return _normalize_package_name(match.group())


def _check_python_dependency_lock(repository_root: Path) -> Check:
    pyproject_path = repository_root / "pyproject.toml"
    lock_path = repository_root / "uv.lock"
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            raise ValueError("uv.lock must be a committed regular file")
        with pyproject_path.open("rb") as source:
            pyproject = tomllib.load(source)
        with lock_path.open("rb") as source:
            lock = tomllib.load(source)
        if lock.get("version") != UV_LOCK_VERSION:
            raise ValueError("uv.lock version is unsupported")
        if lock.get("revision") != UV_LOCK_REVISION:
            raise ValueError("uv.lock revision is unsupported")
        project = pyproject.get("project")
        build_system = pyproject.get("build-system")
        if not isinstance(project, dict) or not isinstance(build_system, dict):
            raise ValueError("pyproject dependency metadata is unavailable")
        if project.get("requires-python") != PROJECT_REQUIRES_PYTHON:
            raise ValueError("pyproject Python requirement drifted")
        if lock.get("requires-python") != LOCK_REQUIRES_PYTHON:
            raise ValueError("uv.lock Python requirement drifted")
        project_name = project.get("name")
        project_version = project.get("version")
        if project_name != "inferdrome" or not isinstance(project_version, str):
            raise ValueError("pyproject package identity is invalid")
        dependencies = project.get("dependencies")
        optional_dependencies = project.get("optional-dependencies")
        build_requirements = build_system.get("requires")
        if not isinstance(dependencies, list) or not isinstance(
            optional_dependencies, dict
        ):
            raise ValueError("pyproject dependency metadata is invalid")
        if not isinstance(build_requirements, list):
            raise ValueError("pyproject build requirements are invalid")
        expected_requirements = [
            (_canonical_project_requirement(requirement), None)
            for requirement in dependencies
        ]
        expected_optional_names: dict[str, tuple[str, ...]] = {}
        for extra, requirements in optional_dependencies.items():
            if (
                not isinstance(extra, str)
                or _NORMALIZED_EXTRA_RE.fullmatch(extra) is None
                or not isinstance(requirements, list)
            ):
                raise ValueError("pyproject optional dependency metadata is invalid")
            canonical_requirements = tuple(
                _canonical_project_requirement(requirement)
                for requirement in requirements
            )
            expected_requirements.extend(
                (requirement, f"extra == '{extra}'")
                for requirement in canonical_requirements
            )
            expected_optional_names[extra] = tuple(
                sorted(
                    _requirement_name(requirement)
                    for requirement in canonical_requirements
                )
            )
        dev_requirements = {
            requirement
            for requirement, marker in expected_requirements
            if marker == "extra == 'dev'"
        }
        for requirement in build_requirements:
            if _canonical_project_requirement(requirement) not in dev_requirements:
                raise ValueError(
                    "build requirements must be represented in the locked dev extra"
                )
        packages = lock.get("package")
        if not isinstance(packages, list) or not packages:
            raise ValueError("uv.lock package inventory is invalid")
        roots = [
            package
            for package in packages
            if isinstance(package, dict) and package.get("name") == "inferdrome"
        ]
        if len(roots) != 1:
            raise ValueError("uv.lock must contain exactly one Inferdrome root")
        root = roots[0]
        if root.get("version") != project_version or root.get("source") != {
            "editable": "."
        }:
            raise ValueError("uv.lock root identity is stale")
        metadata = root.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("uv.lock root metadata is unavailable")
        actual_requirements = metadata.get("requires-dist")
        if not isinstance(actual_requirements, list):
            raise ValueError("uv.lock direct requirement metadata is unavailable")

        def requirement_sort_key(item: tuple[str, str | None]) -> tuple[str, str]:
            return item[0], item[1] or ""

        if sorted(
            (_locked_requirement(item) for item in actual_requirements),
            key=requirement_sort_key,
        ) != sorted(expected_requirements, key=requirement_sort_key):
            raise ValueError("uv.lock is stale relative to pyproject dependencies")
        expected_extras = tuple(optional_dependencies)
        provides_extras = metadata.get("provides-extras")
        if (
            not isinstance(provides_extras, list)
            or tuple(provides_extras) != expected_extras
        ):
            raise ValueError("uv.lock optional dependency metadata is stale")
        if _locked_dependency_names(root.get("dependencies")) != tuple(
            sorted(_requirement_name(requirement) for requirement in dependencies)
        ):
            raise ValueError("uv.lock root dependency inventory is stale")
        locked_optional = root.get("optional-dependencies")
        if not isinstance(locked_optional, dict) or set(locked_optional) != set(
            expected_extras
        ):
            raise ValueError("uv.lock optional dependency inventory is stale")
        for extra, expected_names in expected_optional_names.items():
            if _locked_dependency_names(locked_optional.get(extra)) != expected_names:
                raise ValueError("uv.lock optional dependency inventory is stale")
        artifact_count = 0
        registry_package_count = 0
        for package in packages:
            if not isinstance(package, dict):
                raise ValueError("uv.lock package inventory is invalid")
            if package is root:
                continue
            source = package.get("source")
            if source != {"registry": PYPI_REGISTRY_URL}:
                raise ValueError(
                    "uv.lock contains an unsupported or unhashed package source"
                )
            registry_package_count += 1
            artifacts: list[object] = []
            if "sdist" in package:
                artifacts.append(package["sdist"])
            wheels = package.get("wheels", [])
            if not isinstance(wheels, list):
                raise ValueError("uv.lock wheel inventory is invalid")
            artifacts.extend(wheels)
            if not artifacts:
                raise ValueError("uv.lock registry package has no hashed artifact")
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    raise ValueError("uv.lock artifact metadata is invalid")
                digest = artifact.get("hash")
                url = artifact.get("url")
                if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                    raise ValueError("uv.lock contains an unhashed artifact")
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise ValueError("uv.lock artifact URL is not HTTPS")
                artifact_count += 1
    except (
        OSError,
        UnicodeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as error:
        return Check("python-dependency-lock", "FAIL", str(error))
    return Check(
        "python-dependency-lock",
        "PASS",
        f"uv.lock matches project metadata; {registry_package_count} registry "
        f"packages expose {artifact_count} SHA-256-bound artifacts",
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
        if tag_state == "unannotated":
            return Check("release-tag", "FAIL", tag_detail)
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
    if tag_state == "unannotated":
        return Check("release-tag", "FAIL", tag_detail)
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

    required_jobs = ("engineering", "deployment-qualification", "dashboard")
    missing = tuple(job for job in required_jobs if f"  {job}:" not in workflow)
    if missing:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow is missing jobs: " + ", ".join(missing),
        )
    if "pull_request_target:" in workflow:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow must not use pull_request_target",
        )
    permission_blocks = re.findall(r"(?m)^\s*permissions:\s*$", workflow)
    if (
        len(permission_blocks) != 1
        or re.search(r"(?m)^permissions:\n  contents: read\s*$", workflow) is None
        or re.search(r"(?m)^\s*[^#\s][^:\n]*:\s*write\s*$", workflow) is not None
    ):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow permissions must remain contents: read",
        )
    for match in re.finditer(r"(?m)^\s*uses:\s*([^\s#]+)", workflow):
        invocation = match.group(1)
        if invocation.startswith("./"):
            continue
        if "@" not in invocation:
            return Check(
                "ci-gate-inventory",
                "FAIL",
                f"workflow action {invocation} is not pinned to a full commit SHA",
            )
        action, reference = invocation.rsplit("@", maxsplit=1)
        if _FULL_ACTION_SHA_RE.fullmatch(reference) is None:
            return Check(
                "ci-gate-inventory",
                "FAIL",
                f"workflow action {action} is not pinned to a full commit SHA",
            )
    if _PIP_INSTALL_COMMAND_RE.search(workflow) is not None:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow must not install Python dependencies through pip",
        )
    if "--editable" in workflow or "cache-dependency-path: pyproject.toml" in workflow:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow must not replace the locked Python environment",
        )
    if _uv_sync_command_lines(workflow):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow must not run uv sync outside the frozen CI bootstrap",
        )
    if _uv_lock_command_lines(workflow):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "workflow must not run uv lock outside the checked CI bootstrap",
        )

    job_bodies: dict[str, str] = {}
    for index, job in enumerate(required_jobs):
        body = workflow.split(f"  {job}:", maxsplit=1)[1]
        if index + 1 < len(required_jobs):
            body = body.split(f"  {required_jobs[index + 1]}:", maxsplit=1)[0]
        job_bodies[job] = body
    expected_bootstrap = {
        "engineering": "./scripts/bootstrap_ci_uv.sh --extra dev",
        "deployment-qualification": "./scripts/bootstrap_ci_uv.sh --extra dev",
        "dashboard": ("./scripts/bootstrap_ci_uv.sh --extra dev --extra dashboard"),
    }
    for job, body in job_bodies.items():
        required_fragments = (
            "cache-dependency-path: uv.lock",
            expected_bootstrap[job],
            ".venv/bin/python",
            *(
                ("INFERDROME_UV: .venv/bin/uv",)
                if job == "dashboard"
                else ()
            ),
        )
        missing_fragments = tuple(
            fragment for fragment in required_fragments if fragment not in body
        )
        if missing_fragments:
            return Check(
                "ci-gate-inventory",
                "FAIL",
                f"{job} job is missing locked Python controls: "
                + ", ".join(missing_fragments),
            )
        if body.index(expected_bootstrap[job]) > body.index(".venv/bin/python"):
            return Check(
                "ci-gate-inventory",
                "FAIL",
                f"{job} job does not sync before using the locked environment",
            )
    try:
        bootstrap = (repository_root / "scripts/bootstrap_ci_uv.sh").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as error:
        return Check(
            "ci-gate-inventory", "FAIL", f"could not read uv bootstrap: {error}"
        )
    if _PIP_INSTALL_COMMAND_RE.search(bootstrap) is not None:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "CI uv bootstrap must not install Python dependencies through pip",
        )
    expected_lock = '"$uv_bin" lock --check'
    if _uv_lock_command_lines(bootstrap) != (expected_lock,):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "CI uv bootstrap must contain exactly one uv lock --check command",
        )
    expected_sync = '"$uv_bin" sync --frozen --no-install-project "$@"'
    if _uv_sync_command_lines(bootstrap) != (expected_sync,):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "CI uv bootstrap must contain exactly one frozen uv sync command",
        )
    bootstrap_markers = (
        'readonly uv_version="0.8.17"',
        'readonly uv_archive_sha256="920cbcaad514cc185634f6f0dcd71df5e8f4ee4456d'
        '440a22e0f8c0f142a8203"',
        "https://github.com/astral-sh/uv/releases/download/${uv_version}/",
        "sha256sum --check --strict",
        'export UV_CACHE_DIR="${pip_cache_root%/}/inferdrome-uv"',
        '[[ "$("$uv_bin" --version)" == "uv $uv_version" ]]',
        expected_lock,
        expected_sync,
        'locked_python=".venv/bin/python"',
        'if "$locked_python" -m pip --version >/dev/null 2>&1; then',
        'locked_uv=".venv/bin/uv"',
        'cmp -s -- "$uv_bin" "$locked_uv"',
        'install -m 0755 -- "$uv_bin" "$locked_uv"',
        '[[ "$("$locked_uv" --version)" == "uv $uv_version" ]]',
    )
    if any(marker not in bootstrap for marker in bootstrap_markers):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "CI uv bootstrap is not exact-version and checksum pinned",
        )
    if bootstrap.index(expected_lock) > bootstrap.index(expected_sync):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "CI uv bootstrap must check lock freshness before frozen sync",
        )
    for marker in bootstrap_markers[-6:]:
        if bootstrap.index(marker) < bootstrap.index(expected_sync):
            return Check(
                "ci-gate-inventory",
                "FAIL",
                "CI must establish its pip-less uv handoff after frozen sync",
            )

    try:
        dashboard_gate = (repository_root / "scripts/dashboard_gate.sh").read_text(
            encoding="utf-8"
        )
        package_gate = (
            repository_root / "scripts/dashboard_package_gate.sh"
        ).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            f"could not read dashboard packaging gates: {error}",
        )
    package_gate_invocation = (
        '"$repository_root/scripts/dashboard_package_gate.sh"'
    )
    if dashboard_gate.count(package_gate_invocation) != 1:
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "dashboard gate must invoke the uv-native package gate exactly once",
        )
    if (
        _DIRECT_PIP_COMMAND_RE.search(dashboard_gate) is not None
        or _DIRECT_PIP_COMMAND_RE.search(package_gate) is not None
    ):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "dashboard gates must not invoke a pip executable or Python pip module",
        )
    package_lines = package_gate.splitlines()
    required_package_lines = (
        ('"$inferdrome_uv" build \\', 2),
        ('"$inferdrome_uv" pip install \\', 1),
        ("  --offline \\", 3),
        ("  --no-python-downloads \\", 3),
        ('  --cache-dir "$uv_cache_root" \\', 3),
        ("  --sdist \\", 1),
        ("  --wheel \\", 1),
        ("  --no-build-isolation \\", 2),
        ('  --python "$inferdrome_python" \\', 3),
        ('  --out-dir "$source_dist_root" \\', 1),
        ('  --out-dir "$wheel_dist_root" \\', 1),
        ("  --no-index \\", 1),
        ("  --no-deps \\", 1),
        ("  --no-build \\", 1),
        ("  --link-mode copy \\", 1),
        ('  --target "$install_root" \\', 1),
    )
    if (
        any(
            package_lines.count(line) != expected_count
            for line, expected_count in required_package_lines
        )
        or len(re.findall(r"\bpip\s+install\b", package_gate, re.IGNORECASE))
        != 1
        or 'case "$uv_reported_version" in' not in package_gate
        or '"uv 0.8.17" | "uv 0.8.17 ("*")")' not in package_gate
        or '"$repository_root/scripts/verify_dashboard_install.py"' not in package_gate
    ):
        return Check(
            "ci-gate-inventory",
            "FAIL",
            "dashboard package gate is not exact-version, offline, and uv-native",
        )
    return Check(
        "ci-gate-inventory",
        "PASS",
        "all three jobs use full-SHA actions and one lock-keyed, pip-less uv "
        "environment",
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


def _run_preflight_for_phase(
    repository_root: Path,
    *,
    resolved_phase: Phase,
    phase_selection: Check | None,
    repository_only: bool,
    require_clean: bool,
    run_gates: bool,
) -> tuple[Check, ...]:
    """Return deterministic checks after phase selection."""

    checks = [
        _check_required_files(repository_root),
    ]
    if phase_selection is not None:
        checks.append(phase_selection)
    checks.extend(
        (
            _check_version(repository_root, phase=resolved_phase),
            _check_license_artifact(repository_root),
            _check_python_dependency_lock(repository_root),
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
        checks.append(_check_capture_ancestor(repository_root))
        checks.append(_check_tag(repository_root, phase=resolved_phase, tag=FINAL_TAG))
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
    return _run_preflight_for_phase(
        repository_root,
        resolved_phase=resolved_phase,
        phase_selection=phase_selection,
        repository_only=repository_only,
        require_clean=require_clean,
        run_gates=run_gates,
    )


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
                "PRE_TAG_READY (machine preflight only; tagging still requires "
                "externally verified three-job aggregate CI plus explicit owner "
                "authorization)"
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
    phase, phase_selection = _resolve_phase(repository_root, requested_phase)
    repository_only = bool(arguments.repository_only)
    require_clean = not bool(arguments.allow_dirty)
    if repository_only and not arguments.require_clean and not arguments.allow_dirty:
        require_clean = False
    checks = _run_preflight_for_phase(
        repository_root,
        resolved_phase=phase,
        phase_selection=phase_selection,
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
