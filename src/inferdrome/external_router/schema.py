"""Generate and check isolated external-router-v1 JSON Schema snapshots."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from inferdrome.external_router.canonical import canonical_json_bytes
from inferdrome.external_router.contracts import (
    ExternalRouterEvidence,
    LlmdAttachedRecord,
)

_SCHEMAS: dict[str, type[BaseModel]] = {
    "llm-d-attached-record.schema.json": LlmdAttachedRecord,
    "external-router-evidence.schema.json": ExternalRouterEvidence,
}


def schema_root(repository_root: Path | None = None) -> Path:
    """Locate only the additive external-router schema family."""

    root = repository_root or Path(__file__).resolve().parents[3]
    return root / "schemas" / "external-router" / "v1"


def schema_bytes() -> dict[str, bytes]:
    """Return canonical snapshots with a conventional final newline."""

    return {
        filename: canonical_json_bytes(model.model_json_schema()) + b"\n"
        for filename, model in _SCHEMAS.items()
    }


def write_schemas(repository_root: Path | None = None) -> None:
    """Write only additive external-router schema snapshots."""

    root = schema_root(repository_root)
    root.mkdir(parents=True, exist_ok=True)
    for filename, content in schema_bytes().items():
        (root / filename).write_bytes(content)


def check_schemas(repository_root: Path | None = None) -> None:
    """Fail closed if the committed external-router snapshots drift."""

    root = schema_root(repository_root)
    expected = schema_bytes()
    actual = {path.name for path in root.glob("*.json")} if root.exists() else set()
    if actual != set(expected):
        raise ValueError("external router schema inventory is incomplete or has extras")
    for filename, content in expected.items():
        if (root / filename).read_bytes() != content:
            raise ValueError(f"external router schema {filename} is not current")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m inferdrome.external_router.schema"
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
