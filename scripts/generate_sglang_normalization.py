#!/usr/bin/env python3
"""Generate the synthetic SGLang 0.5.18 normalization contract artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from inferdrome.adapters.sglang import (
    SGLANG_BACKEND,
    SGLANG_BENCHMARK_MODULE,
    SGLANG_DEFAULT_ENDPOINT,
    SGLANG_RELEASE_COMMIT,
    SGLANG_SOURCE_REPOSITORY,
    SGLANG_VERSION,
    SglangInvocationConfig,
    build_sglang_invocation,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.normalization.sglang_0_5 import (
    SGLANG_NORMALIZATION_SCHEMA_ID,
    SGLANG_NORMALIZATION_SCHEMA_VERSION,
    SglangNormalizationReport,
    normalize_sglang_native,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "producer" / "sglang-normalization-v1.schema.json"
)
PROFILE_PATH = (
    REPOSITORY_ROOT / "profiles" / "v1" / "sglang-0.5.18-normalization-profile.json"
)
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "sglang" / "v0_5"
FIXTURE_PATH = FIXTURE_DIRECTORY / "native-detailed.jsonl"
GOLDEN_PATH = FIXTURE_DIRECTORY / "normalized-report.json"
MANIFEST_PATH = FIXTURE_DIRECTORY / "MANIFEST.sha256"


def _fixture_invocation() -> Any:
    config = SglangInvocationConfig(
        python_executable="python3",
        endpoint=SGLANG_DEFAULT_ENDPOINT,
        model="synthetic/sglang-0.5.18-model",
        tokenizer_path="/opt/inferdrome/models/sglang-tokenizer",
        output_path="/opt/inferdrome/evidence/sglang-detailed.jsonl",
        request_count=3,
        concurrency=2,
        seed=42,
        request_rate=Decimal("1.5"),
        input_tokens=4,
        output_tokens=3,
    )
    return build_sglang_invocation(config)


def _native_value() -> dict[str, Any]:
    return {
        "accept_length": 1,
        "backend": SGLANG_BACKEND,
        "concurrency": 2.5,
        "completed": 2,
        "dataset_name": "random-ids",
        "duration": 1.0,
        "errors": ["", "", "synthetic bounded producer failure"],
        "generated_texts": [
            "synthetic response alpha",
            "synthetic response beta",
            "",
        ],
        "input_lens": [4, 4, 4],
        "input_throughput": 8.0,
        "itls": [[0.001, 0.002], [0.003], []],
        "max_concurrency": 2,
        "max_output_tokens_per_s": 5.0,
        "max_concurrent_requests": 2,
        "mean_e2e_latency_ms": 10.0,
        "mean_itl_ms": 2.0,
        "mean_tpot_ms": 2.0,
        "mean_ttft_ms": 16.0,
        "median_e2e_latency_ms": 10.0,
        "median_itl_ms": 2.0,
        "median_tpot_ms": 2.0,
        "median_ttft_ms": 16.0,
        "output_lens": [3, 2, 0],
        "output_throughput": 5.0,
        "p90_e2e_latency_ms": 12.0,
        "p90_itl_ms": 3.0,
        "p90_tpot_ms": 3.0,
        "p90_ttft_ms": 20.0,
        "p95_e2e_latency_ms": 12.0,
        "p95_itl_ms": 3.0,
        "p95_tpot_ms": 3.0,
        "p95_ttft_ms": 20.0,
        "p99_e2e_latency_ms": 12.0,
        "p99_itl_ms": 3.0,
        "p99_tpot_ms": 3.0,
        "p99_ttft_ms": 20.0,
        "random_input_len": 4,
        "random_output_len": 3,
        "random_range_ratio": 0.0,
        "request_rate": 1.5,
        "request_throughput": 2.0,
        "server_info": None,
        "sharegpt_output_len": None,
        "std_e2e_latency_ms": 1.0,
        "std_itl_ms": 0.5,
        "std_tpot_ms": 0.5,
        "std_ttft_ms": 1.0,
        "tag": None,
        "total_input_text_tokens": 8,
        "total_input_tokens": 8,
        "total_input_vision_tokens": 0,
        "total_output_tokens": 5,
        "total_output_tokens_retokenized": 5,
        "total_throughput": 13.0,
        "ttfts": [0.012, 0.020, 0.0],
    }


def _profile() -> dict[str, Any]:
    return {
        "profile_id": "inferdrome.sglang-0.5.18-normalization-profile.v1",
        "schema_version": SGLANG_NORMALIZATION_SCHEMA_VERSION,
        "source": {
            "repository": SGLANG_SOURCE_REPOSITORY,
            "release": SGLANG_VERSION,
            "commit": SGLANG_RELEASE_COMMIT,
        },
        "producer": {
            "benchmark_module": SGLANG_BENCHMARK_MODULE,
            "backend": SGLANG_BACKEND,
            "streaming": True,
            "native_endpoint_semantics": "/generate",
        },
        "native_metrics": {
            "ttft": (
                "request start through first cumulative response chunk with "
                "non-empty text"
            ),
            "itl": (
                "intervals between later qualifying cumulative chunks, "
                "apportioned over newly reported completion tokens"
            ),
            "persisted_fields": [
                "input_lens",
                "output_lens",
                "ttfts",
                "itls",
                "generated_texts",
                "errors",
            ],
        },
        "claims_boundary": {
            "evidence_eligible": False,
            "request_plan_binding": "UNAVAILABLE",
            "request_start_offsets": "UNAVAILABLE",
            "canonical_request_record_v1": "UNSUPPORTED",
            "acceptance_verdict": "NOT_OWNED",
            "statement": (
                "This is an additive producer capability boundary, not evidence "
                "or an acceptance result."
            ),
        },
        "upstream_persisted_output_gaps": [
            "request IDs are not persisted",
            "request start times are not persisted",
            "custom dataset loading can skip malformed rows",
            "accepted custom dataset rows can be shuffled",
        ],
    }


def render_artifacts() -> dict[Path, bytes]:
    invocation = _fixture_invocation()
    native_bytes = canonical_json_bytes(_native_value()) + b"\n"
    normalization = normalize_sglang_native(
        native_bytes,
        invocation,
        synthetic_only=True,
    )
    schema = SglangNormalizationReport.model_json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SGLANG_NORMALIZATION_SCHEMA_ID
    schema["title"] = "Inferdrome SGLang 0.5.18 normalization report v1"
    rendered = {
        SCHEMA_PATH: (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        PROFILE_PATH: (json.dumps(_profile(), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        FIXTURE_PATH: native_bytes,
        GOLDEN_PATH: normalization.report_bytes,
    }
    manifest_lines = [
        f"{hashlib.sha256(content).hexdigest()}  {path.name}"
        for path, content in sorted(
            (
                (path, content)
                for path, content in rendered.items()
                if path.parent == FIXTURE_DIRECTORY
            ),
            key=lambda item: item[0].name,
        )
    ]
    rendered[MANIFEST_PATH] = ("\n".join(manifest_lines) + "\n").encode("utf-8")
    return rendered


def check_artifacts(rendered: dict[Path, bytes]) -> int:
    mismatches = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path, expected in rendered.items()
        if not path.is_file() or path.read_bytes() != expected
    ]
    if mismatches:
        print("SGLang normalization artifacts are stale or missing:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print(f"SGLang normalization artifacts are current ({len(rendered)} files)")
    return 0


def write_artifacts(rendered: dict[Path, bytes]) -> int:
    for path in rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
    for path, content in rendered.items():
        path.write_bytes(content)
    print(f"generated {len(rendered)} SGLang normalization artifacts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_artifacts()
    return check_artifacts(rendered) if args.check else write_artifacts(rendered)


if __name__ == "__main__":
    raise SystemExit(main())
