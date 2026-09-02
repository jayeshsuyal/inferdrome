"""Generated contract snapshots remain closed and Draft 2020-12 valid."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from inferdrome.routing_execution.schema import check_schemas, schema_bytes


def test_routing_execution_schema_snapshots_are_current_closed_and_valid() -> None:
    check_schemas()
    for content in schema_bytes().values():
        Draft202012Validator.check_schema(json.loads(content))
