"""Finite fixed-assignment cache experiments, separate from four-policy studies."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from inferdrome.evaluation.cache_workload import (
    CacheTokenizerUnavailable,
    CacheWorkloadVerification,
    WorkloadPair,
    render_prompt,
    verify_cache_workloads,
)
from inferdrome.evaluation.contracts import (
    MAX_INPUT_BYTES,
    Bounds,
    ClosedModel,
    Endpoint,
    EvaluationConfig,
    EvaluationError,
    Offer,
)
from inferdrome.evaluation.study_config import (
    Digest,
    SafeId,
    Seed,
    StudyProfile,
    StudyReporting,
)
from inferdrome.parsing import bounded_json_float, validate_json_structure
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

CacheCondition = Literal["S0", "S1", "U0", "U1"]
WorkloadFamily = Literal["SHARED", "UNIQUE"]
VerificationStatus = Literal[
    "VERIFIED_PINNED_QWEN3", "SYNTHETIC_TOKENIZER", "UNAVAILABLE"
]
CACHE_CONDITIONS: tuple[CacheCondition, ...] = ("S0", "S1", "U0", "U1")
WILLIAMS_ORDERS: tuple[tuple[CacheCondition, ...], ...] = (
    ("S0", "S1", "U1", "U0"),
    ("S1", "U0", "S0", "U1"),
    ("U0", "U1", "S1", "S0"),
    ("U1", "S0", "U0", "S1"),
)
MAX_CACHE_CELLS = 32
MAX_EXPANDED_WORKLOAD_BYTES = 8 * 1024 * 1024
CELL_METADATA_RESERVE_BYTES = 2 * 1024 * 1024
CACHE_PLAN_RESERVE_BYTES = 1024 * 1024
CACHE_REPORT_RESERVE_BYTES = 8 * 1024 * 1024


def _bounded_text(value: str) -> str:
    if not 1 <= len(value.encode("utf-8")) <= 32_768:
        raise ValueError("cache workload text exceeds its byte bound")
    return value


class CacheCase(ClosedModel):
    unique_document: Annotated[str, Field(min_length=1, max_length=32_768, repr=False)]
    suffix: Annotated[str, Field(min_length=1, max_length=32_768, repr=False)]
    scheduled_ns: Annotated[int, Field(ge=0, le=300_000_000_000)]

    @field_validator("unique_document", "suffix")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _bounded_text(value)


class CacheBlock(ClosedModel):
    block_id: SafeId
    workload_seed: Seed
    shared_document: Annotated[str, Field(min_length=1, max_length=32_768, repr=False)]
    cases: Annotated[tuple[CacheCase, ...], Field(min_length=4, max_length=4000)]
    cell_order: Annotated[tuple[CacheCondition, ...], Field(min_length=4, max_length=4)]
    attempt_ids: Annotated[tuple[SafeId, ...], Field(min_length=4, max_length=4)]

    @field_validator("shared_document")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _bounded_text(value)

    @model_validator(mode="after")
    def finite_cases_and_cells(self) -> Self:
        if set(self.cell_order) != set(CACHE_CONDITIONS):
            raise ValueError("a cache block requires each of the four cells once")
        if len(set(self.attempt_ids)) != 4:
            raise ValueError("cache attempts must be distinct")
        if len({case.unique_document for case in self.cases}) != len(self.cases):
            raise ValueError("unique documents must differ by case")
        if len({case.suffix for case in self.cases}) != len(self.cases):
            raise ValueError("cache suffixes must differ by case")
        if any(
            earlier.scheduled_ns > later.scheduled_ns
            for earlier, later in zip(self.cases, self.cases[1:], strict=False)
        ):
            raise ValueError("cache arrivals must be ordered")
        return self

    def workload_pair(self) -> WorkloadPair:
        return WorkloadPair(
            shared_document=self.shared_document,
            unique_documents=tuple(case.unique_document for case in self.cases),
            suffixes=tuple(case.suffix for case in self.cases),
        )


class CacheLimits(ClosedModel):
    max_planned_requests: Annotated[int, Field(ge=16, le=100_000)] = 100_000
    max_duration_ns: Annotated[int, Field(ge=1, le=86_400_000_000_000)] = (
        86_400_000_000_000
    )
    per_cell_result_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)] = (
        2 * 1024 * 1024
    )
    total_output_bytes: Annotated[int, Field(ge=1, le=1024 * 1024 * 1024)] = (
        1024 * 1024 * 1024
    )


class CacheExperimentConfig(ClosedModel):
    schema_version: Literal["inferdrome.evaluation-cache-config.v1"]
    experiment_id: SafeId
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    model: Literal["Qwen/Qwen3-8B"] = Field(default="Qwen/Qwen3-8B", repr=False)
    endpoints: Annotated[tuple[Endpoint, ...], Field(min_length=2, max_length=2)]
    bounds: Bounds
    max_tokens: Annotated[int, Field(ge=1, le=2047)] = 128
    profile: StudyProfile
    blocks: Annotated[tuple[CacheBlock, ...], Field(min_length=1, max_length=8)]
    preparation_protocol_sha256: Digest
    cache_block_size: Annotated[int, Field(ge=1, le=256)]
    max_model_len: Literal[2048] = 2048
    limits: CacheLimits
    reporting: StudyReporting

    @field_validator("max_model_len", mode="before")
    @classmethod
    def exact_context_limit(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("cache context limit must be an integer primitive")
        return value

    @model_validator(mode="after")
    def finite_matched_experiment(self) -> Self:
        count = len(self.blocks)
        if count not in (1, 4, 8):
            raise ValueError("cache experiments require one, four or eight blocks")
        if len({block.block_id for block in self.blocks}) != count:
            raise ValueError("cache block identities must be unique")
        if (
            len({block.workload_seed for block in self.blocks}) != count
            or len({block.shared_document for block in self.blocks}) != count
        ):
            raise ValueError(
                "repeated cache blocks require distinct seeds and documents"
            )
        attempts = [attempt for block in self.blocks for attempt in block.attempt_ids]
        if len(set(attempts)) != len(attempts):
            raise ValueError("cache attempts must be globally unique")
        if count > 1 and Counter(block.cell_order for block in self.blocks) != Counter(
            {order: count // 4 for order in WILLIAMS_ORDERS}
        ):
            raise ValueError(
                "repeated cache blocks require the balanced Williams orders"
            )
        if self.profile.load_level != "REHEARSAL":
            raise ValueError("cache experiments currently have one rehearsal profile")
        if not (
            self.profile.window_end_ns == self.bounds.duration_ns
            and self.profile.completion_slo_ns < self.bounds.request_timeout_ns
            and self.profile.completion_slo_ns <= self.bounds.drain_ns
        ):
            raise ValueError("cache measurement window or SLO violates replay bounds")
        if (
            self.bounds.max_requests > 4000
            or self.bounds.max_requests * self.bounds.max_content_events > 500_000
        ):
            raise ValueError("cache replay resources exceed their bounds")
        expanded = 0
        for block in self.blocks:
            if any(
                not self.profile.window_start_ns
                <= case.scheduled_ns
                < self.profile.window_end_ns
                for case in block.cases
            ):
                raise ValueError("cache offer lies outside the measured window")
            for family in ("SHARED", "UNIQUE"):
                replay = self.replay_config(block, family)
                expanded += sum(
                    len(offer.prompt.encode("utf-8")) for offer in replay.offers
                )
        if expanded > MAX_EXPANDED_WORKLOAD_BYTES:
            raise ValueError(
                "expanded cache workload exceeds local verification bounds"
            )
        if (
            self.planned_request_count > self.limits.max_planned_requests
            or 4 * count * self.cell_duration_ns > self.limits.max_duration_ns
            or self.reserved_output_bytes > self.limits.total_output_bytes
        ):
            raise ValueError("cache experiment exceeds a declared resource bound")
        return self

    def replay_config(self, block: CacheBlock, family: str) -> EvaluationConfig:
        if family not in ("SHARED", "UNIQUE"):
            raise EvaluationError("cache workload family is unsupported")
        return EvaluationConfig(
            schema_version="inferdrome.evaluation-config.v1",
            source_commit=self.source_commit,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,
            enable_thinking=False,
            endpoints=self.endpoints,
            bounds=self.bounds,
            offers=tuple(
                Offer(
                    scheduled_ns=case.scheduled_ns,
                    endpoint_id="endpoint-a" if index % 2 == 0 else "endpoint-b",
                    prompt=render_prompt(
                        block.shared_document
                        if family == "SHARED"
                        else case.unique_document,
                        case.suffix,
                    ),
                )
                for index, case in enumerate(block.cases)
            ),
        )

    @property
    def planned_request_count(self) -> int:
        return 4 * sum(len(block.cases) for block in self.blocks)

    @property
    def cell_duration_ns(self) -> int:
        # Conservative allowance shared with the study preflight: includes
        # replay cancellation and close grace; external preparation is unmeasured.
        return (
            self.bounds.duration_ns
            + self.bounds.drain_ns
            + 7 * self.bounds.cleanup_timeout_ns
        )

    @property
    def reserved_output_bytes(self) -> int:
        return (
            4
            * len(self.blocks)
            * (self.limits.per_cell_result_bytes + CELL_METADATA_RESERVE_BYTES)
            + CACHE_PLAN_RESERVE_BYTES
            + CACHE_REPORT_RESERVE_BYTES
        )


class CacheVerifier(Protocol):
    def __call__(
        self,
        pairs: tuple[WorkloadPair, ...],
        *,
        block_size: int,
        max_tokens: int,
    ) -> CacheWorkloadVerification: ...


@dataclass(frozen=True)
class CompiledCacheCell:
    index: int
    cell_id: str
    block_id: str
    condition: CacheCondition
    workload_family: WorkloadFamily
    prefix_enabled: bool
    attempt_id: str
    config: EvaluationConfig = field(repr=False)
    config_sha256: str
    workload_sha256: str
    cell_sha256: str
    window_start_ns: int
    window_end_ns: int
    first_content_slo_ns: int
    completion_slo_ns: int
    worst_case_duration_ns: int

    @property
    def mode(self) -> Literal["DECLARED_ENABLED", "DECLARED_DISABLED"]:
        return "DECLARED_ENABLED" if self.prefix_enabled else "DECLARED_DISABLED"

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "cell_id": self.cell_id,
            "block_id": self.block_id,
            "condition": self.condition,
            "workload_family": self.workload_family,
            "prefix_enabled": self.prefix_enabled,
            "mode": self.mode,
            "attempt_id": self.attempt_id,
            "config_sha256": self.config_sha256,
            "workload_sha256": self.workload_sha256,
            "cell_sha256": self.cell_sha256,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "first_content_slo_ns": self.first_content_slo_ns,
            "completion_slo_ns": self.completion_slo_ns,
            "worst_case_duration_ns": self.worst_case_duration_ns,
            "offered_count": len(self.config.offers),
            "routing": "FIXED_ALTERNATING_A_B_BY_OFFER_INDEX",
        }


@dataclass(frozen=True)
class CompiledCachePlan:
    config_sha256: str
    cells: tuple[CompiledCacheCell, ...]
    verification_status: VerificationStatus
    verification: CacheWorkloadVerification | None
    planned_request_count: int
    worst_case_duration_ns: int
    reserved_output_bytes: int
    config: CacheExperimentConfig = field(repr=False)

    @property
    def synthetic(self) -> bool:
        return self.verification_status == "SYNTHETIC_TOKENIZER"

    @property
    def executable(self) -> bool:
        return self.verification_status == "VERIFIED_PINNED_QWEN3"

    @property
    def evidence_class(self) -> Literal["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]:
        return "SYNTHETIC_ONLY" if self.synthetic else "LOCAL_MEASUREMENT_ONLY"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "inferdrome.evaluation-cache-plan.v1",
            "compiler": "FINITE_FIXED_ASSIGNMENT_CACHE_CELLS_V1",
            "experiment_id": self.config.experiment_id,
            "config_sha256": self.config_sha256,
            "cells": [cell.to_dict() for cell in self.cells],
            "verification_status": self.verification_status,
            "verification": self.verification.to_dict() if self.verification else None,
            "executable": self.executable,
            "synthetic": self.synthetic,
            "planned_request_count": self.planned_request_count,
            "worst_case_duration_ns": self.worst_case_duration_ns,
            "reserved_output_bytes": self.reserved_output_bytes,
            "cell_metadata_reserve_bytes": CELL_METADATA_RESERVE_BYTES,
            "plan_reserve_bytes": CACHE_PLAN_RESERVE_BYTES,
            "report_reserve_bytes": CACHE_REPORT_RESERVE_BYTES,
            "bounds": self.config.bounds.model_dump(mode="json"),
            "limits": self.config.limits.model_dump(mode="json"),
            "reporting": self.config.reporting.model_dump(mode="json"),
            "profile": self.config.profile.model_dump(mode="json"),
            "blocks": [
                {
                    "block_id": block.block_id,
                    "workload_seed": block.workload_seed,
                    "cell_order": list(block.cell_order),
                    "attempt_ids": list(block.attempt_ids),
                }
                for block in self.config.blocks
            ],
            "preparation_protocol_sha256": self.config.preparation_protocol_sha256,
            "cache_block_size": self.config.cache_block_size,
            "max_model_len": self.config.max_model_len,
            "declared_source_commit": self.config.source_commit,
            "model_sha256": sha256_digest(self.config.model.encode("utf-8")),
            "request_settings": {
                "max_tokens": self.config.max_tokens,
                "temperature": 0,
                "enable_thinking": False,
                "n": 1,
                "stream": True,
                "include_usage": True,
            },
            "seed_semantics": "DECLARED_FINITE_WORKLOAD_NOT_GENERATION_SEED",
            "model_warmup": "EXTERNALLY_PREPARED_UNMEASURED",
            "external_preparation_requests": "UNAVAILABLE",
            "external_preparation_duration_ns": "UNAVAILABLE",
            "external_preparation_tokens": "UNAVAILABLE",
            "request_budget_scope": "FOREGROUND_REPLAY_ONLY",
            "runtime_identity": "UNVERIFIED",
            "calibration": "UNCALIBRATED_REHEARSAL",
            "cache_hits": "UNAVAILABLE",
            "cost": "UNAVAILABLE",
            "evidence_class": self.evidence_class,
            "evidence_eligible": False,
        }


def _verification(
    config: CacheExperimentConfig,
    tokenizer_root: Path | None,
    verifier: CacheVerifier | None,
) -> CacheWorkloadVerification | None:
    pairs = tuple(block.workload_pair() for block in config.blocks)
    try:
        if verifier is not None:
            if tokenizer_root is not None:
                raise EvaluationError("cache compiler requires one tokenizer source")
            result = verifier(
                pairs, block_size=config.cache_block_size, max_tokens=config.max_tokens
            )
            if type(result) is not CacheWorkloadVerification:
                raise EvaluationError("cache verification result type is invalid")
            result = replace(
                result,
                tokenization_status="SYNTHETIC_TOKENIZER",
                evidence_class="SYNTHETIC_ONLY",
                tokenizer_identity=None,
            )
        elif tokenizer_root is not None:
            result = verify_cache_workloads(
                pairs,
                tokenizer_root=tokenizer_root,
                block_size=config.cache_block_size,
                max_tokens=config.max_tokens,
            )
            if (
                type(result) is not CacheWorkloadVerification
                or result.tokenization_status != "VERIFIED_PINNED_QWEN3"
                or result.evidence_class != "LOCAL_MEASUREMENT_ONLY"
                or result.tokenizer_identity is None
            ):
                raise EvaluationError("pinned cache verification identity is invalid")
        else:
            return None
    except CacheTokenizerUnavailable:
        return None
    if (
        result.block_size != config.cache_block_size
        or result.max_tokens != config.max_tokens
        or result.max_model_len != config.max_model_len
        or result.family_count != len(pairs)
        or result.offer_count != sum(len(pair.suffixes) for pair in pairs)
        or len(result.families) != len(pairs)
    ):
        raise EvaluationError("cache verification does not match the planned workload")
    for index, (pair, family) in enumerate(zip(pairs, result.families, strict=True)):
        if (
            family.family_index != index
            or family.input_sha256 != sha256_digest(canonical_json_bytes(asdict(pair)))
            or len(family.cases) != len(pair.suffixes)
        ):
            raise EvaluationError("cache verification family binding is invalid")
        for case_index, case in enumerate(family.cases):
            if (
                case.case_index != case_index
                or case.shared_prompt_sha256
                != sha256_digest(
                    render_prompt(
                        pair.shared_document, pair.suffixes[case_index]
                    ).encode("utf-8")
                )
                or case.unique_prompt_sha256
                != sha256_digest(
                    render_prompt(
                        pair.unique_documents[case_index], pair.suffixes[case_index]
                    ).encode("utf-8")
                )
            ):
                raise EvaluationError("cache verification prompt binding is invalid")
    return result


def compile_cache_experiment(
    config: CacheExperimentConfig,
    *,
    tokenizer_root: Path | None = None,
    verifier: CacheVerifier | None = None,
) -> CompiledCachePlan:
    """Compile inert finite cells; verification never acquires tokenizer files."""
    verification = _verification(config, tokenizer_root, verifier)
    status: VerificationStatus = (
        "UNAVAILABLE"
        if verification is None
        else "SYNTHETIC_TOKENIZER"
        if verifier is not None
        else "VERIFIED_PINNED_QWEN3"
    )
    config_sha256 = sha256_digest(canonical_json_bytes(config.model_dump(mode="json")))
    cells: list[CompiledCacheCell] = []
    for block in config.blocks:
        replays = {
            family: config.replay_config(block, family)
            for family in ("SHARED", "UNIQUE")
        }
        for condition, attempt_id in zip(
            block.cell_order, block.attempt_ids, strict=True
        ):
            family: WorkloadFamily = "SHARED" if condition.startswith("S") else "UNIQUE"
            replay = replays[family]
            client_sha256 = sha256_digest(
                canonical_json_bytes(replay.model_dump(mode="json"))
            )
            workload = replay.model_dump(mode="json")
            workload["endpoints"] = [
                {"endpoint_id": endpoint.endpoint_id} for endpoint in replay.endpoints
            ]
            workload["profile"] = config.profile.model_dump(mode="json")
            workload_sha256 = sha256_digest(canonical_json_bytes(workload))
            index = len(cells)
            cell_id = f"cell-{index:04d}"
            prefix_enabled = condition.endswith("1")
            identity = {
                "schema_version": "inferdrome.evaluation-cache-cell.v1",
                "experiment_config_sha256": config_sha256,
                "index": index,
                "cell_id": cell_id,
                "block_id": block.block_id,
                "condition": condition,
                "attempt_id": attempt_id,
                "workload_family": family,
                "prefix_enabled": prefix_enabled,
                "config_sha256": client_sha256,
                "workload_sha256": workload_sha256,
                "preparation_protocol_sha256": config.preparation_protocol_sha256,
                "cache_block_size": config.cache_block_size,
            }
            cells.append(
                CompiledCacheCell(
                    index=index,
                    cell_id=cell_id,
                    block_id=block.block_id,
                    condition=condition,
                    workload_family=family,
                    prefix_enabled=prefix_enabled,
                    attempt_id=attempt_id,
                    config=replay,
                    config_sha256=client_sha256,
                    workload_sha256=workload_sha256,
                    cell_sha256=sha256_digest(canonical_json_bytes(identity)),
                    window_start_ns=config.profile.window_start_ns,
                    window_end_ns=config.profile.window_end_ns,
                    first_content_slo_ns=config.profile.first_content_slo_ns,
                    completion_slo_ns=config.profile.completion_slo_ns,
                    worst_case_duration_ns=config.cell_duration_ns,
                )
            )
    plan = CompiledCachePlan(
        config_sha256=config_sha256,
        cells=tuple(cells),
        verification_status=status,
        verification=verification,
        planned_request_count=config.planned_request_count,
        worst_case_duration_ns=len(cells) * config.cell_duration_ns,
        reserved_output_bytes=config.reserved_output_bytes,
        config=config,
    )
    if len(cache_plan_bytes(plan)) > CACHE_PLAN_RESERVE_BYTES:
        raise EvaluationError("cache plan exceeds its metadata reserve")
    return plan


def cache_plan_bytes(plan: CompiledCachePlan) -> bytes:
    return canonical_json_bytes(plan.to_dict()) + b"\n"


def load_cache_config_bytes(content: bytes) -> CacheExperimentConfig:
    """Load bounded unambiguous private JSON with one sanitized failure category."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("non-finite value")

    try:
        if not 1 <= len(content) <= MAX_INPUT_BYTES:
            raise ValueError
        text = content.decode("utf-8")
        validate_json_structure(text)
        json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=bounded_json_float,
        )
        return CacheExperimentConfig.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise EvaluationError("cache configuration violates its contract") from None
