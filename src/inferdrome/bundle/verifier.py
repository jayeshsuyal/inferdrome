"""Offline, read-only, cross-artifact evidence-bundle verification."""

import hmac
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ValidationError

from inferdrome.adapters.fake import (
    FakeNativeResult,
    fake_native_schema_fingerprint,
)
from inferdrome.adapters.vllm_bench import (
    VllmInvocation,
    parse_vllm_version_output,
    validate_vllm_invocation_evidence,
)
from inferdrome.bundle.manifest import IntegrityManifest
from inferdrome.bundle.reader import (
    BundleLimits,
    BundleReader,
    strict_json_value,
    strict_jsonl_lines,
)
from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_bytes,
    digest_canonical_json,
)
from inferdrome.domain.environment import (
    EnvironmentField,
    EnvironmentFieldName,
    EnvironmentManifest,
    ProvenanceKind,
)
from inferdrome.domain.evidence import (
    ArtifactRole,
    EvidenceBundle,
    FakeProducerDescriptor,
    VllmProducerDescriptor,
)
from inferdrome.domain.execution import ExecutionRecord
from inferdrome.domain.experiment import (
    AttachedVllmTarget,
    CanonicalResponseContentPolicy,
    ExperimentSpec,
    FakeExecution,
    PromptContentPolicy,
    SyntheticTarget,
    VllmExecution,
)
from inferdrome.domain.ids import sha256_digest
from inferdrome.domain.metrics import Measurements, MetricDefinitions
from inferdrome.domain.request_plan import InlinePrompt, RequestPlan
from inferdrome.domain.request_record import (
    FakeProducerSemantics,
    RequestRecord,
)
from inferdrome.domain.states import EnvironmentCompleteness, EvidenceEligibility
from inferdrome.errors import (
    AdapterError,
    NormalizationError,
    ReductionError,
    VerificationError,
)
from inferdrome.metrics import reduce_measurements
from inferdrome.normalization import (
    build_vllm_execution_record,
    normalize_vllm_native,
    vllm_native_schema_fingerprint,
)
from inferdrome.resolution.canonicalization import execution_fingerprint


@dataclass(frozen=True)
class VerificationReport:
    bundle_path: Path
    bundle_digest: str
    run_id: str
    artifact_count: int
    total_bytes: int
    descriptor: EvidenceBundle


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _validated_model(
    content: bytes,
    model: type[BaseModel],
    *,
    label: str,
    require_canonical: bool = True,
) -> BaseModel:
    strict_json_value(content, label=label)
    try:
        value = model.model_validate_json(content)
    except ValidationError:
        raise VerificationError(f"{label} failed contract validation") from None
    if require_canonical and content != _canonical_model_bytes(value):
        raise VerificationError(f"{label} is not canonical JSON")
    return value


def _expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _role_paths(descriptor: EvidenceBundle) -> dict[ArtifactRole, str]:
    return {artifact.role: artifact.path for artifact in descriptor.artifacts}


def _verify_manifest(
    reader: BundleReader,
    descriptor: EvidenceBundle,
    role_paths: dict[ArtifactRole, str],
    *,
    expected_bundle_digest: str | None,
) -> tuple[IntegrityManifest, str]:
    manifest_path = role_paths[ArtifactRole.INTEGRITY_MANIFEST]
    manifest_bytes = reader.read_bytes(manifest_path)
    manifest = _validated_model(
        manifest_bytes,
        IntegrityManifest,
        label="integrity manifest",
    )
    if not isinstance(manifest, IntegrityManifest):
        raise AssertionError("validated manifest has unexpected type")
    if manifest.run_id != descriptor.run_id:
        raise VerificationError("manifest belongs to a different run")

    bundle_digest = digest_bytes(DigestDomain.BUNDLE_MANIFEST, manifest_bytes)
    if expected_bundle_digest is not None and not hmac.compare_digest(
        bundle_digest, expected_bundle_digest
    ):
        raise VerificationError("bundle digest does not match retained digest")

    manifest_by_role = {entry.role: entry for entry in manifest.entries}
    for artifact in descriptor.artifacts:
        if artifact.role is ArtifactRole.INTEGRITY_MANIFEST:
            continue
        entry = manifest_by_role.get(artifact.role)
        if entry is None or entry.path != artifact.path:
            raise VerificationError("manifest and artifact inventory disagree")
        content = reader.read_bytes(entry.path)
        if len(content) != entry.size_bytes:
            raise VerificationError("artifact size does not match manifest")
        actual_hash = sha256_digest(content)
        if not hmac.compare_digest(actual_hash, entry.sha256):
            raise VerificationError("artifact hash does not match manifest")
    return manifest, bundle_digest


