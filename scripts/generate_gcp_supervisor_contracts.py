#!/usr/bin/env python3
"""Generate/check the additive v0.2 GCP supervisor schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inferdrome.deployment.gcp_supervisor import (
    gcp_execution_supervisor_contract_schemas,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas/deployment/v2"


def _pretty(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode()


def _render() -> dict[Path, bytes]:
    return {
        SCHEMA_ROOT / filename: _pretty(schema)
        for filename, schema in gcp_execution_supervisor_contract_schemas().items()
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
            print("GCP supervisor schemas are stale")
            return 1
        print(f"GCP supervisor schemas are current ({len(rendered)} files)")
        return 0
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"generated GCP supervisor schemas ({len(rendered)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
