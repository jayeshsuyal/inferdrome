"""No provider, Docker, CUDA, downloads or credential access in these tests."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from inferdrome.deployment.manual_host import (
    H100_PROFILE_ID,
    H100ManualHostInput,
    ManualHostInput,
    input_template,
    main,
    prepare_artifacts,
    routing_config,
)
from inferdrome.deployment.manual_host_schema import artifacts, check
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import (
    ExecutedManifest,
    RoutingExecutionConfig,
    fixed_selected_workload_bytes,
)
from inferdrome.routing_execution.executor import (
    ExecutionError,
    ManualMonotonicClock,
    declared_input_transfer_digest,
    load_config_bytes,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.package import VerificationError
from inferdrome.routing_execution.topology import TopologyAdmissionError, admit_topology
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import (
    StaticEndpointTransport,
    h100_manual_host_fixture_input,
)
from tests.routing_execution_support import config_value as v1_config_value

ROOT = Path(__file__).resolve().parents[2]


def synthetic_input() -> dict[str, Any]:
    """Clearly synthetic in-memory test declarations, never a real host plan."""
    value = input_template()
    value.update(
        {
            "source_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip(),
            "instance_id": "0123456789abcdef0123456789abcdef",
            "region": "synthetic-region",
            "instance_type": "gpu_2x_a100",
            "gpu_uuids": [
                "GPU-00000000-0000-0000-0000-000000000001",
                "GPU-00000000-0000-0000-0000-000000000002",
            ],
            "uid": 2000,
            "gid": 2000,
            "runner_image": {
                "reference": "example.invalid/test-runner@"
                + sha256_digest(b"test runner")
            },
            "serving_image": {
                "reference": "example.invalid/test-engine@"
                + sha256_digest(b"test engine")
            },
            "model_path": "/srv/test-model",
            "preparation_path": "/srv/test-inputs",
            "evidence_path": "/srv/test-evidence",
            "compose_project": "synthetic-campaign",
            "container_subnet": "172.29.71.0/24",
            "endpoint_ipv4": ["172.29.71.2", "172.29.71.3"],
            "request_timeout_ms": 1000,
        }
    )
    value["cleanup"].update(
        {
            "instance_id": value["instance_id"],
            "accountable_operator": "synthetic-operator",
            "terminate_by_utc": "2030-01-01T00:00:00Z",
        }
    )
    return value


def parsed(value: dict[str, Any] | None = None) -> ManualHostInput:
    return ManualHostInput.model_validate_json(
        canonical_json_bytes(value or synthetic_input())
    )


def h100_synthetic_input() -> dict[str, Any]:
    return h100_manual_host_fixture_input(
        source_commit=synthetic_input()["source_commit"]
    )


def h100_parsed(value: dict[str, Any] | None = None) -> H100ManualHostInput:
    return H100ManualHostInput.model_validate_json(
        canonical_json_bytes(value or h100_synthetic_input())
    )


def test_deterministic_plan_has_exact_allocation_and_existing_benchmark_command() -> (
    None
):
    spec = parsed()
    first = prepare_artifacts(spec, ROOT)
    assert first == prepare_artifacts(spec, ROOT)
    assert len(first) == 9
    assert first["selected-workload.jsonl"] == fixed_selected_workload_bytes()
    config = load_config_bytes(first["deployment-config.json"])
    assert (
        config.evidence_destination.declared_input_transfer_sha256
        == declared_input_transfer_digest(config)
    )
    assert config.mode == "LAMBDA_MANUAL_HOST"
    compose = json.loads(first["compose.manual-host.json"])
    for index, name in enumerate(("endpoint-a", "endpoint-b")):
        engine = compose["services"][name]
        assert engine["deploy"]["resources"]["reservations"]["devices"] == [
            {
                "driver": "nvidia",
                "device_ids": [spec.gpu_uuids[index]],
                "capabilities": ["gpu"],
            }
        ]
        assert (
            engine["command"][engine["command"].index("--tensor-parallel-size") + 1]
            == "1"
        )
        assert "ports" not in engine and "network_mode" not in engine
        assert engine["pull_policy"] == "never"
    runner = compose["services"]["runner"]
    assert "deploy" not in runner
    assert runner["entrypoint"][-1] == "inferdrome.routing_execution"
    assert runner["command"][0] == "run"
    plan = json.loads(first["plan.json"])
    assert plan["execute_authority"] == "NONE"
    assert plan["availability_observation"] is plan["price_observation"] is None
    assert plan["observed_host_proof"] is None
    assert plan["reset"] == "NOT_ASSERTED_SEPARATE_SERVING_ENGINE"
    cleanup = json.loads(first["cleanup-handoff.json"])
    assert cleanup["instance_id"] == spec.instance_id
    assert cleanup["termination_state"] == "NOT_REQUESTED_NOT_VERIFIED"
    assert not cleanup["guest_stop_ends_billing"]
    inventory = json.loads(first["preparation-integrity.json"])
    assert inventory["files"] == {
        name: sha256_digest(content)
        for name, content in first.items()
        if name != "preparation-integrity.json"
    }


@pytest.mark.parametrize(
    "key",
    [
        "source_commit",
        "instance_id",
        "region",
        "instance_type",
        "gpu_uuids",
        "runner_image",
        "serving_image",
        "model",
        "runtime",
        "model_manifest_sha256",
        "model_snapshot_sha256",
        "routing_inputs",
        "workload",
        "cleanup",
        "request_timeout_ms",
        "uid",
        "preparation_path",
    ],
)
def test_missing_exact_input_rejected(key: str) -> None:
    value = synthetic_input()
    del value[key]
    with pytest.raises(ValidationError):
        parsed(value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"accelerator_model": "NVIDIA A100-SXM4-40GB"},
        {"tensor_parallel_size": 2},
        {"provider": "GCP"},
        {"os": "Darwin"},
        {"architecture": "arm64"},
        {"source_commit": "0" * 40},
        {"runner_image": {"reference": "example.invalid/test:latest"}},
        {"endpoint_ipv4": ["172.29.71.2", "172.29.71.2"]},
        {"container_subnet": "8.8.8.0/24"},
        {"endpoint_ipv4": ["172.29.71.1", "172.29.71.3"]},
        {"gpu_uuids": ["0", "1"]},
        {"model_path": "/srv/../srv/models"},
        {"evidence_path": "/srv/test-inputs/out"},
        {"api_key": "DO_NOT_ECHO_SYNTHETIC_SECRET"},
    ],
)
def test_wrong_or_unsafe_declarations_rejected(mutation: dict[str, Any]) -> None:
    value = synthetic_input()
    value.update(mutation)
    with pytest.raises(ValidationError):
        parsed(value)


def test_duplicate_gpu_and_image_roles_and_wrong_cleanup_target_rejected() -> None:
    for key in ("gpu", "image", "cleanup", "deadline", "tokenizer", "workload"):
        value = synthetic_input()
        if key == "gpu":
            value["gpu_uuids"][1] = value["gpu_uuids"][0]
        elif key == "image":
            value["runner_image"] = deepcopy(value["serving_image"])
        elif key == "cleanup":
            value["cleanup"]["instance_id"] = "fedcba9876543210fedcba9876543210"
        elif key == "deadline":
            value["cleanup"]["terminate_by_utc"] = "2030-02-30T00:00:00Z"
        elif key == "tokenizer":
            value["model"]["tokenizer_revision"] = "0" * 40
        else:
            value["workload"]["request_denominator"] = 18
        with pytest.raises(ValidationError):
            parsed(value)


def test_template_is_unresolved_and_schema_snapshots_valid() -> None:
    with pytest.raises(ValidationError):
        parsed(input_template())
    assert input_template() == json.loads(
        (
            ROOT / "deployments/manual-host-v1/lambda-two-a100-pcie.input-template.json"
        ).read_bytes()
    )
    assert input_template(H100_PROFILE_ID)["profile_id"] == H100_PROFILE_ID
    with pytest.raises(ValueError, match="unsupported"):
        input_template("unapproved-gpu-profile")
    with pytest.raises(ValidationError):
        h100_parsed(input_template(H100_PROFILE_ID))
    check(ROOT)
    for name, content in artifacts().items():
        if "schema.json" in name:
            Draft202012Validator.check_schema(json.loads(content))


def test_h100_profile_is_closed_and_uses_the_shared_compose_and_prepare_flow(
    capsys: Any,
) -> None:
    assert main(["template"]) == 0
    assert json.loads(capsys.readouterr().out)["profile_id"] == (
        "lambda-manual-two-a100-pcie-40gb-v1"
    )
    assert main(["template", "--profile", H100_PROFILE_ID]) == 0
    template = json.loads(capsys.readouterr().out)
    assert template == input_template(H100_PROFILE_ID)

    spec = h100_parsed()
    files = prepare_artifacts(spec, ROOT)
    config = load_config_bytes(files["deployment-config.json"])
    compose = json.loads(files["compose.manual-host.json"])
    plan = json.loads(files["plan.json"])

    assert config.schema_version == "inferdrome.routing-execution-config.v3"
    assert config.topology.profile_id == H100_PROFILE_ID
    assert config.topology.accelerator_model == "NVIDIA H100-SXM5-80GB"
    assert plan["profile_id"] == H100_PROFILE_ID
    assert plan["required_before_start"][4] == (
        "Exactly two H100 SXM5 80 GB physical GPUs; distinct UUIDs and MIG disabled."
    )
    for index, endpoint in enumerate(("endpoint-a", "endpoint-b")):
        assert compose["services"][endpoint]["deploy"]["resources"]["reservations"][
            "devices"
        ][0]["device_ids"] == [spec.gpu_uuids[index]]
    assert "deploy" not in compose["services"]["runner"]
    assert (
        compose["services"]["runner"]["environment"]["NVIDIA_VISIBLE_DEVICES"]
        == "void"
    )


def test_h100_input_and_config_reject_mixed_or_legacy_profile_forms() -> None:
    value = h100_synthetic_input()
    for key, replacement in (
        ("schema_version", "inferdrome.manual-host-input.v1"),
        ("profile_id", "lambda-manual-two-a100-pcie-40gb-v1"),
        ("accelerator_model", "NVIDIA A100-PCIE-40GB"),
    ):
        candidate = deepcopy(value)
        candidate[key] = replacement
        with pytest.raises(ValidationError):
            h100_parsed(candidate)

    config = routing_config(h100_parsed()).model_dump(mode="json")
    for key, replacement in (
        ("schema_version", "inferdrome.routing-execution-config.v2"),
        ("profile_id", "lambda-manual-two-a100-pcie-40gb-v1"),
    ):
        candidate = deepcopy(config)
        if key == "profile_id":
            candidate["topology"][key] = replacement
        else:
            candidate[key] = replacement
        with pytest.raises(ExecutionError):
            load_config_bytes(canonical_json_bytes(candidate))


def test_v1_remains_closed_and_manual_admission_rejects_public_or_duplicate() -> None:
    # Isolate old literal rejection, independently of new metadata/extra fields.
    for key in ("mode", "accelerator_model"):
        old = v1_config_value(
            selected_sha256=sha256_digest(fixed_selected_workload_bytes())
        )
        if key == "mode":
            old["mode"] = "LAMBDA_MANUAL_HOST"
        else:
            old["mode"] = "GCP_PRIVATE"
            topology = old["topology"]
            assert isinstance(topology, dict)
            topology["accelerator_model"] = "NVIDIA A100-PCIE-40GB"
            topology["accelerator_count"] = 2
        with pytest.raises(ValidationError):
            RoutingExecutionConfig.model_validate_json(canonical_json_bytes(old))
    value = routing_config(parsed()).model_dump(mode="json")
    for version in ("inferdrome.routing-execution-config.v1", value["schema_version"]):
        value["schema_version"] = version
        with pytest.raises(ValidationError):
            RoutingExecutionConfig.model_validate_json(canonical_json_bytes(value))
    from inferdrome.routing_execution.manual_host_contracts import (
        ManualHostRoutingConfig,
    )

    value["schema_version"] = "inferdrome.routing-execution-config.v2"
    for origin in ("https://8.8.8.8:8000", value["endpoints"][0]["origin"]):
        value["endpoints"][1]["origin"] = origin
        config = ManualHostRoutingConfig.model_validate_json(
            canonical_json_bytes(value)
        )
        with pytest.raises(TopologyAdmissionError):
            admit_topology(config)


def test_manual_config_reuses_receipts_sealing_offline_replay_and_not_synthetic_overlay(
    tmp_path: Path,
) -> None:
    files = prepare_artifacts(parsed(), ROOT)
    sealed = run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        tmp_path / "package",
        transport_factory=StaticEndpointTransport,
        clock=ManualMonotonicClock(),
    )
    result = verify_execution_package(
        sealed.path, expected_digest=sealed.retained_digest
    )
    manifest = result.executed_manifest
    assert manifest.schema_version == "inferdrome.routing-executed-manifest.v2"
    assert manifest.mode == "LAMBDA_MANUAL_HOST"
    assert manifest.topology.accelerator_model == "NVIDIA A100-PCIE-40GB"
    assert len(result.producer_receipt.terminal_outcomes) == 18
    assert all(
        t.request_denominator == 6 for t in result.producer_receipt.trial_summaries
    )
    assert all(
        r.endpoint_engine_reset_assertion == "NOT_ASSERTED_SEPARATE_SERVING_ENGINE"
        for r in result.producer_receipt.reset_receipts
    )
    value = manifest.model_dump(mode="json")
    for version in ("inferdrome.routing-executed-manifest.v1", value["schema_version"]):
        value["schema_version"] = version
        with pytest.raises(ValidationError):
            ExecutedManifest.model_validate_json(canonical_json_bytes(value))
    raw = b"".join(p.read_bytes() for p in sealed.path.iterdir())
    assert b"GPU-00000000" not in raw and b"synthetic-operator" not in raw
    assert b"172.29.71.2" not in raw and b"OPERATOR_DECLARED_NOT_OBSERVED" in raw
    assert set(p.name for p in sealed.path.iterdir()) == {
        "executed-manifest.json",
        "input-transfer-receipt.json",
        "producer-receipt.json",
        "integrity-manifest.json",
    }


def test_h100_v3_execution_seals_verifies_and_rejects_manifest_tamper(
    tmp_path: Path,
) -> None:
    files = prepare_artifacts(h100_parsed(), ROOT)
    sealed = run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        tmp_path / "package",
        transport_factory=StaticEndpointTransport,
        clock=ManualMonotonicClock(),
    )
    result = verify_execution_package(
        sealed.path, expected_digest=sealed.retained_digest
    )
    assert result.executed_manifest.schema_version == (
        "inferdrome.routing-executed-manifest.v3"
    )
    assert result.executed_manifest.topology.profile_id == H100_PROFILE_ID
    assert len(result.producer_receipt.terminal_outcomes) == 18

    manifest_path = sealed.path / "executed-manifest.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schema_version"] = "inferdrome.routing-executed-manifest.v2"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    manifest_path.chmod(0o400)
    with pytest.raises(VerificationError):
        verify_execution_package(sealed.path)


def test_capture_failure_still_closes_denominator(tmp_path: Path) -> None:
    files = prepare_artifacts(parsed(), ROOT)
    sealed = run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        tmp_path / "package",
        transport_factory=lambda: StaticEndpointTransport(mode="timeout"),
        clock=ManualMonotonicClock(),
    )
    result = verify_execution_package(sealed.path)
    assert {r.status for r in result.producer_receipt.terminal_outcomes} == {
        "TIMED_OUT",
        "NO_SAFE_ROUTE",
    }
    assert len(result.producer_receipt.terminal_outcomes) == 18


def test_readiness_failure_does_not_publish_success_package(tmp_path: Path) -> None:
    class BrokenReadiness(StaticEndpointTransport):
        def get(self, origin: str, path: str, *, timeout_ms: int) -> Any:
            if path == "/v1/models":
                raise OSError("synthetic readiness failure")
            return super().get(origin, path, timeout_ms=timeout_ms)

    files = prepare_artifacts(parsed(), ROOT)
    with pytest.raises((ExecutionError, OSError)):
        run_execution_from_bytes(
            files["deployment-config.json"],
            files["selected-workload.jsonl"],
            tmp_path / "package",
            transport_factory=BrokenReadiness,
            clock=ManualMonotonicClock(),
        )
    assert not (tmp_path / "package").exists()


def test_one_command_prepare_is_create_only_and_startup_is_inert(
    tmp_path: Path, capsys: Any
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_bytes(canonical_json_bytes(synthetic_input()))
    args = [
        "prepare",
        "--input",
        str(input_path),
        "--output",
        str(tmp_path / "prepared"),
    ]
    assert main(args) == 0
    assert main(args) == 2
    script = tmp_path / "prepared" / "startup.sh"
    completed = subprocess.run(["bash", str(script)], capture_output=True, timeout=5)
    assert completed.returncode == 78
    assert b"Exact plan/operator control" in completed.stderr
    assert subprocess.run(["bash", "-n", str(script)], timeout=5).returncode == 0
    assert "LOCAL_PREPARATION_ONLY" in capsys.readouterr().out


def test_bad_cleanup_handoff_produces_no_artifacts_or_sensitive_error(
    tmp_path: Path, capsys: Any
) -> None:
    value = synthetic_input()
    value["cleanup"]["termination_state"] = "VERIFIED"
    value["api_key"] = "DO_NOT_ECHO_SYNTHETIC_SECRET"
    path = tmp_path / "bad.json"
    path.write_bytes(canonical_json_bytes(value))
    assert (
        main(["prepare", "--input", str(path), "--output", str(tmp_path / "out")]) == 2
    )
    assert not (tmp_path / "out").exists()
    assert "DO_NOT_ECHO" not in capsys.readouterr().err


def test_prepare_module_import_is_provider_and_transport_free() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import inferdrome.deployment.manual_host
assert 'inferdrome.routing_execution.transport' not in sys.modules
assert 'google.auth' not in sys.modules
assert 'google.cloud.compute_v1' not in sys.modules
""",
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
