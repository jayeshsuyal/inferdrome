"""Generated-schema inventory checks for the isolated pre-campaign contracts."""

from __future__ import annotations

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_ID,
    GCP_PRIVATE_CAMPAIGN_CREATE_SCHEMA_ID,
    GCP_PRIVATE_CAMPAIGN_ENGINE_ATTESTATION_SCHEMA_ID,
    GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_ID,
    gcp_private_campaign_contract_schemas,
)
from scripts import generate_gcp_supervisor_contracts as generator


def test_pre_campaign_contracts_are_generated_as_additive_flat_v2_outputs() -> None:
    schemas = gcp_private_campaign_contract_schemas()
    collected = generator._collect_schemas()

    assert set(schemas).issubset(collected)
    assert schemas["gcp-private-campaign-approval.schema.json"]["$id"] == (
        GCP_PRIVATE_CAMPAIGN_APPROVAL_SCHEMA_ID
    )
    assert schemas["gcp-private-campaign-create-request.schema.json"]["$id"] == (
        GCP_PRIVATE_CAMPAIGN_CREATE_SCHEMA_ID
    )
    assert schemas["gcp-private-campaign-readiness.schema.json"]["$id"] == (
        GCP_PRIVATE_CAMPAIGN_READINESS_SCHEMA_ID
    )
    assert schemas["gcp-private-campaign-engine-attestation.schema.json"]["$id"] == (
        GCP_PRIVATE_CAMPAIGN_ENGINE_ATTESTATION_SCHEMA_ID
    )
    assert all(name.startswith("gcp-private-campaign-") for name in schemas)
