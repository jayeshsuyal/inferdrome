"""Public schema registry shared by generation and validation tests."""

from pydantic import BaseModel

from inferdrome.domain.environment import EnvironmentManifest
from inferdrome.domain.evidence import EvidenceBundle
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import ExperimentSpec
from inferdrome.domain.metrics import Measurements, MetricDefinitions
from inferdrome.domain.request_plan import RequestPlan
from inferdrome.domain.request_record import RequestRecord
from inferdrome.domain.trial_set import TrialSet

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "experiment.schema.json": ExperimentSpec,
    "request-plan.schema.json": RequestPlan,
    "request-record.schema.json": RequestRecord,
    "environment.schema.json": EnvironmentManifest,
    "execution.schema.json": ExecutionRecord,
    "metric-definitions.schema.json": MetricDefinitions,
    "measurements.schema.json": Measurements,
    "evidence-bundle.schema.json": EvidenceBundle,
    "trial-set.schema.json": TrialSet,
}

SCHEMA_BASE_URI = "https://schemas.inferdrome.dev/public/v1/"


def public_schema(filename: str, model: type[BaseModel]) -> dict[str, object]:
    """Generate one deterministic Draft 2020-12 public schema document."""

    generated = model.model_json_schema(
        by_alias=True,
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SCHEMA_BASE_URI}{filename}",
        "$comment": (
            "Cross-field semantic invariants are normative; see "
            "docs/PUBLIC_CONTRACTS_V1.md and the conformance vectors."
        ),
        **generated,
    }
