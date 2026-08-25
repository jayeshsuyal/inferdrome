#!/usr/bin/env python3
"""Render or verify the committed deployment specification schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inferdrome.deployment import deployment_spec_schema

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (
    REPOSITORY_ROOT
    / "schemas"
    / "deployment"
    / "v1"
    / "deployment-spec.schema.json"
)


def _render() -> bytes:
    return (
        json.dumps(
            deployment_spec_schema(),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = _render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != rendered:
            print(f"deployment schema is stale: {OUTPUT}")
            return 1
        print("deployment schema is current (1 file)")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(rendered)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
