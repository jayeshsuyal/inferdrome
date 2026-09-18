"""CPU-only checks for the bounded, manual-host operator boundary."""

from __future__ import annotations

import asyncio
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from inferdrome.evaluation import load_calibration_operator as operator
from inferdrome.evaluation import load_calibration_rehearsal as rehearsal
from inferdrome.evaluation.cli import main
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.load_calibration_rehearsal import SubprocessResult
from inferdrome.qwen3_campaign import qwen3_expected_snapshot_sha256
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE
from tests.integration.test_load_calibration_rehearsal import _protocol, _recipe_configs


def _write_private(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o400)


def _directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _prepared(
    tmp_path: Path, *, input_name: str = "inputs"
) -> operator.PreparedHostLocalRehearsal:
    inputs = _directory(tmp_path / input_name)
    origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
    recipes = tuple(
        (
            level,
            *_recipe_configs(origins, level_id=level, offered_count=count, seed=seed),
        )
        for level, count, seed in (("load-low", 7, 11), ("load-high", 14, 29))
    )
    protocol_path = inputs / "protocol.json"
    _write_private(protocol_path, _protocol(recipes))
    recipe_paths: list[Path] = []
    for level, calibration, confirmation in recipes:
        path = inputs / f"{level}.json"
        _write_private(
            path,
            canonical_json_bytes(
                {
                    "schema_version": "inferdrome.load-calibration-operator-recipe.v1",
                    "level_id": level,
                    "calibration_config": calibration.model_dump(mode="json"),
                    "confirmation_config": confirmation.model_dump(mode="json"),
                }
            )
            + b"\n",
        )
        recipe_paths.append(path)
    return operator.prepare_host_local_rehearsal(
        protocol_path=protocol_path,
        recipe_paths=recipe_paths,
        runtime="vllm",
    )


def _authorization(
    prepared: operator.PreparedHostLocalRehearsal,
    *,
    model_snapshot: Path,
    docker_config: Path,
    output_root: Path,
    execute_not_after: datetime,
) -> operator.HostLocalRehearsalAuthorization:
    approved = execute_not_after - timedelta(days=3)
    termination = execute_not_after + timedelta(days=1)

    def timestamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    return operator.HostLocalRehearsalAuthorization.model_validate(
        {
            "schema_version": "inferdrome.load-calibration-host-authorization.v1",
            "confirmation": "AUTHORIZE_VAST_TWO_A100_LOAD_CALIBRATION_V1",
            "authorization_id": "host-run-001",
            "approval_record_id": "approval-001",
            "approved_at_utc": timestamp(approved),
            "execute_not_after_utc": timestamp(execute_not_after),
            "source_commit": prepared.preflight.source_commit,
            "runtime": "vllm",
            "serving_image_reference": VLLM_RUNTIME_IMAGE_REFERENCE,
            "model_id": "Qwen/Qwen3-8B",
            "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
            "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
            "protocol_sha256": prepared.preflight.protocol_sha256,
            "recipe_sha256s": prepared.recipe_sha256s,
            "model_snapshot_path_sha256": operator._path_sha256(model_snapshot),
            "docker_config_directory_sha256": operator._path_sha256(docker_config),
            "output_root_sha256": operator._path_sha256(output_root),
            "ownership_id": "host-run-001",
            "maximum_runtime_seconds": 86_400,
            "termination_safety_margin_seconds": 60,
            "external_guardian": {
                "provider": "VAST_MANUAL_HOST",
                "instance_alias": "vast-run-001",
                "instance_identity_sha256": "sha256:" + "1" * 64,
                "guardian_alias": "guardian-001",
                "guardian_handoff_sha256": "sha256:" + "2" * 64,
                "termination_deadline_utc": timestamp(termination),
                "state": "EXTERNALLY_ARMED_NOT_VERIFIED",
            },
        }
    )


def _operator_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    snapshot = _directory(tmp_path / "model-snapshot")
    docker_config = _directory(tmp_path / "docker-config")
    _write_private(docker_config / "config.json", b"{}")
    output = _directory(tmp_path / "output")
    return snapshot, docker_config, output


