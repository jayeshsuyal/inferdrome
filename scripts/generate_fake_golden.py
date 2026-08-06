#!/usr/bin/env python3
"""Generate the committed deterministic fake-adapter golden artifacts."""

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from inferdrome.adapters.fake import FakeAdapter, FakeObservation
from inferdrome.domain.request_record import RequestStatus
from inferdrome.metrics import reduce_measurements
from inferdrome.resolution import resolve_experiment

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "fake" / "v1"
EXAMPLE_PATH = REPOSITORY_ROOT / "examples" / "fake-smoke.yaml"
RUN_ID = "run-99999999999999999999999999999999"
STARTED_AT = datetime(2026, 8, 5, 23, 30, tzinfo=UTC)


def render_fixture() -> dict[Path, bytes]:
    resolution = resolve_experiment(EXAMPLE_PATH, run_id=RUN_ID)
    observations = (
        FakeObservation(
            status=RequestStatus.SUCCESS,
            input_tokens=4,
            output_tokens=2,
            start_offset_ns=0,
            ttft_ns=10_000_000,
            itl_ns=(20_000_000, 20_000_000),
            response_content="alpha beta",
            producer_error=None,
        ),
        FakeObservation(
            status=RequestStatus.FAILED,
            input_tokens=5,
            output_tokens=0,
            start_offset_ns=100_000_000,
            ttft_ns=None,
            itl_ns=(),
            response_content=None,
            producer_error="synthetic producer failure",
        ),
    )
    fake = FakeAdapter().execute(
        resolution.resolved_spec,
        resolution.request_plan,
        observations=observations,
        started_at=STARTED_AT,
        measurement_window_ns=1_000_000_000,
    )
    reduction = reduce_measurements(fake.execution, fake.request_records)
    rendered = {
        FIXTURE_DIRECTORY / "native-result.json": fake.native_result_bytes,
        FIXTURE_DIRECTORY / "request-records.jsonl": fake.request_records_bytes,
        FIXTURE_DIRECTORY / "execution.json": fake.execution_bytes,
        FIXTURE_DIRECTORY
        / "metric-definitions.json": reduction.metric_definitions_bytes,
        FIXTURE_DIRECTORY / "measurements.json": reduction.measurements_bytes,
    }
    manifest_lines = [
        f"{hashlib.sha256(content).hexdigest()}  {path.name}"
        for path, content in sorted(rendered.items())
    ]
    rendered[FIXTURE_DIRECTORY / "MANIFEST.sha256"] = (
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
        print("fake golden artifacts are stale or missing:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print(f"fake golden artifacts are current ({len(rendered)} files)")
    return 0


def write_fixture(rendered: dict[Path, bytes]) -> int:
    FIXTURE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for path, content in rendered.items():
        path.write_bytes(content)
    print(f"generated {len(rendered)} fake golden artifacts in {FIXTURE_DIRECTORY}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_fixture()
    return check_fixture(rendered) if args.check else write_fixture(rendered)


if __name__ == "__main__":
    raise SystemExit(main())
