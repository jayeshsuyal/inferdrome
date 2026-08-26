#!/usr/bin/env python3
"""Render or check the additive deployment qualification schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inferdrome.deployment import qualification_schema

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (
    REPOSITORY_ROOT
    / "schemas"
    / "deployment"
    / "v1"
    / "deployment-qualification.schema.json"
)


def _render() -> bytes:
    return (
        json.dumps(
            qualification_schema(),
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
    arguments = parser.parse_args()
    rendered = _render()
    if arguments.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != rendered:
            print("deployment qualification schema is stale")
            return 1
        print("deployment qualification schema is current (1 file)")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(rendered)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
