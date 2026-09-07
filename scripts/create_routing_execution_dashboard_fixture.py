#!/usr/bin/env python3
"""Create one local-only v2 execution fixture for dashboard/browser rehearsal.

This helper uses an injected in-process transport and virtual clock. It never
starts Docker, a provider client, a GPU process, or a network listener. The
sealed package deliberately retains only the existing routing-execution schema;
fixture provenance lives alongside it, not inside its closed four-file package.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from inferdrome.deployment.manual_host import (
    ManualHostInput,
    input_template,
    prepare_artifacts,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.executor import (
    ManualMonotonicClock,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.loopback import vllm_0_26_metrics
from inferdrome.routing_execution.transport import TransportResponse

_ROOT = Path(__file__).resolve().parents[1]
_PROVENANCE = "FIXTURE_GENERATED_LOCAL_NOT_PROVIDER_EVIDENCE"
_PACKAGE_NAME = "routing-execution-package"
_PROVENANCE_NAME = "fixture-provenance.json"


class _FixtureTransport:
    """Socket-free local endpoint behavior for a valid sealed package."""

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        del timeout_ms
        if path == "/v1/models":
            return TransportResponse(200, b'{"data":[{"id":"Qwen/Qwen3-8B"}]}')
        if path == "/health":
            return TransportResponse(200, b"")
        if path == "/metrics":
            return TransportResponse(
                200,
                vllm_0_26_metrics(running=4 if origin.endswith(".2:8000") else 1),
            )
        raise AssertionError("fixture endpoint path is unsupported")

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        del origin, body, timeout_ms
        if path != "/v1/chat/completions":
            raise AssertionError("fixture endpoint path is unsupported")
        return TransportResponse(
            200,
            b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}',
        )

    def close(self) -> None:
        return None


def _source_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("fixture source commit is unavailable") from None
    if len(result) != 40 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ValueError("fixture source commit is invalid")
    return result


def _input_value(source_commit: str) -> dict[str, Any]:
    value = input_template()
    value.update(
        {
            "source_commit": source_commit,
            "instance_id": "0123456789abcdef0123456789abcdef",
            "region": "synthetic-region",
            "instance_type": "gpu_2x_a100",
            "gpu_uuids": [
                "GPU-00000000-0000-0000-0000-000000000001",
                "GPU-00000000-0000-0000-0000-000000000002",
            ],
            "uid": 2000,
            "gid": 2000,
            "runner_image": {
                "reference": "example.invalid/test-runner@"
                + sha256_digest(b"routing-execution-dashboard-runner")
            },
            "serving_image": {
                "reference": "example.invalid/test-engine@"
                + sha256_digest(b"routing-execution-dashboard-engine")
            },
            "model_path": "/srv/test-model",
            "preparation_path": "/srv/test-inputs",
            "evidence_path": "/srv/test-evidence",
            "compose_project": "synthetic-campaign",
            "container_subnet": "172.29.71.0/24",
            "endpoint_ipv4": ["172.29.71.2", "172.29.71.3"],
            "request_timeout_ms": 1000,
        }
    )
    cleanup = value["cleanup"]
    assert isinstance(cleanup, dict)
    cleanup.update(
        {
            "instance_id": value["instance_id"],
            "accountable_operator": "synthetic-operator",
            "terminate_by_utc": "2030-01-01T00:00:00Z",
        }
    )
    return value


def _safe_parent(value: Path) -> Path:
    selected = value.absolute()
    try:
        os.lstat(selected)
    except OSError:
        raise ValueError("fixture output parent is unavailable") from None
    if os.path.islink(selected) or not selected.is_dir():
        raise ValueError("fixture output parent is unsafe")
    return selected


def create(output_parent: Path) -> dict[str, str]:
    parent = _safe_parent(output_parent)
    package = parent / _PACKAGE_NAME
    provenance = parent / _PROVENANCE_NAME
    if package.exists() or provenance.exists():
        raise ValueError("fixture output already exists")
    spec = ManualHostInput.model_validate_json(
        canonical_json_bytes(_input_value(_source_commit()))
    )
    files = prepare_artifacts(spec, _ROOT)
    sealed = run_execution_from_bytes(
        files["deployment-config.json"],
        files["selected-workload.jsonl"],
        package,
        transport_factory=_FixtureTransport,
        clock=ManualMonotonicClock(),
    )
    record = {
        "schema_version": "inferdrome.routing-execution-dashboard-fixture.v1",
        "fixture_provenance": _PROVENANCE,
        "package_retained_digest": sealed.retained_digest,
        "provider_action": "NONE",
        "docker_action": "NONE",
        "gpu_action": "NONE",
    }
    try:
        descriptor = os.open(provenance, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json_bytes(record))
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise ValueError("fixture provenance could not be published") from None
    return {
        "root": str(sealed.path),
        "retained_digest": sealed.retained_digest,
        "fixture_provenance": _PROVENANCE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a local-only routing-execution dashboard fixture"
    )
    parser.add_argument("--output-parent", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                create(arguments.output_parent), sort_keys=True, separators=(",", ":")
            )
        )
        return 0
    except (ValueError, OSError):
        parser.error("routing-execution dashboard fixture was rejected")
    raise AssertionError("fixture command dispatch is incomplete")


if __name__ == "__main__":
    raise SystemExit(main())
