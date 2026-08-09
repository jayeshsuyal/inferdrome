"""Resolve untrusted source YAML into frozen public execution inputs."""

import hmac
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from inferdrome.domain.digests import DigestDomain, digest_bytes, digest_canonical_json
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    ConcurrentTraffic,
    EvidenceConfig,
    ExperimentIdentity,
    ExperimentLinks,
    ExperimentSpec,
    FakeExecution,
    MeasurementConfig,
    NativeOutputSensitivity,
    RequestRateTraffic,
    SyntheticTarget,
    TrafficConfig,
    VllmExecution,
    WorkloadConfig,
)
from inferdrome.domain.ids import (
    RunId,
    new_run_id,
    request_id_from_index,
    sha256_digest,
)
from inferdrome.domain.request_plan import (
    DigestOnlyPrompt,
    InlinePrompt,
    PlannedRequest,
    RequestPlan,
    SamplingConfig,
)
from inferdrome.domain.states import Replayability
from inferdrome.errors import ResolutionError, SourceInputError
from inferdrome.resolution.canonicalization import (
    canonical_model_bytes,
    execution_fingerprint,
)
from inferdrome.resolution.io import read_bounded_regular_file, resolve_safe_child
from inferdrome.resolution.source import (
    SourceAttachedVllmTarget,
    SourceConcurrentTraffic,
    SourceExperimentSpec,
    SourceFakeExecution,
    SourceSyntheticTarget,
    SourceVllmExecution,
)
from inferdrome.resolution.workload import parse_custom_workload
from inferdrome.resolution.yaml_loader import load_strict_yaml

MAX_SOURCE_BYTES = 1_048_576
MAX_WORKLOAD_BYTES = 134_217_728


@dataclass(frozen=True)
class ResolutionResult:
    run_id: str
    source_path: Path
    workload_path: Path
    source_bytes: bytes
    workload_bytes: bytes
    resolved_spec: ExperimentSpec
    request_plan: RequestPlan
    resolved_spec_bytes: bytes
    request_plan_bytes: bytes
    source_spec_digest: str
    execution_fingerprint: str
    request_plan_digest: str


def _decimal_text(value: int | str) -> str:
    return str(value)


def _resolve_traffic(source: SourceExperimentSpec) -> TrafficConfig:
    if isinstance(source.traffic, SourceConcurrentTraffic):
        return ConcurrentTraffic(
            kind="concurrent",
            concurrency=source.traffic.concurrency,
            warmup_requests=source.traffic.warmup_requests,
            measured_requests=source.traffic.measured_requests,
        )

    return RequestRateTraffic(
        kind="request_rate",
        requests_per_second=_decimal_text(source.traffic.requests_per_second),
        burstiness=_decimal_text(source.traffic.burstiness),
        max_concurrency=source.traffic.max_concurrency,
        warmup_requests=source.traffic.warmup_requests,
        measured_requests=source.traffic.measured_requests,
    )


def _resolve_public_spec(
    source: SourceExperimentSpec,
    workload_sha256: str,
) -> ExperimentSpec:
    traffic = _resolve_traffic(source)
    maximum_requests = (
        source.execution.max_measured_requests
        if source.execution.max_measured_requests is not None
        else traffic.measured_requests
    )
    execution: VllmExecution | FakeExecution
    target: AttachedVllmTarget | SyntheticTarget

    if isinstance(source.execution, SourceVllmExecution):
        execution = VllmExecution(
            mode="attached_endpoint",
            adapter="vllm_bench_serve",
            adapter_version=source.execution.adapter_version,
            producer_name="vllm",
            producer_version="0.26.0",
            max_runtime_seconds=source.execution.max_runtime_seconds,
            max_measured_requests=maximum_requests,
        )
        if not isinstance(source.target, SourceAttachedVllmTarget):
            raise ResolutionError("vLLM execution requires a vLLM target")
        target = AttachedVllmTarget(
            engine="vllm",
            endpoint=source.target.endpoint,
            api="openai_chat_completions",
            model=source.target.model,
            model_revision=source.target.model_revision,
            tokenizer_revision=source.target.tokenizer_revision,
            engine_version=source.target.engine_version,
        )
        sensitivity = NativeOutputSensitivity.RESPONSE_CONTENT
    else:
        if not isinstance(source.execution, SourceFakeExecution) or not isinstance(
            source.target, SourceSyntheticTarget
        ):
            raise ResolutionError("fake execution requires a synthetic target")
        execution = FakeExecution(
            mode="synthetic_fixture",
            adapter="fake",
            adapter_version=source.execution.adapter_version,
            producer_name="inferdrome_fake",
            producer_version=source.execution.producer_version,
            max_runtime_seconds=source.execution.max_runtime_seconds,
            max_measured_requests=maximum_requests,
        )
        target = SyntheticTarget(
            engine="fake",
            api="synthetic_fixture",
            model=source.target.model,
        )
        sensitivity = NativeOutputSensitivity.NON_SENSITIVE_FIXTURE

    return ExperimentSpec(
        schema_version="inferdrome.experiment.v1",
        experiment=ExperimentIdentity(
            id=source.experiment.id,
            title=source.experiment.title or source.experiment.id,
            hypothesis=source.experiment.hypothesis,
        ),
        execution=execution,
        target=target,
        workload=WorkloadConfig(
            path=source.workload.path,
            sha256=workload_sha256,
            prompt_content_policy=source.workload.prompt_content_policy,
            requested_output_tokens=source.workload.requested_output_tokens,
            temperature=_decimal_text(source.workload.temperature),
            seed=source.workload.seed,
        ),
        traffic=traffic,
        measurement=MeasurementConfig(
            streaming=True,
            ttft_definition=source.measurement.ttft_definition,
            choices_span_definition=source.measurement.choices_span_definition,
            metric_definitions_version=source.measurement.metric_definitions_version,
            reducer_version=source.measurement.reducer_version,
        ),
        evidence=EvidenceConfig(
            native_output_sensitivity=sensitivity,
            canonical_response_content=(
                source.evidence.canonical_response_content
            ),
            include_request_plan=True,
        ),
        links=ExperimentLinks(
            exitspec_contract_digest=source.links.exitspec_contract_digest
        ),
    )