def _verify_fake_native_and_records(
    descriptor: EvidenceBundle,
    native: FakeNativeResult,
    records: tuple[RequestRecord, ...],
    plan: RequestPlan,
) -> None:
    if not isinstance(descriptor.producer, FakeProducerDescriptor):
        raise VerificationError("fake native output requires fake bundle producer")
    expected_fingerprint = fake_native_schema_fingerprint()
    if descriptor.producer.native_schema_fingerprint != expected_fingerprint:
        raise VerificationError("fake native schema fingerprint is unsupported")
    if native.run_id != descriptor.run_id or len(native.rows) != len(records):
        raise VerificationError("fake native population disagrees with bundle")
    if native.producer_version != descriptor.producer.version:
        raise VerificationError("fake native producer version disagrees with bundle")

    for planned, native_row, record in zip(
        plan.requests, native.rows, records, strict=True
    ):
        if not isinstance(record.producer, FakeProducerSemantics):
            raise VerificationError("fake bundle contains non-fake request records")
        if record.producer.native_schema_fingerprint != expected_fingerprint:
            raise VerificationError("request record native fingerprint disagrees")
        if (
            record.producer.producer_version != descriptor.producer.version
            or record.producer.adapter_version != descriptor.producer.adapter_version
        ):
            raise VerificationError("request record producer identity disagrees")
        if (
            native_row.request_id != planned.request_id
            or native_row.producer_request_id != planned.producer_request_id
            or record.request_id != planned.request_id
            or record.producer_request_id != planned.producer_request_id
            or native_row.prompt_sha256 != planned.prompt.sha256
            or record.content.prompt_sha256 != planned.prompt.sha256
            or record.native_source.artifact_path != "native/benchmark-result.json"
        ):
            raise VerificationError("request plan, native row, and record disagree")
        if (
            record.tokens.input_tokens != native_row.input_tokens
            or record.tokens.output_tokens != native_row.output_tokens
            or record.timing.start_offset_ns != native_row.start_offset_ns
            or record.timing.ttft_ns != native_row.ttft_ns
            or record.timing.itl_ns != native_row.itl_ns
            or record.outcome.status is not native_row.status
            or record.outcome.producer_error != native_row.producer_error
        ):
            raise VerificationError("fake canonical observations changed native values")
        expected_response_digest = (
            sha256_digest(native_row.response_content.encode("utf-8"))
            if native_row.response_content is not None
            else None
        )
        if record.content.response_sha256 != expected_response_digest:
            raise VerificationError(
                "canonical response digest disagrees with native row"
            )
        canonical_content = record.content.canonical_response_content
        if descriptor.sensitivity.canonical_response_content_included:
            if (
                native_row.response_content is not None
                and canonical_content != native_row.response_content
            ):
                raise VerificationError(
                    "canonical response content disagrees with native row"
                )
        elif canonical_content is not None:
            raise VerificationError(
                "canonical response content disagrees with native row"
            )


