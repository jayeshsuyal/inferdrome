"""Observer subprocess for Vast: environment-hidden GPUs, not device isolation."""

from __future__ import annotations

import argparse
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from inferdrome.deployment.vast_process import (
    load_plan,
    module_digests,
    parse_deadline,
    read_private,
    routing_config,
    write_private,
)
from inferdrome.errors import InferdromeError
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.executor import (
    load_config_bytes,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.package import verify_execution_package
from inferdrome.routing_execution.vast_contracts import VastRoutingConfig


def run(directory: Path, digest: str) -> None:
    spec = load_plan(directory, digest)
    if datetime.now(UTC) >= parse_deadline(spec.cleanup.terminate_by_utc):
        raise ValueError("observer deadline expired")
    attempt = canonical_json_bytes({"plan_sha256": digest, "no_retry": True})
    if read_private(directory / "execution-attempt.json") != attempt:
        raise ValueError("observer execution attempt differs")
    if (
        os.getuid() != spec.uid
        or os.getgid() != spec.gid
        or os.environ.get("CUDA_VISIBLE_DEVICES") != ""
        or os.environ.get("NVIDIA_VISIBLE_DEVICES") != "void"
        or module_digests() != spec.module_sha256
    ):
        raise ValueError("observer process boundary differs")
    config_bytes = read_private(directory / "execution-config.json")
    config = load_config_bytes(config_bytes)
    observation = read_private(directory / "runtime-observation-ready.json")
    expected_config = routing_config(spec, json.loads(observation))
    if not isinstance(config, VastRoutingConfig) or (
        config.source_commit != spec.source_commit
        or config.container_image != spec.container_image
        or config.artifact_provenance.runtime_observation_sha256
        != sha256_digest(observation)
        or config_bytes != canonical_json_bytes(expected_config.model_dump(mode="json"))
    ):
        raise ValueError("observer source identities differ")
    write_private(directory / "observer-attempt.json", attempt)
    sealed = run_execution_from_bytes(
        config_bytes,
        read_private(directory / "selected-workload.jsonl"),
        Path(spec.evidence_path) / "routing-execution-package",
    )
    verify_execution_package(sealed.path, expected_digest=sealed.retained_digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--execute-plan", required=True)
    args = parser.parse_args(argv)
    try:
        spec = load_plan(args.directory, args.execute_plan)
        seconds = min(
            spec.campaign_timeout_seconds,
            (
                parse_deadline(spec.cleanup.terminate_by_utc) - datetime.now(UTC)
            ).total_seconds(),
        )
        if seconds <= 0:
            raise ValueError("observer deadline expired")
        signal.signal(signal.SIGALRM, _expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        run(args.directory, args.execute_plan)
        return 0
    except (ValueError, OSError, InferdromeError):
        print(json.dumps({"status": "OBSERVER_FAILED"}))
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _expired(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    raise ValueError("observer deadline expired")


if __name__ == "__main__":
    raise SystemExit(main())
