"""Synthetic SFTP v2 contract/admission checks; no account or network actions."""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from inferdrome.deployment import vast_bootstrap as boot
from inferdrome.deployment import vast_guest_ssh as guest_ssh
from inferdrome.deployment import vast_process as prep
from inferdrome.deployment import vast_provider as provider
from inferdrome.deployment.vast_process_schema import artifacts
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.unit.test_vast_bootstrap import launch_intent
from tests.unit.test_vast_guest_ssh import public_key


def sftp_launch_intent() -> boot.SftpLaunchIntent:
    value = launch_intent().model_dump(mode="json")
    value.update(
        schema_version="inferdrome.vast-launch-intent.v2",
        module_sha256=prep.module_digests("v2"),
        transfer_prerequisites={
            "client_public_key": public_key(3),
            "host_key_source": "VAST_AUTHENTICATED_EXACT_ID_LOGS",
            "assertion": "VAST_CONTROL_PLANE_LOG_BINDING_NOT_HARDWARE_ATTESTATION",
        },
        disk={
            "requested_gb": 200,
            "image_unpacked_bytes": 60_000_000_000,
            "runtime_headroom_bytes": 1_073_741_824,
            "measurement_sha256": sha256_digest(b"synthetic measured footprint"),
            "assertion": "OPERATOR_REVIEWED_IMAGE_FOOTPRINT_NOT_PROVIDER_ATTESTED",
        },
    )
    return boot.SftpLaunchIntent.model_validate_json(canonical_json_bytes(value))


def sftp_readback() -> prep.VastSftpLaunchReadback:
    return prep.VastSftpLaunchReadback.model_validate_json(
        canonical_json_bytes(
            {
                "instance_id": 101,
                "requested_image": sftp_launch_intent().container_image.model_dump(
                    mode="json"
                ),
                "launch_mode": "args",
                "user": "0:0",
                "transfer_uid": 2001,
                "transfer_gid": 0,
                "public_port_mappings": [
                    {
                        "purpose": "SSH_MANAGEMENT",
                        "container_port": 2222,
                        "protocol": "tcp",
                        "public_host": "8.8.8.8",
                        "public_port": 2244,
                    }
                ],
                "persistent_volume_ids": [],
                "assertion": "OPERATOR_SUPPLIED_NOT_PROVIDER_ATTESTED",
            }
        )
    )


def test_compiler_has_exact_closed_root_management_wire_and_bound_arguments() -> None:
    intent = sftp_launch_intent()
    wire = provider.compiled_create_bytes(intent)
    assert json.loads(wire) == {
        "image": intent.container_image.reference,
        "disk": 200,
        "runtype": "args",
        "target_state": "running",
        "user": "0:0",
        "env": {"-p 2222:2222": "1"},
        "args": [
            "serve",
            "--run-nonce",
            intent.run_nonce,
            "--intent-sha256",
            boot.record_digest(intent),
            "--execution-deadline",
            intent.execution_deadline_utc,
            "--cleanup-deadline",
            intent.cleanup_deadline_utc,
            "--client-public-key",
            public_key(3),
        ],
    }
    assert wire == canonical_json_bytes(json.loads(wire))
    assert provider.compiled_create_sha256(intent) == sha256_digest(wire)
    assert "broker" not in wire.decode()


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_gb", None),
        ("requested_gb", True),
        ("requested_gb", 1),
        ("requested_gb", 2001),
        ("image_unpacked_bytes", 0),
        ("runtime_headroom_bytes", 1),
        ("measurement_sha256", "unknown"),
    ],
)
def test_invalid_or_unmeasured_disk_cannot_compile(field: str, value: Any) -> None:
    raw = sftp_launch_intent().model_dump(mode="json")
    raw["disk"][field] = value
    with pytest.raises(ValidationError):
        boot.SftpLaunchIntent.model_validate_json(canonical_json_bytes(raw))


def test_missing_disk_and_forged_model_bypass_fail_before_compilation() -> None:
    intent = sftp_launch_intent()
    raw = intent.model_dump(mode="json")
    raw.pop("disk")
    with pytest.raises(ValidationError):
        boot.SftpLaunchIntent.model_validate_json(canonical_json_bytes(raw))
    forged = intent.model_copy(
        update={"disk": intent.disk.model_copy(update={"requested_gb": 1})}
    )
    with pytest.raises(ValidationError):
        provider.compiled_create_bytes(forged)


def test_disk_math_includes_two_snapshots_largest_scratch_and_headroom() -> None:
    intent = sftp_launch_intent()
    raw = intent.disk.model_dump(mode="json")
    sizes = [item["size_bytes"] for item in boot.qwen3_model_manifest()["files"]]
    need = (
        raw["image_unpacked_bytes"]
        + 2 * sum(sizes)
        + max(sizes)
        + raw["runtime_headroom_bytes"]
    )
    minimum = (need + 999_999_999) // 1_000_000_000
    raw["requested_gb"] = minimum
    assert boot.DiskFootprint.model_validate(raw).requested_gb == minimum
    raw["requested_gb"] -= 1
    with pytest.raises(ValidationError):
        boot.DiskFootprint.model_validate(raw)


