"""Generate and check the isolated routing-campaign-v1 JSON Schemas."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from inferdrome.routing_campaign.canonical import canonical_json_bytes
from inferdrome.routing_campaign.contracts import (
    CampaignSummary,
    FaultSchedule,
    IntegrityManifest,
    ResetReceipt,
    RouteDecisionReceipt,
    RoutingCampaignPlan,
    StateObservationRecord,
    TerminalOutcomeReceipt,
    TrialPlan,
    TrialSummary,
)

_SCHEMAS: dict[str, type[BaseModel]] = {
    "routing-campaign-plan.schema.json": RoutingCampaignPlan,
    "routing-fault-schedule.schema.json": FaultSchedule,
    "routing-trial-plan.schema.json": TrialPlan,
    "routing-reset-receipt.schema.json": ResetReceipt,
    "routing-state-observation.schema.json": StateObservationRecord,
    "route-decision-receipt.schema.json": RouteDecisionReceipt,
    "terminal-outcome-receipt.schema.json": TerminalOutcomeReceipt,
    "routing-trial-summary.schema.json": TrialSummary,
    "routing-campaign-summary.schema.json": CampaignSummary,
    "routing-campaign-manifest.schema.json": IntegrityManifest,
}


def schema_bytes() -> dict[str, bytes]:
    """Return canonical generated schema bytes with conventional final LF."""

    return {
        filename: canonical_json_bytes(model.model_json_schema()) + b"\n"
        for filename, model in _SCHEMAS.items()
    }


def schema_root(repository_root: Path | None = None) -> Path:
    """Locate the R1-only schema tree without touching the global registry."""

    root = repository_root or Path(__file__).resolve().parents[3]
    return root / "schemas" / "routing" / "v1"


def write_schemas(repository_root: Path | None = None) -> None:
    """Materialize only additive R1 schema snapshots."""

    root = schema_root(repository_root)
    root.mkdir(parents=True, exist_ok=True)
    for filename, content in schema_bytes().items():
        (root / filename).write_bytes(content)


def check_schemas(repository_root: Path | None = None) -> None:
    """Reject a committed R1 schema snapshot that drifts from the contracts."""

    root = schema_root(repository_root)
    expected = schema_bytes()
    actual_paths = (
        {path.name for path in root.glob("*.json")} if root.exists() else set()
    )
    if actual_paths != set(expected):
        raise ValueError("routing schema inventory is incomplete or has extras")
    for filename, content in expected.items():
        if (root / filename).read_bytes() != content:
            raise ValueError(f"routing schema {filename} is not current")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m inferdrome.routing_campaign.schema"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.write:
        write_schemas()
    else:
        check_schemas()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
