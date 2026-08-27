"""Tests for the offline, fail-closed release preflight."""

from __future__ import annotations

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
    (root / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0.dev0"\n',
        encoding="utf-8",
    )
    (root / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0.dev0"\n',
        encoding="utf-8",
    )
    for relative_path, markers in release_preflight.DOCUMENT_MARKERS:
        (root / relative_path).write_text("\n".join(markers), encoding="utf-8")
    (root / ".github/workflows/ci.yml").write_text(
        "  engineering:\n"
        "  deployment-qualification:\n"
        "  dashboard:\n",
        encoding="utf-8",
    )
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


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_engineering_ci_preflight_resolves_phase_and_fetches_tags() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    engineering_job = workflow.split("  deployment-qualification:", maxsplit=1)[0]

    assert "fetch-depth: 0" in engineering_job
    assert "inputs.release_phase || 'auto'" in engineering_job
    assert '--phase "$RELEASE_PREFLIGHT_PHASE"' in engineering_job


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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    checks = release_preflight.run_preflight(
        tmp_path,
        phase="candidate",
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    version_check = next(
        check for check in checks if check.name == "package-version"
    )
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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")

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
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "inferdrome"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        f'__version__ = "{package_version}"\n',
        encoding="utf-8",
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
        check.status != "FAIL"
        for check in checks
        if check.name != "working-tree"
    )


def test_final_pre_tag_requires_final_version_and_license_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _minimal_repository(tmp_path)
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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")

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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")
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

    release_tag_check = next(
        check for check in checks if check.name == "release-tag"
    )
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
    assert "result: PRE_TAG_READY" in capsys.readouterr().out


def test_post_tag_auto_selects_tag_at_head(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")
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


def test_post_tag_auto_rejects_tag_elsewhere(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")
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

    assert "post-tag record commit" not in checklist
    assert "post-tag repository commit" not in checklist


def test_post_tag_requires_tag_to_point_to_head(tmp_path: Path, monkeypatch) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:4] == ["git", "show-ref", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "inferdrome"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/inferdrome/__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("owner-selected license text\n", encoding="utf-8")
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
