"""TOCTOU and cache-isolation checks for the routing-execution projection."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import inferdrome.dashboard.routing_execution as execution_dashboard
import inferdrome.routing_execution.package as execution_package
from inferdrome.deployment.manual_host import ManualHostInput, prepare_artifacts
from inferdrome.errors import VerificationError
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.executor import (
    ManualMonotonicClock,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.verifier import verify_execution_package
from tests.dashboard.test_routing_execution_dashboard import _source_commit
from tests.routing_execution_support import (
    StaticEndpointTransport,
    manual_host_fixture_input,
)

_ROOT = Path(__file__).resolve().parents[2]


def _sealed_execution(output_root: Path) -> Path:
    spec = ManualHostInput.model_validate_json(
        canonical_json_bytes(manual_host_fixture_input(source_commit=_source_commit()))
    )
    files = prepare_artifacts(spec, _ROOT)
    return run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        output_root,
        transport_factory=StaticEndpointTransport,
        clock=ManualMonotonicClock(),
    ).path


def _make_tree_writable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def _mutable_copy(source: Path, destination: Path) -> Path:
    copied = Path(shutil.copytree(source, destination, copy_function=shutil.copy2))
    _make_tree_writable(copied)
    return copied


def _make_tree_immutable(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o400)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o500)
        current.chmod(0o500)


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


def test_verifier_refuses_same_file_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutable = _mutable_copy(
        _sealed_execution(tmp_path / "sealed-execution"), tmp_path / "mutable-execution"
    )
    original_read = execution_package._read
    replaced = False

    def replace_after_read(
        root: object, scanned: execution_package._ScannedFile
    ) -> bytes:
        nonlocal replaced
        content = original_read(root, scanned)
        if not replaced and scanned.name == "executed-manifest.json":
            replacement = mutable / ".manifest-replacement"
            replacement.write_bytes(content)
            replacement.replace(mutable / scanned.name)
            replaced = True
        return content

    monkeypatch.setattr(execution_package, "_read", replace_after_read)
    with pytest.raises(VerificationError, match="changed during verification"):
        verify_execution_package(mutable, require_immutable=False)
    assert replaced


def test_dashboard_projects_in_memory_snapshot_after_package_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = verify_execution_package(package).report.retained_digest
    original_loader = execution_package.verify_execution_package
    replaced = False

    def load_then_replace(
        path: Path, **kwargs: object
    ) -> execution_package.VerifiedExecutionPackage:
        nonlocal replaced
        verified = original_loader(path, **kwargs)
        _make_tree_writable(path)
        replacement = path / ".after-verified-replacement"
        replacement.write_bytes(b"{")
        replacement.replace(path / "producer-receipt.json")
        replaced = True
        return verified

    monkeypatch.setattr(
        execution_dashboard, "verify_execution_package", load_then_replace
    )
    index = execution_dashboard.RoutingExecutionDashboardIndex(
        routing_execution_root=package, expected_execution_digest=digest
    )
    response = index.refresh()
    assert replaced
    assert response.rejected == ()
    detail = index.get_execution("routing-execution-v1")
    assert detail.summary.mode == "LAMBDA_MANUAL_HOST"
    assert (
        detail.trials[0].requests[2].candidates[0].load.admissibility
        == "INADMISSIBLE"
    )


def test_dashboard_never_caches_a_failed_execution_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = verify_execution_package(package).report.retained_digest

    def reject(
        *_args: object, **_kwargs: object
    ) -> execution_package.VerifiedExecutionPackage:
        raise VerificationError("test-only verifier failure")

    monkeypatch.setattr(execution_dashboard, "verify_execution_package", reject)
    index = execution_dashboard.RoutingExecutionDashboardIndex(
        routing_execution_root=package, expected_execution_digest=digest
    )
    response = index.refresh()
    assert response.routing_executions == ()
    assert response.rejected[0].code == "VERIFICATION_FAILED"
    assert index._cache_by_digest == {}
    with pytest.raises(execution_dashboard.DashboardRoutingExecutionNotFound):
        index.get_execution("routing-execution-v1")


def test_dashboard_withholds_coherently_rehashed_non_integer_load(
    tmp_path: Path,
) -> None:
    package = _sealed_execution(tmp_path / "sealed-execution")
    digest = verify_execution_package(package).report.retained_digest
    candidate = _mutable_copy(package, tmp_path / "malformed-load")
    receipt = json.loads((candidate / "producer-receipt.json").read_bytes())
    load = next(
        row
        for row in receipt["telemetry_observations"]
        if row["signal"] == "LOAD" and row["state"] == "AVAILABLE"
    )
    load["value"] = "HEALTHY"
    decision = next(
        row
        for row in receipt["route_decisions"]
        if row["trial_id"] == load["trial_id"]
        and row["request_id"] == load["request_id"]
        and row["sequence_index"] == load["sequence_index"]
    )
    candidate_state = next(
        row
        for row in decision["candidates"]
        if row["endpoint_id"] == load["endpoint_id"]
    )
    candidate_state["load"]["value"] = "HEALTHY"
    _replace_and_rehash(
        candidate, {"producer-receipt.json": canonical_json_bytes(receipt)}
    )
    _make_tree_immutable(candidate)
    index = execution_dashboard.RoutingExecutionDashboardIndex(
        routing_execution_root=candidate, expected_execution_digest=digest
    )
    response = index.refresh()
    assert response.routing_executions == ()
    assert response.rejected[0].code == "VERIFICATION_FAILED"
    with pytest.raises(execution_dashboard.DashboardRoutingExecutionNotFound):
        index.get_execution("routing-execution-v1")
