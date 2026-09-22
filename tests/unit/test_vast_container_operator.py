"""Offline checks for the versioned ordinary-container operator packet."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from inferdrome.evaluation.vast_startup_ready import (
    VAST_ENGINE_READINESS_SECONDS,
    VAST_SSH_READINESS_SECONDS,
)
from inferdrome.qwen3_campaign import qwen3_expected_snapshot_sha256
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE
from tests.unit.test_load_calibration_operator import _prepared

_DERIVED_IMAGE = (
    "ghcr.io/jayeshsuyal/inferdrome-private-engine@sha256:" + "a" * 64
)
_OTHER_DERIVED_IMAGE = (
    "ghcr.io/jayeshsuyal/inferdrome-private-engine@sha256:" + "b" * 64
)


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
        outer_image_reference=_DERIVED_IMAGE,
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
        outer_image_reference=_DERIVED_IMAGE,
        runtime_executable="/bin/sh",
    )


def _direct_authorization(
    prepared: operator.PreparedVastContainerRehearsal,
    *,
    snapshot: Path,
    output: Path,
    now: datetime,
) -> operator.VastContainerRehearsalAuthorization:
    def timestamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    return operator.VastContainerRehearsalAuthorization.model_validate(
        {
            "schema_version": (
                "inferdrome.load-calibration-vast-container-authorization.v2"
            ),
            "confirmation": "AUTHORIZE_VAST_CONTAINER_TWO_A100_LOAD_CALIBRATION_V1",
            "authorization_id": "direct-run-001",
            "approval_record_id": "approval-001",
            "approved_at_utc": timestamp(now - timedelta(hours=1)),
            "execute_not_after_utc": timestamp(now + timedelta(days=3)),
            "source_commit": prepared.preflight.source_commit,
            "runtime": "vllm",
            "outer_image_reference": _DERIVED_IMAGE,
            "outer_image_state": "DECLARED_BY_OPERATOR_UNVERIFIED",
            "startup_ready_image": {
                "schema_version": (
                    "inferdrome.vast-startup-ready-image-profile.v1"
                ),
                "image_reference": _DERIVED_IMAGE,
                "source_commit": prepared.preflight.source_commit,
                "base_image_reference": VLLM_RUNTIME_IMAGE_REFERENCE,
                "runtime_role": "private-engine",
                "runtime": "vllm",
                "runtime_version": "0.26.0",
                "model_id": "Qwen/Qwen3-8B",
                "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
                "startup_profile": "vast-ssh-public-v1",
                "ssh_server_preinstalled": True,
                "startup_supervisor": "SSHD_FOREGROUND_DIRECT_EXEC",
                "serving_uid": 2000,
                "runtime_package_bootstrap": "FORBIDDEN",
                "registry_access": "ANONYMOUS_PUBLIC_PULL_REQUIRED_UNVERIFIED",
                "ssh_readiness_seconds": 180,
                "engine_readiness_seconds": 300,
            },
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
        outer_image_reference=_DERIVED_IMAGE,
        runtime_executable="/bin/sh",
    )
    second = operator.prepare_vast_container_rehearsal(
        protocol_path=protocol,
        recipe_paths=recipes,
        runtime="vllm",
        outer_image_reference=_DERIVED_IMAGE,
        runtime_executable="/bin/sh",
    )
    assert operator.vast_container_preflight_bytes(
        first.preflight
    ) == operator.vast_container_preflight_bytes(second.preflight)
    assert (
        first.preflight.runtime_identity.outer_image_state
        == "DECLARED_BY_OPERATOR_UNVERIFIED"
    )
    assert first.preflight.runtime_identity.outer_image_reference == _DERIVED_IMAGE
    assert (
        first.preflight.runtime_identity.base_image_reference
        == VLLM_RUNTIME_IMAGE_REFERENCE
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


def test_authorization_requires_exact_public_startup_ready_profile(
    tmp_path: Path,
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime.now(UTC).replace(microsecond=0)
    value = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    ).model_dump(mode="python")

    assert value["startup_ready_image"]["ssh_readiness_seconds"] == 180
    assert value["startup_ready_image"]["engine_readiness_seconds"] == 300
    assert VAST_SSH_READINESS_SECONDS == 180
    assert VAST_ENGINE_READINESS_SECONDS == 300
    for mutation in (
        None,
        {**value["startup_ready_image"], "image_reference": "private:latest"},
        {**value["startup_ready_image"], "source_commit": "b" * 40},
        {**value["startup_ready_image"], "runtime_package_bootstrap": "ALLOWED"},
        {**value["startup_ready_image"], "registry_access": "AUTH_REQUIRED"},
        {**value["startup_ready_image"], "serving_uid": 0},
    ):
        candidate = {**value, "startup_ready_image": mutation}
        with pytest.raises(ValueError):
            operator.VastContainerRehearsalAuthorization.model_validate(candidate)

    for candidate in (
        {**value, "outer_image_reference": _OTHER_DERIVED_IMAGE},
        {
            **value,
            "startup_ready_image": {
                **value["startup_ready_image"],
                "base_image_reference": _DERIVED_IMAGE,
            },
        },
        {
            **value,
            "startup_ready_image": {
                **value["startup_ready_image"],
                "runtime_version": "0.25.0",
            },
        },
        {**value, "model_id": "other/model"},
    ):
        with pytest.raises(ValueError):
            operator.VastContainerRehearsalAuthorization.model_validate(candidate)


def test_historical_v1_authorization_remains_parseable_but_cannot_start_vllm(
    tmp_path: Path,
) -> None:
    prepared = _direct_prepared(tmp_path)
    snapshot = _directory(tmp_path / "snapshot")
    output = _directory(tmp_path / "output")
    now = datetime.now(UTC).replace(microsecond=0)
    value = _direct_authorization(
        prepared, snapshot=snapshot, output=output, now=now
    ).model_dump(mode="python")
    value["schema_version"] = (
        "inferdrome.load-calibration-vast-container-authorization.v1"
    )
    value["outer_image_reference"] = VLLM_RUNTIME_IMAGE_REFERENCE
    value.pop("startup_ready_image")
    historical = operator.VastContainerRehearsalAuthorization.model_validate(value)
    assert historical.startup_ready_image is None
    with pytest.raises(EvaluationError, match="unavailable or expired"):
        operator._authorized(
            historical,
            prepared,
            model_snapshot_path=snapshot,
            output_root=output,
            now=now,
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
                _DERIVED_IMAGE,
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
            outer_image_reference=_DERIVED_IMAGE,
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
                _DERIVED_IMAGE,
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
