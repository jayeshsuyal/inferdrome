"""Shared local fixtures for external-router adapter coverage."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "external-router" / "llmd" / "v1"
)


def fixture_record() -> dict[str, Any]:
    """Return an independently mutable copy of the committed local record."""

    return copy.deepcopy(
        json.loads((FIXTURE_ROOT / "attached-record.json").read_text(encoding="utf-8"))
    )


def fixture_record_bytes(record: dict[str, Any] | None = None) -> bytes:
    """Return one compact but deliberately non-canonical profile input document."""

    return json.dumps(
        fixture_record() if record is None else record,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fixture_router_config() -> bytes:
    """Return local fixture config bytes; adapter output never retains them."""

    return (FIXTURE_ROOT / "router-config.json").read_bytes()
