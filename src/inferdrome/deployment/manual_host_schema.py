"""Snapshots for the additive manual-host profile/input and v2 bridge only."""

from __future__ import annotations

import argparse
from pathlib import Path

from inferdrome.deployment.manual_host import (
    PROFILE_ID,
    ManualHostInput,
    input_template,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.manual_host_contracts import (
    ManualHostExecutedManifest,
    ManualHostRoutingConfig,
)


def artifacts() -> dict[str, bytes]:
    values = {}
    values["schemas/manual-host/v1/manual-host-input.schema.json"] = (
        ManualHostInput.model_json_schema()
    )
    v2 = "schemas/routing-execution/v2/"
    values[v2 + "routing-execution-config.schema.json"] = (
        ManualHostRoutingConfig.model_json_schema()
    )
    values[v2 + "routing-executed-manifest.schema.json"] = (
        ManualHostExecutedManifest.model_json_schema()
    )
    values["deployments/manual-host-v1/lambda-two-a100-pcie.input-template.json"] = (
        input_template()
    )
    values["deployments/manual-host-v1/lambda-two-a100-pcie.profile.json"] = {
        "schema_version": "inferdrome.manual-host-profile.v1",
        "profile_id": PROFILE_ID,
        "provider": "LAMBDA",
        "provisioning": "OPERATOR_SUPPLIED_VM",
        "host_count": 1,
        "accelerator_model": "NVIDIA A100-PCIE-40GB",
        "accelerator_count": 2,
        "nominal_memory_gb_per_gpu": 40,
        "serving_engine_count": 2,
        "tensor_parallel_size": 1,
        "one_engine_per_gpu": True,
        "availability_observation": None,
        "price_observation": None,
        "lifecycle_protection": "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
        "hardware_assertion": "DOCUMENTED_MATCH_NOT_HOST_OBSERVATION",
    }
    return {name: canonical_json_bytes(value) + b"\n" for name, value in values.items()}


def check(repository: Path) -> None:
    for name, content in artifacts().items():
        if (repository / name).read_bytes() != content:
            raise ValueError("manual host snapshot is not current")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if args.write:
        for name, content in artifacts().items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    else:
        check(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
