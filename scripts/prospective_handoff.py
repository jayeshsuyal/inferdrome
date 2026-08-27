"""Fail-closed local transport preparation for the external P1 handoff.

This module deliberately treats ExitSpec contracts as opaque JSON.  It only
checks the identities needed to transport the handoff and to bind each source
file to its case.  It never evaluates an ExitSpec criterion or emits a
verdict.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import rfc8785
import yaml

CASE_IDS = (
    "native-p95-under-20ms",
    "native-p95-under-10ms",
    "semantic-first-nonempty-under-20ms",
)
_DIGEST_PATTERN = r"sha256:[0-9a-f]{64}"
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_CONTRACT_BYTES = 4 * 1024 * 1024
_MAX_CONFIRMATION_BYTES = 512 * 1024
_MAX_SOURCE_BYTES = 512 * 1024
_MAX_WORKLOAD_BYTES = 64 * 1024 * 1024
_MAX_HANDOFF_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_HANDOFF_FILES = 12
_MAX_ARCHIVE_MEMBERS = 64
_P1_SCHEMA_VERSION = "exitspec.inferdrome-prospective-handoff.v1"
_P1_AUTHORITY_BOUNDARY = "EXIT_SPEC_CUSTOMER_CONFIRMED_HANDOFF_ONLY"
_P1_CONFIRMATION_ASSURANCE = "PROCESS_LOCAL_DECLARED_IDENTITY_NOT_AUTHENTICATED"
_P1_CANONICALIZATION_SCHEME = "rfc8785_jcs_v1"
_P1_HASH_ALGORITHM = "sha256_v1"
_P1_LINK_POLICY = "exitspec.producer_link.sha256_canonical_hash.v1"
_P1_WORKLOAD_PATH = "sources/real-gpu/workload.jsonl"
_P1_MANIFEST_KEYS = frozenset(
    {
        "acceptance_verdict",
        "authority_boundary",
        "canonicalization_scheme_id",
        "cases",
        "completion_marker",
        "confirmation_identity_assurance",
        "hash_algorithm_id",
        "link_derivation_policy_id",
        "schema_version",
        "workload_artifact_path",
        "workload_artifact_sha256",
    }
)
_P1_CASE_KEYS = frozenset(
    {
        "case_id",
        "confirmation_artifact_path",
        "confirmation_id",
        "confirmation_record_sha256",
        "contract_artifact_path",
        "contract_artifact_sha256",
        "contract_canonical_hash",
        "contract_confirmation_fingerprint",
        "contract_id",
        "contract_version",
        "methodology",
        "producer_contract_link",
        "source_yaml_artifact_path",
        "source_yaml_artifact_sha256",
    }
)
_P1_METHODOLOGY_KEYS = frozenset(
    {
        "adapter_id",
        "adapter_version",
        "canonical_response_content",
        "canonicalization",
        "case_id",
        "choices_span_definition_id",
        "chronology_assurance",
        "claims_assurance",
        "evidence_schema_version",
        "execution_mode",
        "expected_execution_fingerprint",
        "include_request_plan",
        "latency_population",
        "local_gpu_proof_schema_id",
        "local_gpu_proof_schema_sha256",
        "managed_profile_id",
        "managed_profile_sha256",
        "max_measured_requests",
        "max_runtime_seconds",
        "measurement_streaming",
        "metric_definitions_version",
        "native_output_sensitivity",
        "native_schema_fingerprint",
        "produced_evidence_metric_definition_id",
        "producer_name",
        "producer_version",
        "reducer_id",
        "reducer_version",
        "reliability_population",
        "requested_criterion_metric_definition_id",
        "run_aggregation_policy",
        "sampling",
        "schema_version",
        "sequence_requirement",
        "source_schema_version",
        "target_api",
        "target_endpoint",
        "target_engine",
        "target_engine_version",
        "target_model",
        "target_model_revision",
        "target_tokenizer_revision",
        "traffic",
        "workload_digest",
        "workload_id",
    }
)
_P1_CANONICALIZATION_KEYS = frozenset(
    {
        "canonical_bytes_encoding",
        "canonicalization_scheme_id",
        "hash_algorithm_id",
        "hash_encoding_id",
        "link_derivation_input",
        "link_derivation_operation",
        "link_derivation_policy_id",
    }
)
_P1_RELIABILITY_KEYS = frozenset(
    {
        "denominator",
        "exact_attempts",
        "numerator",
        "operator",
        "population_id",
        "schema_version",
        "threshold_basis_points",
    }
)
_P1_SAMPLING_KEYS = frozenset(
    {
        "policy_id",
        "prompt_content_policy",
        "requested_output_tokens",
        "schema_version",
        "seed",
        "temperature",
    }
)
_P1_TRAFFIC_KEYS = frozenset(
    {
        "configured_concurrency",
        "kind",
        "measured_requests",
        "policy_id",
        "schema_version",
        "warmup_requests",
    }
)
_P1_CONFIRMATION_KEYS = frozenset(
    {
        "agreement_acknowledged",
        "confirmation_id",
        "confirmer_identity",
        "contract_fingerprint",
        "contract_id",
        "contract_version",
        "decided_at",
        "decision",
        "rationale",
    }
)
_P1_CONTRACT_KEYS = frozenset(
    {
        "approved_at",
        "canonical_hash",
        "confirmation_id",
        "created_at",
        "criteria",
        "customer",
        "evidence_retention_policy",
        "frozen_at",
        "id",
        "non_goals",
        "owners",
        "parent_version",
        "status",
        "target_system",
        "use_case",
        "version",
        "workload",
    }
)
_P1_CRITERION_KEYS = frozenset(
    {
        "approved",
        "case_id",
        "concurrency_semantics",
        "criterion_type",
        "error_rate",
        "evidence_identity",
        "evidence_policy",
        "human_added",
        "id",
        "must_have",
        "normalized_claim",
        "owner",
        "source",
        "title",
        "ttft_p95",
    }
)
_P1_TTFT_KEYS = frozenset(
    {
        "aggregation",
        "definition_id",
        "equality_outcome",
        "metric",
        "minimum_successful_samples",
        "must_pass",
        "operator",
        "population",
        "reducer_id",
        "schema_version",
        "threshold_ns",
        "unit",
    }
)
_P1_ERROR_RATE_KEYS = frozenset(
    {
        "aggregation",
        "denominator",
        "exact_attempts",
        "metric",
        "must_pass",
        "numerator",
        "operator",
        "threshold_basis_points",
    }
)


class ProspectiveHandoffError(RuntimeError):
    """Expected, user-facing handoff rejection."""


@dataclass(frozen=True)
class HandoffFile:
    relative_path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class HandoffCase:
    case_id: str
    contract: HandoffFile
    confirmation: HandoffFile
    source: HandoffFile
    contract_digest: str


@dataclass(frozen=True)
class HandoffSnapshot:
    """The one-read byte snapshot used for every subsequent handoff action."""

    root: Path
    manifest: HandoffFile
    complete: HandoffFile
    workload: HandoffFile
    cases: tuple[HandoffCase, ...]
    files: tuple[HandoffFile, ...]
    archive_path: Path | None = None
    archive_sha256: str | None = None
    archive_size_bytes: int | None = None

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    @property
    def workload_sha256(self) -> str:
        return self.workload.sha256

    @property
    def expected_contract_digests(self) -> tuple[str, ...]:
        return tuple(case.contract_digest for case in self.cases)


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _strict_json(content: bytes, *, label: str) -> dict[str, Any]:
    if not content or len(content) > _MAX_MANIFEST_BYTES:
        raise ProspectiveHandoffError(f"{label} is empty or exceeds its size limit")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid number: {token}")
            ),
            parse_float=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid float: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProspectiveHandoffError(f"{label} is not strict JSON") from None
    if not isinstance(value, dict):
        raise ProspectiveHandoffError(f"{label} must be a JSON object")
    return value


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _bare_hash(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProspectiveHandoffError(f"{label} must be a lowercase bare SHA-256 hash")
    return value


def _tagged_digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ProspectiveHandoffError(f"{label} must be a sha256: digest link")
    return value


def _inode_identity(metadata: os.stat_result) -> tuple[int, int]:
    """Return the stable identity used to bind a path to an opened inode."""

    return metadata.st_dev, metadata.st_ino


def _require_exact_keys(
    value: dict[str, Any], expected: frozenset[str], *, label: str
) -> None:
    if set(value) != expected:
        raise ProspectiveHandoffError(f"{label} has unexpected or missing fields")


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that does not silently overwrite duplicate keys."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise yaml.YAMLError("source YAML contains a duplicate or non-string key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _strict_source_yaml(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.load(content, Loader=_UniqueSafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError):
        raise ProspectiveHandoffError(f"{label} is not strict YAML") from None
    if not isinstance(value, dict):
        raise ProspectiveHandoffError(f"{label} must be a YAML object")
    return value


def _deep_exact(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict) and isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _deep_exact(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _deep_exact(item, expected_item)
            for item, expected_item in zip(actual, expected, strict=True)
        )
    return actual == expected


def _bounded_text(value: object, *, label: str, maximum: int = 4_096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProspectiveHandoffError(f"{label} has invalid bounded text")
    return value


def _timestamp(value: object, *, label: str) -> datetime:
    selected = _bounded_text(value, label=label, maximum=32)
    if (
        re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)",
            selected,
        )
        is None
    ):
        raise ProspectiveHandoffError(f"{label} has an invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(selected.replace("Z", "+00:00"))
    except ValueError:
        raise ProspectiveHandoffError(f"{label} has an invalid UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ProspectiveHandoffError(f"{label} has an invalid UTC timestamp")
    return parsed.astimezone(UTC)


def _expected_methodology(case_id: str, workload_digest: str) -> dict[str, Any]:
    requested_metric = (
        "first_nonempty_choices_delta_content_v1"
        if case_id == "semantic-first-nonempty-under-20ms"
        else "vllm_first_choices_event_v0_26"
    )
    return {
        "adapter_id": "vllm_bench_serve",
        "adapter_version": "1.0.0",
        "canonical_response_content": "omit",
        "canonicalization": {
            "canonical_bytes_encoding": "utf-8_rfc8785_jcs",
            "canonicalization_scheme_id": _P1_CANONICALIZATION_SCHEME,
            "hash_algorithm_id": _P1_HASH_ALGORITHM,
            "hash_encoding_id": "lowercase_hex_without_prefix",
            "link_derivation_input": "bare_canonical_hash",
            "link_derivation_operation": "prefix_sha256_no_second_hash",
            "link_derivation_policy_id": _P1_LINK_POLICY,
        },
        "case_id": case_id,
        "choices_span_definition_id": "last_choices_event_span_v1",
        "chronology_assurance": "UNAVAILABLE",
        "claims_assurance": "INTERNAL_CONSISTENCY_ONLY",
        "evidence_schema_version": "inferdrome.evidence.v1",
        "execution_mode": "attached_endpoint",
        "expected_execution_fingerprint": (
            "sha256:76d984ea57a0e7cb00520255a6e362f22885d713a875195a7397771937060edd"
        ),
        "include_request_plan": True,
        "latency_population": "successful_measured_requests_with_observed_ttft",
        "local_gpu_proof_schema_id": "urn:inferdrome:local-gpu-proof:v1",
        "local_gpu_proof_schema_sha256": (
            "sha256:cf83bbdea2bba4c30b8f0e2c5f34f34a4077501207881fdbdab021571d665547"
        ),
        "managed_profile_id": "inferdrome.managed-vllm-0.26-evidence-profile.v1",
        "managed_profile_sha256": (
            "sha256:9d03b5d0822ed829ddbfa4c87c75530885b9ad51ee2c0cb7c5e31a075996fe34"
        ),
        "max_measured_requests": 100,
        "max_runtime_seconds": 900,
        "measurement_streaming": True,
        "metric_definitions_version": "1.0.0",
        "native_output_sensitivity": "RESPONSE_CONTENT",
        "native_schema_fingerprint": (
            "sha256:3a4fdee6fe9b45ce5b42c41fd3bfc6614245a36ecfe6f94de92b59717a136abb"
        ),
        "produced_evidence_metric_definition_id": "vllm_first_choices_event_v0_26",
        "producer_name": "vllm",
        "producer_version": "0.26.0",
        "reducer_id": "nearest_rank_v1",
        "reducer_version": "1.0.0",
        "reliability_population": {
            "denominator": "all_measured_requests",
            "exact_attempts": 100,
            "numerator": "failed_or_anomalous_native_measured_requests",
            "operator": "lt",
            "population_id": "exitspec.inferdrome-reliability.v1",
            "schema_version": "exitspec.inferdrome-reliability-population.v1",
            "threshold_basis_points": 100,
        },
        "requested_criterion_metric_definition_id": requested_metric,
        "run_aggregation_policy": "independent_single_run_no_pooling",
        "sampling": {
            "policy_id": "inferdrome.qwen2.5-deterministic.v1",
            "prompt_content_policy": "include",
            "requested_output_tokens": 32,
            "schema_version": "exitspec.inferdrome-sampling.v1",
            "seed": 42,
            "temperature": 0,
        },
        "schema_version": "exitspec.inferdrome-evidence-identity.v2",
        "sequence_requirement": "OPERATOR_MUST_FREEZE_BEFORE_MEASUREMENT",
        "source_schema_version": "inferdrome.source-experiment.v1",
        "target_api": "openai_chat_completions",
        "target_endpoint": "http://127.0.0.1:18080/",
        "target_engine": "vllm",
        "target_engine_version": "0.26.0",
        "target_model": "Qwen/Qwen2.5-0.5B-Instruct",
        "target_model_revision": "7ae557604adf67be50417f59c2c2f167def9a775",
        "target_tokenizer_revision": "7ae557604adf67be50417f59c2c2f167def9a775",
        "traffic": {
            "configured_concurrency": 4,
            "kind": "concurrent",
            "measured_requests": 100,
            "policy_id": "inferdrome.concurrent.vllm.v1",
            "schema_version": "exitspec.inferdrome-traffic.v1",
            "warmup_requests": 10,
        },
        "workload_digest": workload_digest,
        "workload_id": "inferdrome.qwen2.5-real-gpu-workload.v1",
    }


_P1_CASE_METADATA: dict[str, dict[str, Any]] = {
    "native-p95-under-20ms": {
        "criterion_id": "INFERDROME-P1-NATIVE-P95-20MS",
        "threshold_ns": 20_000_000,
    },
    "native-p95-under-10ms": {
        "criterion_id": "INFERDROME-P1-NATIVE-P95-10MS",
        "threshold_ns": 10_000_000,
    },
    "semantic-first-nonempty-under-20ms": {
        "criterion_id": "INFERDROME-P1-SEMANTIC-FIRST-NONEMPTY-20MS",
        "threshold_ns": 20_000_000,
    },
}


def _validate_criterion(
    criterion: dict[str, Any],
    methodology: dict[str, Any],
    *,
    case_id: str,
    owners: list[str],
) -> None:
    selected = _P1_CASE_METADATA[case_id]
    metric = methodology["requested_criterion_metric_definition_id"]
    _require_exact_keys(criterion, _P1_CRITERION_KEYS, label=f"{case_id} criterion")
    if (
        criterion["approved"] is not True
        or criterion["case_id"] != case_id
        or criterion["concurrency_semantics"]
        != "configured_maximum_concurrency_not_observed_overlap"
        or criterion["criterion_type"] != "inference_performance_v4"
        or not _deep_exact(criterion["evidence_identity"], methodology)
        or criterion["human_added"] is not True
        or criterion["id"] != selected["criterion_id"]
        or criterion["must_have"] is not True
        or criterion["source"] is not None
    ):
        raise ProspectiveHandoffError(f"{case_id} criterion semantics drifted")
    _bounded_text(criterion["title"], label=f"{case_id} criterion title")
    _bounded_text(criterion["normalized_claim"], label=f"{case_id} criterion claim")
    _bounded_text(criterion["evidence_policy"], label=f"{case_id} evidence policy")
    owner = _bounded_text(criterion["owner"], label=f"{case_id} criterion owner")
    if owner not in owners:
        raise ProspectiveHandoffError(f"{case_id} criterion owner is not bound")
    error_rate = criterion["error_rate"]
    ttft = criterion["ttft_p95"]
    if not isinstance(error_rate, dict) or not isinstance(ttft, dict):
        raise ProspectiveHandoffError(f"{case_id} criterion metrics are invalid")
    _require_exact_keys(error_rate, _P1_ERROR_RATE_KEYS, label=f"{case_id} error rate")
    _require_exact_keys(ttft, _P1_TTFT_KEYS, label=f"{case_id} TTFT criterion")
    expected_error_rate = {
        "aggregation": "rate",
        "denominator": "all_measured_requests",
        "exact_attempts": 100,
        "metric": "error_rate",
        "must_pass": True,
        "numerator": "failed_or_anomalous_native_measured_requests",
        "operator": "lt",
        "threshold_basis_points": 100,
    }
    expected_ttft = {
        "aggregation": "p95",
        "definition_id": metric,
        "equality_outcome": "FAIL",
        "metric": "time_to_first_token",
        "minimum_successful_samples": 100,
        "must_pass": True,
        "operator": "lt",
        "population": "successful_measured_requests_with_observed_ttft",
        "reducer_id": "nearest_rank_v1",
        "schema_version": "exitspec.inferdrome-ttft-p95.v2",
        "threshold_ns": selected["threshold_ns"],
        "unit": "nanoseconds",
    }
    if not _deep_exact(error_rate, expected_error_rate) or not _deep_exact(
        ttft, expected_ttft
    ):
        raise ProspectiveHandoffError(f"{case_id} criterion metrics drifted")


def _validate_source_document(
    source: dict[str, Any],
    *,
    case_id: str,
    producer_link: str,
    workload_digest: str,
) -> None:
    _require_exact_keys(
        source,
        frozenset(
            {
                "evidence",
                "execution",
                "experiment",
                "links",
                "schema_version",
                "target",
                "traffic",
                "workload",
            }
        ),
        label=f"{case_id} source YAML",
    )
    experiment = source["experiment"]
    execution = source["execution"]
    target = source["target"]
    workload = source["workload"]
    traffic = source["traffic"]
    evidence = source["evidence"]
    links = source["links"]
    if not all(
        isinstance(value, dict)
        for value in (experiment, execution, target, workload, traffic, evidence, links)
    ):
        raise ProspectiveHandoffError(f"{case_id} source YAML sections are invalid")
    _require_exact_keys(
        experiment,
        frozenset({"hypothesis", "id", "title"}),
        label=f"{case_id} source experiment",
    )
    _require_exact_keys(
        execution,
        frozenset({"max_measured_requests", "max_runtime_seconds", "mode"}),
        label=f"{case_id} source execution",
    )
    _require_exact_keys(
        target,
        frozenset(
            {
                "engine",
                "engine_version",
                "endpoint",
                "model",
                "model_revision",
                "tokenizer_revision",
            }
        ),
        label=f"{case_id} source target",
    )
    _require_exact_keys(
        workload,
        frozenset(
            {
                "path",
                "prompt_content_policy",
                "requested_output_tokens",
                "seed",
                "sha256",
                "temperature",
            }
        ),
        label=f"{case_id} source workload",
    )
    _require_exact_keys(
        traffic,
        frozenset({"concurrency", "kind", "measured_requests", "warmup_requests"}),
        label=f"{case_id} source traffic",
    )
    _require_exact_keys(
        evidence,
        frozenset({"canonical_response_content"}),
        label=f"{case_id} source evidence",
    )
    _require_exact_keys(
        links,
        frozenset({"exitspec_contract_digest"}),
        label=f"{case_id} source links",
    )
    if (
        source["schema_version"] != "inferdrome.source-experiment.v1"
        or experiment["id"] != f"inferdrome-p1-{case_id}"
        or not _deep_exact(
            execution,
            {
                "mode": "attached_endpoint",
                "max_runtime_seconds": 900,
                "max_measured_requests": 100,
            },
        )
        or not _deep_exact(
            target,
            {
                "engine": "vllm",
                "endpoint": "http://127.0.0.1:18080",
                "model": "Qwen/Qwen2.5-0.5B-Instruct",
                "model_revision": "7ae557604adf67be50417f59c2c2f167def9a775",
                "tokenizer_revision": "7ae557604adf67be50417f59c2c2f167def9a775",
                "engine_version": "0.26.0",
            },
        )
        or not _deep_exact(
            workload,
            {
                "path": "real-gpu/workload.jsonl",
                "sha256": workload_digest,
                "prompt_content_policy": "include",
                "requested_output_tokens": 32,
                "temperature": 0,
                "seed": 42,
            },
        )
        or not _deep_exact(
            traffic,
            {
                "kind": "concurrent",
                "concurrency": 4,
                "warmup_requests": 10,
                "measured_requests": 100,
            },
        )
        or evidence != {"canonical_response_content": "omit"}
        or links != {"exitspec_contract_digest": producer_link}
    ):
        raise ProspectiveHandoffError(f"{case_id} source YAML methodology drifted")
    _bounded_text(experiment["title"], label=f"{case_id} source title")
    _bounded_text(experiment["hypothesis"], label=f"{case_id} source hypothesis")


def _contract_confirmation_fingerprint(
    contract: dict[str, Any], *, case_id: str
) -> str:
    payload = {
        key: contract[key]
        for key in (
            "id",
            "version",
            "customer",
            "use_case",
            "target_system",
            "workload",
            "criteria",
            "owners",
            "non_goals",
            "evidence_retention_policy",
        )
    }
    try:
        canonical = rfc8785.dumps(payload)
    except (rfc8785.CanonicalizationError, TypeError, ValueError):
        raise ProspectiveHandoffError(
            f"{case_id} contract confirmation payload is not canonicalizable"
        ) from None
    return hashlib.sha256(
        b"exitspec-contract-confirmation-v1\x00" + canonical
    ).hexdigest()


def _validate_frozen_contract(
    contract: dict[str, Any],
    methodology: dict[str, Any],
    *,
    case_id: str,
    confirmation_id: str,
    workload_digest: str,
) -> str:
    _require_exact_keys(contract, _P1_CONTRACT_KEYS, label=f"{case_id} frozen contract")
    approved_at = _timestamp(
        contract["approved_at"], label=f"{case_id} contract approved time"
    )
    created_at = _timestamp(
        contract["created_at"], label=f"{case_id} contract created time"
    )
    frozen_at = _timestamp(
        contract["frozen_at"], label=f"{case_id} contract frozen time"
    )
    if approved_at != created_at or created_at > frozen_at:
        raise ProspectiveHandoffError(f"{case_id} contract timestamps are not bound")
    if (
        contract["confirmation_id"] != confirmation_id
        or contract["id"] != f"inferdrome-p1-{case_id}"
        or contract["parent_version"] is not None
        or contract["status"] != "FROZEN"
        or contract["version"] != "1.0.0"
    ):
        raise ProspectiveHandoffError(f"{case_id} frozen contract identity drifted")
    _bounded_text(contract["customer"], label=f"{case_id} contract customer")
    _bounded_text(contract["use_case"], label=f"{case_id} contract use case")
    _bounded_text(
        contract["evidence_retention_policy"],
        label=f"{case_id} retention policy",
    )
    owners = contract["owners"]
    non_goals = contract["non_goals"]
    criteria = contract["criteria"]
    target_system = contract["target_system"]
    workload = contract["workload"]
    if (
        not isinstance(owners, list)
        or not 1 <= len(owners) <= 16
        or not isinstance(non_goals, list)
        or len(non_goals) != 3
        or not isinstance(criteria, list)
        or len(criteria) != 1
        or not isinstance(target_system, dict)
        or not isinstance(workload, dict)
    ):
        raise ProspectiveHandoffError(f"{case_id} frozen contract shape is invalid")
    owner_values = [
        _bounded_text(owner, label=f"{case_id} contract owner") for owner in owners
    ]
    if len(set(owner_values)) != len(owner_values):
        raise ProspectiveHandoffError(f"{case_id} contract owners are duplicated")
    for index, non_goal in enumerate(non_goals):
        _bounded_text(non_goal, label=f"{case_id} non-goal {index}")
    if not _deep_exact(
        target_system,
        {
            "endpoint_class": "retained-loopback-vllm-benchmark",
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "provider": "inferdrome-managed-vllm",
        },
    ) or not _deep_exact(
        workload,
        {
            "fixture_path": "real-gpu/workload.jsonl",
            "sha256": workload_digest.removeprefix("sha256:"),
        },
    ):
        raise ProspectiveHandoffError(f"{case_id} frozen contract target drifted")
    criterion = criteria[0]
    if not isinstance(criterion, dict):
        raise ProspectiveHandoffError(f"{case_id} criterion is invalid")
    _validate_criterion(
        criterion,
        methodology,
        case_id=case_id,
        owners=owner_values,
    )
    canonical_hash = _bare_hash(
        contract.get("canonical_hash"), label=f"{case_id} canonical hash"
    )
    try:
        computed = hashlib.sha256(
            rfc8785.dumps(
                {
                    key: value
                    for key, value in contract.items()
                    if key != "canonical_hash"
                }
            )
        ).hexdigest()
    except (rfc8785.CanonicalizationError, TypeError, ValueError):
        raise ProspectiveHandoffError(
            f"{case_id} contract is not canonicalizable"
        ) from None
    if computed != canonical_hash:
        raise ProspectiveHandoffError(
            f"{case_id} canonical hash does not match contract"
        )
    return canonical_hash


def _validate_confirmation(
    confirmation: dict[str, Any],
    contract: dict[str, Any],
    *,
    case_id: str,
    expected_confirmation_id: str,
    expected_fingerprint: str,
) -> None:
    _require_exact_keys(
        confirmation, _P1_CONFIRMATION_KEYS, label=f"{case_id} confirmation"
    )
    created_at = _timestamp(
        contract["created_at"], label=f"{case_id} contract created time"
    )
    decided_at = _timestamp(
        confirmation["decided_at"], label=f"{case_id} confirmation decision time"
    )
    frozen_at = _timestamp(
        contract["frozen_at"], label=f"{case_id} contract frozen time"
    )
    if not (created_at <= decided_at <= frozen_at):
        raise ProspectiveHandoffError(
            f"{case_id} confirmation timestamps are out of order"
        )
    if (
        confirmation["agreement_acknowledged"] is not True
        or confirmation["confirmation_id"] != expected_confirmation_id
        or confirmation["contract_fingerprint"] != expected_fingerprint
        or confirmation["contract_id"] != contract["id"]
        or confirmation["contract_version"] != contract["version"]
        or confirmation["decision"] != "CONFIRM"
    ):
        raise ProspectiveHandoffError(f"{case_id} confirmation binding is invalid")
    _bounded_text(
        confirmation["confirmer_identity"],
        label=f"{case_id} confirmer identity",
        maximum=512,
    )
    _bounded_text(
        confirmation["rationale"],
        label=f"{case_id} confirmation rationale",
    )


def _workload_digest(value: object) -> str:
    return _tagged_digest(value, label="handoff workload digest")


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ProspectiveHandoffError(f"{label} has an unsafe relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProspectiveHandoffError(f"{label} has an unsafe relative path")
    return value


def _read_regular_once(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[bytes, tuple[int, ...]]:
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        identity = _identity(before)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("not a single-link regular file")
        if before.st_size > maximum_bytes:
            raise ValueError("file is too large")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(content) > maximum_bytes or _identity(after) != identity:
            raise ValueError("file changed while it was read")
        path_identity = _identity(os.lstat(path))
        if path_identity != identity:
            raise ValueError("file changed after it was read")
        return content, identity
    except (OSError, ValueError):
        raise ProspectiveHandoffError(f"{label} is unavailable or unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _walk_inventory(root: Path) -> dict[str, tuple[int, ...]]:
    try:
        root_metadata = os.lstat(root)
    except OSError:
        raise ProspectiveHandoffError(
            "prospective handoff root is unavailable"
        ) from None
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise ProspectiveHandoffError(
            "prospective handoff root must be a real directory"
        )
    result: dict[str, tuple[int, ...]] = {"": _identity(root_metadata)}
    regular_file_count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise ProspectiveHandoffError(
                "prospective handoff cannot be enumerated"
            ) from None
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise ProspectiveHandoffError(
                    "prospective handoff entry cannot be inspected"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ProspectiveHandoffError(
                    f"prospective handoff contains a symlink: {relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                result[relative] = _identity(metadata)
                pending.append(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                regular_file_count += 1
                if regular_file_count > _MAX_HANDOFF_FILES:
                    raise ProspectiveHandoffError(
                        "prospective handoff has too many files"
                    )
                result[relative] = _identity(metadata)
            else:
                raise ProspectiveHandoffError(
                    f"prospective handoff contains an unsafe entry: {relative}"
                )
    return result


def _file_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    return path


def _case_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = manifest.get("cases")
    if not isinstance(raw, list) or len(raw) != len(CASE_IDS):
        raise ProspectiveHandoffError("handoff manifest omits its canonical cases")
    result: dict[str, dict[str, Any]] = {}
    for expected_case_id, value in zip(CASE_IDS, raw, strict=True):
        if not isinstance(value, dict) or value.get("case_id") != expected_case_id:
            raise ProspectiveHandoffError("handoff case entry is invalid")
        _require_exact_keys(
            value,
            _P1_CASE_KEYS,
            label=f"{expected_case_id} manifest case",
        )
        case_id = value["case_id"]
        if case_id in result:
            raise ProspectiveHandoffError("handoff case IDs are duplicated")
        result[case_id] = value
    return result


def snapshot_handoff(
    handoff_root: Path,
    *,
    expected_manifest_sha256: str | None = None,
    expected_workload_sha256: str | None = None,
) -> HandoffSnapshot:
    """Read the complete handoff exactly once and validate its byte snapshot."""

    if expected_manifest_sha256 is None or expected_workload_sha256 is None:
        raise ProspectiveHandoffError(
            "handoff validation requires explicit manifest and workload pins"
        )
    root = handoff_root.expanduser().absolute()
    before = _walk_inventory(root)
    allowed_root = {".complete", "handoff-manifest.json"}
    file_paths = {
        path for path, identity in before.items() if path and stat.S_ISREG(identity[2])
    }
    if len(file_paths) != _MAX_HANDOFF_FILES:
        raise ProspectiveHandoffError(
            "prospective handoff must contain exactly the bounded P1 file set"
        )
    if set(before) & {".complete", "handoff-manifest.json"} != allowed_root:
        raise ProspectiveHandoffError(
            "prospective handoff is missing its marker or manifest"
        )
    allowed_directories = {
        "contracts",
        "confirmations",
        "sources",
        "sources/real-gpu",
    }
    if any(
        stat.S_ISDIR(identity[2]) and path not in allowed_directories
        for path, identity in before.items()
        if path
    ):
        raise ProspectiveHandoffError("prospective handoff contains an extra path")

    maximums = {
        ".complete": 1_024,
        "handoff-manifest.json": _MAX_MANIFEST_BYTES,
        "sources/real-gpu/workload.jsonl": _MAX_WORKLOAD_BYTES,
    }
    contents: dict[str, bytes] = {}
    identities: dict[str, tuple[int, ...]] = {}
    for relative, identity in before.items():
        if not relative or not stat.S_ISREG(identity[2]):
            continue
        maximum = maximums.get(relative)
        if maximum is None:
            if relative.startswith("contracts/"):
                maximum = _MAX_CONTRACT_BYTES
            elif relative.startswith("confirmations/"):
                maximum = _MAX_CONFIRMATION_BYTES
            elif relative.startswith("sources/"):
                maximum = _MAX_SOURCE_BYTES
            else:
                raise ProspectiveHandoffError(
                    f"prospective handoff contains an extra path: {relative}"
                )
        contents[relative], identities[relative] = _read_regular_once(
            _file_path(root, relative),
            label=f"handoff file {relative}",
            maximum_bytes=maximum,
        )
    after = _walk_inventory(root)
    if after != before:
        raise ProspectiveHandoffError("prospective handoff changed while it was staged")

    manifest_value = _strict_json(
        contents["handoff-manifest.json"], label="handoff manifest"
    )
    _require_exact_keys(manifest_value, _P1_MANIFEST_KEYS, label="handoff manifest")
    if (
        manifest_value["schema_version"] != _P1_SCHEMA_VERSION
        or manifest_value["authority_boundary"] != _P1_AUTHORITY_BOUNDARY
        or manifest_value["confirmation_identity_assurance"]
        != _P1_CONFIRMATION_ASSURANCE
        or manifest_value["canonicalization_scheme_id"] != _P1_CANONICALIZATION_SCHEME
        or manifest_value["hash_algorithm_id"] != _P1_HASH_ALGORITHM
        or manifest_value["link_derivation_policy_id"] != _P1_LINK_POLICY
        or manifest_value["completion_marker"] != ".complete"
        or manifest_value["workload_artifact_path"] != _P1_WORKLOAD_PATH
        or manifest_value["acceptance_verdict"] is not None
    ):
        raise ProspectiveHandoffError("handoff manifest acceptance boundary is invalid")
    try:
        manifest_canonical = rfc8785.dumps(manifest_value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError):
        raise ProspectiveHandoffError(
            "handoff manifest is not canonical JSON"
        ) from None
    if manifest_canonical != contents["handoff-manifest.json"]:
        raise ProspectiveHandoffError("handoff manifest is not canonical JSON")
    case_values = _case_entries(manifest_value)
    file_entries: dict[str, tuple[str, int | None]] = {}
    for case_id in CASE_IDS:
        selected = case_values[case_id]
        expected_paths = {
            "contract_artifact_path": f"contracts/{case_id}.frozen.json",
            "confirmation_artifact_path": f"confirmations/{case_id}.confirmation.json",
            "source_yaml_artifact_path": f"sources/{case_id}.yaml",
        }
        for path_field, expected_path in expected_paths.items():
            if selected[path_field] != expected_path:
                raise ProspectiveHandoffError(
                    f"{case_id} has a non-canonical {path_field}"
                )
        for path_field, digest_field in (
            ("contract_artifact_path", "contract_artifact_sha256"),
            ("confirmation_artifact_path", "confirmation_record_sha256"),
            ("source_yaml_artifact_path", "source_yaml_artifact_sha256"),
        ):
            path = _safe_relative(selected[path_field], label=f"{case_id} artifact")
            if path in file_entries:
                raise ProspectiveHandoffError("handoff artifact paths are duplicated")
            file_entries[path] = (
                _tagged_digest(selected[digest_field], label=f"{case_id} artifact"),
                None,
            )
    file_entries[_P1_WORKLOAD_PATH] = (
        _tagged_digest(
            manifest_value["workload_artifact_sha256"],
            label="handoff workload artifact",
        ),
        None,
    )
    expected_paths = set(contents) - {"handoff-manifest.json", ".complete"}
    if set(file_entries) != expected_paths:
        raise ProspectiveHandoffError(
            "handoff manifest file allowlist disagrees with the snapshot"
        )
    for relative, (digest, size) in file_entries.items():
        actual = _digest_bytes(contents[relative])
        if actual != digest or (size is not None and size != len(contents[relative])):
            raise ProspectiveHandoffError(
                f"handoff file hash or size disagrees: {relative}"
            )
    workload_digest = _workload_digest(manifest_value["workload_artifact_sha256"])
    if workload_digest != _digest_bytes(contents["sources/real-gpu/workload.jsonl"]):
        raise ProspectiveHandoffError("handoff workload digest disagrees")
    if _tagged_digest(
        expected_manifest_sha256, label="expected handoff manifest digest"
    ) != _digest_bytes(contents["handoff-manifest.json"]):
        raise ProspectiveHandoffError(
            "handoff manifest digest disagrees with its operator pin"
        )
    if (
        _tagged_digest(expected_workload_sha256, label="expected workload digest")
        != workload_digest
    ):
        raise ProspectiveHandoffError("workload digest disagrees with its operator pin")

    cases: list[HandoffCase] = []
    contract_digests: set[str] = set()
    producer_links: set[str] = set()
    confirmation_ids: set[str] = set()
    selected_paths: set[str] = set()
    for case_id in CASE_IDS:
        selected = case_values[case_id]
        contract_path = selected["contract_artifact_path"]
        confirmation_path = selected["confirmation_artifact_path"]
        source_path = selected["source_yaml_artifact_path"]
        selected_paths.update({contract_path, confirmation_path, source_path})
        contract_bytes = contents.get(contract_path)
        confirmation_bytes = contents.get(confirmation_path)
        source_bytes = contents.get(source_path)
        if contract_bytes is None or confirmation_bytes is None or source_bytes is None:
            raise ProspectiveHandoffError(f"{case_id} handoff files are missing")
        contract_value = _strict_json(
            contract_bytes, label=f"{case_id} frozen contract"
        )
        confirmation_value = _strict_json(
            confirmation_bytes, label=f"{case_id} confirmation"
        )
        try:
            contract_canonical = rfc8785.dumps(contract_value)
            confirmation_canonical = rfc8785.dumps(confirmation_value)
        except (rfc8785.CanonicalizationError, TypeError, ValueError):
            raise ProspectiveHandoffError(
                f"{case_id} JSON artifact is not canonical"
            ) from None
        if (
            contract_canonical != contract_bytes
            or confirmation_canonical != confirmation_bytes
        ):
            raise ProspectiveHandoffError(f"{case_id} JSON artifact is not canonical")
        methodology = selected["methodology"]
        if not isinstance(methodology, dict):
            raise ProspectiveHandoffError(f"{case_id} methodology is invalid")
        _require_exact_keys(
            methodology, _P1_METHODOLOGY_KEYS, label=f"{case_id} methodology"
        )
        for nested_name, nested_keys in (
            ("canonicalization", _P1_CANONICALIZATION_KEYS),
            ("reliability_population", _P1_RELIABILITY_KEYS),
            ("sampling", _P1_SAMPLING_KEYS),
            ("traffic", _P1_TRAFFIC_KEYS),
        ):
            nested_value = methodology.get(nested_name)
            if not isinstance(nested_value, dict):
                raise ProspectiveHandoffError(
                    f"{case_id} methodology {nested_name} is invalid"
                )
            _require_exact_keys(
                nested_value,
                nested_keys,
                label=f"{case_id} methodology {nested_name}",
            )
        if not _deep_exact(
            methodology, _expected_methodology(case_id, workload_digest)
        ):
            raise ProspectiveHandoffError(f"{case_id} methodology identity drifted")
        expected_confirmation_id = selected["confirmation_id"]
        if not isinstance(expected_confirmation_id, str) or not re.fullmatch(
            r"cnf_[0-9a-f]{64}", expected_confirmation_id
        ):
            raise ProspectiveHandoffError(f"{case_id} confirmation identity is invalid")
        selected_fingerprint = _bare_hash(
            selected["contract_confirmation_fingerprint"],
            label=f"{case_id} confirmation fingerprint",
        )
        canonical_hash = _validate_frozen_contract(
            contract_value,
            methodology,
            case_id=case_id,
            confirmation_id=expected_confirmation_id,
            workload_digest=workload_digest,
        )
        if selected["contract_canonical_hash"] != canonical_hash:
            raise ProspectiveHandoffError(
                f"{case_id} manifest canonical hash disagrees with contract"
            )
        producer_link = _tagged_digest(
            selected["producer_contract_link"],
            label=f"{case_id} producer contract link",
        )
        if producer_link != f"sha256:{canonical_hash}":
            raise ProspectiveHandoffError(f"{case_id} producer contract link disagrees")
        expected_fingerprint = _contract_confirmation_fingerprint(
            contract_value, case_id=case_id
        )
        if selected_fingerprint != expected_fingerprint:
            raise ProspectiveHandoffError(
                f"{case_id} confirmation fingerprint disagrees"
            )
        _validate_confirmation(
            confirmation_value,
            contract_value,
            case_id=case_id,
            expected_confirmation_id=expected_confirmation_id,
            expected_fingerprint=selected_fingerprint,
        )
        source_document = _strict_source_yaml(
            source_bytes, label=f"{case_id} source YAML"
        )
        _validate_source_document(
            source_document,
            case_id=case_id,
            producer_link=producer_link,
            workload_digest=workload_digest,
        )
        if canonical_hash in contract_digests or producer_link in producer_links:
            raise ProspectiveHandoffError(
                "the three frozen contract hashes and links must be distinct"
            )
        if expected_confirmation_id in confirmation_ids:
            raise ProspectiveHandoffError(
                "the three confirmation identities must be distinct"
            )
        contract_digests.add(canonical_hash)
        producer_links.add(producer_link)
        confirmation_ids.add(expected_confirmation_id)
        cases.append(
            HandoffCase(
                case_id=case_id,
                contract=HandoffFile(
                    contract_path, contract_bytes, _digest_bytes(contract_bytes)
                ),
                confirmation=HandoffFile(
                    confirmation_path,
                    confirmation_bytes,
                    _digest_bytes(confirmation_bytes),
                ),
                source=HandoffFile(
                    source_path, source_bytes, _digest_bytes(source_bytes)
                ),
                contract_digest=producer_link,
            )
        )
    contract_paths = {path for path in expected_paths if path.startswith("contracts/")}
    confirmation_paths = {
        path for path in expected_paths if path.startswith("confirmations/")
    }
    source_paths = {
        path
        for path in expected_paths
        if path.startswith("sources/") and path != "sources/real-gpu/workload.jsonl"
    }
    if (
        contract_paths != {case.contract.relative_path for case in cases}
        or confirmation_paths != {case.confirmation.relative_path for case in cases}
        or source_paths != {case.source.relative_path for case in cases}
        or len(source_paths) != 3
        or any(
            Path(path).suffix.lower() not in {".yaml", ".yml"} for path in source_paths
        )
        or selected_paths != contract_paths | confirmation_paths | source_paths
    ):
        raise ProspectiveHandoffError(
            "handoff must contain exactly three contracts, confirmations, "
            "and source YAMLs"
        )
    complete = HandoffFile(
        ".complete", contents[".complete"], _digest_bytes(contents[".complete"])
    )
    if complete.content != b"exitspec.inferdrome-prospective-handoff.complete.v1\n":
        raise ProspectiveHandoffError("handoff completion marker bytes are invalid")
    all_files = tuple(
        HandoffFile(relative, contents[relative], _digest_bytes(contents[relative]))
        for relative in sorted(contents)
    )
    return HandoffSnapshot(
        root=root,
        manifest=HandoffFile(
            "handoff-manifest.json",
            contents["handoff-manifest.json"],
            _digest_bytes(contents["handoff-manifest.json"]),
        ),
        complete=complete,
        workload=HandoffFile(
            "sources/real-gpu/workload.jsonl",
            contents["sources/real-gpu/workload.jsonl"],
            _digest_bytes(contents["sources/real-gpu/workload.jsonl"]),
        ),
        cases=tuple(cases),
        files=all_files,
    )


def _write_snapshot_file(root: Path, file: HandoffFile) -> None:
    destination = _file_path(root, file.relative_path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(file.content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ProspectiveHandoffError(
            "prospective handoff snapshot staging failed"
        ) from None


def create_handoff_archive(
    snapshot: HandoffSnapshot, destination: Path
) -> HandoffSnapshot:
    """Create a bounded archive solely from retained snapshot bytes."""

    if destination.exists() or destination.is_symlink():
        raise ProspectiveHandoffError(
            "prospective handoff archive destination already exists"
        )
    descriptor: int | None = None
    created_inode: tuple[int, int] | None = None
    with tempfile.TemporaryDirectory(prefix="inferdrome-p1-handoff-") as temporary:
        staged = Path(temporary) / "handoff"
        staged.mkdir(mode=0o700)
        for file in snapshot.files:
            _write_snapshot_file(staged, file)
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created_inode = _inode_identity(os.fstat(descriptor))
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                with (
                    gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
                    tarfile.open(
                        fileobj=compressed, mode="w:", format=tarfile.USTAR_FORMAT
                    ) as archive,
                ):
                    for file in snapshot.files:
                        info = tarfile.TarInfo(file.relative_path)
                        info.size = len(file.content)
                        info.mode = 0o400
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        archive.addfile(info, io.BytesIO(file.content))
                output.flush()
                os.fsync(output.fileno())
            metadata = os.lstat(destination)
            if (
                created_inode is None
                or _inode_identity(metadata) != created_inode
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not 1 <= metadata.st_size <= _MAX_HANDOFF_ARCHIVE_BYTES
            ):
                raise OSError
        except (OSError, tarfile.TarError):
            if descriptor is not None:
                os.close(descriptor)
            raise ProspectiveHandoffError(
                "prospective handoff archive is unavailable"
            ) from None
    archived_bytes, archive_identity = _read_regular_once(
        destination,
        label="prospective handoff archive",
        maximum_bytes=_MAX_HANDOFF_ARCHIVE_BYTES,
    )
    if created_inode is None or archive_identity[:2] != created_inode:
        raise ProspectiveHandoffError(
            "prospective handoff archive changed after it was created"
        )
    digest = _digest_bytes(archived_bytes)
    return HandoffSnapshot(
        root=snapshot.root,
        manifest=snapshot.manifest,
        complete=snapshot.complete,
        workload=snapshot.workload,
        cases=snapshot.cases,
        files=snapshot.files,
        archive_path=destination,
        archive_sha256=digest,
        archive_size_bytes=len(archived_bytes),
    )


def handoff_archive_limit() -> int:
    return _MAX_HANDOFF_ARCHIVE_BYTES
