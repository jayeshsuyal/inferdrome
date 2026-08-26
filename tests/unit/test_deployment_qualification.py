"""Injected-behavior coverage for the local Compose qualification boundary."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from inferdrome.deployment import (
    QUALIFICATION_CLEANUP_ACTION,
    ProcessResult,
    QualificationError,
    canonical_qualification_bytes,
    expected_compose_output_bytes,
    main,
    qualification_schema,
    qualify_compose_mock,
    verify_qualification_report_bytes,
)
from inferdrome.deployment.qualification import _publish_report
from inferdrome.domain.ids import sha256_digest
from inferdrome.execution.cancellation import CancellationReason, CancellationToken

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCHEMA = (
    REPOSITORY_ROOT
    / "schemas"
    / "deployment"
    / "v1"
    / "deployment-qualification.schema.json"
)
SOURCE_REVISION = "1cf2714328e3b71127b06a542c7454de75a5f5be"
PROJECT = "inferdrome-qual-test"


class FakeProcessRunner:
    """Small Docker/git double that never evaluates shell text."""

    def __init__(
        self,
        *,
        up_status: int = 0,
        cleanup_statuses: list[int] | None = None,
        output: bytes | None = None,
        context_host: str = "unix:///var/run/docker.sock",
        image_output: bytes | None = None,
        on_up: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self.up_status = up_status
        self.cleanup_statuses = list(cleanup_statuses or [0])
        self.output = output if output is not None else expected_compose_output_bytes()
        self.context_host = context_host
        self.image_output = image_output
        self.on_up = on_up
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ProcessResult:
        del timeout_seconds
        command = tuple(argv)
        self.calls.append(command)
        assert all(";" not in item and "&&" not in item for item in command)
        assert "DOCKER_HOST" not in env
        assert "DOCKER_CONTEXT" not in env
        if command[:5] == ("git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify"):
            return ProcessResult(0, (SOURCE_REVISION + "\n").encode())
        if command[:4] == ("git", "-C", str(REPOSITORY_ROOT), "status"):
            return ProcessResult(0, b"")
        if command[0:3] == ("docker", "context", "inspect"):
            return ProcessResult(0, json.dumps(self.context_host).encode() + b"\n")
        if command[0:3] == ("docker", "compose", "version"):
            return ProcessResult(0, b"2.39.1\n")
        if command[0:2] == ("docker", "compose") and "config" in command:
            return ProcessResult(0)
        if command[0:2] == ("docker", "compose") and "up" in command:
            evidence = Path(env["INFERDROME_COMPOSE_EVIDENCE_DIR"])
            (evidence / "runner-output.json").write_bytes(self.output)
            if self.on_up is not None:
                self.on_up(env)
            return ProcessResult(self.up_status)
        if command[0:3] == ("docker", "image", "inspect"):
            if self.image_output is not None:
                return ProcessResult(0, self.image_output)
            digest = "sha256:" + "a" * 64
            if any("RepoDigests" in item for item in command):
                return ProcessResult(0, b"[]\n")
            return ProcessResult(0, json.dumps(digest).encode() + b"\n")
        if command[0:2] == ("docker", "compose") and "down" in command:
            status = self.cleanup_statuses.pop(0) if self.cleanup_statuses else 0
            return ProcessResult(status)
        if command[0:2] in {
            ("docker", "ps"),
            ("docker", "network"),
            ("docker", "volume"),
        }:
            return ProcessResult(0, b"")
        raise AssertionError(f"unexpected argv: {command}")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    return root


def _cleanup_calls(fake: FakeProcessRunner) -> list[tuple[str, ...]]:
    return [call for call in fake.calls if "down" in call]


def test_schema_is_current_closed_and_additive() -> None:
    committed = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(committed)
    assert committed == qualification_schema()
    assert "schemas/public/v1" not in json.dumps(committed)

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    assert_closed(committed)


def test_success_binds_exact_inputs_output_images_and_scoped_cleanup(
    tmp_path: Path,
) -> None:
    fake = FakeProcessRunner()
    execution = qualify_compose_mock(
        output_root=_root(tmp_path),
        runner=fake,
        project_name=PROJECT,
        uid=os.getuid(),
        gid=os.getgid(),
        source_revision=SOURCE_REVISION,
    )

    report = execution.report
    assert report.source_revision == SOURCE_REVISION
    assert report.qualification_mode == "SYNTHETIC_ONLY"
    assert report.synthetic_only is True
    assert report.evidence_eligible is False
    assert report.provider_execution == "NOT_PERFORMED"
    assert report.gpu_execution == "NOT_PERFORMED"
    assert report.deployment_receipt_issued is False
    assert report.evidence_published is False
    assert report.output_observation.sha256 == sha256_digest(
        expected_compose_output_bytes()
    )
    assert report.cleanup.action == QUALIFICATION_CLEANUP_ACTION
    assert report.cleanup.project_name == PROJECT
    assert report.cleanup.residual_container_count == 0
    assert report.cleanup.residual_network_count == 0
    assert report.cleanup.residual_volume_count == 0
    assert all(
        item.observation_status == "OBSERVED" for item in report.image_observations
    )
    assert "inferdrome/compose-mock:development" not in canonical_qualification_bytes(
        report
    ).decode()
    verify_qualification_report_bytes(
        (execution.report_path / "qualification.json").read_bytes(),
        expected_project=PROJECT,
        expected_source_revision=SOURCE_REVISION,
        expected_deployment_spec_digest=report.deployment_spec_digest,
        expected_compose_file_sha256=report.compose_file_sha256,
        expected_compose_contract_sha256=report.compose_contract_sha256,
        expected_runtime_contract_sha256=report.runtime_contract_sha256,
        expected_output_sha256=report.output_observation.sha256,
    )

    down = _cleanup_calls(fake)
    assert len(down) == 1
    assert down[0] == (
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(REPOSITORY_ROOT / "compose.yaml"),
        "down",
        "--remove-orphans",
        "--volumes",
    )
    residual_queries = [
        call
        for call in fake.calls
        if call[:2] in {
            ("docker", "ps"),
            ("docker", "network"),
            ("docker", "volume"),
        }
    ]
    assert len(residual_queries) == 6
    assert all(
        call[-2:] == ("--filter", f"label=com.docker.compose.project={PROJECT}")
        for call in residual_queries
    )


def test_cli_requires_explicit_synthetic_confirmation(tmp_path: Path, capsys) -> None:
    report_root = _root(tmp_path)
    assert main(["--output-root", str(report_root)]) == 2
    assert "--confirm-synthetic-compose" in capsys.readouterr().err


def test_malformed_output_is_rejected_and_cleanup_still_runs(tmp_path: Path) -> None:
    fake = FakeProcessRunner(output=b'{"synthetic_only":false}')
    report_root = _root(tmp_path)
    with pytest.raises(QualificationError) as error:
        qualify_compose_mock(
            output_root=report_root,
            runner=fake,
            project_name=PROJECT,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    assert error.value.code == "OUTPUT_INVALID"
    assert len(_cleanup_calls(fake)) == 1
    assert list(report_root.iterdir()) == []


def test_cleanup_failure_dominates_compose_failure_and_retries(tmp_path: Path) -> None:
    fake = FakeProcessRunner(up_status=17, cleanup_statuses=[17, 18])
    report_root = _root(tmp_path)
    with pytest.raises(QualificationError) as error:
        qualify_compose_mock(
            output_root=report_root,
            runner=fake,
            project_name=PROJECT,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    assert error.value.code == "CLEANUP_FAILED"
    assert len(_cleanup_calls(fake)) == 2
    assert not list(report_root.iterdir())


def test_cancellation_after_up_is_cleaned_and_not_published(tmp_path: Path) -> None:
    token = CancellationToken()
    report_root = _root(tmp_path)

    def cancel(_env: dict[str, str]) -> None:
        token.request(CancellationReason.USER)

    fake = FakeProcessRunner(on_up=cancel)
    with pytest.raises(QualificationError) as error:
        qualify_compose_mock(
            output_root=report_root,
            runner=fake,
            project_name=PROJECT,
            uid=os.getuid(),
            gid=os.getgid(),
            cancellation=token,
        )
    assert error.value.code == "CANCELLED"
    assert len(_cleanup_calls(fake)) == 1
    assert not list(report_root.iterdir())


def test_project_collision_is_rejected_without_cleanup(tmp_path: Path) -> None:
    fake = FakeProcessRunner()
    original = fake.run

    def collision(
        argv: list[str] | tuple[str, ...],
        *,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ProcessResult:
        command = tuple(argv)
        if command[:2] == ("docker", "ps"):
            return ProcessResult(0, ("a" * 64 + "\n").encode())
        if command[:3] == ("docker", "container", "inspect"):
            return ProcessResult(
                0,
                b'{"com.docker.compose.project":"inferdrome-qual-test"}\n',
            )
        return original(argv, env=env, timeout_seconds=timeout_seconds)

    fake.run = collision  # type: ignore[method-assign]
    with pytest.raises(QualificationError) as error:
        qualify_compose_mock(
            output_root=_root(tmp_path),
            runner=fake,
            project_name=PROJECT,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    assert error.value.code == "PROJECT_NOT_UNIQUE"
    assert not _cleanup_calls(fake)


def test_remote_docker_context_is_rejected_before_workflow(tmp_path: Path) -> None:
    fake = FakeProcessRunner(context_host="ssh://docker.example.invalid")
    with pytest.raises(QualificationError) as error:
        qualify_compose_mock(
            output_root=_root(tmp_path),
            runner=fake,
            project_name=PROJECT,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    assert error.value.code == "REMOTE_DOCKER_DISALLOWED"
    assert not any("up" in call for call in fake.calls)


def test_canonical_parser_rejects_duplicate_unknown_and_reordered_report_fields(
    tmp_path: Path,
) -> None:
    execution = qualify_compose_mock(
        output_root=_root(tmp_path),
        runner=FakeProcessRunner(),
        project_name=PROJECT,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    raw = canonical_qualification_bytes(execution.report)
    value = json.loads(raw)
    reordered = json.dumps(
        {key: value[key] for key in reversed(tuple(value))},
        separators=(",", ":"),
    ).encode()
    with pytest.raises(ValueError, match="canonical"):
        verify_qualification_report_bytes(reordered)
    duplicate = raw.replace(
        b'"schema_version":"inferdrome.deployment-qualification.v1"',
        b'"schema_version":"inferdrome.deployment-qualification.v1",'
        b'"schema_version":"inferdrome.deployment-qualification.v1"',
        1,
    )
    with pytest.raises(ValueError, match="keys must be unique"):
        verify_qualification_report_bytes(duplicate)
    value["unexpected"] = True
    with pytest.raises(ValueError):
        verify_qualification_report_bytes(json.dumps(value).encode())


def test_path_and_no_replace_publication_attacks_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(QualificationError) as error:
        qualify_compose_mock(
            output_root=link,
            runner=FakeProcessRunner(),
            project_name=PROJECT,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    assert error.value.code == "PATH_INVALID"


def test_publication_never_replaces_an_existing_report(tmp_path: Path) -> None:
    report_root = _root(tmp_path)
    execution = qualify_compose_mock(
        output_root=report_root,
        runner=FakeProcessRunner(),
        project_name=PROJECT,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    report_path = execution.report_path / "qualification.json"
    original = report_path.read_bytes()
    with pytest.raises(QualificationError) as error:
        _publish_report(report_root, execution.report)
    assert error.value.code == "REPORT_PUBLICATION_FAILED"
    assert report_path.read_bytes() == original
