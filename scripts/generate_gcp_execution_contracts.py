#!/usr/bin/env python3
"""Generate/check additive GCP guarded-execution schemas and fixture."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from inferdrome.deployment import (
    ARM_CONFIRMATION,
    GcpPlanningContext,
    canonical_gcp_execution_arm_bytes,
    gcp_execution_contract_schemas,
    issue_gcp_execution_arm,
    parse_deployment_spec_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "deployments/v1/examples/gcp-dry-run-reference.json"
INVENTORY = ROOT / "tests/fixtures/gcp-v1/synthetic-inventory.json"
CONTEXT = ROOT / "tests/fixtures/gcp-v1/synthetic-planning-context.json"
SCHEMA_ROOT = ROOT / "schemas/deployment/v1"
ARM_FIXTURE = ROOT / "tests/fixtures/gcp-v1/synthetic-execution-arm.json"


def _pretty(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode()


def _render() -> dict[Path, bytes]:
    spec = parse_deployment_spec_json(SPEC.read_bytes())
    inventory = parse_gcp_inventory_json(INVENTORY.read_bytes())
    context = GcpPlanningContext.model_validate_json(CONTEXT.read_bytes())
    plan = plan_gcp_dry_run(spec, inventory, context)
    arm = issue_gcp_execution_arm(
        plan=plan,
        expected_spec=spec,
        expected_inventory=inventory,
        expected_context=context,
        controller_id="ctl-a1b2c3d4",
        nonce="0123456789abcdef0123456789abcdef",
        issued_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        max_controller_duration_seconds=600,
        confirmation=ARM_CONFIRMATION,
    )
    rendered = {
        SCHEMA_ROOT / filename: _pretty(schema)
        for filename, schema in gcp_execution_contract_schemas().items()
    }
    rendered[ARM_FIXTURE] = canonical_gcp_execution_arm_bytes(arm)
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = _render()
    mismatches = [
        path
        for path, expected in rendered.items()
        if not path.is_file() or path.read_bytes() != expected
    ]
    if args.check:
        if mismatches:
            print("GCP guarded-execution artifacts are stale")
            return 1
        print(f"GCP guarded-execution artifacts are current ({len(rendered)} files)")
        return 0
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"generated GCP guarded-execution artifacts ({len(rendered)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