@pytest.mark.parametrize("seconds", [0, 1, 9, 601])
def test_management_cleanup_reserve_is_ten_to_six_hundred(seconds: int) -> None:
    raw = sftp_launch_intent().model_dump(mode="json")
    raw["cleanup_deadline_utc"] = (
        prep.parse_deadline(raw["execution_deadline_utc"]) + timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValidationError):
        boot.SftpLaunchIntent.model_validate_json(canonical_json_bytes(raw))


@pytest.mark.parametrize("kind", ["legacy", "missing", "extra"])
def test_v2_module_inventory_cannot_silently_reuse_legacy_artifacts(kind: str) -> None:
    raw = sftp_launch_intent().model_dump(mode="json")
    if kind == "legacy":
        raw["module_sha256"] = prep.module_digests()
    elif kind == "missing":
        raw["module_sha256"].pop("vast_guest_ssh")
    else:
        raw["module_sha256"]["unknown"] = sha256_digest(b"extra")
    with pytest.raises(ValidationError):
        boot.SftpLaunchIntent.model_validate_json(canonical_json_bytes(raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("public_host", "127.0.0.1"),
        ("public_host", "10.0.0.1"),
        ("public_host", "008.8.8.8"),
        ("public_host", "example.invalid"),
        ("public_host", "224.0.0.1"),
        ("public_port", 0),
        ("public_port", True),
        ("container_port", 8000),
        ("protocol", "udp"),
    ],
)
def test_mapping_rejects_inference_ports_and_noncanonical_public_endpoint(
    field: str,
    value: Any,
) -> None:
    raw = sftp_readback().model_dump(mode="json")
    raw["public_port_mappings"][0][field] = value
    with pytest.raises(ValidationError):
        prep.VastSftpLaunchReadback.model_validate_json(canonical_json_bytes(raw))


@pytest.mark.parametrize("count", [0, 2])
def test_readback_requires_exactly_one_management_mapping(count: int) -> None:
    raw = sftp_readback().model_dump(mode="json")
    raw["public_port_mappings"] *= count
    with pytest.raises(ValidationError):
        prep.VastSftpLaunchReadback.model_validate_json(canonical_json_bytes(raw))


def test_legacy_models_reject_root_sftp_facts_and_v2_prerequisites() -> None:
    with pytest.raises(ValidationError):
        prep.VastLaunchReadback.model_validate_json(boot.record_bytes(sftp_readback()))
    raw = sftp_launch_intent().model_dump(mode="json")
    raw["schema_version"] = "inferdrome.vast-launch-intent.v1"
    with pytest.raises(ValidationError):
        boot.LaunchIntent.model_validate_json(canonical_json_bytes(raw))
    old_wire = json.loads(provider.compiled_create_bytes(launch_intent()))
    assert old_wire["user"] == "2000:0" and old_wire["env"] == {}
    assert old_wire["args"][0] == "bootstrap" and old_wire["disk"] == 80


