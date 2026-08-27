"""Tests for the offline, fail-closed release preflight."""

from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_repository_only_preflight_is_deterministic() -> None:
    first = release_preflight.run_preflight(
        REPOSITORY_ROOT,
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )
    second = release_preflight.run_preflight(
        REPOSITORY_ROOT,
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
        repository_only=True,
        require_clean=False,
        run_gates=False,
    )

    version_check = next(
        check for check in checks if check.name == "development-version"
    )
    assert version_check.status == "FAIL"
    assert "0.1.0.dev0" in version_check.detail


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


def test_release_closure_reports_open_manual_inputs(capsys) -> None:
    result = release_preflight.main(["--allow-dirty"])

    captured = capsys.readouterr().out
    assert result == 1
    assert "[SKIPPED] engineering-gate" in captured
    assert "[PENDING] manual-exitspec-outcomes" in captured
    assert "result: BLOCKED" in captured
