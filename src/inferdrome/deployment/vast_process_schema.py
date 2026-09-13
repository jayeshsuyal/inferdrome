"""Generate/check the separate Vast preparation input and cleanup schemas."""

from __future__ import annotations

import argparse
from pathlib import Path

from inferdrome.deployment.vast_process import (
    DestroyReadback,
    VastProcessInput,
    template,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes


def artifacts() -> dict[str, bytes]:
    values = {
        "schemas/vast-process/v1/input.schema.json": (
            VastProcessInput.model_json_schema()
        ),
        "schemas/vast-process/v1/destroy-readback.schema.json": (
            DestroyReadback.model_json_schema()
        ),
        "deployments/vast-process-v1/input-template.json": template(),
    }
    return {name: canonical_json_bytes(value) + b"\n" for name, value in values.items()}


def check(repository: Path) -> None:
    for name, content in artifacts().items():
        if (repository / name).read_bytes() != content:
            raise ValueError("Vast process snapshot is not current")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[3]
    if args.write:
        for name, content in artifacts().items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    else:
        check(repository)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
