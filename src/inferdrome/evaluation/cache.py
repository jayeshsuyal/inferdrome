"""One externally prepared cache cell, with immutable local attribution records.

No serving management, preparation traffic, retry, resume, or runtime attestation
is performed here. The caller prepares the two existing engines independently.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from inferdrome.evaluation.cache_config import (
    CompiledCacheCell,
    CompiledCachePlan,
)
from inferdrome.evaluation.cache_config import (
    cache_plan_bytes as cache_plan_bytes,
)
from inferdrome.evaluation.contracts import ClosedModel, EndpointId, EvaluationError
from inferdrome.evaluation.faults import close_routing_clients
from inferdrome.evaluation.runner import (
    Clock,
    EvaluationResult,
    SystemClock,
    Transport,
    run_evaluation,
)
from inferdrome.evaluation.study_config import Digest, SafeId
from inferdrome.evaluation.study_files import StudyDirectory, trial_filename
from inferdrome.evaluation.study_validation import load_fixed_population_bytes
from inferdrome.evaluation.transport import AiohttpTransport
from inferdrome.parsing import StructuredDataLimits, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.vllm_compose import VLLM_RUNTIME_IMAGE_REFERENCE

PLAN_LIMIT = 1024 * 1024
MANIFEST_LIMIT = 1024 * 1024
CELL_METADATA_BYTES = PLAN_LIMIT + MANIFEST_LIMIT
_JSON_LIMITS = StructuredDataLimits(
    max_depth=33, max_tokens=4_000_100, max_integer_digits=16
)
CellId = Annotated[str, Field(pattern=r"^cell-[0-9]{4}$")]
CacheMode = Literal["UNKNOWN", "DECLARED_ENABLED", "DECLARED_DISABLED"]
CacheStatus = Literal["COMPLETED", "CANCELLED", "ABORTED"]
CacheReason = Literal[
    "COMPLETED", "CANCELLED", "EXECUTION_FAILED", "RESULT_LIMIT", "DURATION_LIMIT"
]
Cleanup = Literal["CONFIRMED_BY_LOCAL_RESULT", "UNCONFIRMED", "NOT_STARTED"]
EvidenceClass = Literal["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]


class CacheEndpointPreparation(ClosedModel):
    endpoint_id: EndpointId
    process_generation_sha256: Digest | None
    prefix_caching: CacheMode


class CachePreparation(ClosedModel):
    """Private declarations and content references, never executable hooks."""

    schema_version: Literal["inferdrome.evaluation-cache-preparation.v1"]
    plan_sha256: Digest
    cell_id: CellId
    cell_sha256: Digest
    attempt_id: SafeId
    preparation_id: SafeId
    preparation_protocol_sha256: Digest
    config_sha256: Digest
    order_position: Annotated[int, Field(ge=0, le=31)]
    previous_attempt_id: SafeId | None
    runtime_recipe_sha256: Digest | None
    serving_image_reference: Annotated[str, Field(max_length=200)] = (
        VLLM_RUNTIME_IMAGE_REFERENCE
    )
    model_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"] = (
        "b968826d9c46dd6066d109eabc6255188de91218"
    )
    tokenizer_revision: Literal["b968826d9c46dd6066d109eabc6255188de91218"] = (
        "b968826d9c46dd6066d109eabc6255188de91218"
    )
    max_model_len: Literal[2048] = 2048
    cache_block_size: Annotated[int, Field(ge=1, le=256)]
    endpoints: Annotated[
        tuple[CacheEndpointPreparation, ...], Field(min_length=2, max_length=2)
    ]
    initial_prefix_state: Literal["UNKNOWN", "DECLARED_ABSENT"]
    method: Literal["UNKNOWN", "DECLARED_FRESH_PROCESSES", "DECLARED_REVIEWED_CLEAR"]
    method_reference: Digest | None
    model_warmup_reference: Digest | None
    model_warmup_overlap: Literal["UNKNOWN", "DECLARED_DISJOINT"]
    chronology_reference: Digest | None
    exclusive_traffic: Literal["UNKNOWN", "DECLARED_NO_OTHER_TRAFFIC"]
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False

    @field_validator("max_model_len", "evidence_eligible", mode="before")
    @classmethod
    def exact_primitives(cls, value: object, info: ValidationInfo) -> object:
        expected = int if info.field_name == "max_model_len" else bool
        if type(value) is not expected:
            raise ValueError("preparation literal has the wrong primitive")
        return value

    @model_validator(mode="after")
    def existing_runtime(self) -> Self:
        if self.serving_image_reference != VLLM_RUNTIME_IMAGE_REFERENCE or tuple(
            endpoint.endpoint_id for endpoint in self.endpoints
        ) != ("endpoint-a", "endpoint-b"):
            raise ValueError("preparation does not match the pinned pair")
        generations = tuple(row.process_generation_sha256 for row in self.endpoints)
        if generations[0] is not None and generations[0] == generations[1]:
            raise ValueError("endpoint process generations must be distinct")
        return self


class CacheCellArtifact(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-cache-cell-result.v1"]
    plan_sha256: Digest
    cell_id: CellId
    cell_sha256: Digest
    attempt_id: SafeId
    config_sha256: Digest
    workload_sha256: Digest
    preparation_sha256: Digest
    result: dict[str, Any]


class CacheCellManifest(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-cache-cell-manifest.v1"]
    plan_sha256: Digest
    cell_id: CellId
    cell_sha256: Digest
    attempt_id: SafeId
    preparation: CachePreparation
    preparation_sha256: Digest
    status: CacheStatus
    reason: CacheReason
    cleanup: Cleanup
    elapsed_ns: Annotated[int, Field(ge=0, le=2**53 - 1)]
    result_filename: Annotated[str, Field(pattern=r"^trial-[0-9]{4}\.json$")] | None
    result_sha256: Digest | None
    evidence_class: EvidenceClass
    runtime_verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    evidence_eligible: Literal[False] = False


@dataclass(frozen=True)
class ValidatedCacheCell:
    cell_id: str
    attempt_id: str
    status: CacheStatus
    reason: CacheReason
    cleanup: Cleanup
    preparation_sha256: str
    result_sha256: str | None
    evidence_class: EvidenceClass
    attribution_eligible: bool
    attribution_reasons: tuple[str, ...]
    elapsed_ns: int
    result: EvaluationResult | None
    preparation: CachePreparation


def _json_model[Model: BaseModel](
    content: bytes, model: type[Model], *, limit: int
) -> Model:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_number(_number: str) -> Any:
        raise ValueError

    try:
        if type(content) is not bytes or not 1 <= len(content) <= limit:
            raise ValueError
        text = content.decode("utf-8")
        validate_json_structure(text, limits=_JSON_LIMITS)
        json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
        return model.model_validate_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError(
            "cache artifact violates its closed JSON contract"
        ) from None


def load_cache_preparation_bytes(content: bytes) -> CachePreparation:
    return _json_model(content, CachePreparation, limit=MANIFEST_LIMIT)


def _encoded(model: BaseModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _preparation_digest(preparation: CachePreparation) -> str:
    return sha256_digest(canonical_json_bytes(preparation.model_dump(mode="json")))


def _expected_cell(plan: CompiledCachePlan, cell_id: str) -> CompiledCacheCell:
    cell = next((cell for cell in plan.cells if cell.cell_id == cell_id), None)
    if cell is None:
        raise EvaluationError("cache cell is not in the expected plan")
    return cell


def validate_cache_preparation(
    plan: CompiledCachePlan,
    cell: CompiledCacheCell,
    preparation: CachePreparation,
) -> tuple[str, ...]:
    """Check bindings; return unknown declaration reasons without attesting them."""
    previous = plan.cells[cell.index - 1].attempt_id if cell.index else None
    if (
        cell != _expected_cell(plan, cell.cell_id)
        or preparation.plan_sha256 != sha256_digest(cache_plan_bytes(plan))
        or preparation.cell_id != cell.cell_id
        or preparation.cell_sha256 != cell.cell_sha256
        or preparation.attempt_id != cell.attempt_id
        or preparation.config_sha256 != cell.config_sha256
        or preparation.preparation_protocol_sha256
        != plan.config.preparation_protocol_sha256
        or preparation.order_position != cell.index
        or preparation.previous_attempt_id != previous
        or preparation.cache_block_size != plan.config.cache_block_size
        or any(
            row.prefix_caching not in ("UNKNOWN", cell.mode)
            for row in preparation.endpoints
        )
    ):
        raise EvaluationError("cache preparation does not bind the expected cell")
    unknown: list[str] = []
    if any(row.prefix_caching == "UNKNOWN" for row in preparation.endpoints):
        unknown.append("UNKNOWN_CACHE_MODE")
    if any(row.process_generation_sha256 is None for row in preparation.endpoints):
        unknown.append("UNKNOWN_PROCESS_GENERATION")
    if preparation.runtime_recipe_sha256 is None:
        unknown.append("UNKNOWN_RUNTIME_RECIPE")
    if preparation.initial_prefix_state == "UNKNOWN":
        unknown.append("UNKNOWN_INITIAL_PREFIX_STATE")
    if preparation.method == "UNKNOWN" or preparation.method_reference is None:
        unknown.append("UNKNOWN_PREPARATION_METHOD")
    if (
        preparation.model_warmup_reference is None
        or preparation.model_warmup_overlap == "UNKNOWN"
    ):
        unknown.append("UNKNOWN_WARMUP")
    if preparation.chronology_reference is None:
        unknown.append("UNKNOWN_CHRONOLOGY")
    if preparation.exclusive_traffic == "UNKNOWN":
        unknown.append("UNCONTROLLED_TRAFFIC")
    return tuple(unknown)


def _artifact_bytes(
    plan: CompiledCachePlan,
    cell: CompiledCacheCell,
    preparation: CachePreparation,
    result: EvaluationResult,
) -> bytes:
    return _encoded(
        CacheCellArtifact(
            schema_version="inferdrome.evaluation-cache-cell-result.v1",
            plan_sha256=sha256_digest(cache_plan_bytes(plan)),
            cell_id=cell.cell_id,
            cell_sha256=cell.cell_sha256,
            attempt_id=cell.attempt_id,
            config_sha256=cell.config_sha256,
            workload_sha256=cell.workload_sha256,
            preparation_sha256=_preparation_digest(preparation),
            result=result.to_dict(),
        )
    )


def _load_artifact(
    content: bytes,
    plan: CompiledCachePlan,
    cell: CompiledCacheCell,
    preparation: CachePreparation,
) -> EvaluationResult:
    artifact = _json_model(
        content,
        CacheCellArtifact,
        limit=plan.config.limits.per_cell_result_bytes,
    )
    if (
        _encoded(artifact) != content
        or artifact.plan_sha256 != sha256_digest(cache_plan_bytes(plan))
        or artifact.cell_id != cell.cell_id
        or artifact.cell_sha256 != cell.cell_sha256
        or artifact.attempt_id != cell.attempt_id
        or artifact.config_sha256 != cell.config_sha256
        or artifact.workload_sha256 != cell.workload_sha256
        or artifact.preparation_sha256 != _preparation_digest(preparation)
    ):
        raise EvaluationError("cache result does not bind the expected cell")
    result = load_fixed_population_bytes(
        canonical_json_bytes(artifact.result) + b"\n", cell.config
    )
    expected = "SYNTHETIC_ONLY" if plan.synthetic else "LOCAL_MEASUREMENT_ONLY"
    if result.evidence_class != expected:
        raise EvaluationError("cache result evidence classes cannot be mixed")
    return result


async def run_cache_cell(
    plan: CompiledCachePlan,
    cell_id: str,
    preparation: CachePreparation,
    output_dir: Path,
    *,
    transport: Transport | None = None,
    clock: Clock | None = None,
    stop: asyncio.Event | None = None,
) -> CacheCellManifest:
    """Own one supplied transport or create one after all offline preflights.

    Synthetic tokenizers require an explicitly injected loopback transport;
    the CLI has no injection seam. Python transports/clocks are trusted code.
    A supplied transport is closed even if input or output reservation fails.
    """
    handed_off = False
    owned = transport
    cleanup_ns = plan.config.bounds.cleanup_timeout_ns
    try:
        cell = _expected_cell(plan, cell_id)
        validate_cache_preparation(plan, cell, preparation)
        if not plan.executable and not (
            plan.synthetic
            and transport is not None
            and all(
                endpoint.origin.startswith("http://127.0.0.1:")
                for endpoint in cell.config.endpoints
            )
        ):
            raise EvaluationError(
                "cache execution requires verified local tokenization"
            )
        plan_content = cache_plan_bytes(plan)
        result_limit = plan.config.limits.per_cell_result_bytes
        clock = clock or SystemClock()
        stop = stop or asyncio.Event()
        started = clock.now_ns()
        evidence: EvidenceClass = (
            "SYNTHETIC_ONLY" if plan.synthetic else "LOCAL_MEASUREMENT_ONLY"
        )
        with StudyDirectory.create(
            output_dir, budget=CELL_METADATA_BYTES + result_limit
        ) as directory:
            directory.write("plan.json", plan_content, limit=PLAN_LIMIT)
            status: CacheStatus = "ABORTED"
            reason: CacheReason = "EXECUTION_FAILED"
            cleanup: Cleanup = "NOT_STARTED"
            result_name: str | None = None
            result_sha: str | None = None
            try:
                if owned is None:
                    owned = AiohttpTransport(cell.config)
                cleanup = "UNCONFIRMED"
                handed_off = True
                result = await run_evaluation(
                    cell.config, owned, clock=clock, stop=stop
                )
                cleanup = "CONFIRMED_BY_LOCAL_RESULT"
                result = replace(result, evidence_class=evidence)
                content = _artifact_bytes(plan, cell, preparation, result)
                if len(content) > result_limit:
                    reason = "RESULT_LIMIT"
                else:
                    _load_artifact(content, plan, cell, preparation)
                    name = trial_filename(cell.index)
                    directory.write(name, content, limit=result_limit)
                    result_name = name
                    result_sha = sha256_digest(content)
                    status = "CANCELLED" if result.cancelled else "COMPLETED"
                    reason = status
            except asyncio.CancelledError:
                # The replay closes owned tasks/connections before propagating.
                # Without a returned result the confirmation remains unavailable.
                status = "ABORTED"
                reason = "CANCELLED"
            except Exception:
                status = "ABORTED"
                reason = "EXECUTION_FAILED"
            elapsed = clock.now_ns() - started
            if elapsed > cell.worst_case_duration_ns:
                status, reason = "ABORTED", "DURATION_LIMIT"
                result_name, result_sha = None, None
            manifest = CacheCellManifest(
                schema_version="inferdrome.evaluation-cache-cell-manifest.v1",
                plan_sha256=sha256_digest(plan_content),
                cell_id=cell.cell_id,
                cell_sha256=cell.cell_sha256,
                attempt_id=cell.attempt_id,
                preparation=preparation,
                preparation_sha256=_preparation_digest(preparation),
                status=status,
                reason=reason,
                cleanup=cleanup,
                elapsed_ns=elapsed,
                result_filename=result_name,
                result_sha256=result_sha,
                evidence_class=evidence,
            )
            directory.write("manifest.json", _encoded(manifest), limit=MANIFEST_LIMIT)
            return manifest
    finally:
        if owned is not None and not handed_off:
            await close_routing_clients((owned,), cleanup_ns)


def load_cache_cell(
    plan: CompiledCachePlan, cell: CompiledCacheCell, path: Path
) -> ValidatedCacheCell:
    """Read one expected private cell directory with finite file/JSON budgets."""
    if cell != _expected_cell(plan, cell.cell_id):
        raise EvaluationError("cache cell does not match the compiled plan")
    with StudyDirectory.open(
        path, budget=CELL_METADATA_BYTES + plan.config.limits.per_cell_result_bytes
    ) as directory:
        if directory.read("plan.json", limit=PLAN_LIMIT) != cache_plan_bytes(plan):
            raise EvaluationError("cache result directory has a different plan")
        content = directory.read("manifest.json", limit=MANIFEST_LIMIT)
        manifest = _json_model(content, CacheCellManifest, limit=MANIFEST_LIMIT)
        if (
            _encoded(manifest) != content
            or manifest.plan_sha256 != sha256_digest(cache_plan_bytes(plan))
            or manifest.cell_id != cell.cell_id
            or manifest.cell_sha256 != cell.cell_sha256
            or manifest.attempt_id != cell.attempt_id
            or manifest.preparation_sha256 != _preparation_digest(manifest.preparation)
            or manifest.evidence_class
            != ("SYNTHETIC_ONLY" if plan.synthetic else "LOCAL_MEASUREMENT_ONLY")
        ):
            raise EvaluationError("cache manifest violates its expected binding")
        reasons = validate_cache_preparation(plan, cell, manifest.preparation)
        result: EvaluationResult | None = None
        if manifest.status == "ABORTED":
            if (
                manifest.result_filename is not None
                or manifest.result_sha256 is not None
                or manifest.reason == "COMPLETED"
            ):
                raise EvaluationError("aborted cache cell cannot claim a result")
            if (
                (manifest.reason == "DURATION_LIMIT")
                != (manifest.elapsed_ns > cell.worst_case_duration_ns)
                or (
                    manifest.reason == "RESULT_LIMIT"
                    and manifest.cleanup != "CONFIRMED_BY_LOCAL_RESULT"
                )
                or (
                    manifest.reason == "CANCELLED" and manifest.cleanup != "UNCONFIRMED"
                )
            ):
                raise EvaluationError("aborted cache cell has inconsistent causality")
        else:
            if (
                manifest.result_filename != trial_filename(cell.index)
                or manifest.result_sha256 is None
                or manifest.cleanup != "CONFIRMED_BY_LOCAL_RESULT"
                or manifest.reason != manifest.status
            ):
                raise EvaluationError("returned cache cell has inconsistent completion")
            raw = directory.read(
                trial_filename(cell.index),
                limit=plan.config.limits.per_cell_result_bytes,
            )
            if sha256_digest(raw) != manifest.result_sha256:
                raise EvaluationError("cache cell result bytes changed")
            result = _load_artifact(raw, plan, cell, manifest.preparation)
            if (
                result.cancelled != (manifest.status == "CANCELLED")
                or result.elapsed_ns > manifest.elapsed_ns
                or manifest.elapsed_ns > cell.worst_case_duration_ns
            ):
                raise EvaluationError("cache cell completion timeline is inconsistent")
        if plan.verification_status == "UNAVAILABLE":
            reasons = (*reasons, "TOKENIZATION_UNAVAILABLE")
        return ValidatedCacheCell(
            cell.cell_id,
            cell.attempt_id,
            manifest.status,
            manifest.reason,
            manifest.cleanup,
            manifest.preparation_sha256,
            manifest.result_sha256,
            manifest.evidence_class,
            not reasons,
            reasons,
            manifest.elapsed_ns,
            result,
            manifest.preparation,
        )
