"""Coherently rehashed vLLM evidence still cannot cross semantic boundaries."""

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from inferdrome.bundle import verify_bundle
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import sha256_digest
from inferdrome.errors import VerificationError
from tests.conftest import SealedVllmFixture


def _mutable_copy(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    for directory, directory_names, filenames in os.walk(destination):
        current = Path(directory)
        current.chmod(0o700)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        for filename in filenames:
            (current / filename).chmod(0o600)
    return destination


def _rehash_manifest_entries(bundle: Path, relative_paths: set[str]) -> None:
    manifest_path = bundle / "integrity" / "artifact-hashes.json"
    manifest = json.loads(manifest_path.read_bytes())
    remaining = set(relative_paths)
    for entry in manifest["entries"]:
        relative_path = entry["path"]
        if relative_path not in remaining:
            continue
        content = (bundle / relative_path).read_bytes()
        entry["size_bytes"] = len(content)
        entry["sha256"] = f"sha256:{hashlib.sha256(content).hexdigest()}"
        remaining.remove(relative_path)
    assert not remaining
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def _cohere_native_hash_references(bundle: Path) -> None:
    native_bytes = (bundle / "native" / "benchmark-result.json").read_bytes()
    execution_path = bundle / "execution.json"
    execution: dict[str, Any] = json.loads(execution_path.read_bytes())
    execution["native_result_sha256"] = sha256_digest(native_bytes)
    execution_bytes = canonical_json_bytes(execution)
    execution_path.write_bytes(execution_bytes)

    measurements_path = bundle / "derived" / "measurements.json"
    measurements: dict[str, Any] = json.loads(measurements_path.read_bytes())
    measurements["execution_sha256"] = sha256_digest(execution_bytes)
    measurements_path.write_bytes(canonical_json_bytes(measurements))
    _rehash_manifest_entries(
        bundle,
        {
            "native/benchmark-result.json",
            "execution.json",
            "derived/measurements.json",
        },
    )


def test_rehashed_native_latency_cannot_override_canonical_records(
    sealed_vllm_bundle: SealedVllmFixture,
    tmp_path: Path,
) -> None:
    bundle = _mutable_copy(
        sealed_vllm_bundle.sealed.path,
        tmp_path / "changed-native-latency",
    )
    native_path = bundle / "native" / "benchmark-result.json"
    native = json.loads(native_path.read_bytes())
    native["ttfts"][0] += 0.001
    native_path.write_bytes(
        json.dumps(native, separators=(",", ":"), allow_nan=False).encode()
    )
    _cohere_native_hash_references(bundle)

    with pytest.raises(VerificationError, match="canonical records"):
        verify_bundle(bundle, require_immutable=False)


def test_rehashed_unknown_native_shape_is_not_guessed(
    sealed_vllm_bundle: SealedVllmFixture,
    tmp_path: Path,
) -> None:
    bundle = _mutable_copy(
        sealed_vllm_bundle.sealed.path,
        tmp_path / "unknown-native-shape",
    )
    native_path = bundle / "native" / "benchmark-result.json"
    native = json.loads(native_path.read_bytes())
    native["new_upstream_field"] = "unknown"
    native_path.write_bytes(
        json.dumps(native, separators=(",", ":"), allow_nan=False).encode()
    )
    _cohere_native_hash_references(bundle)

    with pytest.raises(VerificationError, match="normalization failed"):
        verify_bundle(bundle, require_immutable=False)


def test_rehashed_invocation_cannot_change_configured_concurrency(
    sealed_vllm_bundle: SealedVllmFixture,
    tmp_path: Path,
) -> None:
    bundle = _mutable_copy(
        sealed_vllm_bundle.sealed.path,
        tmp_path / "changed-invocation",
    )
    invocation_path = bundle / "native" / "invocation.json"
    invocation = json.loads(invocation_path.read_bytes())
    argv = invocation["argv"]
    argv[argv.index("--max-concurrency") + 1] = "99"
    invocation_path.write_bytes(canonical_json_bytes(invocation))
    _rehash_manifest_entries(bundle, {"native/invocation.json"})

    with pytest.raises(VerificationError, match="invocation evidence"):
        verify_bundle(bundle, require_immutable=False)


def test_rehashed_invocation_cannot_separate_preflight_result_from_response(
    sealed_vllm_bundle: SealedVllmFixture,
    tmp_path: Path,
) -> None:
    bundle = _mutable_copy(
        sealed_vllm_bundle.sealed.path,
        tmp_path / "changed-preflight-result",
    )
    invocation_path = bundle / "native" / "invocation.json"
    invocation = json.loads(invocation_path.read_bytes())
    invocation["endpoint_preflight"]["result"]["status"] = 201
    invocation_path.write_bytes(canonical_json_bytes(invocation))
    _rehash_manifest_entries(bundle, {"native/invocation.json"})

    with pytest.raises(VerificationError, match="invocation evidence"):
        verify_bundle(bundle, require_immutable=False)


@pytest.mark.parametrize(
    ("field_name", "mutation", "message"),
    [
        ("server.model_id", {"value": "different/model"}, "server model"),
        (
            "target.model_revision",
            {"value": "different-revision"},
            "target identity",
        ),
        (
            "producer.version",
            {"evidence_path": "native/invocation.json"},
            "producer version evidence",
        ),
        (
            "client.os",
            {"evidence_path": "missing/evidence.json"},
            "not in the bundle",
        ),
    ],
)
def test_rehashed_environment_claims_remain_cross_bound(
    sealed_vllm_bundle: SealedVllmFixture,
    tmp_path: Path,
    field_name: str,
    mutation: dict[str, str],
    message: str,
) -> None:
    bundle = _mutable_copy(
        sealed_vllm_bundle.sealed.path,
        tmp_path / field_name.replace(".", "-"),
    )
    environment_path = bundle / "environment.json"
    environment = json.loads(environment_path.read_bytes())
    field = next(
        item for item in environment["fields"] if item["name"] == field_name
    )
    field.update(mutation)
    environment_path.write_bytes(canonical_json_bytes(environment))
    _rehash_manifest_entries(bundle, {"environment.json"})

    with pytest.raises(VerificationError, match=message):
        verify_bundle(bundle, require_immutable=False)


def test_rehashed_version_artifact_cannot_claim_another_producer(
    sealed_vllm_bundle: SealedVllmFixture,
    tmp_path: Path,
) -> None:
    bundle = _mutable_copy(
        sealed_vllm_bundle.sealed.path,
        tmp_path / "changed-version",
    )
    version_path = bundle / "native" / "producer-version.txt"
    version_path.write_bytes(b"0.27.0\n")
    _rehash_manifest_entries(bundle, {"native/producer-version.txt"})

    with pytest.raises(VerificationError, match="producer version"):
        verify_bundle(bundle, require_immutable=False)
