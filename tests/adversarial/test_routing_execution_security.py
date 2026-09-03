"""Adversarial checks for PR-B source, redaction, and sealed-package boundaries."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.errors import VerificationError
from inferdrome.qwen3_campaign import qwen3_workload_prompts
from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    canonical_jsonl_bytes,
    sha256_digest,
)
from inferdrome.routing_execution.executor import (
    ExecutionError,
    ManualMonotonicClock,
    run_execution,
)
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.routing_execution_support import StaticEndpointTransport, write_inputs


def _sealed(root: Path, *, mode: str = "normal") -> Path:
    config_path, workload_path, _, _ = write_inputs(root)
    sealed = run_execution(
        config_path,
        workload_path,
        root / "package",
        transport_factory=lambda: StaticEndpointTransport(mode=mode),
        clock=ManualMonotonicClock(),
    )
    return sealed.path


def _writable(root: Path) -> None:
    root.chmod(0o700)
    for path in root.iterdir():
        path.chmod(0o600)


def _immutable(root: Path) -> None:
    for path in root.iterdir():
        if path.is_symlink():
            continue
        path.chmod(0o400, follow_symlinks=False)
    root.chmod(0o500)


def _replace_and_rehash(root: Path, replacements: dict[str, bytes]) -> None:
    for filename, content in replacements.items():
        (root / filename).write_bytes(content)
    integrity_path = root / "integrity-manifest.json"
    integrity = json.loads(integrity_path.read_bytes())
    for entry in integrity["entries"]:
        content = replacements.get(entry["path"])
        if content is not None:
            entry["sha256"] = sha256_digest(content)
            entry["size_bytes"] = len(content)
    integrity_path.write_bytes(canonical_json_bytes(integrity))


def test_package_redacts_raw_endpoint_prompt_and_verifies_offline(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    package_bytes = b"".join(path.read_bytes() for path in package.iterdir())
    assert qwen3_workload_prompts()[0].encode("utf-8") not in package_bytes
    assert b"127.0.0.1:18081" not in package_bytes
    assert b"http://" not in package_bytes
    verified = verify_execution_package(package)
    assert verified.report.execution_id == "routing-execution-v1"


def test_verifier_rejects_symlink_hardlink_and_coherently_rehashed_sensitive_field(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    link_copy = tmp_path / "link-copy"
    shutil.copytree(package, link_copy, copy_function=shutil.copy2)
    _writable(link_copy)
    (link_copy / "input-transfer-receipt.json").unlink()
    os.symlink("producer-receipt.json", link_copy / "input-transfer-receipt.json")
    _immutable(link_copy)
    with pytest.raises(VerificationError):
        verify_execution_package(link_copy)

    hard_copy = tmp_path / "hard-copy"
    shutil.copytree(package, hard_copy, copy_function=shutil.copy2)
    _writable(hard_copy)
    (hard_copy / "input-transfer-receipt.json").unlink()
    os.link(
        hard_copy / "producer-receipt.json",
        hard_copy / "input-transfer-receipt.json",
    )
    _immutable(hard_copy)
    with pytest.raises(VerificationError):
        verify_execution_package(hard_copy)

    redaction_copy = tmp_path / "redaction-copy"
    shutil.copytree(package, redaction_copy, copy_function=shutil.copy2)
    _writable(redaction_copy)
    receipt_path = redaction_copy / "producer-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["origin"] = "https://198.51.100.9:8443"
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_path.write_bytes(receipt_bytes)
    integrity_path = redaction_copy / "integrity-manifest.json"
    integrity = json.loads(integrity_path.read_bytes())
    for entry in integrity["entries"]:
        if entry["path"] == "producer-receipt.json":
            entry["sha256"] = sha256_digest(receipt_bytes)
            entry["size_bytes"] = len(receipt_bytes)
    integrity_path.write_bytes(canonical_json_bytes(integrity))
    _immutable(redaction_copy)
    with pytest.raises(VerificationError):
        verify_execution_package(redaction_copy)

    parent_link = tmp_path / "package-parent-link"
    os.symlink(".", parent_link)
    with pytest.raises(VerificationError):
        verify_execution_package(parent_link / package.name)


def test_source_symlink_and_malformed_metrics_fail_closed_without_fabrication(
    tmp_path: Path,
) -> None:
    config_path, workload_path, _, _ = write_inputs(tmp_path)
    config_path.unlink()
    os.symlink("workload.jsonl", config_path)
    with pytest.raises(ExecutionError):
        run_execution(
            config_path,
            workload_path,
            tmp_path / "unsafe-package",
            transport_factory=StaticEndpointTransport,
        )

    safe_root = tmp_path / "malformed"
    safe_root.mkdir(mode=0o700)
    package = _sealed(safe_root, mode="malformed_metrics")
    receipt = verify_execution_package(package).producer_receipt
    load_rows = [row for row in receipt.telemetry_observations if row.signal == "LOAD"]
    assert load_rows
    assert all(row.value == "UNAVAILABLE" for row in load_rows)
    assert all(row.admissibility == "INADMISSIBLE" for row in load_rows)
    assert all(row.selected_endpoint_id is None for row in receipt.route_decisions)
    assert all(row.status == "NO_SAFE_ROUTE" for row in receipt.terminal_outcomes)


def test_verifier_detects_post_read_package_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _sealed(tmp_path)
    import inferdrome.routing_execution.package as package_module

    original = package_module._read
    replaced = False

    def mutate(scan: object, scanned: object) -> bytes:
        nonlocal replaced
        value = original(scan, scanned)  # type: ignore[arg-type]
        if not replaced:
            replaced = True
            _writable(package)
            replacement = package / ".replacement"
            replacement.write_bytes((package / "executed-manifest.json").read_bytes())
            os.replace(replacement, package / "executed-manifest.json")
            _immutable(package)
        return value

    monkeypatch.setattr(package_module, "_read", mutate)
    with pytest.raises(VerificationError):
        verify_execution_package(package)


def test_source_parent_symlink_and_input_transfer_tamper_stop_before_transport(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real-inputs"
    real.mkdir(mode=0o700)
    config_path, workload_path, config, _ = write_inputs(real)
    link = tmp_path / "linked-inputs"
    os.symlink(real.name, link)
    factory_calls = 0

    def factory() -> StaticEndpointTransport:
        nonlocal factory_calls
        factory_calls += 1
        return StaticEndpointTransport()

    with pytest.raises(ExecutionError):
        run_execution(
            link / config_path.name,
            link / workload_path.name,
            tmp_path / "symlink-package",
            transport_factory=factory,
        )
    assert factory_calls == 0

    destination = config["evidence_destination"]
    assert isinstance(destination, dict)
    destination["declared_input_transfer_sha256"] = "sha256:" + ("f" * 64)
    config_path.write_bytes(canonical_json_bytes(config))
    with pytest.raises(ExecutionError):
        run_execution(
            config_path,
            workload_path,
            tmp_path / "transfer-tamper-package",
            transport_factory=factory,
        )
    assert factory_calls == 0


def test_held_evidence_root_cannot_publish_into_same_user_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root rename after reservation cannot yield a returned replacement receipt."""

    tmp_path.chmod(0o700)
    config_path, workload_path, _, _ = write_inputs(tmp_path)
    displaced = tmp_path.parent / f"{tmp_path.name}-displaced"
    import inferdrome.routing_execution.package as package_module

    original_publish = package_module.EvidenceReservation.publish
    swapped = False

    def replace_root_before_publish(
        self: object, *args: object, **kwargs: object
    ) -> object:
        nonlocal swapped
        assert not swapped
        swapped = True
        tmp_path.rename(displaced)
        tmp_path.mkdir(mode=0o700)
        return original_publish(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        package_module.EvidenceReservation, "publish", replace_root_before_publish
    )
    held_root = SafeDirFD.open(tmp_path)
    try:
        with pytest.raises(ExecutionError, match="evidence sealing failed"):
            run_execution(
                config_path,
                workload_path,
                tmp_path / "package",
                transport_factory=StaticEndpointTransport,
                clock=ManualMonotonicClock(),
                evidence_parent=held_root,
            )
    finally:
        held_root.close()

    assert swapped
    assert not (tmp_path / "package").exists()
    assert (displaced / "package").is_dir()