def test_group_readable_export_publication_survives_private_guest_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = sftp_launch_intent()
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    downloads = exchange / "downloads"
    downloads.mkdir(mode=0o750)
    downloads.chmod(0o750)
    monkeypatch.setattr(guest_ssh, "require_experiment_identity", lambda: None)
    monkeypatch.setattr(
        boot,
        "observe_guest",
        lambda *a, **kw: {
            "instance_id": 101,
            "source_commit": intent.source_commit,
            "module_sha256": intent.module_sha256,
            "gpu_uuids": [
                "GPU-" + "1" * 8 + "-1111-1111-1111-" + "1" * 12,
                "GPU-" + "2" * 8 + "-2222-2222-2222-" + "2" * 12,
            ],
            "uid": 2000,
            "gid": 0,
        },
    )
    guest = boot.GuestBootstrap(
        intent.run_nonce,
        boot.record_digest(intent),
        intent.execution_deadline_utc,
        root=tmp_path / "guest",
        profile="v2",
        exchange=exchange,
    )
    old_umask = os.umask(0o077)
    try:
        guest.initialize()
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE((downloads / "export").stat().st_mode) == 0o750
    assert stat.S_IMODE((downloads / "ready.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((guest.root / "private").stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o660])
def test_upload_admission_rejects_wrong_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    monkeypatch.setattr(boot, "_UPLOAD_UID", os.getuid())
    monkeypatch.setattr(boot, "_UPLOAD_GID", os.getgid())
    tmp_path.chmod(0o750)
    target = tmp_path / "intent.json"
    target.write_bytes(b"synthetic")
    target.chmod(mode)
    with pytest.raises(boot.BootstrapFailure, match="UPLOAD_INVALID"):
        boot._upload_bytes(tmp_path, "intent.json")
    target.chmod(0o640)
    assert boot._upload_bytes(tmp_path, "intent.json") == b"synthetic"


def test_v2_identity_guard_runs_before_any_guest_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = sftp_launch_intent()
    root = tmp_path / "guest"

    def refuse() -> None:
        raise guest_ssh.StartupFailure("VAST_EXPERIMENT_IDENTITY_INVALID")

    monkeypatch.setattr(guest_ssh, "require_experiment_identity", refuse)
    guest = boot.GuestBootstrap(
        intent.run_nonce,
        boot.record_digest(intent),
        intent.execution_deadline_utc,
        root=root,
        profile="v2",
    )
    with pytest.raises(guest_ssh.StartupFailure, match="EXPERIMENT_IDENTITY_INVALID"):
        guest.initialize()
    assert not root.exists()


def test_v1_schema_and_template_bytes_remain_frozen_before_sftp_v2() -> None:
    # Recorded from pre-SFTP head 8d096270979bca55eab112f067f2122b9ae835b2.
    expected = {
        "bootstrap-approval": (
            "1c248beac439293329d64429c494829454ca97dcef7fdddb2bcc029e10901d5c"
        ),
        "bootstrap-ready": (
            "fa70ad9a1a96c934dd162aedaef8b6ced5d3385506dc271d9ad630b581ba296f"
        ),
        "bootstrap-result": (
            "25143a2785fc1713547fcb108e5d25b9d8b3551c18585f49b171e6714551bc0a"
        ),
        "bootstrap-stage": (
            "25104909c09610c86e3cb543c952c72731fb70cd34a79a2533123137dc841877"
        ),
        "control-intent": (
            "96ae3772413b870f3f2aca2f69205a7aac28672fcaa8ae5f500df2bdaf0618fc"
        ),
        "destroy-readback": (
            "102843a2feff5fffcc3a08c6806ecae39eb55d4338dc61b452475f66c6cf2853"
        ),
        "input": ("7ccab1951c0b044b1973ed9a853162e9b4a9f164982543ed45d17f4be55750f5"),
        "launch-intent": (
            "1dae9940715e8921a6e2bde200f623aca49a818be4377940ea8fcc1b81d68b59"
        ),
    }
    generated = artifacts()
    repository = Path(__file__).resolve().parents[2]
    for name, digest in expected.items():
        path = f"schemas/vast-process/v1/{name}.schema.json"
        assert sha256_digest(generated[path]) == "sha256:" + digest
        assert (repository / path).read_bytes() == generated[path]
    path = "deployments/vast-process-v1/input-template.json"
    assert sha256_digest(generated[path]) == (
        "sha256:8b673394b4ed94c1a4cb7af68d7253dfe7437ee452a3558d22cda3e6e3faeedc"
    )
    assert (repository / path).read_bytes() == generated[path]


def test_v2_staging_wait_does_not_consume_the_operator_approval_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = sftp_launch_intent().model_copy(
        update={
            "stage_timeout_seconds": 60,
            "approval_timeout_seconds": 10,
        }
    )
    ready = boot.SftpReady.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.vast-bootstrap-ready.v2",
                "run_nonce": intent.run_nonce,
                "intent_sha256": boot.record_digest(intent),
                "execution_deadline_utc": intent.execution_deadline_utc,
                "instance_id": 101,
                "source_commit": intent.source_commit,
                "module_sha256": intent.module_sha256,
                "gpu_uuids": [
                    "GPU-11111111-1111-1111-1111-111111111111",
                    "GPU-22222222-2222-2222-2222-222222222222",
                ],
                "uid": 2000,
                "gid": 0,
                "assertion": "LOCAL_OBSERVATIONS_NOT_PROVIDER_ATTESTATION",
            }
        )
    )
    readback = sftp_readback()
    plan = boot.make_plan(intent, ready, readback)
    elapsed = [0.0]
    receives: list[tuple[str, float]] = []
    approvals: list[tuple[bytes, float]] = []

    def approve(content: bytes, seconds: float) -> str:
        approvals.append((content, seconds))
        return boot.record_digest(plan)

    workflow = boot.BootstrapWorkflow(
        intent,
        lambda _: None,
        tmp_path / "operator",
        None,
        lambda _: readback,
        approve,
    )
    workflow.ready_record = ready
    monkeypatch.setattr(workflow, "_bind", lambda _: None)
    workflow._transfer = SimpleNamespace(send=lambda *args: None)
    workflow.budget = SimpleNamespace(remaining=lambda: 3000 - elapsed[0])
    monkeypatch.setattr(boot, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))

    def receive(name: str, seconds: float) -> None:
        receives.append((name, seconds))
        assert name == "plan.json" and seconds > 30
        elapsed[0] += 30
        boot.write_record(workflow.local, name, boot.record_bytes(plan))

    monkeypatch.setattr(workflow, "_receive_control", receive)
    workflow.stage(101, seconds=3000)
    assert receives == [("plan.json", 60)]
    workflow.approve(101, seconds=3000)
    assert receives == [("plan.json", 60)]
    assert approvals == [(boot.record_bytes(plan), 10)]
    assert workflow.plan == plan
