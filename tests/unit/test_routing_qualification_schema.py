"""Generated schema checks for the additive qualification-only contract."""

from __future__ import annotations

from inferdrome.routing_qualification.schema import (
    check_schemas,
    schema_bytes,
    schema_root,
)


def test_routing_qualification_schema_snapshot_is_current_and_closed() -> None:
    check_schemas()
    root = schema_root()
    assert {path.name for path in root.glob("*.json")} == set(schema_bytes())
    assert all(
        (root / filename).read_bytes() == content
        for filename, content in schema_bytes().items()
    )
