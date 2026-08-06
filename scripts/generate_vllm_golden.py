#!/usr/bin/env python3
"""Generate normalized goldens from the pinned real vLLM 0.26.0 capture."""

import argparse
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inferdrome.metrics import reduce_measurements
from inferdrome.normalization import (
    build_vllm_execution_record,
    normalize_vllm_native,
)
from inferdrome.resolution import resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "vllm" / "v0_26"
GOLDEN_DIRECTORY = FIXTURE_ROOT / "golden"
CAPTURE_PATH = (
    REPOSITORY_ROOT
    / "spikes"
    / "vllm-0.26.0"
    / "fixtures"
    / "client-macos-empty"
    / "native"
    / "benchmark-result.json"
)
RUN_ID = "run-77777777777777777777777777777777"
STARTED_AT = datetime(2026, 8, 5, 22, 18, tzinfo=UTC)
EXPECTED_METADATA = {
    "inferdrome_producer_version": "0.26.0",
    "inferdrome_spike_id": "vllm-0.26.0-client-capability",
}
EXPECTED_TOKENIZER_ID = (
    "/private/tmp/inferdrome-capability-fixture-workspace-clean-2/"
    "spikes/vllm-0.26.0/tokenizer"
)


def render_fixture() -> dict[Path, bytes]:
    resolution = resolve_experiment(FIXTURE_ROOT / "source.yaml", run_id=RUN_ID)
    native_bytes = CAPTURE_PATH.read_bytes()
    normalization = normalize_vllm_native(
        native_bytes,
        resolution.resolved_spec,
        resolution.request_plan,
        expected_metadata=EXPECTED_METADATA,
        expected_tokenizer_id=EXPECTED_TOKENIZER_ID,
    )
    execution = build_vllm_execution_record(
        normalization,
        resolution.request_plan,
        native_bytes,
        started_at=STARTED_AT,
        ended_at=STARTED_AT + timedelta(seconds=1),
        producer_exit_status=0,
    )
    reduction = reduce_measurements(execution, normalization.request_records)
    rendered = {
        GOLDEN_DIRECTORY / "native-result.json": native_bytes,
        GOLDEN_DIRECTORY
        / "request-records.jsonl": normalization.request_records_bytes,
        GOLDEN_DIRECTORY / "execution.json": reduction.execution_bytes,
        GOLDEN_DIRECTORY
        / "metric-definitions.json": reduction.metric_definitions_bytes,
        GOLDEN_DIRECTORY / "measurements.json": reduction.measurements_bytes,
    }
    manifest_lines = [
        f"{hashlib.sha256(content).hexdigest()}  {path.name}"
        for path, content in sorted(rendered.items())
    ]
    rendered[GOLDEN_DIRECTORY / "MANIFEST.sha256"] = (
        "\n".join(manifest_lines) + "\n"
    ).encode()
    return rendered


def check_fixture(rendered: dict[Path, bytes]) -> int:
    mismatches = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path, expected in rendered.items()
        if not path.exists() or path.read_bytes() != expected
    ]
    if mismatches:
        print("vLLM golden artifacts are stale or missing:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print(f"vLLM golden artifacts are current ({len(rendered)} files)")
    return 0


def write_fixture(rendered: dict[Path, bytes]) -> int:
    GOLDEN_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for path, content in rendered.items():
        path.write_bytes(content)
    print(f"generated {len(rendered)} vLLM golden artifacts in {GOLDEN_DIRECTORY}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_fixture()
    return check_fixture(rendered) if args.check else write_fixture(rendered)


if __name__ == "__main__":
    raise SystemExit(main())