def test_fixed_workload_and_published_identifier_are_not_config_injection_paths(
    tmp_path: Path,
) -> None:
    config_path, workload_path, config, _ = write_inputs(tmp_path)
    workload_path.write_bytes(
        canonical_jsonl_bytes({"prompt": f"arbitrary-{index}"} for index in range(6))
    )
    factory_calls = 0

    def factory() -> StaticEndpointTransport:
        nonlocal factory_calls
        factory_calls += 1
        return StaticEndpointTransport()

    with pytest.raises(ExecutionError):
        run_execution(
            config_path,
            workload_path,
            tmp_path / "wrong-workload-package",
            transport_factory=factory,
        )
    assert factory_calls == 0

    config["execution_id"] = "203.0.113.1:8443"
    config_path.write_bytes(canonical_json_bytes(config))
    with pytest.raises(ExecutionError):
        run_execution(
            config_path,
            workload_path,
            tmp_path / "unsafe-identifier-package",
            transport_factory=factory,
        )
    assert factory_calls == 0


def test_verifier_rejects_coherently_rehashed_semantic_selection_tamper(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    copy = tmp_path / "semantic-copy"
    shutil.copytree(package, copy, copy_function=shutil.copy2)
    _writable(copy)
    receipt_path = copy / "producer-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    decision = receipt["route_decisions"][0]
    terminal_id = decision["terminal_outcome_id"]
    decision["selected_endpoint_id"] = "endpoint-a"
    for terminal in receipt["terminal_outcomes"]:
        if terminal["terminal_outcome_id"] == terminal_id:
            terminal["selected_endpoint_id"] = "endpoint-a"
            break
    receipt_bytes = canonical_json_bytes(receipt)
    _replace_and_rehash(copy, {"producer-receipt.json": receipt_bytes})
    _immutable(copy)
    with pytest.raises(VerificationError):
        verify_execution_package(copy)


def test_verifier_rejects_coherently_rehashed_fault_and_terminal_time_tamper(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    fault_copy = tmp_path / "fault-time-copy"
    shutil.copytree(package, fault_copy, copy_function=shutil.copy2)
    _writable(fault_copy)
    receipt = json.loads((fault_copy / "producer-receipt.json").read_bytes())
    receipt["fault_receipts"][0]["activated_at_monotonic_ns"] = 0
    _replace_and_rehash(
        fault_copy, {"producer-receipt.json": canonical_json_bytes(receipt)}
    )
    _immutable(fault_copy)
    with pytest.raises(VerificationError):
        verify_execution_package(fault_copy)

    terminal_copy = tmp_path / "terminal-time-copy"
    shutil.copytree(package, terminal_copy, copy_function=shutil.copy2)
    _writable(terminal_copy)
    receipt = json.loads((terminal_copy / "producer-receipt.json").read_bytes())
    terminal = next(
        row for row in receipt["terminal_outcomes"] if row["sequence_index"] == 1
    )
    terminal["started_at_monotonic_ns"] = 0
    _replace_and_rehash(
        terminal_copy, {"producer-receipt.json": canonical_json_bytes(receipt)}
    )
    _immutable(terminal_copy)
    with pytest.raises(VerificationError):
        verify_execution_package(terminal_copy)


def test_verifier_rejects_coherently_rehashed_unsupported_mode_topology(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    copy = tmp_path / "topology-copy"
    shutil.copytree(package, copy, copy_function=shutil.copy2)
    _writable(copy)
    manifest = json.loads((copy / "executed-manifest.json").read_bytes())
    manifest["mode"] = "GCP_PRIVATE"
    manifest_bytes = canonical_json_bytes(manifest)
    receipt = json.loads((copy / "producer-receipt.json").read_bytes())
    receipt["executed_manifest_sha256"] = sha256_digest(manifest_bytes)
    _replace_and_rehash(
        copy,
        {
            "executed-manifest.json": manifest_bytes,
            "producer-receipt.json": canonical_json_bytes(receipt),
        },
    )
    _immutable(copy)
    with pytest.raises(VerificationError):
        verify_execution_package(copy)


def test_verifier_rejects_coherently_rehashed_transfer_config_mismatch(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    copy = tmp_path / "transfer-config-copy"
    shutil.copytree(package, copy, copy_function=shutil.copy2)
    _writable(copy)
    transfer = json.loads((copy / "input-transfer-receipt.json").read_bytes())
    transfer["config_sha256"] = "sha256:" + ("f" * 64)
    transfer_bytes = canonical_json_bytes(transfer)
    manifest = json.loads((copy / "executed-manifest.json").read_bytes())
    manifest["input_transfer_receipt_sha256"] = sha256_digest(transfer_bytes)
    manifest_bytes = canonical_json_bytes(manifest)
    receipt = json.loads((copy / "producer-receipt.json").read_bytes())
    receipt["executed_manifest_sha256"] = sha256_digest(manifest_bytes)
    receipt["input_transfer_receipt_sha256"] = sha256_digest(transfer_bytes)
    _replace_and_rehash(
        copy,
        {
            "input-transfer-receipt.json": transfer_bytes,
            "executed-manifest.json": manifest_bytes,
            "producer-receipt.json": canonical_json_bytes(receipt),
        },
    )
    _immutable(copy)
    with pytest.raises(VerificationError):
        verify_execution_package(copy)


def test_verifier_rejects_coherently_rehashed_stale_load_with_fresh_age(
    tmp_path: Path,
) -> None:
    package = _sealed(tmp_path)
    copy = tmp_path / "stale-age-copy"
    shutil.copytree(package, copy, copy_function=shutil.copy2)
    _writable(copy)
    receipt = json.loads((copy / "producer-receipt.json").read_bytes())
    row = next(
        item
        for item in receipt["telemetry_observations"]
        if item["signal"] == "LOAD"
        and item["sequence_index"] == 2
        and item["state"] == "STALE"
    )
    row["decision_at_monotonic_ns"] = row["sampled_at_monotonic_ns"] + 1
    row["age_ns"] = 1
    _replace_and_rehash(copy, {"producer-receipt.json": canonical_json_bytes(receipt)})
    _immutable(copy)
    with pytest.raises(VerificationError):
        verify_execution_package(copy)

    bound_copy = tmp_path / "stale-bound-copy"
    shutil.copytree(package, bound_copy, copy_function=shutil.copy2)
    _writable(bound_copy)
    receipt = json.loads((bound_copy / "producer-receipt.json").read_bytes())
    row = next(
        item
        for item in receipt["telemetry_observations"]
        if item["signal"] == "LOAD"
        and item["sequence_index"] == 2
        and item["state"] == "STALE"
    )
    row["freshness_bound_ns"] = 0
    decision = next(
        item
        for item in receipt["route_decisions"]
        if item["trial_id"] == row["trial_id"]
        and item["request_id"] == row["request_id"]
        and item["sequence_index"] == row["sequence_index"]
    )
    candidate = next(
        item
        for item in decision["candidates"]
        if item["endpoint_id"] == row["endpoint_id"]
    )
    candidate["load"]["freshness_bound_ns"] = 0
    _replace_and_rehash(
        bound_copy, {"producer-receipt.json": canonical_json_bytes(receipt)}
    )
    _immutable(bound_copy)
    with pytest.raises(VerificationError):
        verify_execution_package(bound_copy)
