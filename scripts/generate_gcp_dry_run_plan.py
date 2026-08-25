#!/usr/bin/env python3
"""Generate or check the additive GCP inventory/plan artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inferdrome.deployment import (
    GcpPlanningContext,
    canonical_gcp_plan_bytes,
    gcp_inventory_schema,
    gcp_plan_schema,
    parse_deployment_spec_json,
    parse_gcp_inventory_json,
    plan_gcp_dry_run,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC = (
    REPOSITORY_ROOT / "deployments" / "v1" / "examples" / "gcp-dry-run-reference.json"
)
INVENTORY = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "gcp-v1" / "synthetic-inventory.json"
)
CONTEXT = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "gcp-v1"
    / "synthetic-planning-context.json"
)
INVENTORY_SCHEMA = (
    REPOSITORY_ROOT / "schemas" / "deployment" / "v1" / "gcp-inventory.schema.json"
)
PLAN_SCHEMA = REPOSITORY_ROOT / "schemas" / "deployment" / "v1" / "gcp-plan.schema.json"
PLAN = REPOSITORY_ROOT / "plans" / "gcp" / "v1" / "gcp-dry-run-reference.plan.json"


def _pretty(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _render() -> dict[Path, bytes]:
    spec = parse_deployment_spec_json(SPEC.read_bytes())
    inventory = parse_gcp_inventory_json(INVENTORY.read_bytes())
    context = GcpPlanningContext.model_validate_json(CONTEXT.read_bytes())
    plan = plan_gcp_dry_run(spec, inventory, context)
    return {
        INVENTORY_SCHEMA: _pretty(gcp_inventory_schema()),
        PLAN_SCHEMA: _pretty(gcp_plan_schema()),
        PLAN: canonical_gcp_plan_bytes(plan),
    }


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
            print("GCP dry-run artifacts are stale")
            return 1
        print("GCP dry-run artifacts are current (3 files)")
        return 0
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print("generated GCP dry-run artifacts (3 files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
