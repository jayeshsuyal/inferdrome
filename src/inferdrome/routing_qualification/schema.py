"""Generate and check additive stale-telemetry qualification schema snapshots."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inferdrome.routing_campaign.canonical import canonical_json_bytes
from inferdrome.routing_qualification.contracts import (
    _TERMINAL_STATUSES,
    _TRIALS,
    StaleTelemetryQualification,
)

_SCHEMA_FILENAME = "stale-telemetry-qualification.schema.json"


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    """Require the generated Pydantic shape before applying closed constraints."""

    if not isinstance(value, dict):
        raise ValueError(f"generated qualification schema {label} is unavailable")
    return value


def _stale_telemetry_schema() -> dict[str, Any]:
    """Add every JSON-Schema-expressible fixed-vector constraint.

    Pydantic models the terminal population as a typed mapping so the canonical
    descriptor retains status names. Generated JSON Schema needs the closed key
    inventory and ordered tuple specialization spelled out here. Arithmetic
    closure remains explicitly runtime/offline-verifier-only: Draft 2020-12
    has no general cross-property sum keyword.
    """

    schema = StaleTelemetryQualification.model_json_schema()
    definitions = _mapping(schema.get("$defs"), label="$defs")
    terminal_population = _mapping(
        definitions.get("TerminalPopulation"), label="TerminalPopulation"
    )
    terminal_properties = _mapping(
        terminal_population.get("properties"), label="TerminalPopulation.properties"
    )
    terminal_values = _mapping(
        terminal_properties.get("values"), label="TerminalPopulation.values"
    )
    terminal_values.update(
        {
            "$comment": (
                "STRUCTURAL-ONLY LIMIT: this schema closes every status key and "
                "its 0..6 range, but does not encode cross-property arithmetic. "
                "The runtime and offline verifier require the five values to sum "
                "exactly to the fixed six-request denominator."
            ),
            "properties": {
                status: {"type": "integer", "minimum": 0, "maximum": 6}
                for status in _TERMINAL_STATUSES
            },
            "required": list(_TERMINAL_STATUSES),
            "additionalProperties": False,
            "minProperties": len(_TERMINAL_STATUSES),
            "maxProperties": len(_TERMINAL_STATUSES),
        }
    )

    root_properties = _mapping(schema.get("properties"), label="properties")
    trials = _mapping(root_properties.get("trials"), label="trials")
    trials["prefixItems"] = [
        {
            "allOf": [
                {"$ref": "#/$defs/QualifiedTrial"},
                {
                    "type": "object",
                    "properties": {
                        "policy_id": {"const": policy_id, "type": "string"},
                        "trial_id": {"const": trial_id, "type": "string"},
                    },
                    "required": ["policy_id", "trial_id"],
                },
            ]
        }
        for policy_id, trial_id in _TRIALS
    ]
    trials["items"] = False
    schema["$comment"] = (
        "STRUCTURAL JSON SCHEMA: exact terminal-status inventory, per-status "
        "range, and ordered policy/trial pairs are encoded here. RUNTIME/OFFLINE "
        "VERIFIER ONLY: terminal arithmetic closure, canonical bytes, digests, "
        "receipt identities, reset semantics, and source replay binding."
    )
    return schema


def schema_root(repository_root: Path | None = None) -> Path:
    """Locate the additive qualification-only schema tree."""

    root = repository_root or Path(__file__).resolve().parents[3]
    return root / "schemas" / "routing-qualification" / "v1"


def schema_bytes() -> dict[str, bytes]:
    """Return canonical snapshots with one conventional final newline."""

    return {
        _SCHEMA_FILENAME: canonical_json_bytes(_stale_telemetry_schema()) + b"\n"
    }


def write_schemas(repository_root: Path | None = None) -> None:
    """Write only additive v0.3 qualification contract snapshots."""

    root = schema_root(repository_root)
    root.mkdir(parents=True, exist_ok=True)
    for filename, content in schema_bytes().items():
        (root / filename).write_bytes(content)


def check_schemas(repository_root: Path | None = None) -> None:
    """Reject missing, extra, or stale qualification schema snapshots."""

    root = schema_root(repository_root)
    expected = schema_bytes()
    actual = {path.name for path in root.glob("*.json")} if root.exists() else set()
    if actual != set(expected):
        raise ValueError(
            "routing qualification schema inventory is incomplete or has extras"
        )
    for filename, content in expected.items():
        if (root / filename).read_bytes() != content:
            raise ValueError(f"routing qualification schema {filename} is not current")


def main(argv: Sequence[str] | None = None) -> int:
    """Generate or check the isolated qualification schema snapshot."""

    parser = argparse.ArgumentParser(
        prog="python -m inferdrome.routing_qualification.schema"
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