def _build_request_plan(
    *,
    run_id: str,
    source_digest: str,
    spec: ExperimentSpec,
    prompts: tuple[str, ...],
) -> RequestPlan:
    measured_count = spec.traffic.measured_requests
    if len(prompts) < measured_count:
        raise ResolutionError("workload has fewer prompts than measured requests")

    include_prompts = spec.workload.prompt_content_policy.value == "include"
    prefix = f"{run_id}-"
    requests = []
    for sequence_index, prompt in enumerate(prompts[:measured_count]):
        prompt_digest = sha256_digest(prompt.encode("utf-8"))
        prompt_material = (
            InlinePrompt(kind="inline", text=prompt, sha256=prompt_digest)
            if include_prompts
            else DigestOnlyPrompt(kind="digest_only", sha256=prompt_digest)
        )
        requests.append(
            PlannedRequest(
                sequence_index=sequence_index,
                request_id=request_id_from_index(sequence_index),
                producer_request_id=f"{prefix}{sequence_index}",
                prompt=prompt_material,
                sampling=SamplingConfig(
                    requested_output_tokens=spec.workload.requested_output_tokens,
                    temperature=spec.workload.temperature,
                    seed=spec.workload.seed,
                ),
            )
        )

    replayability = Replayability.FULL if include_prompts else Replayability.LIMITED
    return RequestPlan(
        schema_version="inferdrome.request-plan.v1",
        run_id=run_id,
        experiment_id=spec.experiment.id,
        source_spec_digest=source_digest,
        ordering="sequence_index_ascending_v1",
        request_id_derivation="sequence_index_decimal_v1",
        producer_request_id_derivation="prefix_plus_sequence_index_v1",
        producer_request_id_prefix=prefix,
        replayability=replayability,
        traffic=spec.traffic,
        requests=tuple(requests),
    )


