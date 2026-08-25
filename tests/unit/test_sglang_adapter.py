"""Strict, synthetic-only tests for the SGLang 0.5.18 boundary."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import inferdrome.normalization.sglang_0_5 as sglang_normalization
from inferdrome.adapters.sglang import (
    SGLANG_BENCHMARK_MODULE,
    SGLANG_DEFAULT_ENDPOINT,
    SGLANG_RELEASE_COMMIT,
    SGLANG_VERSION,
    SglangInvocation,
    SglangInvocationConfig,
    build_sglang_invocation,
    parse_sglang_version_output,
    preflight_sglang_invocation,
    probe_sglang_version,
    validate_sglang_invocation,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.errors import AdapterError, NormalizationError
from inferdrome.execution import ProcessCapture, ProcessTermination
from inferdrome.normalization.sglang_0_5 import (
    normalize_sglang_native,
    parse_sglang_detailed_jsonl,
    validate_sglang_normalization_report,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "sglang" / "v0_5"


def _config(tmp_path: Path, **updates: Any) -> SglangInvocationConfig:
    tokenizer_path = tmp_path / "tokenizer"
    tokenizer_path.mkdir(exist_ok=True)
    value: dict[str, Any] = {
        "python_executable": "python3",
        "endpoint": SGLANG_DEFAULT_ENDPOINT,
        "model": "synthetic/sglang-0.5.18-model",
        "tokenizer_path": str(tokenizer_path),
        "output_path": str(tmp_path / "native.jsonl"),
        "request_count": 3,
        "concurrency": 2,
        "seed": 42,
        "request_rate": Decimal("1.5"),
        "input_tokens": 4,
        "output_tokens": 3,
    }
    value.update(updates)
    return SglangInvocationConfig(**value)


def _invocation(tmp_path: Path, **updates: Any) -> SglangInvocation:
    return build_sglang_invocation(_config(tmp_path, **updates))


def _native(**updates: Any) -> bytes:
    value = json.loads(FIXTURE_ROOT.joinpath("native-detailed.jsonl").read_bytes())
    assert isinstance(value, dict)
    value.update(updates)
    return canonical_json_bytes(value) + b"\n"


def _capture(*, stdout: bytes = b"0.5.18", exit_status: int = 0) -> ProcessCapture:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return ProcessCapture(
        argv=("python3",),
        started_at=now,
        ended_at=now,
        exit_status=exit_status,
        termination=ProcessTermination.EXITED,
        stdout=stdout,
        stderr=b"",
    )


def test_invocation_is_exact_no_shell_and_canonical(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    assert invocation.argv[:3] == ("python3", "-m", SGLANG_BENCHMARK_MODULE)
    assert "--backend" in invocation.argv
    assert invocation.argv[invocation.argv.index("--backend") + 1] == "sglang"
    assert "--output-details" in invocation.argv
    assert "--stream" not in invocation.argv
    assert "--tokenize-prompt" in invocation.argv
    assert "random-ids" in invocation.argv
    assert "--random-range-ratio" in invocation.argv
    assert "inf" not in invocation.argv
    assert "sh" not in invocation.argv and "bash" not in invocation.argv
    assert not any(
        option.lower().replace("-", "") in {"apikey", "token", "authorization"}
        for option in invocation.argv
    )
    assert validate_sglang_invocation(invocation.evidence_bytes) == invocation
    assert invocation.evidence_bytes == canonical_json_bytes(
        json.loads(invocation.evidence_bytes)
    )


def test_reordered_invocation_object_has_same_validated_meaning(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    value = json.loads(invocation.evidence_bytes)
    reordered = {key: value[key] for key in reversed(tuple(value))}
    assert validate_sglang_invocation(canonical_json_bytes(reordered)) == invocation


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value.update({"backend": "vllm"}),
        lambda value: value.update({"dataset_name": "random"}),
    ],
)
def test_invocation_unknown_or_mutated_contract_rejects(
    tmp_path: Path, mutation: Any
) -> None:
    invocation = _invocation(tmp_path)
    value = json.loads(invocation.evidence_bytes)
    mutation(value)
    with pytest.raises(AdapterError):
        validate_sglang_invocation(canonical_json_bytes(value))


def test_invocation_duplicate_key_rejects(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    duplicate = invocation.evidence_bytes.replace(
        b'"backend":"sglang"', b'"backend":"sglang","backend":"sglang"'
    )
    with pytest.raises(AdapterError, match="duplicate"):
        validate_sglang_invocation(duplicate)


def test_output_preflight_rejects_existing_and_symlinked_paths(tmp_path: Path) -> None:
    existing = tmp_path / "existing.jsonl"
    existing.write_bytes(b"x")
    with pytest.raises(AdapterError):
        preflight_sglang_invocation(
            build_sglang_invocation(_config(tmp_path, output_path=str(existing)))
        )

    target = tmp_path / "target.jsonl"
    target.write_bytes(b"x")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(AdapterError):
        preflight_sglang_invocation(
            build_sglang_invocation(_config(tmp_path, output_path=str(link)))
        )


def test_output_preflight_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(AdapterError):
        preflight_sglang_invocation(
            build_sglang_invocation(
                _config(tmp_path, output_path=str(link / "x.jsonl"))
            )
        )


def test_output_preflight_accepts_real_tokenizer_and_absent_output(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    assert preflight_sglang_invocation(invocation) == invocation


def test_builder_is_pure_for_canonical_deployment_paths(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        tokenizer_path="/opt/inferdrome/models/sglang-tokenizer",
        output_path="/opt/inferdrome/evidence/sglang-detailed.jsonl",
    )
    first = build_sglang_invocation(config)
    second = build_sglang_invocation(config)
    assert first.evidence_bytes == second.evidence_bytes
    assert first.evidence_sha256 == second.evidence_sha256
    with pytest.raises(AdapterError):
        preflight_sglang_invocation(first)


@pytest.mark.parametrize(
    "content", [b"0.5.18\n", b"0.5.17", b"v0.5.18", b"0.5.18\n0.5.18"]
)
def test_version_probe_parser_is_exact(content: bytes) -> None:
    if content in {b"0.5.18", b"0.5.18\n"}:
        assert parse_sglang_version_output(content) == SGLANG_VERSION
    else:
        with pytest.raises(AdapterError):
            parse_sglang_version_output(content)


def test_version_probe_uses_injected_runner_without_network(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **kwargs: Any) -> ProcessCapture:
        calls.append(argv)
        assert kwargs["environment"] == {
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        }
        return _capture()

    result = probe_sglang_version(cwd=tmp_path, process_runner=runner)
    assert result.observed_version == SGLANG_VERSION
    assert calls == [
        (
            "python3",
            "-c",
            (
                "import importlib.metadata,sys; "
                'sys.stdout.write(importlib.metadata.version("sglang"))'
            ),
        )
    ]


def test_version_probe_failure_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="did not complete"):
        probe_sglang_version(
            cwd=tmp_path,
            process_runner=lambda _argv, **_kwargs: _capture(exit_status=1),
        )


def test_native_fixture_and_report_are_current_and_ineligible(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path,
        output_path="/opt/inferdrome/evidence/sglang-detailed.jsonl",
        model="synthetic/sglang-0.5.18-model",
        tokenizer_path="/opt/inferdrome/models/sglang-tokenizer",
    )
    native = FIXTURE_ROOT.joinpath("native-detailed.jsonl").read_bytes()
    golden = FIXTURE_ROOT.joinpath("normalized-report.json").read_bytes()
    result = normalize_sglang_native(native, invocation, synthetic_only=True)
    assert result.report_bytes == golden
    assert result.report.evidence_eligible is False
    assert result.report.request_plan_binding == "UNAVAILABLE"
    assert result.report.request_start_offsets == "UNAVAILABLE"
    assert result.report.canonical_request_record_v1 == "UNSUPPORTED"
    assert result.report.acceptance_verdict == "NOT_OWNED"
    assert result.report.rows[0].ttft_ns == 12_000_000
    assert result.report.rows[2].ttft_ns is None
    assert "synthetic response alpha" not in result.report_bytes.decode()
    assert validate_sglang_normalization_report(
        golden,
        invocation,
        expected_native_bytes=native,
    )


def test_native_real_shaped_nullable_and_numeric_fields_are_supported() -> None:
    native = parse_sglang_detailed_jsonl(
        _native(),
        expected_request_count=3,
        max_output_tokens=3,
    )
    assert native.tag is None
    assert native.server_info_present is False
    assert native.server_info_sha256 is None
    assert native.concurrency == Decimal("2.5")
    assert native.accept_length == Decimal(1)
    assert native.errors == ("", "", "synthetic bounded producer failure")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"backend": "vllm"}),
        lambda value: value.update({"unknown": 1}),
        lambda value: value.update({"completed": 3}),
        lambda value: value.update({"ttfts": [0, 0, 0]}),
        lambda value: value.update({"total_output_tokens": 4}),
        lambda value: value.update({"output_lens": [3, 2, 99]}),
    ],
)
def test_native_adversarial_mutations_reject_without_echo(
    tmp_path: Path, mutation: Any
) -> None:
    value = json.loads(_native())
    mutation(value)
    candidate = canonical_json_bytes(value) + b"\n"
    with pytest.raises(NormalizationError) as error:
        parse_sglang_detailed_jsonl(
            candidate,
            expected_request_count=3,
            max_output_tokens=3,
        )
    assert "synthetic response" not in str(error.value)


@pytest.mark.parametrize(
    "candidate",
    [
        _native() + _native(),
        b"\xff",
        _native().replace(
            b'"backend":"sglang"', b'"backend":"sglang","backend":"sglang"'
        ),
        _native().replace(b"0.012", b"NaN"),
    ],
)
def test_native_jsonl_shape_rejects(candidate: bytes) -> None:
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(
            candidate,
            expected_request_count=3,
            max_output_tokens=3,
        )


def test_nested_server_info_is_hashed_not_serialized(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    native = _native(server_info={"secret_like_runtime_id": "opaque-value"})
    report = normalize_sglang_native(native, invocation).report
    serialized = report.model_dump_json()
    assert "opaque-value" not in serialized
    assert report.server_info_present is True
    assert report.server_info_sha256 is not None


def test_server_info_bounds_reject() -> None:
    huge = _native(server_info={"x": "x" * 20_000})
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(huge, expected_request_count=3, max_output_tokens=3)


def test_type_coercion_and_failed_sentinel_reject() -> None:
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(
            _native(input_lens=[4, True, 2]),
            expected_request_count=3,
            max_output_tokens=3,
        )
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(
            _native(ttfts=[0.012, 0.020, 0.001]),
            expected_request_count=3,
            max_output_tokens=3,
        )
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(
            _native(output_lens=[3, 2, 1]),
            expected_request_count=3,
            max_output_tokens=3,
        )
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(
            _native(
                generated_texts=[
                    "synthetic response alpha",
                    "synthetic response beta",
                    "failed-but-content",
                ]
            ),
            expected_request_count=3,
            max_output_tokens=3,
        )


def test_normalized_timing_rejects_rfc8785_integer_overflow(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    with pytest.raises(NormalizationError):
        normalize_sglang_native(
            _native(ttfts=[9007199.254740992, 0.020, 0.0]),
            invocation,
        )


def test_random_ids_and_concurrency_bind_to_invocation(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    with pytest.raises(NormalizationError):
        normalize_sglang_native(_native(input_lens=[4, 4, 2]), invocation)
    with pytest.raises(NormalizationError):
        normalize_sglang_native(_native(max_concurrent_requests=3), invocation)


def test_normalized_report_cross_input_substitution_rejects(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    result = normalize_sglang_native(_native(), invocation)
    other = invocation
    forged = result.report.model_copy(
        update={"invocation_sha256": "sha256:" + "0" * 64}
    )
    with pytest.raises(NormalizationError):
        validate_sglang_normalization_report(
            canonical_json_bytes(forged.model_dump(mode="json")), other
        )


def test_normalized_report_fingerprint_and_row_order_are_bound(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    native = _native()
    result = normalize_sglang_native(native, invocation)
    forged_fingerprint = result.report.model_copy(
        update={"native_schema_fingerprint": "sha256:" + "0" * 64}
    )
    with pytest.raises(NormalizationError):
        validate_sglang_normalization_report(
            canonical_json_bytes(forged_fingerprint.model_dump(mode="json")),
            invocation,
            expected_native_bytes=native,
        )
    forged_rows = result.report.model_copy(
        update={
            "rows": (
                result.report.rows[1],
                result.report.rows[0],
                result.report.rows[2],
            )
        }
    )
    with pytest.raises(NormalizationError):
        validate_sglang_normalization_report(
            canonical_json_bytes(forged_rows.model_dump(mode="json")),
            invocation,
            expected_native_bytes=native,
        )


def test_normalized_report_native_replay_rejects_row_tampering(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    native = _native()
    result = normalize_sglang_native(native, invocation)
    forged_row = result.report.rows[0].model_copy(update={"ttft_ns": 13_000_000})
    forged = result.report.model_copy(
        update={"rows": (forged_row, result.report.rows[1], result.report.rows[2])}
    )
    with pytest.raises(NormalizationError):
        validate_sglang_normalization_report(
            canonical_json_bytes(forged.model_dump(mode="json")),
            invocation,
            expected_native_bytes=native,
        )


def test_normalized_report_size_bound_rejects_before_parse(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    with pytest.raises(NormalizationError):
        validate_sglang_normalization_report(b" " * 2_097_153, invocation)


def test_normalizer_emits_only_reports_accepted_by_size_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    monkeypatch.setattr(sglang_normalization, "_MAX_REPORT_BYTES", 1)
    with pytest.raises(NormalizationError):
        normalize_sglang_native(_native(), invocation)


def test_server_info_integer_domain_failure_is_bounded() -> None:
    value = json.loads(_native())
    value["server_info"] = 9_007_199_254_740_993
    candidate = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    with pytest.raises(NormalizationError):
        parse_sglang_detailed_jsonl(
            candidate,
            expected_request_count=3,
            max_output_tokens=3,
        )


def test_no_sglang_literals_enter_frozen_public_schema_files() -> None:
    frozen = REPOSITORY_ROOT / "schemas" / "public" / "v1"
    assert not any(
        "sglang" in path.read_text(encoding="utf-8").lower()
        for path in frozen.glob("*.json")
    )
    assert SGLANG_RELEASE_COMMIT.startswith("71de97")


def test_output_destination_is_not_created_by_builder(tmp_path: Path) -> None:
    output = tmp_path / "native.jsonl"
    _invocation(tmp_path)
    assert not output.exists()
    assert os.path.isdir(tmp_path)
