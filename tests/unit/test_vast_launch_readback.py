"""Unknown provider inventories cannot become empty declarations by omission."""

import pytest
from pydantic import ValidationError

from inferdrome.deployment.vast_process import VastLaunchReadback, template
from inferdrome.routing_execution.canonical import canonical_json_bytes


@pytest.mark.parametrize("missing", ["public_port_mappings", "persistent_volume_ids"])
def test_missing_launch_inventory_is_not_treated_as_empty(missing: str) -> None:
    value = template()["launch_readback"]
    value["instance_id"] = 101
    value["requested_image"] = {
        "reference": "example.invalid/synthetic@sha256:" + "a" * 64
    }
    assert VastLaunchReadback.model_validate_json(canonical_json_bytes(value))
    del value[missing]
    with pytest.raises(ValidationError):
        VastLaunchReadback.model_validate_json(canonical_json_bytes(value))
