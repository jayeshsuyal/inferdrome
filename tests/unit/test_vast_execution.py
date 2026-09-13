"""Synthetic, socket-free coverage for the additive Vast process contract."""

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

from inferdrome.qwen3_campaign import (
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest_sha256,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import (
    fixed_selected_workload_bytes,
    fixed_selected_workload_sha256,
)
from inferdrome.routing_execution.executor import (
    ExecutionError,
    ManualMonotonicClock,
    declared_input_transfer_digest,
    load_config_bytes,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.manual_host_contracts import MANIFEST_ADAPTER
from inferdrome.routing_execution.package import VerificationError
from inferdrome.routing_execution.topology import admit_topology
from inferdrome.routing_execution.vast_schema import check_schemas, schema_bytes
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import StaticEndpointTransport, config_value


def vast_config_value(*, source_commit: str = "a" * 40) -> dict[str, Any]:
    """Synthetic declarations only; never an observed Vast instance or plan."""

    value = config_value(selected_sha256=fixed_selected_workload_sha256())
    value.update(
        schema_version="inferdrome.routing-execution-config.v4",
        mode="VAST_MANUAL_CONTAINER",
        source_commit=source_commit,
        container_image={
            "reference": "example.invalid/synthetic-vast@"
            + sha256_digest(b"synthetic shared container")
        },
        artifact_provenance={
            "container_image_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
            "source_commit": source_commit,
            "observer_artifact_sha256": sha256_digest(b"synthetic observer artifact"),
            "supervisor_artifact_sha256": sha256_digest(
                b"synthetic supervisor artifact"
            ),
            "model_manifest_sha256": qwen3_model_manifest_sha256(),
            "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
            "runtime_observation_sha256": sha256_digest(
                b"synthetic process observation"
            ),
            "runtime_assertion": "LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION",
        },
        topology={
            "profile_id": "vast-container-two-h100-sxm5-80gb-v1",
            "provider": "VAST_AI",
            "provisioning": "OPERATOR_SUPPLIED_CONTAINER",
            "identity_assertion": "OPERATOR_DECLARED_NOT_OBSERVED",
            "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
            "declaration_sha256": sha256_digest(b"synthetic Vast declaration"),
            "instance_identity_sha256": sha256_digest(b"synthetic Vast instance"),
            "gpu_uuid_sha256": [
                sha256_digest(b"synthetic GPU a"),
                sha256_digest(b"synthetic GPU b"),
            ],
            "container_count": 1,
            "serving_engine_count": 2,
            "one_engine_per_endpoint": True,
            "tensor_parallel_size": 1,
            "accelerator_model": "NVIDIA H100-SXM5-80GB",
            "accelerator_count": 2,
            "isolation_boundary": "SEPARATE_PROCESSES_SHARED_CONTAINER",
            "observer_gpu_isolation": "ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED",
        },
    )
    del value["runner_image"]
    del value["serving_image"]
    for endpoint in value["endpoints"]:
        del endpoint["serving_image"]
        endpoint["container_image"] = deepcopy(value["container_image"])
    return value


def vast_config_bytes(*, source_commit: str = "a" * 40) -> bytes:
    """Bind the synthetic input transfer for a sealed injected-transport fixture."""

    value = vast_config_value(source_commit=source_commit)
    config = load_config_bytes(canonical_json_bytes(value))
    value["evidence_destination"]["declared_input_transfer_sha256"] = (
        declared_input_transfer_digest(config)
    )
    return canonical_json_bytes(value)


def seal_vast_fixture(
    output: Path, *, source_commit: str = "a" * 40, mode: str = "success"
) -> Path:
    """Produce synthetic four-file evidence without a provider, GPU or socket."""

    return run_execution_from_bytes(
        vast_config_bytes(source_commit=source_commit),
        fixed_selected_workload_bytes(),
        output,
        transport_factory=lambda: StaticEndpointTransport(mode=mode),
        clock=ManualMonotonicClock(),
    ).path


def test_v4_admits_two_loopback_process_endpoints() -> None:
    config = load_config_bytes(vast_config_bytes())
    topology = admit_topology(config)
    assert topology.config.schema_version == "inferdrome.routing-execution-config.v4"
    assert [endpoint.canonical_origin for endpoint in topology.endpoints] == [
        "http://127.0.0.1:18081",
        "http://127.0.0.1:18082",
    ]


@pytest.mark.parametrize("mode", ["success", "timeout"])
def test_v4_seals_verifies_and_preserves_receipt_meaning(
    tmp_path: Path, mode: str
) -> None:
    package = seal_vast_fixture(tmp_path / "package", mode=mode)
    verified = verify_execution_package(package)
    manifest = verified.executed_manifest.model_dump(mode="json")
    assert manifest["schema_version"] == "inferdrome.routing-executed-manifest.v4"
    assert manifest["mode"] == "VAST_MANUAL_CONTAINER"
    assert "container_image" in manifest
    assert "runner_image" not in manifest and "serving_image" not in manifest
    assert manifest["artifact_provenance"] == vast_config_value()["artifact_provenance"]
    assert len(verified.producer_receipt.terminal_outcomes) == 18
    assert all(
        row.request_denominator == 6
        for row in verified.producer_receipt.trial_summaries
    )
    assert all(
        row.endpoint_engine_reset_assertion == "NOT_ASSERTED_SEPARATE_SERVING_ENGINE"
        for row in verified.producer_receipt.reset_receipts
    )
    unavailable = [
        row
        for row in verified.producer_receipt.telemetry_observations
        if row.signal in {"GPU_DCGM", "KV_CACHE"}
    ]
    assert unavailable and all(row.state == "UNAVAILABLE" for row in unavailable)
    assert set(path.name for path in package.iterdir()) == {
        "executed-manifest.json",
        "input-transfer-receipt.json",
        "producer-receipt.json",
        "integrity-manifest.json",
    }
    raw = b"".join(path.read_bytes() for path in package.iterdir())
    assert b"127.0.0.1" not in raw and b"http://" not in raw


@pytest.mark.parametrize("version", [1, 2, 3])
def test_old_versions_reject_vast_shape(version: int) -> None:
    value = vast_config_value()
    value["schema_version"] = f"inferdrome.routing-execution-config.v{version}"
    with pytest.raises(ExecutionError):
        load_config_bytes(canonical_json_bytes(value))


@pytest.mark.parametrize(
    "origin",
    [
        "http://10.0.0.1:8000",
        "http://198.51.100.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:80",
        "https://127.0.0.1:8443",
        "http://127.0.0.1.:8000",
        "http://127.0.0.1:18081/",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000?token=secret",
    ],
)
def test_invalid_v4_origin_rejected_before_transport(
    tmp_path: Path, origin: str
) -> None:
    value = vast_config_value()
    value["endpoints"][1]["origin"] = origin
    config = load_config_bytes(canonical_json_bytes(value))
    value["evidence_destination"]["declared_input_transfer_sha256"] = (
        declared_input_transfer_digest(config)
    )
    called = False

    def forbidden_transport() -> StaticEndpointTransport:
        nonlocal called
        called = True
        raise AssertionError("invalid topology reached transport")

    with pytest.raises(ExecutionError):
        run_execution_from_bytes(
            canonical_json_bytes(value),
            fixed_selected_workload_bytes(),
            tmp_path / "package",
            transport_factory=forbidden_transport,
        )
    assert not called and not (tmp_path / "package").exists()


def test_v4_existing_cli_verifies_and_inspects_the_same_sealed_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from inferdrome.routing_execution.cli import main

    package = seal_vast_fixture(tmp_path / "package")
    expected = verify_execution_package(package).report.retained_digest
    assert main(["verify", str(package), "--expected-digest", expected]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "retained_digest": expected,
        "valid": True,
    }
    assert main(["inspect", str(package)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["retained_digest"] == expected
    assert inspected["verified"] is True
    assert len(inspected["terminal_population"]) == 3


@pytest.mark.parametrize(
    ("section", "key", "replacement"),
    [
        ("topology", "provider", "LAMBDA"),
        ("topology", "provisioning", "OPERATOR_SUPPLIED_VM"),
        ("topology", "identity_assertion", "PROVIDER_ATTESTED"),
        ("topology", "isolation_boundary", "SEPARATE_CONTAINERS"),
        ("topology", "observer_gpu_isolation", "DEVICE_ACCESS_DENIED"),
        ("topology", "lifecycle_protection", "PROVIDER_ENFORCED_TTL"),
        ("topology", "profile_id", "lambda-manual-two-h100-sxm5-80gb-v1"),
        ("topology", "accelerator_model", "NVIDIA A100-PCIE-40GB"),
        ("topology", "accelerator_count", 1),
        ("topology", "container_count", 2),
        ("topology", "tensor_parallel_size", 2),
        ("topology", "serving_engine_count", 1),
        ("topology", "one_engine_per_endpoint", False),
        ("topology", "runner_separate_from_serving", True),
        ("topology", "instance_id", "raw-provider-id"),
        ("artifact_provenance", "container_image_assertion", "LOCALLY_OBSERVED"),
        ("artifact_provenance", "source_commit", "b" * 40),
        ("artifact_provenance", "model_manifest_sha256", "sha256:" + "0" * 64),
        ("artifact_provenance", "model_snapshot_sha256", "sha256:" + "0" * 64),
        ("artifact_provenance", "runtime_observation_sha256", "not-a-digest"),
        ("artifact_provenance", "runtime_assertion", "PROVIDER_ATTESTED"),
        ("artifact_provenance", "process_environment", {"TOKEN": "secret"}),
    ],
)
def test_v4_rejects_unsupported_claims_and_provenance(
    section: str, key: str, replacement: Any
) -> None:
    value = vast_config_value()
    value[section][key] = replacement
    with pytest.raises(ExecutionError):
        load_config_bytes(canonical_json_bytes(value))


@pytest.mark.parametrize("mutation", ["duplicate-gpu", "endpoint-image", "role-pair"])
def test_v4_rejects_inconsistent_image_and_gpu_bindings(mutation: str) -> None:
    value = vast_config_value()
    if mutation == "duplicate-gpu":
        value["topology"]["gpu_uuid_sha256"][1] = value["topology"]["gpu_uuid_sha256"][
            0
        ]
    elif mutation == "endpoint-image":
        value["endpoints"][1]["container_image"] = {
            "reference": "example.invalid/other@" + sha256_digest(b"other image")
        }
    else:
        value["runner_image"] = deepcopy(value["container_image"])
        value["serving_image"] = deepcopy(value["container_image"])
    with pytest.raises(ExecutionError):
        load_config_bytes(canonical_json_bytes(value))


def test_v4_manifest_does_not_reinterpret_legacy_versions(tmp_path: Path) -> None:
    package = seal_vast_fixture(tmp_path / "package")
    value = json.loads((package / "executed-manifest.json").read_bytes())
    for version in (1, 2, 3):
        value["schema_version"] = f"inferdrome.routing-executed-manifest.v{version}"
        with pytest.raises(ValidationError):
            MANIFEST_ADAPTER.validate_json(canonical_json_bytes(value))


def test_v4_coherently_rehashed_false_isolation_claim_is_rejected(
    tmp_path: Path,
) -> None:
    package = seal_vast_fixture(tmp_path / "package")
    package.chmod(0o700)
    for path in package.iterdir():
        path.chmod(0o600)
    manifest_path = package / "executed-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["topology"]["observer_gpu_isolation"] = "DEVICE_ACCESS_DENIED"
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    receipt_path = package / "producer-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["executed_manifest_sha256"] = sha256_digest(manifest_bytes)
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    integrity_path = package / "integrity-manifest.json"
    integrity = json.loads(integrity_path.read_bytes())
    for entry in integrity["entries"]:
        content = (package / entry["path"]).read_bytes()
        entry.update(sha256=sha256_digest(content), size_bytes=len(content))
    integrity_path.write_bytes(canonical_json_bytes(integrity))
    for path in package.iterdir():
        path.chmod(0o400)
    package.chmod(0o500)
    with pytest.raises(VerificationError, match="manifest violates its contract"):
        verify_execution_package(package)


def test_v4_schema_snapshots_are_closed_and_valid() -> None:
    check_schemas()
    for content in schema_bytes().values():
        schema = json.loads(content)
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False


def test_v4_offline_verifier_does_not_import_runtime_or_transport() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import inferdrome.routing_execution.verifier; "
            "assert 'inferdrome.routing_execution.executor' not in sys.modules; "
            "assert 'inferdrome.routing_execution.transport' not in sys.modules; "
            "assert not any(name.startswith('inferdrome.deployment.vast') "
            "for name in sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
