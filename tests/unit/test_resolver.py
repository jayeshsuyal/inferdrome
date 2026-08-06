"""Resolver behavior over untrusted source YAML and custom JSONL."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from inferdrome.domain.digests import DigestDomain, digest_bytes
from inferdrome.domain.experiment import NativeOutputSensitivity
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.request_plan import DigestOnlyPrompt, InlinePrompt
from inferdrome.domain.states import Replayability
from inferdrome.errors import ResolutionError, SourceInputError
from inferdrome.resolution.resolver import resolve_experiment
from inferdrome.resolution.workload import parse_custom_workload
from inferdrome.resolution.yaml_loader import load_strict_yaml

RUN_ID = "run-11111111111111111111111111111111"


def _write_source(
    root: Path,
    *,
    prompt_policy: str = "include",
    include_expected_hash: bool = True,
    title: str | None = None,
    endpoint: str = "http://127.0.0.1:8000",
    mode: str = "attached_endpoint",
) -> tuple[Path, bytes, dict[str, Any]]:
    workload_directory = root / "workloads"
    workload_directory.mkdir()
    workload_bytes = b"".join(
        [
            b'{"prompt":"alpha"}\n',
            b'{"prompt":"beta"}\n',
            b'{"prompt":"gamma"}\n',
        ]
    )
    (workload_directory / "prompts.jsonl").write_bytes(workload_bytes)

    if mode == "attached_endpoint":
        execution: dict[str, Any] = {"mode": "attached_endpoint"}
        target: dict[str, Any] = {
            "engine": "vllm",
            "endpoint": endpoint,
            "model": "acme/model",
            "model_revision": "a" * 40,
            "tokenizer_revision": "b" * 40,
            "engine_version": "0.26.0",
        }
    else:
        execution = {"mode": "synthetic_fixture"}
        target = {"engine": "fake"}

    identity: dict[str, Any] = {"id": "resolver-test"}
    if title is not None:
        identity["title"] = title
    document: dict[str, Any] = {
        "schema_version": "inferdrome.source-experiment.v1",
        "experiment": identity,
        "execution": execution,
        "target": target,
        "workload": {
            "path": "workloads/prompts.jsonl",
            "prompt_content_policy": prompt_policy,
            "requested_output_tokens": 8,
            "temperature": 0,
            "seed": 7,
        },
        "traffic": {
            "kind": "concurrent",
            "concurrency": 2,
            "warmup_requests": 1,
            "measured_requests": 2,
        },
    }
    if include_expected_hash:
        document["workload"]["sha256"] = sha256_digest(workload_bytes)
    source_path = root / "experiment.yaml"
    source_path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )
    return source_path, workload_bytes, document


def test_resolver_expands_defaults_and_freezes_request_order(tmp_path: Path) -> None:
    source_path, workload_bytes, _ = _write_source(tmp_path)

    result = resolve_experiment(source_path, run_id=RUN_ID)

    assert result.run_id == RUN_ID
    assert result.workload_bytes == workload_bytes
    assert result.resolved_spec.experiment.title == "resolver-test"
    assert result.resolved_spec.execution.producer_version == "0.26.0"
    assert result.resolved_spec.execution.max_measured_requests == 2
    assert result.resolved_spec.workload.sha256 == sha256_digest(workload_bytes)
    assert result.source_spec_digest == digest_bytes(
        DigestDomain.SOURCE_SPEC, result.source_bytes
    )
    assert result.request_plan.replayability is Replayability.FULL
    assert [request.request_id for request in result.request_plan.requests] == [
        "req-00000000",
        "req-00000001",
    ]
    assert [
        request.producer_request_id for request in result.request_plan.requests
    ] == [f"{RUN_ID}-0", f"{RUN_ID}-1"]
    assert isinstance(result.request_plan.requests[0].prompt, InlinePrompt)
    assert result.request_plan.requests[0].prompt.text == "alpha"
    assert json.loads(result.resolved_spec_bytes) == result.resolved_spec.model_dump(
        mode="json"
    )


def test_same_inputs_and_run_id_resolve_byte_identically(tmp_path: Path) -> None:
    source_path, _, _ = _write_source(tmp_path)

    first = resolve_experiment(source_path, run_id=RUN_ID)
    second = resolve_experiment(source_path, run_id=RUN_ID)

    assert first.resolved_spec_bytes == second.resolved_spec_bytes
    assert first.request_plan_bytes == second.request_plan_bytes
    assert first.execution_fingerprint == second.execution_fingerprint
    assert first.request_plan_digest == second.request_plan_digest


def test_human_title_does_not_change_execution_fingerprint(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_source, _, _ = _write_source(first_root, title="First title")
    second_source, _, _ = _write_source(second_root, title="Second title")

    first = resolve_experiment(first_source, run_id=RUN_ID)
    second = resolve_experiment(second_source, run_id=RUN_ID)

    assert first.execution_fingerprint == second.execution_fingerprint
    assert first.source_spec_digest != second.source_spec_digest
    assert first.resolved_spec_bytes != second.resolved_spec_bytes


def test_hash_only_policy_forces_limited_replayability(tmp_path: Path) -> None:
    source_path, _, _ = _write_source(tmp_path, prompt_policy="hash_only")

    result = resolve_experiment(source_path, run_id=RUN_ID)

    assert result.request_plan.replayability is Replayability.LIMITED
    assert isinstance(result.request_plan.requests[0].prompt, DigestOnlyPrompt)
    assert b'"alpha"' not in result.request_plan_bytes


def test_request_rate_decimals_are_normalized_without_floats(tmp_path: Path) -> None:
    source_path, _, document = _write_source(tmp_path)
    document["traffic"] = {
        "kind": "request_rate",
        "requests_per_second": 2,
        "burstiness": "1.25",
        "max_concurrency": 4,
        "warmup_requests": 1,
        "measured_requests": 2,
    }
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    result = resolve_experiment(source_path, run_id=RUN_ID)

    assert result.resolved_spec.traffic.kind == "request_rate"
    assert result.resolved_spec.traffic.requests_per_second == "2"
    assert result.resolved_spec.traffic.burstiness == "1.25"


def test_fake_resolution_derives_synthetic_sensitivity(tmp_path: Path) -> None:
    source_path, _, _ = _write_source(tmp_path, mode="synthetic_fixture")

    result = resolve_experiment(source_path, run_id=RUN_ID)

    assert result.resolved_spec.execution.producer_name == "inferdrome_fake"
    assert result.resolved_spec.target.engine == "fake"
    assert (
        result.resolved_spec.evidence.native_output_sensitivity
        is NativeOutputSensitivity.NON_SENSITIVE_FIXTURE
    )


@pytest.mark.parametrize("field", ["adapter_version", "producer_version"])
def test_fake_resolution_rejects_unknown_producer_contract(
    tmp_path: Path,
    field: str,
) -> None:
    source_path, _, document = _write_source(tmp_path, mode="synthetic_fixture")
    document["execution"][field] = "9.9.9"
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(SourceInputError, match="strict validation"):
        resolve_experiment(source_path, run_id=RUN_ID)


def test_strict_mode_requires_declared_workload_hash(tmp_path: Path) -> None:
    source_path, workload_bytes, _ = _write_source(
        tmp_path, include_expected_hash=False
    )

    with pytest.raises(ResolutionError, match="expected workload digest"):
        resolve_experiment(source_path, run_id=RUN_ID)

    result = resolve_experiment(source_path, run_id=RUN_ID, strict=False)
    assert result.resolved_spec.workload.sha256 == sha256_digest(workload_bytes)


def test_mismatched_workload_hash_fails_closed(tmp_path: Path) -> None:
    source_path, _, document = _write_source(tmp_path)
    document["workload"]["sha256"] = f"sha256:{'0' * 64}"
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResolutionError, match="does not match"):
        resolve_experiment(source_path, run_id=RUN_ID)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8000?api_key=topsecret",
        "http://user:topsecret@127.0.0.1:8000",
        "http://127.0.0.1:8000/#topsecret",
    ],
)
def test_secret_bearing_urls_fail_without_echoing_secret(
    tmp_path: Path, endpoint: str
) -> None:
    source_path, _, _ = _write_source(tmp_path, endpoint=endpoint)

    with pytest.raises(SourceInputError) as caught:
        resolve_experiment(source_path, run_id=RUN_ID)
    assert "topsecret" not in str(caught.value)


def test_strict_mode_requires_model_and_tokenizer_revisions(tmp_path: Path) -> None:
    source_path, _, document = _write_source(tmp_path)
    document["target"]["model_revision"] = None
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResolutionError, match="model and tokenizer revisions"):
        resolve_experiment(source_path, run_id=RUN_ID)

    assert resolve_experiment(source_path, run_id=RUN_ID, strict=False)


def test_workload_must_cover_measured_request_count(tmp_path: Path) -> None:
    source_path, _, document = _write_source(tmp_path)
    document["traffic"]["measured_requests"] = 4
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResolutionError, match="fewer prompts"):
        resolve_experiment(source_path, run_id=RUN_ID)


def test_resolved_cross_field_bounds_fail_with_safe_error(tmp_path: Path) -> None:
    source_path, _, document = _write_source(tmp_path)
    document["execution"]["max_measured_requests"] = 1
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResolutionError, match="contract invariants"):
        resolve_experiment(source_path, run_id=RUN_ID)


def test_workload_symlink_is_rejected(tmp_path: Path) -> None:
    source_path, _, _ = _write_source(tmp_path)
    workload_path = tmp_path / "workloads" / "prompts.jsonl"
    real_path = tmp_path / "real.jsonl"
    workload_path.rename(real_path)
    workload_path.symlink_to(real_path)

    with pytest.raises(SourceInputError, match="symlink"):
        resolve_experiment(source_path, run_id=RUN_ID)


def test_source_symlink_is_rejected(tmp_path: Path) -> None:
    source_path, _, _ = _write_source(tmp_path)
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(source_path)

    with pytest.raises(SourceInputError, match="unsafe"):
        resolve_experiment(alias, run_id=RUN_ID)


@pytest.mark.parametrize(
    "content",
    [
        b'{"prompt":"one","prompt":"two"}\n',
        b'{"prompt":"one","extra":true}\n',
        b'{"prompt":1}\n',
        b"\n",
        b'{"prompt":NaN}\n',
    ],
)
def test_invalid_workload_lines_fail_closed(content: bytes) -> None:
    with pytest.raises(SourceInputError):
        parse_custom_workload(content)


def test_yaml_duplicate_keys_and_aliases_are_rejected() -> None:
    with pytest.raises(SourceInputError, match="unique"):
        load_strict_yaml(b"key: first\nkey: second\n")
    with pytest.raises(SourceInputError, match="aliases"):
        load_strict_yaml(b"first: &value one\nsecond: *value\n")


def test_yaml_float_is_not_accepted_for_deterministic_decimal(tmp_path: Path) -> None:
    source_path, _, document = _write_source(tmp_path)
    document["workload"]["temperature"] = 0.5
    source_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(SourceInputError, match="strict validation"):
        resolve_experiment(source_path, run_id=RUN_ID)
