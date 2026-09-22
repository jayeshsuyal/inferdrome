"""Offline checks for the versioned ordinary-container operator packet."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from inferdrome.evaluation import load_calibration_operator as export_operator
from inferdrome.evaluation import vast_container_operator as operator
from inferdrome.evaluation.cli import main
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.direct_process_lifecycle import (
    REAL_GPU_STARTUP_TIMEOUT_NS,
    DirectProcessLease,
)
from inferdrome.evaluation.load_calibration_rehearsal import (
    SubprocessResult,
    _validate_lifecycle_reservation,
)
from inferdrome.evaluation.vast_stock_vllm import (
    VAST_STOCK_CUDA_VERSION,
    VAST_STOCK_CUDART_VERSION,
    VAST_STOCK_UPSTREAM_IMAGE_TAG,
    VAST_STOCK_VLLM_BUILD_COMMIT,
    VAST_STOCK_VLLM_CONFIG_DIGEST,
    VAST_STOCK_VLLM_ENTRYPOINT,
    VAST_STOCK_VLLM_IMAGE_REFERENCE,
    VAST_STOCK_VLLM_INDEX_DIGEST,
    VAST_STOCK_VLLM_TAG,
    VAST_STOCK_VLLM_VERSION,
)
from inferdrome.qwen3_campaign import qwen3_expected_snapshot_sha256
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE
from tests.unit.test_load_calibration_operator import _prepared


def _inputs(tmp_path: Path) -> tuple[Path, tuple[Path, Path]]:
    _prepared(tmp_path)
    root = tmp_path / "inputs"
    return root / "protocol.json", (root / "load-low.json", root / "load-high.json")


def _directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _direct_prepared(tmp_path: Path) -> operator.PreparedVastContainerRehearsal:
    protocol, recipes = _inputs(tmp_path)
    return operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=VAST_STOCK_VLLM_IMAGE_REFERENCE,
        runtime_executable="/bin/sh",
    )


def _direct_prepared_with_split_reservation(
    tmp_path: Path,
) -> operator.PreparedVastContainerRehearsal:
    protocol, recipes = _inputs(tmp_path)
    value = json.loads(protocol.read_bytes())
    value["preparation"]["warmup_reset_max_duration_ns"] = 420_000_000_000
    value["preparation"]["cleanup_max_duration_ns"] = 240_000_000_000
    protocol.chmod(0o600)
    protocol.write_bytes(canonical_json_bytes(value) + b"\n")
    protocol.chmod(0o400)
    return operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=VAST_STOCK_VLLM_IMAGE_REFERENCE,
        runtime_executable="/bin/sh",
    )


def _direct_authorization(
    prepared: operator.PreparedVastContainerRehearsal,
    *,
    snapshot: Path,
    output: Path,
    now: datetime,
    gpu_model: str = "NVIDIA A100-SXM4-40GB",
) -> operator.VastContainerRehearsalAuthorization:
    def timestamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    return operator.VastContainerRehearsalAuthorization.model_validate(
        {
            "schema_version": (
                "inferdrome.load-calibration-vast-container-authorization.v2"
            ),
            "confirmation": (
                "AUTHORIZE_VAST_STOCK_VLLM_TWO_A100_LOAD_CALIBRATION_V2"
            ),
            "authorization_id": "direct-run-001",
            "approval_record_id": "approval-001",
            "approved_at_utc": timestamp(now - timedelta(hours=1)),
            "execute_not_after_utc": timestamp(now + timedelta(days=3)),
            "source_commit": prepared.preflight.source_commit,
            "runtime": "vllm",
            "outer_image_reference": VAST_STOCK_VLLM_IMAGE_REFERENCE,
            "outer_image_state": "DECLARED_BY_OPERATOR_UNVERIFIED",
            "runtime_executable_identity_sha256": (
                prepared.preflight.runtime_identity.executable_identity_sha256
            ),
            "model_id": "Qwen/Qwen3-8B",
            "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
            "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
            "protocol_sha256": prepared.preflight.protocol_sha256,
            "recipe_sha256s": prepared.preflight.recipe_sha256s,
            "model_snapshot_path_sha256": operator._path_sha256(snapshot),
            "output_root_sha256": operator._path_sha256(output),
            "ownership_id": "direct-run-001",
            "maximum_runtime_seconds": 86_400,
            "termination_safety_margin_seconds": 60,
            "external_guardian": {
                "provider": "VAST_MANUAL_HOST",
                "instance_alias": "vast-run-001",
                "instance_identity_sha256": "sha256:" + "1" * 64,
                "guardian_alias": "guardian-001",
                "guardian_handoff_sha256": "sha256:" + "2" * 64,
                "termination_deadline_utc": timestamp(now + timedelta(days=4)),
                "state": "EXTERNALLY_ARMED_NOT_VERIFIED",
            },
            "provider": "VAST",
            "provider_account_alias": "vast-account",
            "region_or_zone_alias": "vast-location",
            "gpu_type": gpu_model,
            "gpu_count": 2,
            "maximum_cost_usd_micros": 5_000_000,
            "evidence_destination_sha256": "sha256:" + "3" * 64,
            "stock_profile": {
                "schema_version": "inferdrome.vast-stock-vllm-profile.v1",
                "image_tag": VAST_STOCK_VLLM_TAG,
                "image_reference": VAST_STOCK_VLLM_IMAGE_REFERENCE,
                "multiarch_index_digest": VAST_STOCK_VLLM_INDEX_DIGEST,
                "image_config_digest": VAST_STOCK_VLLM_CONFIG_DIGEST,
                "platform": "linux/amd64",
                "entrypoint": (VAST_STOCK_VLLM_ENTRYPOINT,),
                "vllm_version": VAST_STOCK_VLLM_VERSION,
                "cuda_version": VAST_STOCK_CUDA_VERSION,
                "cudart_version": VAST_STOCK_CUDART_VERSION,
                "upstream_image_tag": VAST_STOCK_UPSTREAM_IMAGE_TAG,
                "vllm_build_commit": VAST_STOCK_VLLM_BUILD_COMMIT,
                "python_series": "3.12.x",
                "gpu_count": 2,
                "provider_startup_boundary": (
                    "VAST_STOCK_ENTRYPOINT_SSH_PORTAL_SUPERVISION"
                ),
            },
            "stock_host_receipt": {
                "schema_version": "inferdrome.vast-stock-host-receipt.v1",
                "observed_at_utc": timestamp(now - timedelta(seconds=30)),
                "valid_until_utc": timestamp(now + timedelta(minutes=4)),
                "source_commit": prepared.preflight.source_commit,
                "provider_instance_alias": "vast-run-001",
                "image_reference": VAST_STOCK_VLLM_IMAGE_REFERENCE,
                "image_config_digest": VAST_STOCK_VLLM_CONFIG_DIGEST,
                "entrypoint": (VAST_STOCK_VLLM_ENTRYPOINT,),
                "vllm_version": VAST_STOCK_VLLM_VERSION,
                "cuda_version": VAST_STOCK_CUDA_VERSION,
                "cudart_version": VAST_STOCK_CUDART_VERSION,
                "python_version": "3.12.11",
                "runtime_executable_identity_sha256": (
                    prepared.preflight.runtime_identity.executable_identity_sha256
                ),
                "model_id": "Qwen/Qwen3-8B",
                "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
                "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
                "model_snapshot_path_sha256": operator._path_sha256(snapshot),
                "endpoint_origins": operator._origins(prepared.prepared),
                "gpus": (
                    {
                        "index": 0,
                        "gpu_alias": "gpu-zero",
                        "uuid_sha256": "sha256:" + "4" * 64,
                        "model": gpu_model,
                    },
                    {
                        "index": 1,
                        "gpu_alias": "gpu-one",
                        "uuid_sha256": "sha256:" + "5" * 64,
                        "model": gpu_model,
                    },
                ),
                "gpus_idle": True,
                "ports_closed": True,
                "runtime_install_performed": False,
                "image_build_performed": False,
            },
        }
    )


class _Commands:
    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    async def run(self, argv: tuple[str, ...], *, timeout_ns: int) -> SubprocessResult:
        assert timeout_ns > 0
        self.argvs.append(argv)
        return SubprocessResult(argv=argv, returncode=0, stdout=b"", stderr=b"")


class _Processes:
    def __init__(self) -> None:
        self.started: list[DirectProcessLease] = []
        self.terminated: list[DirectProcessLease] = []

    async def start(
        self,
        executable: object,
        argv: tuple[str, ...],
        *,
        environment: dict[str, str],
    ) -> DirectProcessLease:
        del executable
        assert environment["CUDA_VISIBLE_DEVICES"] == str(len(self.started))
        lease = DirectProcessLease(
            pid=100 + len(self.started),
            process_group_id=100 + len(self.started),
            argv_sha256=sha256_digest(canonical_json_bytes(list(argv))),
        )
        self.started.append(lease)
        return lease

    async def terminate(self, lease: DirectProcessLease, *, timeout_ns: int) -> int:
        assert timeout_ns > 0
        self.terminated.append(lease)
        return -15


def test_preflight_observes_executable_but_never_constructs_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, recipes = _inputs(tmp_path)

    def unexpected_engine(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("offline preflight constructed an engine")

    monkeypatch.setattr(
        operator, "TwoEngineVllmDirectProcessLifecycle", unexpected_engine
    )
    first = operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=VAST_STOCK_VLLM_IMAGE_REFERENCE,
        runtime_executable="/bin/sh",
    )
    second = operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=VAST_STOCK_VLLM_IMAGE_REFERENCE,
        runtime_executable="/bin/sh",
    )
    assert operator.vast_container_preflight_bytes(
        first.preflight
    ) == operator.vast_container_preflight_bytes(second.preflight)
    assert (
        first.preflight.runtime_identity.outer_image_state
        == "DECLARED_BY_OPERATOR_UNVERIFIED"
    )
    assert (
        first.preflight.runtime_identity.executable_observation
        == "LOCAL_METADATA_OBSERVED"
    )
    assert first.preflight.provider_action_performed is False
    assert first.preflight.evidence_eligible is False


def test_preflight_rejects_an_unpinned_outer_image_before_any_runtime(
    tmp_path: Path,
) -> None:
    protocol, recipes = _inputs(tmp_path)
    with pytest.raises(EvaluationError, match="outer image"):
        operator.prepare_vast_container_rehearsal(
            protocol_path=protocol,
            recipe_paths=recipes,
            runtime="vllm",
            outer_image_reference="vllm/vllm-openai:latest",
            runtime_executable="/bin/sh",
        )


def test_stock_authorization_rejects_wrong_gpu_count_and_mutable_image(
    tmp_path: Path,
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    )
    content = authorization.model_dump(mode="python")
    content["gpu_count"] = 1
    with pytest.raises(ValidationError):
        operator.VastContainerRehearsalAuthorization.model_validate(content)


@pytest.mark.parametrize(
    "gpu_model",
    ("NVIDIA A100-PCIE-40GB", "NVIDIA A100-SXM4-40GB"),
)
def test_stock_authorization_accepts_only_the_two_reviewed_a100_40gb_variants(
    tmp_path: Path, gpu_model: str
) -> None:
    prepared = _direct_prepared(tmp_path)
    authorization = _direct_authorization(
        prepared,
        snapshot=_directory(tmp_path / "snapshot"),
        output=_directory(tmp_path / "output"),
        now=datetime(2030, 1, 1, tzinfo=UTC),
        gpu_model=gpu_model,
    )
    assert authorization.gpu_type == gpu_model
    assert authorization.stock_host_receipt is not None
    assert {item.model for item in authorization.stock_host_receipt.gpus} == {
        gpu_model
    }


@pytest.mark.parametrize(
    "gpu_model",
    (
        "NVIDIA H100-80GB-HBM3",
        "NVIDIA A100-SXM4-80GB",
        "NVIDIA A100-PCIE-80GB",
        "NVIDIA A100 40GB",
    ),
)
def test_stock_authorization_rejects_unsupported_or_wrong_memory_gpu(
    tmp_path: Path, gpu_model: str
) -> None:
    prepared = _direct_prepared(tmp_path)
    authorization = _direct_authorization(
        prepared,
        snapshot=_directory(tmp_path / "snapshot"),
        output=_directory(tmp_path / "output"),
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    content = authorization.model_dump(mode="python")
    content["gpu_type"] = gpu_model
    receipt = dict(content["stock_host_receipt"])
    receipt["gpus"] = tuple(
        {**item, "model": gpu_model} for item in receipt["gpus"]
    )
    content["stock_host_receipt"] = receipt
    with pytest.raises(ValidationError):
        operator.VastContainerRehearsalAuthorization.model_validate(content)


def test_stock_authorization_rejects_mixed_supported_gpu_variants(
    tmp_path: Path,
) -> None:
    prepared = _direct_prepared(tmp_path)
    authorization = _direct_authorization(
        prepared,
        snapshot=_directory(tmp_path / "snapshot"),
        output=_directory(tmp_path / "output"),
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    content = authorization.model_dump(mode="python")
    receipt = dict(content["stock_host_receipt"])
    first, second = receipt["gpus"]
    receipt["gpus"] = (
        {**first, "model": "NVIDIA A100-PCIE-40GB"},
        {**second, "model": "NVIDIA A100-SXM4-40GB"},
    )
    content["stock_host_receipt"] = receipt
    with pytest.raises(ValidationError, match="one exact GPU model"):
        operator.VastContainerRehearsalAuthorization.model_validate(content)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("stock_profile", "image_config_digest", "sha256:" + "0" * 64),
        ("stock_profile", "vllm_version", "0.25.0"),
        ("stock_host_receipt", "gpus_idle", False),
        ("stock_host_receipt", "ports_closed", False),
        ("stock_host_receipt", "model_id", "other/model"),
    ),
)
def test_stock_authorization_rejects_wrong_runtime_or_host_fact(
    tmp_path: Path, section: str, field: str, value: object
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    content = authorization.model_dump(mode="python")
    nested = dict(content[section])
    nested[field] = value
    content[section] = nested
    with pytest.raises(ValidationError):
        operator.VastContainerRehearsalAuthorization.model_validate(content)
    content = authorization.model_dump(mode="python")
    content["outer_image_reference"] = VAST_STOCK_VLLM_TAG
    with pytest.raises(ValidationError):
        operator.VastContainerRehearsalAuthorization.model_validate(content)


def test_historical_v1_remains_parseable_but_cannot_launch_stock_vllm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    current = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    ).model_dump(mode="python")
    current.update(
        schema_version="inferdrome.load-calibration-vast-container-authorization.v1",
        confirmation="AUTHORIZE_VAST_CONTAINER_TWO_A100_LOAD_CALIBRATION_V1",
        outer_image_reference=VLLM_RUNTIME_IMAGE_REFERENCE,
        provider=None,
        provider_account_alias=None,
        region_or_zone_alias=None,
        gpu_type=None,
        gpu_count=None,
        maximum_cost_usd_micros=None,
        evidence_destination_sha256=None,
        stock_profile=None,
        stock_host_receipt=None,
    )
    historical = operator.VastContainerRehearsalAuthorization.model_validate(current)

    monkeypatch.setattr(
        operator,
        "TwoEngineVllmDirectProcessLifecycle",
        lambda *args, **kwargs: pytest.fail("lifecycle constructed"),
    )
    with pytest.raises(EvaluationError, match="authorization is unavailable"):
        asyncio.run(
            operator.run_authorized_vast_container_rehearsal(
                prepared,
                authorization=historical,
                model_snapshot_path=snapshot,
                output_root=output,
                session_anchor_utc=now,
                session_started_ns=10,
                utc_clock=lambda: now,
                monotonic_clock=iter((11,)).__next__,
            )
        )


def test_stale_stock_host_receipt_fails_before_engine_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    )
    assert authorization.stock_host_receipt is not None
    stale = authorization.model_copy(
        update={
            "stock_host_receipt": authorization.stock_host_receipt.model_copy(
                update={
                    "observed_at_utc": "2029-12-31T23:50:00Z",
                    "valid_until_utc": "2029-12-31T23:54:00Z",
                }
            )
        }
    )
    monkeypatch.setattr(
        operator,
        "TwoEngineVllmDirectProcessLifecycle",
        lambda *args, **kwargs: pytest.fail("lifecycle constructed"),
    )
    with pytest.raises(EvaluationError, match="receipt is unavailable or stale"):
        asyncio.run(
            operator.run_authorized_vast_container_rehearsal(
                prepared,
                authorization=stale,
                model_snapshot_path=snapshot,
                output_root=output,
                session_anchor_utc=now,
                session_started_ns=10,
                utc_clock=lambda: now,
                monotonic_clock=iter((11,)).__next__,
            )
        )


def test_wrong_stock_receipt_source_fails_before_engine_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    )
    assert authorization.stock_host_receipt is not None
    wrong_source = authorization.model_copy(
        update={
            "stock_host_receipt": authorization.stock_host_receipt.model_copy(
                update={"source_commit": "f" * 40}
            )
        }
    )
    monkeypatch.setattr(
        operator,
        "TwoEngineVllmDirectProcessLifecycle",
        lambda *args, **kwargs: pytest.fail("lifecycle constructed"),
    )
    with pytest.raises(EvaluationError, match="receipt is unavailable or stale"):
        asyncio.run(
            operator.run_authorized_vast_container_rehearsal(
                prepared,
                authorization=wrong_source,
                model_snapshot_path=snapshot,
                output_root=output,
                session_anchor_utc=now,
                session_started_ns=10,
                utc_clock=lambda: now,
                monotonic_clock=iter((11,)).__next__,
            )
        )


def test_authorized_gpu_variant_must_match_both_observed_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    ).model_copy(update={"gpu_type": "NVIDIA A100-PCIE-40GB"})
    monkeypatch.setattr(
        operator,
        "TwoEngineVllmDirectProcessLifecycle",
        lambda *args, **kwargs: pytest.fail("lifecycle constructed"),
    )
    with pytest.raises(EvaluationError, match="receipt is unavailable or stale"):
        asyncio.run(
            operator.run_authorized_vast_container_rehearsal(
                prepared,
                authorization=authorization,
                model_snapshot_path=snapshot,
                output_root=output,
                session_anchor_utc=now,
                session_started_ns=10,
                utc_clock=lambda: now,
                monotonic_clock=iter((11,)).__next__,
            )
        )


def test_cli_preflight_writes_only_a_canonical_offline_packet(tmp_path: Path) -> None:
    protocol, recipes = _inputs(tmp_path)
    output_root = tmp_path / "preflight-output"
    output_root.mkdir()
    output = output_root / "preflight.json"
    assert (
        main(
            [
                "load-calibration-vast-container-preflight",
                "--protocol",
                str(protocol),
                "--recipe",
                str(recipes[0]),
                "--recipe",
                str(recipes[1]),
                "--runtime",
                "vllm",
                "--outer-image-reference",
                VAST_STOCK_VLLM_IMAGE_REFERENCE,
                "--runtime-executable",
                "/bin/sh",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_bytes().endswith(b"\n")
    assert output.read_bytes() == operator.vast_container_preflight_bytes(
        operator.prepare_vast_container_rehearsal(
            protocol_path=protocol,
            recipe_paths=recipes,
            runtime="vllm",
            outer_image_reference=VAST_STOCK_VLLM_IMAGE_REFERENCE,
            runtime_executable="/bin/sh",
        ).preflight
    )


def test_run_requires_the_new_exact_direct_process_confirmation(tmp_path: Path) -> None:
    protocol, recipes = _inputs(tmp_path)
    assert (
        main(
            [
                "load-calibration-vast-container-run",
                "--protocol",
                str(protocol),
                "--recipe",
                str(recipes[0]),
                "--recipe",
                str(recipes[1]),
                "--runtime",
                "vllm",
                "--outer-image-reference",
                VAST_STOCK_VLLM_IMAGE_REFERENCE,
                "--runtime-executable",
                "/bin/sh",
                "--authorization",
                str(tmp_path / "not-read.json"),
                "--model-snapshot",
                str(tmp_path / "not-read-model"),
                "--output-root",
                str(tmp_path / "not-read-output"),
                "--execute-approval",
                "wrong",
            ]
        )
        == 2
    )


def test_direct_authorized_execution_exports_and_reverifies_its_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise the direct authorization-to-retrieval wiring with CPU fakes."""

    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    retrieval = _directory(tmp_path / "retrieval")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    )
    commands, processes = _Commands(), _Processes()
    original_lifecycle = operator.TwoEngineVllmDirectProcessLifecycle
    observed: list[object] = []

    def fake_lifecycle(
        origins: tuple[str, str], **kwargs: object
    ) -> object:
        lifecycle = original_lifecycle(
            origins,
            **kwargs,
            snapshot_verifier=lambda _path: None,
            port_closed_probe=lambda _port, _timeout: None,
        )

        async def ready(trial: object, stop: asyncio.Event) -> None:
            assert not stop.is_set()
            assert (
                trial
                == prepared.prepared.rehearsal.candidates[0].calibration_plan.trials[0]
            )

        lifecycle._await_ready_and_warm = ready  # type: ignore[method-assign]
        observed.append(lifecycle)
        return lifecycle

    async def fake_run(
        rehearsal: object, output_root: Path, **kwargs: object
    ) -> SimpleNamespace:
        assert rehearsal == prepared.prepared.rehearsal
        assert output_root == output
        lifecycle = kwargs["lifecycle"]
        stop = kwargs["stop"]
        trial = prepared.prepared.rehearsal.candidates[0].calibration_plan.trials[0]
        await lifecycle.prepare(trial, stop=stop)  # type: ignore[union-attr]
        await lifecycle.cleanup(trial, stop=stop)  # type: ignore[union-attr]
        return SimpleNamespace()

    monkeypatch.setattr(operator, "TwoEngineVllmDirectProcessLifecycle", fake_lifecycle)
    monkeypatch.setattr(operator, "run_rehearsal", fake_run)
    result = asyncio.run(
        operator.run_authorized_vast_container_rehearsal(
            prepared,
            authorization=authorization,
            model_snapshot_path=snapshot,
            output_root=output,
            session_anchor_utc=now,
            session_started_ns=10,
            utc_clock=lambda: now,
            monotonic_clock=iter((11, 12)).__next__,
            command_runner=commands,
            process_runner=processes,
        )
    )
    assert isinstance(result, SimpleNamespace)
    assert len(observed) == 1
    assert len(commands.argvs) == 4
    assert processes.terminated == list(reversed(processes.started))
    sidecars = {
        "operator-vast-container-session.json",
        "operator-vast-container-attempt-plan.json",
        "operator-vast-container-process-receipts.json",
        "operator-vast-container-outcome.json",
    }
    assert sidecars.issubset(path.name for path in output.iterdir())
    archive = retrieval / "direct.tar"
    assert (
        main(
            [
                "load-calibration-host-export",
                "--output-root",
                str(output),
                "--archive",
                str(archive),
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["source_state"] == "COMPLETE"
    assert exported["artifact_count"] == len(sidecars)
    assert (
        main(
            [
                "load-calibration-host-verify-export",
                "--archive",
                str(archive),
                "--expected-archive-sha256",
                exported["archive_sha256"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["archive_sha256"] == exported[
        "archive_sha256"
    ]


def test_direct_execution_uses_the_real_gpu_startup_budget_not_the_test_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator grants the reviewed 300s startup allowance."""

    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    authorization = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    )
    commands, processes = _Commands(), _Processes()
    captured: list[int] = []

    def fake_lifecycle(origins: tuple[str, str], **kwargs: object) -> object:
        startup_timeout_ns = kwargs["startup_timeout_ns"]
        assert isinstance(startup_timeout_ns, int)
        captured.append(startup_timeout_ns)
        return SimpleNamespace()

    async def fake_run(
        rehearsal: object, output_root: Path, **kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace()

    monkeypatch.setattr(
        operator, "TwoEngineVllmDirectProcessLifecycle", fake_lifecycle
    )
    monkeypatch.setattr(operator, "run_rehearsal", fake_run)
    asyncio.run(
        operator.run_authorized_vast_container_rehearsal(
            prepared,
            authorization=authorization,
            model_snapshot_path=snapshot,
            output_root=output,
            session_anchor_utc=now,
            session_started_ns=10,
            utc_clock=lambda: now,
            monotonic_clock=iter((11, 12)).__next__,
            command_runner=commands,
            process_runner=processes,
        )
    )
    assert captured == [REAL_GPU_STARTUP_TIMEOUT_NS]
    assert REAL_GPU_STARTUP_TIMEOUT_NS == 300_000_000_000
    assert REAL_GPU_STARTUP_TIMEOUT_NS > 120_000_000_000


def test_real_direct_lifecycle_accepts_only_connected_split_reservation(
    tmp_path: Path,
) -> None:
    """Exercise the actual compiler, protocol and lifecycle reservation seam."""

    prepared = _direct_prepared_with_split_reservation(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    origins = operator._origins(prepared.prepared)
    lifecycle = operator.TwoEngineVllmDirectProcessLifecycle(
        origins,
        ownership_id="direct-run-001",
        model_snapshot_path=snapshot,
        executable=operator.resolve_direct_runtime("/bin/sh"),
        command_runner=_Commands(),
        process_runner=_Processes(),
        startup_timeout_ns=REAL_GPU_STARTUP_TIMEOUT_NS,
    )

    protocol = prepared.prepared.rehearsal.calibration_plan.protocol
    assert lifecycle.required_prepare_timeout_ns == 420_000_000_000
    assert lifecycle.required_cleanup_timeout_ns == 240_000_000_000
    assert protocol.preparation.warmup_reset_max_duration_ns == 420_000_000_000
    assert protocol.preparation.cleanup_max_duration_ns == 240_000_000_000
    _validate_lifecycle_reservation(lifecycle, protocol)

    legacy = protocol.model_copy(
        update={
            "preparation": protocol.preparation.model_copy(
                update={
                    "warmup_reset_max_duration_ns": 240_000_000_000,
                    "cleanup_max_duration_ns": None,
                }
            )
        }
    )
    with pytest.raises(
        EvaluationError,
        match="protocol lifecycle reserve cannot run the supplied lifecycle",
    ):
        _validate_lifecycle_reservation(lifecycle, legacy)


def test_legacy_lifecycle_requirement_must_fit_prepare_and_cleanup_reserves(
    tmp_path: Path,
) -> None:
    protocol = _direct_prepared_with_split_reservation(
        tmp_path
    ).prepared.rehearsal.calibration_plan.protocol
    legacy = SimpleNamespace(required_operation_timeout_ns=240_000_000_000)

    _validate_lifecycle_reservation(legacy, protocol)

    insufficient_cleanup = protocol.model_copy(
        update={
            "preparation": protocol.preparation.model_copy(
                update={"cleanup_max_duration_ns": 1}
            )
        }
    )
    with pytest.raises(
        EvaluationError,
        match="protocol lifecycle reserve cannot run the supplied lifecycle",
    ):
        _validate_lifecycle_reservation(legacy, insufficient_cleanup)

    omitted_cleanup = protocol.model_copy(
        update={
            "preparation": protocol.preparation.model_copy(
                update={"cleanup_max_duration_ns": None}
            )
        }
    )
    _validate_lifecycle_reservation(legacy, omitted_cleanup)


def test_direct_export_marks_failure_or_partial_and_rejects_mixed_or_unknown_roots(
    tmp_path: Path,
) -> None:
    def sidecar(name: str, value: dict[str, object]) -> None:
        path = output / name
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(canonical_json_bytes(value) + b"\n")
        path.chmod(0o400)

    output = _directory(tmp_path / "output")
    retrieval = _directory(tmp_path / "retrieval")
    authorization_sha256 = "sha256:" + "a" * 64
    sidecar(
        "operator-vast-container-session.json",
        {
            "schema_version": "inferdrome.load-calibration-vast-container-session.v1",
            "authorization_sha256": authorization_sha256,
        },
    )
    sidecar(
        "operator-vast-container-attempt-plan.json",
        {
            "schema_version": (
                "inferdrome.load-calibration-vast-container-attempt-plan.v1"
            ),
            "authorization_sha256": authorization_sha256,
        },
    )
    sidecar(
        "operator-vast-container-process-receipts.json",
        {
            "schema_version": (
                "inferdrome.load-calibration-vast-container-process-receipts.v1"
            )
        },
    )
    sidecar(
        "operator-vast-container-outcome.json",
        {
            "schema_version": "inferdrome.load-calibration-vast-container-outcome.v1",
            "state": "FAILED",
        },
    )
    failed = export_operator.export_host_local_rehearsal(
        output, retrieval / "failed.tar"
    )
    assert failed.source_state == "PARTIAL_OR_ABORTED"

    (output / "operator-vast-container-outcome.json").unlink()
    partial = export_operator.export_host_local_rehearsal(
        output, retrieval / "partial.tar"
    )
    assert partial.source_state == "PARTIAL_OR_ABORTED"

    sidecar(
        "operator-vast-container-attempt-plan.json",
        {
            "schema_version": (
                "inferdrome.load-calibration-vast-container-attempt-plan.v1"
            ),
            "authorization_sha256": "sha256:" + "b" * 64,
        },
    )
    with pytest.raises(EvaluationError, match="identities are inconsistent"):
        export_operator.export_host_local_rehearsal(
            output, retrieval / "inconsistent.tar"
        )
    sidecar(
        "operator-vast-container-attempt-plan.json",
        {
            "schema_version": (
                "inferdrome.load-calibration-vast-container-attempt-plan.v1"
            ),
            "authorization_sha256": authorization_sha256,
        },
    )

    sidecar(
        "operator-outcome.json",
        {
            "schema_version": "inferdrome.load-calibration-host-outcome.v1",
            "state": "COMPLETED",
        },
    )
    with pytest.raises(EvaluationError, match="mixes operator identities"):
        export_operator.export_host_local_rehearsal(output, retrieval / "mixed.tar")
    (output / "operator-outcome.json").unlink()

    unknown = output / "unexpected.json"
    unknown.write_bytes(b"{}\n")
    unknown.chmod(0o400)
    with pytest.raises(EvaluationError, match="undeclared file"):
        export_operator.export_host_local_rehearsal(output, retrieval / "unknown.tar")
