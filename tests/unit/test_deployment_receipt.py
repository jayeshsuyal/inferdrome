"""Strict outer deployment receipt, provenance, and publication coverage."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

import inferdrome.immutable as immutable
from inferdrome.deployment import (
    LOCAL_PROVIDER_ADAPTER_ID,
    LOCAL_PROVIDER_ADAPTER_VERSION,
    LOCAL_RUNTIME_ADAPTER_ID,
    LOCAL_RUNTIME_ADAPTER_VERSION,
    CleanupResult,
    DeploymentReceipt,
    LifecycleCoordinator,
    LifecycleErrorCode,
    LifecycleStatus,
    LocalMockRuntimeAdapter,
    LocalProviderAdapter,
    ReceiptAdapterIdentity,
    ReceiptEvidenceAnchor,
    ReceiptPublicationError,
    canonical_deployment_receipt_bytes,
    deployment_receipt_id,
    deployment_receipt_schema,
    deployment_receipt_sha256,
    issue_synthetic_local_receipt,
    parse_deployment_receipt_json,
    parse_deployment_spec_json,
    publish_deployment_receipt,
    verify_deployment_receipt,
    verify_deployment_receipt_bytes,
    verify_published_deployment_receipt,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import sha256_digest
from inferdrome.errors import VerificationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPOSITORY_ROOT / "deployments" / "v1" / "examples"
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "schemas"
    / "deployment"
    / "v1"
    / "deployment-receipt.schema.json"
)
V1_FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "receipt-v1"
    / "local-synthetic-receipt.json"
)
V2_FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "receipt-v2"
    / "local-synthetic-receipt.json"
)
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
PROVIDER_IDENTITY = ReceiptAdapterIdentity(
    adapter_id=LOCAL_PROVIDER_ADAPTER_ID,
    adapter_version=LOCAL_PROVIDER_ADAPTER_VERSION,
)
RUNTIME_IDENTITY = ReceiptAdapterIdentity(
    adapter_id=LOCAL_RUNTIME_ADAPTER_ID,
    adapter_version=LOCAL_RUNTIME_ADAPTER_VERSION,
)


def _spec():
    return parse_deployment_spec_json(
        (EXAMPLE_ROOT / "local-mock.json").read_bytes()
    )


def _outcome(spec=None):
    return LifecycleCoordinator(
        LocalProviderAdapter(), LocalMockRuntimeAdapter()
    ).run(spec or _spec(), lambda endpoint: None)


def _receipt():
    spec = _spec()
    outcome = _outcome(spec)
    receipt = issue_synthetic_local_receipt(
        spec,
        outcome,
        source_repository_commit=SOURCE_COMMIT,
    )
    return spec, outcome, receipt


def _expected(spec, outcome, *, version: str = "0.3.0.dev0") -> dict[str, Any]:
    return {
        "expected_spec": spec,
        "expected_outcome": outcome,
        "expected_source_repository_commit": SOURCE_COMMIT,
        "expected_inferdrome_version": version,
        "expected_provider_adapter": PROVIDER_IDENTITY,
        "expected_runtime_adapter": RUNTIME_IDENTITY,
    }


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def test_committed_schema_is_closed_and_current() -> None:
    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(committed)
    assert committed == deployment_receipt_schema()

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


def test_frozen_v1_synthetic_vector_is_preserved_byte_for_byte() -> None:
    spec = _spec()
    outcome = _outcome(spec)
    fixture_bytes = V1_FIXTURE_PATH.read_bytes()
    receipt = parse_deployment_receipt_json(fixture_bytes)

    assert receipt.receipt_id == deployment_receipt_id(receipt)
    assert sha256_digest(fixture_bytes) == (
        "sha256:f38482a3966797447610904af75f2b8b9a9c52531f48784727c063f55aa570e0"
    )
    assert verify_deployment_receipt(
        receipt, **_expected(spec, outcome, version="0.1.0")
    ) == receipt


def test_deterministic_v2_synthetic_vector_and_identity_rules() -> None:
    spec, outcome, receipt = _receipt()
    fixture_bytes = V2_FIXTURE_PATH.read_bytes()
    # This additive fixture remains exact canonical receipt bytes; the frozen
    # v1 fixture above remains byte-for-byte historical evidence.
    assert fixture_bytes == canonical_deployment_receipt_bytes(receipt)
    assert parse_deployment_receipt_json(fixture_bytes) == receipt
    assert receipt.receipt_id == deployment_receipt_id(receipt)
    assert sha256_digest(fixture_bytes) == (
        "sha256:836543a8e7ed2f6bd3bae4cf40b0590ddfc4031bd3130aaa682a7df5234e66cc"
    )
    assert deployment_receipt_sha256(receipt) == sha256_digest(
        canonical_deployment_receipt_bytes(receipt)
    )
    assert verify_deployment_receipt(receipt, **_expected(spec, outcome)) == receipt
    assert (
        verify_deployment_receipt_bytes(fixture_bytes, **_expected(spec, outcome))
        == receipt
    )


def test_reordered_json_has_one_meaning_but_is_not_published_as_canonical() -> None:
    spec, outcome, receipt = _receipt()
    value = json.loads(canonical_deployment_receipt_bytes(receipt))
    reordered = {key: value[key] for key in reversed(tuple(value))}
    parsed = parse_deployment_receipt_json(
        json.dumps(reordered, indent=2, ensure_ascii=False)
    )
    assert parsed == receipt
    assert canonical_deployment_receipt_bytes(parsed) == (
        canonical_deployment_receipt_bytes(receipt)
    )
    assert deployment_receipt_id(parsed) == deployment_receipt_id(receipt)
    with pytest.raises(VerificationError, match="canonical JSON"):
        verify_deployment_receipt_bytes(
            json.dumps(reordered).encode(), **_expected(spec, outcome)
        )


@pytest.mark.parametrize(
    "raw_builder",
    [
        lambda raw: raw.replace(
            b'"receipt_id":"',
            b'"receipt_id":"sha256:' + b"0" * 64 + b'","receipt_id":"',
            1,
        ),
        lambda raw: raw.replace(
            b'"adapter_id":"inferdrome.provider.local"',
            b'"adapter_id":"inferdrome.provider.local",'
            b'"adapter_id":"inferdrome.provider.local"',
            1,
        ),
    ],
)
def test_duplicate_keys_reject_before_model_validation(raw_builder) -> None:
    _, _, receipt = _receipt()
    raw = canonical_deployment_receipt_bytes(receipt)
    duplicate = raw_builder(raw)
    with pytest.raises(ValueError, match="keys must be unique"):
        parse_deployment_receipt_json(duplicate)
    with pytest.raises(ValueError, match="keys must be unique"):
        DeploymentReceipt.model_validate_json(duplicate)


@pytest.mark.parametrize(
    "path",
    [
        ("unexpected",),
        ("provider_adapter", "unexpected"),
        ("local_controller_facts", "cleanup_state", "unexpected"),
        ("lifecycle_outcome", "trace", 0, "unexpected"),
    ],
)
def test_unknown_fields_reject_at_every_receipt_boundary(
    path: tuple[str | int, ...],
) -> None:
    _, _, receipt = _receipt()
    value = json.loads(canonical_deployment_receipt_bytes(receipt))
    parent = value
    for component in path[:-1]:
        parent = parent[component]
    parent[path[-1]] = "must-not-be-accepted"
    with pytest.raises((ValidationError, ValueError)):
        parse_deployment_receipt_json(canonical_json_bytes(value))


def test_payload_mutation_and_cross_input_substitution_reject() -> None:
    spec, outcome, receipt = _receipt()
    mutated = receipt.model_copy(
        update={
            "deployment_spec_digest": "sha256:" + "0" * 64,
        }
    )
    with pytest.raises(VerificationError, match="deployment specification"):
        verify_deployment_receipt(mutated, **_expected(spec, outcome))

    mutated_bytes = json.loads(canonical_deployment_receipt_bytes(receipt))
    mutated_bytes["source_repository_commit"] = "f" * 40
    with pytest.raises(VerificationError):
        verify_deployment_receipt_bytes(
            canonical_json_bytes(mutated_bytes), **_expected(spec, outcome)
        )

    substituted_spec = parse_deployment_spec_json(
        (EXAMPLE_ROOT / "lambda-dry-run-reference.json").read_bytes()
    )
    with pytest.raises(VerificationError, match="deployment specification"):
        verify_deployment_receipt(
            receipt,
            **{**_expected(spec, outcome), "expected_spec": substituted_spec},
        )

    altered_outcome = outcome.model_copy(update={"status": LifecycleStatus.FAILED})
    with pytest.raises(VerificationError, match="lifecycle outcome"):
        verify_deployment_receipt(
            receipt,
            **{**_expected(spec, outcome), "expected_outcome": altered_outcome},
        )

    with pytest.raises(VerificationError, match="adapter identity"):
        verify_deployment_receipt(
            receipt,
            **{
                **_expected(spec, outcome),
                "expected_provider_adapter": ReceiptAdapterIdentity(
                    adapter_id="inferdrome.provider.other",
                    adapter_version="1.0.0",
                ),
            },
        )
    with pytest.raises(VerificationError, match="adapter identity"):
        verify_deployment_receipt(
            receipt,
            **{
                **_expected(spec, outcome),
                "expected_runtime_adapter": ReceiptAdapterIdentity(
                    adapter_id="inferdrome.runtime.other",
                    adapter_version="1.0.0",
                ),
            },
        )
    with pytest.raises(VerificationError, match="anchor"):
        verify_deployment_receipt(
            receipt,
            **{
                **_expected(spec, outcome),
                "expected_evidence_anchor": ReceiptEvidenceAnchor(
                    run_id="run-0123456789abcdef0123456789abcdef",
                    bundle_digest="sha256:" + "1" * 64,
                    archive_digest=None,
                    capture_manifest_digest=None,
                ),
            },
        )


def test_schema_version_and_nonfinite_or_oversized_inputs_fail_closed() -> None:
    _, _, receipt = _receipt()
    value = json.loads(canonical_deployment_receipt_bytes(receipt))
    value["schema_version"] = "inferdrome.deployment-receipt.v2"
    with pytest.raises((ValidationError, ValueError)):
        parse_deployment_receipt_json(canonical_json_bytes(value))
    with pytest.raises(ValueError, match="non-finite"):
        parse_deployment_receipt_json(
            canonical_deployment_receipt_bytes(receipt).replace(
                b'"evidence_anchor":null', b'"evidence_anchor":NaN'
            )
        )
    with pytest.raises(ValueError, match="exceeds"):
        parse_deployment_receipt_json(
            b'{"' + b"a" * (1024 * 1024) + b'":1}'
        )


def test_synthetic_boundary_rejects_evidence_provider_and_invoice_claims() -> None:
    _, _, receipt = _receipt()
    value = json.loads(canonical_deployment_receipt_bytes(receipt))
    anchor = {
        "run_id": "run-0123456789abcdef0123456789abcdef",
        "bundle_digest": "sha256:" + "1" * 64,
        "archive_digest": None,
        "capture_manifest_digest": None,
    }
    mutations = [
        {"evidence_anchor": anchor},
        {
            "externally_asserted": {
                "provider_attestation": {
                    "provider_id": "local",
                    "attestation_digest": "sha256:" + "2" * 64,
                }
            }
        },
        {
            "invoice_truth": {
                "status": "EXTERNALLY_REPORTED",
                "currency": "USD",
                "amount_usd": "1.00",
            }
        },
    ]
    for mutation in mutations:
        candidate = {**value, **mutation}
        with pytest.raises((ValidationError, ValueError)):
            parse_deployment_receipt_json(canonical_json_bytes(candidate))


def test_publication_revalidates_untrusted_in_memory_receipts_before_side_effects(
    tmp_path: Path,
) -> None:
    _, outcome, receipt = _receipt()
    mutated_lifecycle = outcome.model_copy(update={"orphaned": True})
    mutated_cleanup = receipt.local_controller_facts.cleanup_state.model_copy(
        update={"orphaned": True}
    )
    mutated_controller = receipt.local_controller_facts.model_copy(
        update={"cleanup_state": mutated_cleanup}
    )
    forged_id = "sha256:" + "0" * 64
    constructed_fields = dict(receipt)
    constructed_fields["receipt_id"] = forged_id
    candidates = (
        receipt.model_copy(update={"source_repository_commit": "f" * 40}),
        receipt.model_copy(update={"lifecycle_outcome": mutated_lifecycle}),
        receipt.model_copy(
            update={"local_controller_facts": mutated_controller}
        ),
        receipt.model_copy(update={"receipt_id": forged_id}),
        DeploymentReceipt.model_construct(**constructed_fields),
    )

    for index, candidate in enumerate(candidates):
        root = tmp_path / f"candidate-{index}"
        with pytest.raises(ReceiptPublicationError):
            publish_deployment_receipt(root=root, receipt=candidate)
        assert not root.exists()

    published = publish_deployment_receipt(
        root=tmp_path / "candidate-0", receipt=receipt
    )
    assert published.path.name == receipt.receipt_id


def test_failed_cleanup_remains_synthetic_and_cannot_become_executed() -> None:
    class FailingProvider(LocalProviderAdapter):
        def cleanup(self, spec, handle):
            self.cleanup_count += 1
            return CleanupResult(
                confirmed=False,
                orphaned=True,
                error_code=LifecycleErrorCode.CLEANUP_UNCONFIRMED,
            )

    spec = _spec()
    outcome = LifecycleCoordinator(
        FailingProvider(), LocalMockRuntimeAdapter()
    ).run(spec, lambda endpoint: None)
    assert outcome.status is LifecycleStatus.FAILED
    receipt = issue_synthetic_local_receipt(
        spec,
        outcome,
        source_repository_commit=SOURCE_COMMIT,
    )
    assert receipt.synthetic_only is True
    assert receipt.evidence_eligible is False
    assert receipt.evidence_anchor is None

    candidate = json.loads(canonical_deployment_receipt_bytes(receipt))
    candidate.update(
        {
            "receipt_kind": "executed",
            "synthetic_only": False,
            "evidence_eligible": True,
            "evidence_anchor": {
                "run_id": "run-0123456789abcdef0123456789abcdef",
                "bundle_digest": "sha256:" + "1" * 64,
                "archive_digest": None,
                "capture_manifest_digest": None,
            },
            "externally_asserted": {
                "provider_attestation": {
                    "provider_id": "local",
                    "attestation_digest": "sha256:" + "2" * 64,
                }
            },
        }
    )
    with pytest.raises((ValidationError, ValueError)):
        parse_deployment_receipt_json(canonical_json_bytes(candidate))


@pytest.mark.parametrize("secret_field", ["api_key", "private_key", "secret_value"])
def test_secret_shaped_fields_are_rejected_without_echoing_values(
    secret_field: str,
) -> None:
    _, _, receipt = _receipt()
    secret = "sk-DO_NOT_SERIALIZE_7f3b98c1d2e4a6b8"
    value = json.loads(canonical_deployment_receipt_bytes(receipt))
    value["externally_asserted"][secret_field] = secret
    raw = canonical_json_bytes(value)
    parsers = (parse_deployment_receipt_json, DeploymentReceipt.model_validate_json)
    for parser in parsers:
        with pytest.raises(ValueError) as error:
            parser(raw)
        assert secret not in str(error.value)


def test_publication_is_no_replace_read_back_verified_and_tamper_evident(
    tmp_path: Path,
) -> None:
    spec, outcome, receipt = _receipt()
    try:
        published = publish_deployment_receipt(
            root=tmp_path / "receipts", receipt=receipt
        )
        assert published.path.name == receipt.receipt_id
        assert stat.S_IMODE(os.lstat(published.path).st_mode) == 0o500
        assert stat.S_IMODE(
            os.lstat(published.path / "receipt.json").st_mode
        ) == 0o400
        assert verify_published_deployment_receipt(
            published.path, **_expected(spec, outcome)
        ).receipt_sha256 == published.receipt_sha256
        original = (published.path / "receipt.json").read_bytes()

        with pytest.raises(ReceiptPublicationError):
            publish_deployment_receipt(root=tmp_path / "receipts", receipt=receipt)
        assert (published.path / "receipt.json").read_bytes() == original

        (published.path / "receipt.json").chmod(0o600)
        with pytest.raises(ReceiptPublicationError, match="receipt"):
            verify_published_deployment_receipt(
                published.path, **_expected(spec, outcome)
            )
        (published.path / "receipt.json").write_bytes(original + b" ")
        with pytest.raises(ReceiptPublicationError, match="receipt"):
            verify_published_deployment_receipt(
                published.path, **_expected(spec, outcome)
            )
    finally:
        _make_tree_writable(tmp_path)


def test_publication_rejects_symlinks_and_path_traversal_is_unrepresentable(
    tmp_path: Path,
) -> None:
    _, _, receipt = _receipt()
    real_root = tmp_path / "real"
    link_root = tmp_path / "link"
    real_root.mkdir()
    link_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ReceiptPublicationError):
        publish_deployment_receipt(root=link_root, receipt=receipt)

    published = publish_deployment_receipt(root=real_root, receipt=receipt)
    descriptor = published.path / "receipt.json"
    original = descriptor.read_bytes()
    published.path.chmod(0o700)
    descriptor.chmod(0o700)
    descriptor.unlink()
    descriptor.symlink_to(tmp_path / "secret.txt")
    (tmp_path / "secret.txt").write_bytes(original)
    with pytest.raises(ReceiptPublicationError):
        verify_published_deployment_receipt(
            published.path, **_expected(_spec(), _outcome())
        )


def test_concurrent_publication_has_one_winner_and_no_partial_publication(
    tmp_path: Path,
) -> None:
    _, _, receipt = _receipt()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    publish_deployment_receipt,
                    root=tmp_path / "receipts",
                    receipt=receipt,
                )
                for _ in range(2)
            ]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as error:
                    results.append(error)
        successes = [
            result for result in results if not isinstance(result, Exception)
        ]
        assert len(successes) == 1
        assert (tmp_path / "receipts" / receipt.receipt_id / "receipt.json").is_file()
        assert not list((tmp_path / "receipts").glob(f"{receipt.receipt_id}.*"))
    except Exception:
        _make_tree_writable(tmp_path)
        raise
    finally:
        _make_tree_writable(tmp_path)


def test_failed_private_stage_is_retryable_without_public_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, receipt = _receipt()
    original_rename = immutable._rename_no_replace
    calls = 0

    def fail_once(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected stage failure")
        original_rename(source, destination)

    try:
        monkeypatch.setattr(immutable, "_rename_no_replace", fail_once)
        with pytest.raises(ReceiptPublicationError):
            publish_deployment_receipt(root=tmp_path / "receipts", receipt=receipt)
        assert not (tmp_path / "receipts" / receipt.receipt_id).exists()
        assert list((tmp_path / "receipts").glob(".inferdrome-stage-*"))

        published = publish_deployment_receipt(
            root=tmp_path / "receipts", receipt=receipt
        )
        assert published.path.name == receipt.receipt_id
    finally:
        _make_tree_writable(tmp_path)
