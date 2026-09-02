"""Generate and check the isolated routing-execution-v1 JSON Schema snapshots."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.contracts import (
    ExecutedManifest,
    FaultReceipt,
    InputTransferReceipt,
    IntegrityManifest,
    ProducerReceipt,
    ResetReceipt,
    RouteDecisionReceipt,
    RoutingExecutionConfig,
    TelemetryObservation,
    TerminalOutcomeReceipt,
    TrialSummary,
)

_SCHEMAS: dict[str, type[BaseModel]] = {
    "routing-execution-config.schema.json": RoutingExecutionConfig,
    "routing-input-transfer-receipt.schema.json": InputTransferReceipt,
    "routing-executed-manifest.schema.json": ExecutedManifest,
    "routing-execution-reset-receipt.schema.json": ResetReceipt,
    "routing-execution-fault-receipt.schema.json": FaultReceipt,
    "routing-execution-telemetry-observation.schema.json": TelemetryObservation,
    "routing-execution-decision-receipt.schema.json": RouteDecisionReceipt,
    "routing-execution-terminal-receipt.schema.json": TerminalOutcomeReceipt,
    "routing-execution-trial-summary.schema.json": TrialSummary,
    "routing-producer-receipt.schema.json": ProducerReceipt,
    "routing-execution-integrity-manifest.schema.json": IntegrityManifest,
}


def schema_root(repository_root: Path | None = None) -> Path:
    """Locate the isolated PR-B schema family without touching frozen v1 trees."""

    root = repository_root or Path(__file__).resolve().parents[3]
    return root / "schemas" / "routing-execution" / "v1"


def schema_bytes() -> dict[str, bytes]:
    """Return canonical schema snapshots with a conventional final newline."""

    return {
        filename: canonical_json_bytes(model.model_json_schema()) + b"\n"
        for filename, model in _SCHEMAS.items()
    }


def write_schemas(repository_root: Path | None = None) -> None:
    """Write only additive routing-execution snapshots."""

    root = schema_root(repository_root)
    root.mkdir(parents=True, exist_ok=True)
    for filename, content in schema_bytes().items():
        (root / filename).write_bytes(content)


def check_schemas(repository_root: Path | None = None) -> None:
    """Fail closed if the committed isolated schema inventory drifts."""

    root = schema_root(repository_root)
    expected = schema_bytes()
    actual = {path.name for path in root.glob("*.json")} if root.exists() else set()
    if actual != set(expected):
        raise ValueError(
            "routing execution schema inventory is incomplete or has extras"
        )
    for filename, content in expected.items():
        if (root / filename).read_bytes() != content:
            raise ValueError(f"routing execution schema {filename} is not current")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m inferdrome.routing_execution.schema"
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