def test_preflight_is_deterministic_and_never_constructs_a_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_runtime(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("preflight constructed a runtime")

    monkeypatch.setattr(
        operator, "TwoEngineVllmSubprocessLifecycle", unexpected_runtime
    )
    first = _prepared(tmp_path)
    second = _prepared(tmp_path, input_name="inputs-second")
    assert operator.preflight_bytes(first.preflight) == operator.preflight_bytes(
        second.preflight
    )
    assert first.preflight.provider_action_performed is False
    assert first.preflight.evidence_eligible is False


def test_cli_preflight_is_offline_and_writes_the_canonical_packet(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    output = _directory(tmp_path / "preflight-output") / "preflight.json"
    assert (
        main(
            [
                "load-calibration-host-preflight",
                "--protocol",
                str(tmp_path / "inputs" / "protocol.json"),
                "--recipe",
                str(tmp_path / "inputs" / "load-low.json"),
                "--recipe",
                str(tmp_path / "inputs" / "load-high.json"),
                "--runtime",
                "vllm",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_bytes() == operator.preflight_bytes(prepared.preflight)


def test_expiry_during_authorization_blocks_runtime_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path)
    snapshot, docker_config, output = _operator_paths(tmp_path)
    initial = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _authorization(
        prepared,
        model_snapshot=snapshot,
        docker_config=docker_config,
        output_root=output,
        execute_not_after=initial + timedelta(days=2),
    )
    current = initial
    original_authorized = operator._authorized

    def advance_during_authorization(*args: object, **kwargs: object) -> None:
        nonlocal current
        original_authorized(*args, **kwargs)
        current = initial + timedelta(days=3)

    def unexpected_runtime(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("expired packet reached local Docker construction")

    monkeypatch.setattr(
        operator, "TwoEngineVllmSubprocessLifecycle", unexpected_runtime
    )
    monkeypatch.setattr(operator, "_authorized", advance_during_authorization)
    with pytest.raises(EvaluationError, match="expired before dispatch"):
        asyncio.run(
            operator.run_authorized_host_local_rehearsal(
                prepared,
                authorization=authorization,
                model_snapshot_path=snapshot,
                docker_config_directory=docker_config,
                output_root=output,
                utc_clock=lambda: current,
                monotonic_clock=iter((10, 11)).__next__,
            )
        )
    assert list(output.iterdir()) == []


def test_pinned_docker_runner_records_exact_constrained_argv(tmp_path: Path) -> None:
    _, docker_config, _ = _operator_paths(tmp_path)
    seen: list[tuple[str, ...]] = []

    class FakeRunner:
        async def run(
            self, argv: tuple[str, ...], *, timeout_ns: int
        ) -> SubprocessResult:
            assert timeout_ns == 123
            seen.append(argv)
            return SubprocessResult(argv=argv, returncode=0, stdout=b"", stderr=b"")

    runner = operator.PinnedLocalDockerRunner(docker_config, runner=FakeRunner())
    result = asyncio.run(runner.run(("docker", "ps", "--quiet"), timeout_ns=123))
    assert result.argv == ("docker", "ps", "--quiet")
    assert seen == [
        (
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "--config",
            str(docker_config),
            "ps",
            "--quiet",
        )
    ]
    assert len(runner.receipts) == 1
    assert runner.receipts[0].state == "RETURNED"


def test_pinned_docker_runner_records_a_runner_error(tmp_path: Path) -> None:
    _, docker_config, _ = _operator_paths(tmp_path)

    class FailingRunner:
        async def run(
            self, argv: tuple[str, ...], *, timeout_ns: int
        ) -> SubprocessResult:
            del argv, timeout_ns
            raise EvaluationError("fake command interruption")

    runner = operator.PinnedLocalDockerRunner(docker_config, runner=FailingRunner())
    with pytest.raises(EvaluationError, match="fake command interruption"):
        asyncio.run(runner.run(("docker", "run", "example"), timeout_ns=123))
    assert len(runner.receipts) == 1
    assert runner.receipts[0].state == "RUNNER_ERROR"
    assert runner.receipts[0].returncode is None


def test_sglang_completed_reset_receipt_is_sanitized_and_bound(tmp_path: Path) -> None:
    trial = _prepared(tmp_path).rehearsal.candidates[0].calibration_plan.trials[0]

    def reset(endpoint_id: str, offset: int) -> SimpleNamespace:
        return SimpleNamespace(
            endpoint_id=endpoint_id,
            source_profile_sha256="sha256:" + "1" * 64,
            projected_launch_sha256="sha256:" + "2" * 64,
            readiness_config_sha256="sha256:" + "3" * 64,
            reset=SimpleNamespace(
                before=SimpleNamespace(
                    started_ns=10 + offset, completed_ns=11 + offset
                ),
                flush_completed_ns=12 + offset,
                after=SimpleNamespace(started_ns=13 + offset, completed_ns=14 + offset),
                disposition="SERVER_ACCEPTED",
                scheduler_source_age="UNKNOWN",
                runtime_verification="UNVERIFIED",
                evidence_eligible=False,
            ),
        )

    lifecycle = SimpleNamespace(
        last_reset=(reset("endpoint-a", 0), reset("endpoint-b", 10))
    )
    receipt = rehearsal._sglang_prepare_receipt(
        lifecycle, phase="CALIBRATION", trial=trial
    )
    assert receipt is not None
    assert receipt.source_trial_id == trial.trial_id
    assert [item.endpoint_id for item in receipt.resets] == ["endpoint-a", "endpoint-b"]
    assert b"SERVER_ACCEPTED" in rehearsal._sglang_prepare_bytes(
        phase="CALIBRATION",
        plan=_prepared(tmp_path, input_name="second-inputs")
        .rehearsal.candidates[0]
        .calibration_plan,
        receipts=(receipt,),
    )


def test_export_rejects_unsafe_entries_and_verifies_regular_allowlist(
    tmp_path: Path,
) -> None:
    output = _directory(tmp_path / "output")
    _write_private(
        output / "operator-outcome.json",
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.load-calibration-host-outcome.v1",
                "state": "COMPLETED",
                "reason": "REHEARSAL_FINISHED",
                "provider_termination_verified": False,
                "evidence_eligible": False,
            }
        )
        + b"\n",
    )
    study = _directory(output / "calibration-load-low")
    _write_private(study / "plan.json", b"{}\n")
    archive_root = _directory(tmp_path / "retrieval")
    verified = operator.export_host_local_rehearsal(output, archive_root / "packet.tar")
    assert verified.source_state == "COMPLETE"
    assert verified.artifact_count == 2
    assert (
        operator.verify_retrieved_host_local_export(
            archive_root / "packet.tar",
            expected_archive_sha256=verified.archive_sha256,
        )
        == verified
    )
    os.chmod(archive_root / "packet.tar", 0o600)
    with (archive_root / "packet.tar").open("ab") as archive:
        archive.write(b"tamper")
    with pytest.raises(EvaluationError, match="invalid"):
        operator.verify_retrieved_host_local_export(
            archive_root / "packet.tar",
            expected_archive_sha256=verified.archive_sha256,
        )
    with pytest.raises(EvaluationError, match="could not be published"):
        operator.export_host_local_rehearsal(output, archive_root / "packet.tar")
    os.unlink(archive_root / "packet.tar")
    hardlink = tmp_path / "operator-outcome-copy.json"
    os.link(output / "operator-outcome.json", hardlink)
    with pytest.raises(EvaluationError, match="unsafe"):
        operator.export_host_local_rehearsal(output, archive_root / "hardlink.tar")
    os.unlink(hardlink)
    unsafe = _directory(tmp_path / "unsafe")
    os.symlink("elsewhere", unsafe / "operator-outcome.json")
    with pytest.raises(EvaluationError, match="unsafe"):
        operator.export_host_local_rehearsal(unsafe, archive_root / "unsafe.tar")


def test_export_bounds_are_checked_before_file_reads_or_allocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _directory(tmp_path / "output")
    _write_private(output / "operator-outcome.json", b"x" * 5)
    archive_root = _directory(tmp_path / "retrieval")

    def unexpected_read(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("oversized export file was read")

    monkeypatch.setattr(operator, "_MAX_EXPORT_BYTES", 4)
    monkeypatch.setattr(operator.os, "read", unexpected_read)
    with pytest.raises(EvaluationError, match="file exceeds its bound"):
        operator.export_host_local_rehearsal(output, archive_root / "oversized.tar")


def test_export_file_count_is_checked_before_any_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _directory(tmp_path / "output")
    _write_private(output / "operator-outcome.json", b"{}\n")
    _write_private(output / "calibration-plan.json", b"{}\n")
    archive_root = _directory(tmp_path / "retrieval")

    def unexpected_read(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("over-limit export inventory was read")

    monkeypatch.setattr(operator, "_MAX_EXPORT_FILES", 1)
    monkeypatch.setattr(operator, "_read_regular", unexpected_read)
    with pytest.raises(EvaluationError, match="inventory exceeds its bound"):
        operator.export_host_local_rehearsal(output, archive_root / "over-count.tar")


def test_export_rejects_a_file_that_grows_during_its_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _directory(tmp_path / "output")
    outcome = output / "operator-outcome.json"
    _write_private(outcome, b"x")
    archive_root = _directory(tmp_path / "retrieval")
    original_read = operator.os.read
    expanded = False

    def append_after_first_read(descriptor: int, count: int) -> bytes:
        nonlocal expanded
        content = original_read(descriptor, count)
        if not expanded:
            expanded = True
            os.chmod(outcome, 0o600)
            with outcome.open("ab") as destination:
                destination.write(b"growth")
        return content

    monkeypatch.setattr(operator.os, "read", append_after_first_read)
    with pytest.raises(EvaluationError, match="changed during read"):
        operator.export_host_local_rehearsal(output, archive_root / "growth.tar")
    assert expanded


def test_retrieved_export_hash_and_manifest_stay_bound_to_one_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def output(name: str, state: str) -> Path:
        root = _directory(tmp_path / name)
        _write_private(
            root / "operator-outcome.json",
            canonical_json_bytes(
                {
                    "schema_version": "inferdrome.load-calibration-host-outcome.v1",
                    "state": state,
                    "reason": "REHEARSAL_FINISHED",
                    "provider_termination_verified": False,
                    "evidence_eligible": False,
                }
            )
            + b"\n",
        )
        return root

    archive_root = _directory(tmp_path / "retrieval")
    first = operator.export_host_local_rehearsal(
        output("first-output", "COMPLETED"), archive_root / "first.tar"
    )
    second = operator.export_host_local_rehearsal(
        output("second-output", "FAILED"), archive_root / "second.tar"
    )
    original_open = operator.tarfile.open
    replaced = False

    def replace_path_then_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(archive_root / "second.tar", archive_root / "first.tar")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(operator.tarfile, "open", replace_path_then_open)
    with pytest.raises(EvaluationError, match="invalid"):
        operator.verify_retrieved_host_local_export(
            archive_root / "first.tar",
            expected_archive_sha256=second.archive_sha256,
        )
    assert replaced
    assert first.archive_sha256 != second.archive_sha256