def _decode_text(content: bytes, *, label: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise VerificationError(f"{label} is not valid UTF-8") from None


def _verify_customer_eligible_environment(
    descriptor: EvidenceBundle,
    invocation: VllmInvocation,
    environment: EnvironmentManifest,
    environment_by_name: dict[EnvironmentFieldName, EnvironmentField],
    role_paths: dict[ArtifactRole, str],
) -> None:
    if descriptor.evidence_eligibility is not EvidenceEligibility.CUSTOMER_ELIGIBLE:
        return
    proof = invocation.local_gpu_proof
    if proof is None:
        raise VerificationError("customer-eligible vLLM evidence lacks local GPU proof")
    if descriptor.environment_completeness is not EnvironmentCompleteness.COMPLETE:
        raise VerificationError("customer-eligible vLLM environment is incomplete")
    if (
        proof.captured_at > environment.captured_at
        or environment.captured_at != descriptor.created_at
    ):
        raise VerificationError(
            "customer-eligible environment capture time disagrees with execution"
        )
    invocation_path = role_paths[ArtifactRole.PRODUCER_INVOCATION]
    version_path = role_paths[ArtifactRole.PRODUCER_VERSION]
    expected: dict[
        EnvironmentFieldName,
        tuple[str | int, ProvenanceKind, str],
    ] = {
        EnvironmentFieldName.CLIENT_OS: (
            proof.client_os,
            ProvenanceKind.CLIENT_OBSERVED,
            invocation_path,
        ),
        EnvironmentFieldName.CLIENT_ARCH: (
            proof.client_arch,
            ProvenanceKind.CLIENT_OBSERVED,
            invocation_path,
        ),
        EnvironmentFieldName.CLIENT_PYTHON_VERSION: (
            proof.client_python_version,
            ProvenanceKind.CLIENT_OBSERVED,
            invocation_path,
        ),
        EnvironmentFieldName.PRODUCER_VERSION: (
            proof.producer_distribution.version,
            ProvenanceKind.LOCALLY_VERIFIED,
            version_path,
        ),
        EnvironmentFieldName.PRODUCER_DISTRIBUTION_SHA256: (
            proof.producer_distribution.sha256,
            ProvenanceKind.LOCALLY_VERIFIED,
            invocation_path,
        ),
        EnvironmentFieldName.TARGET_ENGINE_VERSION: (
            proof.producer_distribution.version,
            ProvenanceKind.LOCALLY_VERIFIED,
            invocation_path,
        ),
        EnvironmentFieldName.TARGET_MODEL_REVISION: (
            proof.model_snapshot.revision,
            ProvenanceKind.CONFIGURED,
            invocation_path,
        ),
        EnvironmentFieldName.TARGET_TOKENIZER_REVISION: (
            proof.tokenizer_snapshot.revision,
            ProvenanceKind.CONFIGURED,
            invocation_path,
        ),
        EnvironmentFieldName.SERVER_MODEL_ID: (
            invocation.preflight.result.target_model,
            ProvenanceKind.SERVER_REPORTED,
            invocation_path,
        ),
        EnvironmentFieldName.GPU_MODEL: (
            proof.gpu_model,
            ProvenanceKind.LOCALLY_VERIFIED,
            invocation_path,
        ),
        EnvironmentFieldName.GPU_COUNT: (
            len(proof.gpus),
            ProvenanceKind.LOCALLY_VERIFIED,
            invocation_path,
        ),
        EnvironmentFieldName.CUDA_VERSION: (
            proof.cuda_runtime_version,
            ProvenanceKind.LOCALLY_VERIFIED,
            invocation_path,
        ),
        EnvironmentFieldName.DRIVER_VERSION: (
            proof.driver_version,
            ProvenanceKind.LOCALLY_VERIFIED,
            invocation_path,
        ),
    }
    for name, (value, provenance, evidence_path) in expected.items():
        field = environment_by_name[name]
        if (
            field.value != value
            or field.provenance is not provenance
            or field.evidence_path != evidence_path
        ):
            raise VerificationError(
                "customer-eligible environment disagrees with local GPU proof"
            )


def verify_bundle(
    bundle_path: Path,
    *,
    expected_bundle_digest: str | None = None,
    limits: BundleLimits | None = None,
    require_immutable: bool = True,
) -> VerificationReport:
    """Verify a closed bundle without mutation, execution, or network access."""

    reader = BundleReader(
        bundle_path,
        limits=limits,
        require_immutable=require_immutable,
    )
    descriptor_bytes = reader.read_bytes("bundle.json")
    descriptor = _validated_model(
        descriptor_bytes,
        EvidenceBundle,
        label="bundle descriptor",
    )
    if not isinstance(descriptor, EvidenceBundle):
        raise AssertionError("validated descriptor has unexpected type")
    role_paths = _role_paths(descriptor)
    if role_paths[ArtifactRole.BUNDLE_DESCRIPTOR] != "bundle.json":
        raise VerificationError(
            "bundle descriptor must inventory itself at bundle.json"
        )

    declared_paths = set(role_paths.values())
    if set(reader.files) != declared_paths:
        raise VerificationError("bundle contains missing or undeclared files")
    if reader.directories != _expected_directories(declared_paths):
        raise VerificationError("bundle contains missing or undeclared directories")

    _, bundle_digest = _verify_manifest(
        reader,
        descriptor,
        role_paths,
        expected_bundle_digest=expected_bundle_digest,
    )

    def read_role(role: ArtifactRole) -> bytes:
        return reader.read_bytes(role_paths[role])

    resolved = _validated_model(
        read_role(ArtifactRole.RESOLVED_SPEC),
        ExperimentSpec,
        label="resolved experiment",
    )
    plan = _validated_model(
        read_role(ArtifactRole.REQUEST_PLAN),
        RequestPlan,
        label="request plan",
    )
    environment = _validated_model(
        read_role(ArtifactRole.ENVIRONMENT),
        EnvironmentManifest,
        label="environment manifest",
    )
    execution = _validated_model(
        read_role(ArtifactRole.EXECUTION),
        ExecutionRecord,
        label="execution record",
    )
    definitions = _validated_model(
        read_role(ArtifactRole.METRIC_DEFINITIONS),
        MetricDefinitions,
        label="metric definitions",
    )
    measurements = _validated_model(
        read_role(ArtifactRole.MEASUREMENTS),
        Measurements,
        label="measurements",
    )
    if not isinstance(resolved, ExperimentSpec):
        raise AssertionError
    if not isinstance(plan, RequestPlan):
        raise AssertionError
    if not isinstance(environment, EnvironmentManifest):
        raise AssertionError
    if not isinstance(execution, ExecutionRecord):
        raise AssertionError
    if not isinstance(definitions, MetricDefinitions):
        raise AssertionError
    if not isinstance(measurements, Measurements):
        raise AssertionError

    record_bytes = read_role(ArtifactRole.REQUEST_RECORDS)
    record_lines = strict_jsonl_lines(
        record_bytes,
        label="canonical request records",
        max_line_bytes=reader.limits.max_jsonl_line_bytes,
    )
    records_list = []
    for line in record_lines:
        try:
            record = RequestRecord.model_validate_json(line)
        except ValidationError:
            raise VerificationError(
                "request record failed contract validation"
            ) from None
        if line != _canonical_model_bytes(record):
            raise VerificationError("request record is not canonical JSON")
        records_list.append(record)
    records = tuple(records_list)

    source_bytes = read_role(ArtifactRole.ORIGINAL_SPEC)
    source_digest = digest_bytes(DigestDomain.SOURCE_SPEC, source_bytes)
    if descriptor.digests.source_spec_digest != source_digest:
        raise VerificationError("source-spec digest disagrees with original bytes")
    if plan.source_spec_digest != source_digest:
        raise VerificationError("request plan source digest disagrees")
    if descriptor.digests.execution_fingerprint != execution_fingerprint(resolved):
        raise VerificationError("execution fingerprint disagrees with resolved spec")
    plan_value = plan.model_dump(mode="json", by_alias=True, exclude_none=False)
    plan_digest = digest_canonical_json(DigestDomain.REQUEST_PLAN, plan_value)
    if descriptor.digests.request_plan_digest != plan_digest:
        raise VerificationError("request-plan digest disagrees")
    definition_value = definitions.model_dump(
        mode="json", by_alias=True, exclude_none=False
    )
    definition_digest = digest_canonical_json(
        DigestDomain.METRIC_DEFINITIONS, definition_value
    )
    if descriptor.digests.metric_definitions_digest != definition_digest:
        raise VerificationError("metric-definition digest disagrees")
    if descriptor.digests.exitspec_contract_digest != (
        resolved.links.exitspec_contract_digest
    ):
        raise VerificationError("ExitSpec contract linkage disagrees")

    run_ids = {
        descriptor.run_id,
        plan.run_id,
        environment.run_id,
        execution.run_id,
        measurements.run_id,
        *(record.run_id for record in records),
    }
    if len(run_ids) != 1:
        raise VerificationError("bundle artifacts disagree on run ID")
    if descriptor.experiment_id != resolved.experiment.id:
        raise VerificationError("bundle and resolved experiment IDs disagree")
    if plan.experiment_id != resolved.experiment.id:
        raise VerificationError("request plan belongs to a different experiment")
    traffic_disagrees = (
        plan.traffic != resolved.traffic
        or execution.configured_traffic != resolved.traffic
    )
    if traffic_disagrees:
        raise VerificationError("resolved, planned, and executed traffic disagree")
    if descriptor.environment_completeness is not environment.completeness:
        raise VerificationError("environment completeness summary disagrees")
    if descriptor.replayability is not plan.replayability:
        raise VerificationError("bundle replayability disagrees with request plan")
    if isinstance(descriptor.producer, FakeProducerDescriptor):
        if not isinstance(resolved.execution, FakeExecution) or not isinstance(
            resolved.target, SyntheticTarget
        ):
            raise VerificationError("fake bundle requires a synthetic resolved spec")
        if (
            resolved.execution.producer_version != descriptor.producer.version
            or resolved.execution.adapter_version
            != descriptor.producer.adapter_version
        ):
            raise VerificationError("resolved and bundled producer identities disagree")
    elif not isinstance(resolved.execution, VllmExecution) or not isinstance(
        resolved.target, AttachedVllmTarget
    ):
        raise VerificationError("vLLM bundle requires an attached resolved spec")
    elif (
        resolved.execution.producer_version != descriptor.producer.version
        or resolved.execution.adapter_version
        != descriptor.producer.adapter_version
    ):
        raise VerificationError("resolved and bundled producer identities disagree")
    prompt_content_present = any(
        isinstance(request.prompt, InlinePrompt) for request in plan.requests
    )
    prompt_policy_includes = (
        resolved.workload.prompt_content_policy is PromptContentPolicy.INCLUDE
    )
    if prompt_content_present != prompt_policy_includes:
        raise VerificationError("resolved prompt policy disagrees with request plan")
    if descriptor.sensitivity.prompt_content_in_request_plan != prompt_policy_includes:
        raise VerificationError(
            "prompt-content sensitivity disagrees with request plan"
        )
    canonical_policy_includes = (
        resolved.evidence.canonical_response_content
        is CanonicalResponseContentPolicy.INCLUDE
    )
    if (
        descriptor.sensitivity.canonical_response_content_included
        != canonical_policy_includes
    ):
        raise VerificationError("canonical response-content sensitivity disagrees")
    if not descriptor.sensitivity.native_response_content_present:
        raise VerificationError("native response-content presence is hidden")

    environment_by_name = {field.name: field for field in environment.fields}
    if any(
        field.evidence_path is not None
        and field.evidence_path not in declared_paths
        for field in environment.fields
    ):
        raise VerificationError("environment evidence path is not in the bundle")
    environment_producer_version = environment_by_name[
        EnvironmentFieldName.PRODUCER_VERSION
    ].value
    if environment_producer_version != descriptor.producer.version:
        raise VerificationError("environment producer version disagrees")

    invocation_bytes = read_role(ArtifactRole.PRODUCER_INVOCATION)
    invocation_value = strict_json_value(
        invocation_bytes,
        label="producer invocation",
    )
    if not isinstance(invocation_value, dict):
        raise VerificationError("producer invocation must be a JSON object")
    vllm_invocation: VllmInvocation | None = None
    if isinstance(descriptor.producer, VllmProducerDescriptor):
        if not isinstance(resolved.target, AttachedVllmTarget):
            raise VerificationError("vLLM bundle requires an attached target")
        vllm_target = resolved.target
        try:
            vllm_invocation = validate_vllm_invocation_evidence(
                invocation_bytes,
                resolved,
                plan,
                execution_fingerprint=descriptor.digests.execution_fingerprint,
            )
        except AdapterError as error:
            raise VerificationError("vLLM invocation evidence is invalid") from error
        server_model = environment_by_name[EnvironmentFieldName.SERVER_MODEL_ID]
        if (
            server_model.value != vllm_target.model
            or server_model.provenance is not ProvenanceKind.SERVER_REPORTED
            or server_model.evidence_path
            != role_paths[ArtifactRole.PRODUCER_INVOCATION]
        ):
            raise VerificationError("environment server model evidence disagrees")
        producer_version_field = environment_by_name[
            EnvironmentFieldName.PRODUCER_VERSION
        ]
        if (
            producer_version_field.evidence_path
            != role_paths[ArtifactRole.PRODUCER_VERSION]
        ):
            raise VerificationError("environment producer version evidence disagrees")
        configured_environment_values = {
            EnvironmentFieldName.TARGET_ENGINE_VERSION: (
                vllm_target.engine_version
            ),
            EnvironmentFieldName.TARGET_MODEL_REVISION: (
                vllm_target.model_revision
            ),
            EnvironmentFieldName.TARGET_TOKENIZER_REVISION: (
                vllm_target.tokenizer_revision
            ),
        }
        if any(
            expected is not None
            and environment_by_name[name].value is not None
            and environment_by_name[name].value != expected
            for name, expected in configured_environment_values.items()
        ):
            raise VerificationError("environment target identity disagrees")
        _verify_customer_eligible_environment(
            descriptor,
            vllm_invocation,
            environment,
            environment_by_name,
            role_paths,
        )

    producer_version_bytes = read_role(ArtifactRole.PRODUCER_VERSION)
    if isinstance(descriptor.producer, FakeProducerDescriptor):
        producer_version = _decode_text(
            producer_version_bytes,
            label="producer version",
        ).strip()
        if producer_version != descriptor.producer.version:
            raise VerificationError("producer version artifact disagrees")
    else:
        try:
            parse_vllm_version_output(producer_version_bytes)
        except AdapterError as error:
            raise VerificationError("producer version artifact disagrees") from error

    exit_status_text = _decode_text(
        read_role(ArtifactRole.PRODUCER_EXIT_STATUS), label="producer exit status"
    )
    if exit_status_text != f"{execution.producer_exit_status}\n":
        raise VerificationError("producer exit-status artifact disagrees")
    _decode_text(read_role(ArtifactRole.NATIVE_STDOUT), label="producer stdout")
    _decode_text(read_role(ArtifactRole.NATIVE_STDERR), label="producer stderr")

    native_bytes = read_role(ArtifactRole.NATIVE_RESULT)
    if execution.native_result_sha256 != sha256_digest(native_bytes):
        raise VerificationError("execution native-result hash disagrees")
    if isinstance(descriptor.producer, FakeProducerDescriptor):
        native = _validated_model(
            native_bytes,
            FakeNativeResult,
            label="fake native result",
        )
        if not isinstance(native, FakeNativeResult):
            raise AssertionError
        _verify_fake_native_and_records(descriptor, native, records, plan)
    else:
        if vllm_invocation is None:
            raise AssertionError
        expected_fingerprint = vllm_native_schema_fingerprint()
        if descriptor.producer.native_schema_fingerprint != expected_fingerprint:
            raise VerificationError("vLLM native schema fingerprint is unsupported")
        try:
            normalized = normalize_vllm_native(
                native_bytes,
                resolved,
                plan,
                expected_metadata=vllm_invocation.metadata,
                expected_tokenizer_id=str(vllm_invocation.paths.tokenizer_path),
            )
        except NormalizationError as error:
            raise VerificationError("vLLM native normalization failed") from error
        if normalized.native_schema_fingerprint != expected_fingerprint:
            raise VerificationError("vLLM normalizer fingerprint disagrees")
        if (
            normalized.request_records != records
            or normalized.request_records_bytes != record_bytes
        ):
            raise VerificationError("vLLM canonical records changed native values")
        expected_execution = build_vllm_execution_record(
            normalized,
            plan,
            native_bytes,
            started_at=execution.started_at,
            ended_at=execution.ended_at,
            producer_exit_status=execution.producer_exit_status,
        )
        if execution != expected_execution:
            raise VerificationError("vLLM execution record changed native values")

    try:
        recalculated = reduce_measurements(
            execution,
            records,
            metric_definitions=definitions,
        )
    except ReductionError as error:
        raise VerificationError("measurement recalculation failed") from error
    if recalculated.request_records_bytes != record_bytes:
        raise VerificationError("request-record bytes disagree with reducer input")
    if recalculated.measurements != measurements:
        raise VerificationError("stored measurements disagree with recalculation")
    if recalculated.measurements_bytes != read_role(ArtifactRole.MEASUREMENTS):
        raise VerificationError("stored measurement bytes are not deterministic")

    reader.assert_unchanged()

    return VerificationReport(
        bundle_path=reader.root,
        bundle_digest=bundle_digest,
        run_id=descriptor.run_id,
        artifact_count=len(reader.files),
        total_bytes=reader.total_bytes,
        descriptor=descriptor,
    )