def resolve_experiment(
    source_path: Path,
    *,
    run_id: str | None = None,
    strict: bool = True,
) -> ResolutionResult:
    """Resolve one source file and its workload without rereading either input."""

    absolute_source_path = source_path.absolute()
    source_bytes = read_bounded_regular_file(
        absolute_source_path,
        label="source experiment",
        limit=MAX_SOURCE_BYTES,
    )
    source_value = load_strict_yaml(source_bytes)
    try:
        source_json = json.dumps(source_value, allow_nan=False)
        source = SourceExperimentSpec.model_validate_json(source_json)
    except (TypeError, ValueError, ValidationError):
        raise SourceInputError("source experiment failed strict validation") from None

    if strict:
        if source.workload.sha256 is None:
            raise ResolutionError("strict mode requires an expected workload digest")
        if isinstance(source.target, SourceAttachedVllmTarget) and (
            source.target.model_revision is None
            or source.target.tokenizer_revision is None
        ):
            raise ResolutionError(
                "strict mode requires model and tokenizer revisions"
            )

    workload_path = resolve_safe_child(
        absolute_source_path.parent,
        source.workload.path,
        label="workload",
    )
    workload_bytes = read_bounded_regular_file(
        workload_path,
        label="workload",
        limit=MAX_WORKLOAD_BYTES,
    )
    workload_sha256 = sha256_digest(workload_bytes)
    if source.workload.sha256 is not None and not hmac.compare_digest(
        source.workload.sha256,
        workload_sha256,
    ):
        raise ResolutionError("workload digest does not match the expected value")

    prompts = parse_custom_workload(workload_bytes)
    selected_run_id = run_id or new_run_id()
    try:
        selected_run_id = TypeAdapter(RunId).validate_python(
            selected_run_id, strict=True
        )
    except ValidationError:
        raise ResolutionError("run ID is invalid") from None

    source_digest = digest_bytes(DigestDomain.SOURCE_SPEC, source_bytes)
    try:
        spec = _resolve_public_spec(source, workload_sha256)
        plan = _build_request_plan(
            run_id=selected_run_id,
            source_digest=source_digest,
            spec=spec,
            prompts=prompts,
        )
    except ValidationError:
        raise ResolutionError("resolved contract invariants failed") from None
    resolved_bytes = canonical_model_bytes(spec)
    plan_bytes = canonical_model_bytes(plan)
    return ResolutionResult(
        run_id=selected_run_id,
        source_path=absolute_source_path,
        workload_path=workload_path,
        source_bytes=source_bytes,
        workload_bytes=workload_bytes,
        resolved_spec=spec,
        request_plan=plan,
        resolved_spec_bytes=resolved_bytes,
        request_plan_bytes=plan_bytes,
        source_spec_digest=source_digest,
        execution_fingerprint=execution_fingerprint(spec),
        request_plan_digest=digest_canonical_json(
            DigestDomain.REQUEST_PLAN,
            plan.model_dump(mode="json", by_alias=True, exclude_none=False),
        ),
    )


def validate_resolution_result(resolution: ResolutionResult) -> None:
    """Rebuild and verify every execution-relevant field without rereading paths."""

    if (
        not isinstance(resolution.source_bytes, bytes)
        or not isinstance(resolution.workload_bytes, bytes)
        or len(resolution.source_bytes) > MAX_SOURCE_BYTES
        or len(resolution.workload_bytes) > MAX_WORKLOAD_BYTES
        or resolution.source_path != resolution.source_path.absolute()
        or resolution.workload_path != resolution.workload_path.absolute()
    ):
        raise ResolutionError("resolved execution input is internally inconsistent")
    source_value = load_strict_yaml(resolution.source_bytes)
    try:
        source_json = json.dumps(source_value, allow_nan=False)
        source = SourceExperimentSpec.model_validate_json(source_json)
        selected_run_id = TypeAdapter(RunId).validate_python(
            resolution.run_id,
            strict=True,
        )
    except (TypeError, ValueError, ValidationError):
        raise ResolutionError(
            "resolved execution input is internally inconsistent"
        ) from None
    expected_workload_path = resolve_safe_child(
        resolution.source_path.parent,
        source.workload.path,
        label="workload",
    )
    workload_sha256 = sha256_digest(resolution.workload_bytes)
    if (
        expected_workload_path != resolution.workload_path
        or (
            source.workload.sha256 is not None
            and not hmac.compare_digest(
                source.workload.sha256,
                workload_sha256,
            )
        )
    ):
        raise ResolutionError("resolved execution input is internally inconsistent")
    prompts = parse_custom_workload(resolution.workload_bytes)
    source_digest = digest_bytes(
        DigestDomain.SOURCE_SPEC,
        resolution.source_bytes,
    )
    try:
        spec = _resolve_public_spec(source, workload_sha256)
        plan = _build_request_plan(
            run_id=selected_run_id,
            source_digest=source_digest,
            spec=spec,
            prompts=prompts,
        )
    except ValidationError:
        raise ResolutionError(
            "resolved execution input is internally inconsistent"
        ) from None
    resolved_bytes = canonical_model_bytes(spec)
    plan_bytes = canonical_model_bytes(plan)
    request_plan_digest = digest_canonical_json(
        DigestDomain.REQUEST_PLAN,
        plan.model_dump(mode="json", by_alias=True, exclude_none=False),
    )
    if not (
        resolution.resolved_spec == spec
        and resolution.request_plan == plan
        and resolution.resolved_spec_bytes == resolved_bytes
        and resolution.request_plan_bytes == plan_bytes
        and hmac.compare_digest(resolution.source_spec_digest, source_digest)
        and hmac.compare_digest(
            resolution.execution_fingerprint,
            execution_fingerprint(spec),
        )
        and hmac.compare_digest(
            resolution.request_plan_digest,
            request_plan_digest,
        )
    ):
        raise ResolutionError("resolved execution input is internally inconsistent")
