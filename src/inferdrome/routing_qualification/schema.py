"""Generate and check additive stale-telemetry qualification schema snapshots."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from inferdrome.routing_campaign.canonical import canonical_json_bytes
from inferdrome.routing_qualification.contracts import StaleTelemetryQualification

_SCHEMAS: dict[str, type[BaseModel]] = {
    "stale-telemetry-qualification.schema.json": StaleTelemetryQualification,
}


def schema_root(repository_root: Path | None = None) -> Path:
    """Locate the additive qualification-only schema tree."""

    root = repository_root or Path(__file__).resolve().parents[3]
    return root / "schemas" / "routing-qualification" / "v1"


def schema_bytes() -> dict[str, bytes]:
    """Return canonical snapshots with one conventional final newline."""

    return {
        filename: canonical_json_bytes(model.model_json_schema()) + b"\n"
        for filename, model in _SCHEMAS.items()
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
