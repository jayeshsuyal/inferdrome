"""Tests for the offline, fail-closed release preflight."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from scripts import release_preflight

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _minimal_repository(root: Path) -> None:
    for relative_path in release_preflight.REQUIRED_FILES:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    for relative_path in (
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
        "scripts/bootstrap_ci_uv.sh",
        "scripts/dashboard_gate.sh",
        "scripts/dashboard_package_gate.sh",
    ):
        (root / relative_path).write_bytes(
            (REPOSITORY_ROOT / relative_path).read_bytes()
        )
    (root / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0.dev0"\n',
        encoding="utf-8",
    )
    for relative_path, markers in release_preflight.DOCUMENT_MARKERS:
        (root / relative_path).write_text("\n".join(markers), encoding="utf-8")
    checklist = root / "docs/V0_1_RELEASE_CHECKLIST.md"
    checklist.write_text(
        "\n".join(
            [
                *release_preflight.DOCUMENT_MARKERS[3][1],
                *(
                    f"- [ ] {item.marker}"
                    for item in release_preflight.MANUAL_RELEASE_ITEMS
                ),
            ]
        ),
        encoding="utf-8",
    )


def _set_package_versions(
    root: Path,
    *,
    project_version: str,
    package_version: str,
) -> None:
    pyproject_path = root / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    pyproject, count = re.subn(
        r'(?m)^version = "[^"]+"$',
        f'version = "{project_version}"',
        pyproject,
        count=1,
    )
    assert count == 1
    pyproject_path.write_text(pyproject, encoding="utf-8")

    package_path = root / "src/inferdrome/__init__.py"
    package_path.write_text(
        f'__version__ = "{package_version}"\n',
        encoding="utf-8",
    )

    lock_path = root / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    lock, count = re.subn(
        r'(\[\[package\]\]\nname = "inferdrome"\nversion = ")[^"]+',
        rf"\g<1>{project_version}",
        lock,
        count=1,
    )
    assert count == 1
    lock_path.write_text(lock, encoding="utf-8")


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _named_check(
    checks: tuple[release_preflight.Check, ...],
    name: str,
) -> release_preflight.Check:
    return next(check for check in checks if check.name == name)


def test_engineering_ci_preflight_resolves_phase_and_fetches_tags() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    engineering_job = workflow.split("  deployment-qualification:", maxsplit=1)[0]

    assert "fetch-depth: 0" in engineering_job
    assert "inputs.release_phase || 'auto'" in engineering_job
    assert '--phase "$RELEASE_PREFLIGHT_PHASE"' in engineering_job


def test_python_ci_uses_the_hash_locked_frozen_environment() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    bootstrap = (REPOSITORY_ROOT / "scripts/bootstrap_ci_uv.sh").read_text(
        encoding="utf-8"
    )
    dashboard_gate = (REPOSITORY_ROOT / "scripts/dashboard_gate.sh").read_text(
        encoding="utf-8"
    )
    package_gate = (
        REPOSITORY_ROOT / "scripts/dashboard_package_gate.sh"
    ).read_text(encoding="utf-8")

    assert workflow.count("cache-dependency-path: uv.lock") == 3
    assert workflow.count("./scripts/bootstrap_ci_uv.sh --extra dev") == 3
    assert "pip install" not in workflow
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", reference)
        for reference in re.findall(r"(?m)^\s*uses:\s*[^@\s]+@([^\s#]+)", workflow)
    )
    assert '"$uv_bin" lock --check' in bootstrap
    assert '"$uv_bin" sync --frozen --no-install-project "$@"' in bootstrap
    assert 'if "$locked_python" -m pip --version' in bootstrap
    assert 'install -m 0755 -- "$uv_bin" "$locked_uv"' in bootstrap
    assert "INFERDROME_UV: .venv/bin/uv" in workflow
    assert '"$repository_root/scripts/dashboard_package_gate.sh"' in dashboard_gate
    assert '"$inferdrome_python" -m pip' not in dashboard_gate
    assert package_gate.count('"$inferdrome_uv" build \\') == 2
    assert package_gate.count('"$inferdrome_uv" pip install \\') == 1
    assert '"$inferdrome_python" -m pip' not in package_gate
    assert release_preflight._check_ci_gate_inventory(REPOSITORY_ROOT).status == "PASS"


def test_missing_uv_lock_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "uv.lock").unlink()

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="candidate",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    assert _named_check(checks, "required-files").status == "FAIL"
    lock_check = _named_check(checks, "python-dependency-lock")
    assert lock_check.status == "FAIL"
    assert "uv.lock" in lock_check.detail


def test_pyproject_dependency_drift_fails_lock_check(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            '"jsonschema>=4.23,<5",',
            '"jsonschema>=4.24,<5",',
            1,
        ),
        encoding="utf-8",
    )

    check = release_preflight._check_python_dependency_lock(tmp_path)

    assert check.status == "FAIL"
    assert "stale relative to pyproject" in check.detail


def test_uv_lock_metadata_drift_fails_lock_check(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            'name = "inferdrome"\nversion = "0.1.0.dev0"',
            'name = "inferdrome"\nversion = "0.1.0"',
            1,
        ),
        encoding="utf-8",
    )

    check = release_preflight._check_python_dependency_lock(tmp_path)

    assert check.status == "FAIL"
    assert "root identity is stale" in check.detail


def test_unhashed_uv_artifact_fails_lock_check(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            'hash = "sha256:',
            'hash = "sha256:invalid-',
            1,
        ),
        encoding="utf-8",
    )

    check = release_preflight._check_python_dependency_lock(tmp_path)

    assert check.status == "FAIL"
    assert "unhashed artifact" in check.detail


def test_non_registry_uv_source_fails_lock_check(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(
            'source = { registry = "https://pypi.org/simple" }',
            'source = { git = "https://example.invalid/repository.git", '
            'rev = "0123456789abcdef0123456789abcdef01234567" }',
            1,
        ),
        encoding="utf-8",
    )

    check = release_preflight._check_python_dependency_lock(tmp_path)

    assert check.status == "FAIL"
    assert "unsupported or unhashed package source" in check.detail


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "expected_detail"),
    (
        (
            ".github/workflows/ci.yml",
            "cache-dependency-path: uv.lock",
            "cache-dependency-path: pyproject.toml",
            "must not replace the locked Python environment",
        ),
        (
            "scripts/bootstrap_ci_uv.sh",
            'sync --frozen --no-install-project "$@"',
            'sync --no-install-project "$@"',
            "exactly one frozen uv sync command",
        ),
        (
            "scripts/bootstrap_ci_uv.sh",
            '"$uv_bin" lock --check',
            '"$uv_bin" lock',
            "exactly one uv lock --check command",
        ),
        (
            ".github/workflows/ci.yml",
            "run: ./scripts/bootstrap_ci_uv.sh --extra dev",
            'run: python -m pip install --editable ".[dev]"',
            "must not install Python dependencies through pip",
        ),
        (
            ".github/workflows/ci.yml",
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/checkout@v7",
            "not pinned to a full commit SHA",
        ),
    ),
)
def test_ci_lock_control_drift_fails_closed(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
    expected_detail: str,
) -> None:
    _minimal_repository(tmp_path)
    path = tmp_path / relative_path
    original = path.read_text(encoding="utf-8")
    assert old in original
    path.write_text(original.replace(old, new, 1), encoding="utf-8")

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert expected_detail in check.detail


def test_workflow_extra_uv_sync_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    workflow_path = tmp_path / ".github/workflows/ci.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8")
        + "\n      - run: uv sync --extra dev\n",
        encoding="utf-8",
    )

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert "must not run uv sync outside" in check.detail


def test_workflow_extra_uv_lock_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    workflow_path = tmp_path / ".github/workflows/ci.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8") + "\n      - run: uv lock\n",
        encoding="utf-8",
    )

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert "must not run uv lock outside" in check.detail


def test_workflow_pip3_install_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    workflow_path = tmp_path / ".github/workflows/ci.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8")
        + '\n      - run: pip3.12 install --editable ".[dev]"\n',
        encoding="utf-8",
    )

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert "must not install Python dependencies through pip" in check.detail


def test_bootstrap_extra_non_frozen_uv_sync_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    bootstrap_path = tmp_path / "scripts/bootstrap_ci_uv.sh"
    bootstrap_path.write_text(
        bootstrap_path.read_text(encoding="utf-8")
        + '\n"$uv_bin" sync --no-install-project "$@"\n',
        encoding="utf-8",
    )

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert "exactly one frozen uv sync command" in check.detail


def test_bootstrap_extra_mutable_uv_lock_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    bootstrap_path = tmp_path / "scripts/bootstrap_ci_uv.sh"
    bootstrap_path.write_text(
        bootstrap_path.read_text(encoding="utf-8") + '\n"$uv_bin" lock\n',
        encoding="utf-8",
    )

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert "exactly one uv lock --check command" in check.detail


def test_bootstrap_pip_install_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    bootstrap_path = tmp_path / "scripts/bootstrap_ci_uv.sh"
    bootstrap_path.write_text(
        bootstrap_path.read_text(encoding="utf-8")
        + '\npython -m pip install --editable ".[dev]"\n',
        encoding="utf-8",
    )

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert "must not install Python dependencies through pip" in check.detail


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "expected_detail"),
    (
        (
            "scripts/bootstrap_ci_uv.sh",
            'install -m 0755 -- "$uv_bin" "$locked_uv"',
            'install -m 0755 -- "$uv_bin" ".venv/bin/other"',
            "exact-version and checksum pinned",
        ),
        (
            "scripts/bootstrap_ci_uv.sh",
            'if "$locked_python" -m pip --version',
            'if "$locked_python" -c "pass"',
            "exact-version and checksum pinned",
        ),
        (
            ".github/workflows/ci.yml",
            "INFERDROME_UV: .venv/bin/uv",
            "INFERDROME_UV: uv",
            "missing locked Python controls",
        ),
        (
            "scripts/dashboard_package_gate.sh",
            '"$inferdrome_uv" pip install \\',
            '"$inferdrome_python" -m pip install \\',
            "must not invoke a pip executable",
        ),
        (
            "scripts/dashboard_package_gate.sh",
            "  --no-build-isolation \\",
            "  --config-setting isolated=true \\",
            "not exact-version, offline, and uv-native",
        ),
        (
            "scripts/dashboard_package_gate.sh",
            "  --no-index \\",
            "  --index https://example.invalid/simple \\",
            "not exact-version, offline, and uv-native",
        ),
    ),
)
def test_ci_pipless_package_contract_drift_fails_closed(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
    expected_detail: str,
) -> None:
    _minimal_repository(tmp_path)
    path = tmp_path / relative_path
    original = path.read_text(encoding="utf-8")
    assert old in original
    path.write_text(original.replace(old, new, 1), encoding="utf-8")

    check = release_preflight._check_ci_gate_inventory(tmp_path)

    assert check.status == "FAIL"
    assert expected_detail in check.detail


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "expected_detail"),
    (
        (
            "LICENSE",
            "Apache License",
            "Modified License",
            "canonical Apache License 2.0 file",
        ),
        (
            "pyproject.toml",
            'license = "Apache-2.0"',
            'license = "MIT"',
            "license expression must be Apache-2.0",
        ),
        (
            "pyproject.toml",
            'license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSES/*.txt"]',
            'license-files = ["THIRD_PARTY_NOTICES.md"]',
            "license-files must include LICENSE",
        ),
        (
            "pyproject.toml",
            'Repository = "https://github.com/jayeshsuyal/inferdrome"',
            'Repository = "https://example.invalid/inferdrome"',
            "Repository URL is not canonical",
        ),
    ),
)
def test_package_license_metadata_fails_closed_on_drift(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
    expected_detail: str,
) -> None:
    _minimal_repository(tmp_path)
    path = tmp_path / relative_path
    original = path.read_text(encoding="utf-8")
    assert old in original
    path.write_text(original.replace(old, new, 1), encoding="utf-8")

    check = release_preflight._check_license_artifact(tmp_path)

    assert check.status == "FAIL"
    assert expected_detail in check.detail


def test_repository_only_preflight_is_deterministic() -> None:
    first = release_preflight.run_preflight(
        REPOSITORY_ROOT,
        phase="candidate",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )
    second = release_preflight.run_preflight(
        REPOSITORY_ROOT,
        phase="candidate",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    assert first == second
    assert all(check.status != "FAIL" for check in first)
    assert any(check.status == "SKIPPED" for check in first)


def test_missing_claim_boundary_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        "Inferdrome produces measurements. ExitSpec owns customer acceptance.",
        encoding="utf-8",
    )

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="candidate",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    claim_check = next(check for check in checks if check.name == "claim-boundaries")
    assert claim_check.status == "FAIL"
    assert "dry-run/reference" in claim_check.detail


def test_development_version_mismatch_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(
        tmp_path,
        project_version="0.1.0",
        package_version="0.1.0.dev0",
    )

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="candidate",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    version_check = next(check for check in checks if check.name == "package-version")
    assert version_check.status == "FAIL"
    assert "0.1.0.dev0" in version_check.detail


def test_auto_phase_selects_candidate_for_exact_development_versions(
    tmp_path: Path,
) -> None:
    _minimal_repository(tmp_path)

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="auto",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    selection_check = next(check for check in checks if check.name == "phase-selection")
    assert selection_check.status == "PASS"
    assert "candidate" in selection_check.detail
    assert (
        next(check for check in checks if check.name == "package-version").status
        == "PASS"
    )


def test_auto_phase_selects_final_pre_tag_for_exact_final_versions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        assert command[:4] == ["git", "show-ref", "--verify", "--quiet"]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(release_preflight.subprocess, "run", fake_run)
    checks = release_preflight.run_preflight(
        tmp_path,
        phase="auto",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    selection_check = next(check for check in checks if check.name == "phase-selection")
    assert selection_check.status == "PASS"
    assert "final-pre-tag" in selection_check.detail
    assert (
        next(check for check in checks if check.name == "package-version").status
        == "PASS"
    )
    assert (
        next(check for check in checks if check.name == "license-artifact").status
        == "PASS"
    )
    assert (
        next(check for check in checks if check.name == "release-tag").status == "PASS"
    )


@pytest.mark.parametrize(
    ("project_version", "package_version"),
    (("0.2.0", "0.2.0"), ("0.1.0", "0.1.0.dev0")),
)
def test_auto_phase_rejects_unknown_or_mismatched_versions(
    tmp_path: Path,
    project_version: str,
    package_version: str,
) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(
        tmp_path,
        project_version=project_version,
        package_version=package_version,
    )

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="auto",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    selection_check = next(check for check in checks if check.name == "phase-selection")
    assert selection_check.status == "FAIL"
    assert "exactly" in selection_check.detail


def test_gate_option_delegates_to_existing_gate_scripts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _minimal_repository(tmp_path)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(release_preflight.subprocess, "run", fake_run)

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="candidate",
        repository_only=True,
        require_clean=True,
        run_gates=True,
    )

    assert commands[0][:3] == ["git", "status", "--porcelain=v1"]
    assert [Path(command[0]).name for command in commands[1:]] == [
        "engineering_gate.sh",
        "dashboard_gate.sh",
    ]
    assert all(
        check.status != "FAIL" for check in checks if check.name != "working-tree"
    )


def test_final_pre_tag_requires_final_version_and_license_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "LICENSE").unlink()
    (tmp_path / "docs/V0_1_RELEASE_CHECKLIST.md").write_text(
        (tmp_path / "docs/V0_1_RELEASE_CHECKLIST.md")
        .read_text(encoding="utf-8")
        .replace(
            "- [ ] Select and add the repository license",
            "- [x] Select and add the repository license",
        ),
        encoding="utf-8",
    )

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(release_preflight.subprocess, "run", fake_run)
    checks = release_preflight.run_preflight(
        tmp_path,
        phase="final-pre-tag",
        repository_only=False,
        require_clean=False,
        run_gates=False,
    )

    version_check = next(check for check in checks if check.name == "package-version")
    license_check = next(check for check in checks if check.name == "license-artifact")
    manual_license = next(
        check for check in checks if check.name == "manual-license-selection"
    )
    assert version_check.status == "FAIL"
    assert license_check.status == "FAIL"
    assert manual_license.status == "MANUAL"


def test_final_pre_tag_can_pass_machine_checks_without_a_tag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        assert command[:4] == ["git", "show-ref", "--verify", "--quiet"]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(release_preflight.subprocess, "run", fake_run)
    checks = release_preflight.run_preflight(
        tmp_path,
        phase="final-pre-tag",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    assert (
        next(check for check in checks if check.name == "package-version").status
        == "PASS"
    )
    assert (
        next(check for check in checks if check.name == "license-artifact").status
        == "PASS"
    )
    assert (
        next(check for check in checks if check.name == "release-tag").status == "PASS"
    )


def test_pre_tag_ready_without_aggregate_ci_or_tag(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")
    checklist = tmp_path / "docs/V0_1_RELEASE_CHECKLIST.md"
    checklist_text = checklist.read_text(encoding="utf-8")
    for item in release_preflight.MANUAL_RELEASE_ITEMS:
        checklist_text = checklist_text.replace(
            f"- [ ] {item.marker}", f"- [x] {item.marker}"
        )
    checklist.write_text(checklist_text, encoding="utf-8")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "status", "--porcelain=v1"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:4] == ["git", "show-ref", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        assert Path(command[0]).name in {"engineering_gate.sh", "dashboard_gate.sh"}
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(release_preflight.subprocess, "run", fake_run)
    checks = release_preflight.run_preflight(
        tmp_path,
        phase="final-pre-tag",
        repository_only=False,
        require_clean=True,
        run_gates=True,
    )

    release_tag_check = next(check for check in checks if check.name == "release-tag")
    aggregate_check = next(check for check in checks if check.name == "aggregate-ci")
    assert release_tag_check.status == "PASS"
    assert aggregate_check.status == "MANUAL"
    assert not release_preflight._has_failure(checks)
    assert not release_preflight._has_pending_manual_input(checks)
    assert not release_preflight._has_skipped_check(checks)

    release_preflight._print_report(
        checks,
        phase="final-pre-tag",
        repository_only=False,
        run_gates=True,
    )
    output = capsys.readouterr().out
    assert "result: PRE_TAG_READY" in output
    assert "machine preflight only" in output
    assert "externally verified three-job aggregate CI" in output
    assert "explicit owner authorization" in output


def test_post_tag_auto_selects_tag_at_head(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "release commit",
    )
    _git(
        tmp_path,
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "tag",
        "-a",
        "v0.1.0",
        "-m",
        "Inferdrome v0.1.0",
    )

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="auto",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )

    selection_check = next(check for check in checks if check.name == "phase-selection")
    tag_check = next(check for check in checks if check.name == "release-tag")
    assert selection_check.status == "PASS"
    assert "post-tag" in selection_check.detail
    assert tag_check.status == "PASS"


def test_post_tag_rejects_lightweight_tag_but_accepts_annotated_tag(
    tmp_path: Path,
) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    commit_arguments = (
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "release commit",
    )
    _git(tmp_path, *commit_arguments)

    _git(tmp_path, "tag", "v0.1.0")
    lightweight_checks = release_preflight.run_preflight(
        tmp_path,
        phase="post-tag",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )
    lightweight_tag_check = next(
        check for check in lightweight_checks if check.name == "release-tag"
    )
    assert lightweight_tag_check.status == "FAIL"
    assert "not an annotated tag" in lightweight_tag_check.detail

    _git(tmp_path, "tag", "--delete", "v0.1.0")
    _git(
        tmp_path,
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "tag",
        "-a",
        "v0.1.0",
        "-m",
        "Inferdrome v0.1.0",
    )
    annotated_checks = release_preflight.run_preflight(
        tmp_path,
        phase="post-tag",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )
    annotated_tag_check = next(
        check for check in annotated_checks if check.name == "release-tag"
    )
    assert annotated_tag_check.status == "PASS"


def test_post_tag_auto_rejects_tag_elsewhere(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    commit_arguments = (
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
    )
    _git(tmp_path, *commit_arguments, "release commit")
    _git(
        tmp_path,
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "tag",
        "-a",
        "v0.1.0",
        "-m",
        "Inferdrome v0.1.0",
    )
    (tmp_path / "README.md").write_text("post-tag commit\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, *commit_arguments, "post-tag commit")

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="auto",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )

    selection_check = next(check for check in checks if check.name == "phase-selection")
    assert selection_check.status == "FAIL"
    assert "next development-cycle" in selection_check.detail


def test_release_docs_do_not_require_a_post_tag_repository_commit() -> None:
    checklist = (REPOSITORY_ROOT / "docs/V0_1_RELEASE_CHECKLIST.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(checklist.split())

    assert "post-tag record commit" not in checklist
    assert "post-tag repository commit" not in checklist
    assert "post-tag verification result must not be added to" in checklist
    assert "already-created annotated tag message" in checklist
    assert "Post-tag verification facts belong in that external record" in normalized
    assert (
        "After all three jobs pass, put the exact release SHA, three CI run URLs, "
        "tag verification, and remaining external sign-off in the GitHub Release "
        "or another explicit external immutable release record."
        in normalized
    )
    assert (
        "tag verification, and remaining external sign-off in the annotated tag message"
        not in normalized
    )
    assert "annotated tag message" in checklist


def test_post_tag_requires_tag_to_point_to_head(tmp_path: Path, monkeypatch) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:4] == ["git", "show-ref", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["git", "cat-file", "-t"]:
            return subprocess.CompletedProcess(command, 0, stdout="tag\n", stderr="")
        if "refs/tags/v0.1.0^{commit}" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="tag-commit\n",
                stderr="",
            )
        assert command[-1] == "HEAD"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="head-commit\n",
            stderr="",
        )

    monkeypatch.setattr(release_preflight.subprocess, "run", fake_run)
    checks = release_preflight.run_preflight(
        tmp_path,
        phase="post-tag",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    tag_check = next(check for check in checks if check.name == "release-tag")
    assert tag_check.status == "FAIL"
    assert "does not point to the checked commit" in tag_check.detail


def test_tag_phases_use_the_actual_repository_tag_namespace(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    _set_package_versions(tmp_path, project_version="0.1.0", package_version="0.1.0")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "release commit",
    )

    pre_tag_checks = release_preflight.run_preflight(
        tmp_path,
        phase="final-pre-tag",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )
    assert (
        next(check for check in pre_tag_checks if check.name == "release-tag").status
        == "PASS"
    )

    _git(
        tmp_path,
        "-c",
        "user.name=Inferdrome Test",
        "-c",
        "user.email=test@example.invalid",
        "tag",
        "-a",
        "v0.1.0",
        "-m",
        "Inferdrome v0.1.0",
    )
    pre_tag_after_tag = release_preflight.run_preflight(
        tmp_path,
        phase="final-pre-tag",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )
    assert (
        next(check for check in pre_tag_after_tag if check.name == "release-tag").status
        == "FAIL"
    )

    post_tag_checks = release_preflight.run_preflight(
        tmp_path,
        phase="post-tag",
        repository_only=True,
        require_clean=True,
        run_gates=False,
    )
    tag_check = next(check for check in post_tag_checks if check.name == "release-tag")
    assert tag_check.status == "PASS"
    assert "v0.1.0 points to HEAD" in tag_check.detail


def test_release_closure_reports_open_manual_inputs(capsys) -> None:
    result = release_preflight.main(["--allow-dirty"])

    captured = capsys.readouterr().out
    assert result == 1
    assert "[SKIPPED] engineering-gate" in captured
    assert "phase: candidate" in captured
    assert "[PENDING] manual-exitspec-outcomes" in captured
    assert "result: BLOCKED" in captured


def test_main_resolves_auto_phase_once(monkeypatch, capsys) -> None:
    original_resolve_phase = release_preflight._resolve_phase
    calls = 0

    def resolve_phase_once(repository_root: Path, requested_phase):
        nonlocal calls
        calls += 1
        return original_resolve_phase(repository_root, requested_phase)

    monkeypatch.setattr(release_preflight, "_resolve_phase", resolve_phase_once)
    result = release_preflight.main(["--repository-only", "--allow-dirty"])

    assert result == 0
    assert calls == 1
    assert "phase: candidate" in capsys.readouterr().out
