"""Generate/check only the additive Vast v4 routing-execution snapshots."""

from __future__ import annotations

import argparse
from pathlib import Path

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.vast_contracts import (
    VastExecutedManifest,
    VastRoutingConfig,
)


def schema_bytes() -> dict[str, bytes]:
    """Return closed v4 snapshots without importing deployment or changing v1-v3."""

    return {
        filename: canonical_json_bytes(model.model_json_schema()) + b"\n"
        for filename, model in (
            ("routing-execution-config.schema.json", VastRoutingConfig),
            ("routing-executed-manifest.schema.json", VastExecutedManifest),
        )
    }


def schema_root(repository: Path | None = None) -> Path:
    root = repository or Path(__file__).resolve().parents[3]
    return root / "schemas" / "routing-execution" / "v4"


def check_schemas(repository: Path | None = None) -> None:
    root = schema_root(repository)
    expected = schema_bytes()
    if {path.name for path in root.glob("*.json")} != set(expected):
        raise ValueError("Vast schema inventory is incomplete or has extras")
    for filename, content in expected.items():
        if (root / filename).read_bytes() != content:
            raise ValueError("Vast schema snapshot is not current")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write:
        root = schema_root()
        root.mkdir(parents=True, exist_ok=True)
        for filename, content in schema_bytes().items():
            (root / filename).write_bytes(content)
    else:
        check_schemas()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
