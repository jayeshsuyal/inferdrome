"""Command-line interface for the frozen Inferdrome v0.1 workflow."""

import argparse
import json
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn, cast

from inferdrome import __version__
from inferdrome.bundle import recalculate_bundle, verify_bundle
from inferdrome.domain.controlled_comparison import (
    ConcurrencyIndependentVariable,
    OutcomeSelector,
    frozen_outcome_selector,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import ConcurrentTraffic
from inferdrome.domain.metrics import Aggregation, MetricId
from inferdrome.domain.states import EvidenceEligibility, RunState
from inferdrome.errors import (
    AdapterError,
    CancellationRequested,
    InferdromeError,
    VerificationError,
)
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
)
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.gpu_proof import ManagedVllmConfig
from inferdrome.qwen3_campaign import QWEN3_8B_PROFILE_ID
from inferdrome.resolution import resolve_experiment
from inferdrome.workspace import RunWorkspace

_VALIDATION_RUN_ID = "run-00000000000000000000000000000000"
_Command = Callable[[argparse.Namespace], int]


def _path(namespace: argparse.Namespace, name: str) -> Path:
    return Path(cast(str, getattr(namespace, name)))


def _optional_path(namespace: argparse.Namespace, name: str) -> Path | None:
    value = cast(str | None, getattr(namespace, name))
    return Path(value) if value is not None else None


def _optional_text(namespace: argparse.Namespace, name: str) -> str | None:
    return cast(str | None, getattr(namespace, name))


def _gpu_index(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("GPU index must be an integer")
    index = int(value)
    if index > 255:
        raise argparse.ArgumentTypeError("GPU index must be between 0 and 255")
    return index


def _port(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("port must be an integer")
    port = int(value)
    if port < 1 or port > 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _comparison_repetitions(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("repetitions must be an integer")
    repetitions = int(value)
    if not 2 <= repetitions <= 100:
        raise argparse.ArgumentTypeError(
            "repetitions must be between 2 and 100 per arm"
        )
    return repetitions


def _outcome_selector(value: str) -> OutcomeSelector:
    try:
        metric_text, aggregation_text = value.split(":", maxsplit=1)
        return frozen_outcome_selector(
            MetricId(metric_text),
            Aggregation(aggregation_text),
        )
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "primary outcome must be a valid METRIC:AGGREGATION pair"
        ) from None


def _managed_vllm_config(
    namespace: argparse.Namespace,
) -> ManagedVllmConfig | None:
    enabled = cast(bool, namespace.managed_local_vllm)
    model_path = _optional_path(namespace, "managed_model_path")
    gpu_index = cast(int | None, namespace.managed_gpu_index)
    startup_timeout = cast(float | None, namespace.managed_startup_timeout_seconds)
    capability_profile_id = _optional_text(
        namespace,
        "managed_capability_profile",
    )
    if not enabled:
        if (
            model_path is not None
            or gpu_index is not None
            or startup_timeout is not None
            or capability_profile_id is not None
        ):
            raise AdapterError("managed vLLM options require --managed-local-vllm")
        return None
    if model_path is None:
        raise AdapterError("--managed-local-vllm requires --managed-model-path")
    return ManagedVllmConfig(
        model_path=model_path.absolute(),
        gpu_indices=(gpu_index if gpu_index is not None else 0,),
        startup_timeout_seconds=(
            startup_timeout if startup_timeout is not None else 900.0
        ),
        capability_profile_id=capability_profile_id,
    )


def _json_output(value: object) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    )


def _add_resolution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", help="source experiment YAML")
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        default=True,
        help="permit unresolved workload or target pins while keeping them visible",
    )


def _command_validate(namespace: argparse.Namespace) -> int:
    resolution = resolve_experiment(
        _path(namespace, "source"),
        run_id=_VALIDATION_RUN_ID,
        strict=cast(bool, namespace.strict),
    )
    _json_output(
        {
            "execution_mode": resolution.resolved_spec.execution.mode,
            "measured_requests": resolution.resolved_spec.traffic.measured_requests,
            "valid": True,
            "workload_sha256": resolution.resolved_spec.workload.sha256,
        }
    )
    return 0


def _command_resolve(namespace: argparse.Namespace) -> int:
    resolution = resolve_experiment(
        _path(namespace, "source"),
        run_id=_optional_text(namespace, "run_id"),
        strict=cast(bool, namespace.strict),
    )
    value = {
        "digests": {
            "execution_fingerprint": resolution.execution_fingerprint,
            "request_plan_digest": resolution.request_plan_digest,
            "source_spec_digest": resolution.source_spec_digest,
        },
        "request_plan": resolution.request_plan.model_dump(
            mode="json", by_alias=True, exclude_none=False
        ),
        "resolved_experiment": resolution.resolved_spec.model_dump(
            mode="json", by_alias=True, exclude_none=False
        ),
        "run_id": resolution.run_id,
    }
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    return 0


@contextmanager
def _signal_cancellation(token: CancellationToken) -> Iterator[None]:
    selected = (signal.SIGINT, signal.SIGTERM)
    previous: dict[signal.Signals, Any] = {}

    def request_cancellation(_signum: int, _frame: FrameType | None) -> None:
        token.request(CancellationReason.SIGNAL)

    try:
        for selected_signal in selected:
            previous[selected_signal] = signal.getsignal(selected_signal)
            signal.signal(selected_signal, request_cancellation)
        yield
    finally:
        for selected_signal, handler in previous.items():
            signal.signal(selected_signal, handler)


def _command_run(namespace: argparse.Namespace) -> int:
    cancellation = CancellationToken()
    with _signal_cancellation(cancellation):
        result = run_experiment(
            _path(namespace, "source"),
            runs_root=_path(namespace, "runs_root"),
            run_id=_optional_text(namespace, "run_id"),
            strict=cast(bool, namespace.strict),
            tokenizer_path=_optional_path(namespace, "tokenizer_path"),
            managed_vllm=_managed_vllm_config(namespace),
            cancellation=cancellation,
            expected_exitspec_contract_digest=_optional_text(
                namespace,
                "expected_exitspec_contract_digest",
            ),
        )
    sealed = result.sealed_bundle
    _json_output(
        {
            "bundle_digest": sealed.bundle_digest,
            "bundle_path": str(sealed.path),
            "evidence_eligibility": sealed.descriptor.evidence_eligibility.value,
            "integrity_status": sealed.descriptor.integrity_status,
            "run_id": result.resolution.run_id,
            "workspace_path": str(result.workspace.path),
        }
    )
    return 0


def _run_path(namespace: argparse.Namespace) -> Path:
    target = cast(str, namespace.run)
    candidate = Path(target)
    if candidate.is_absolute() or "/" in target or candidate.exists():
        return candidate
    return _path(namespace, "runs_root") / target


def _command_inspect(namespace: argparse.Namespace) -> int:
    workspace = RunWorkspace.open(_run_path(namespace))
    state = workspace.current_state()
    bundle_path = workspace.path / "bundle"
    bundle: dict[str, object] | None = None
    if state.state is RunState.COMPLETE and not bundle_path.exists():
        raise VerificationError("complete workspace is missing its sealed bundle")
    if state.state is not RunState.COMPLETE and bundle_path.exists():
        raise VerificationError("non-complete workspace contains a published bundle")
    if bundle_path.exists():
        report = verify_bundle(bundle_path)
        bundle = {
            "artifact_count": report.artifact_count,
            "bundle_digest": report.bundle_digest,
            "evidence_eligibility": report.descriptor.evidence_eligibility.value,
            "environment_completeness": (
                report.descriptor.environment_completeness.value
            ),
            "path": str(report.bundle_path),
            "total_bytes": report.total_bytes,
        }
    _json_output(
        {
            "bundle": bundle,
            "integrity_status": state.integrity_status.value,
            "run_id": workspace.run_id,
            "sequence_index": state.sequence_index,
            "state": state.state.value,
            "workspace_path": str(workspace.path),
        }
    )
    return 0


def _command_bundle_verify(namespace: argparse.Namespace) -> int:
    report = verify_bundle(
        _path(namespace, "bundle"),
        expected_bundle_digest=_optional_text(namespace, "expected_digest"),
    )
    if (
        cast(bool, namespace.require_customer_eligible)
        and report.descriptor.evidence_eligibility
        is not EvidenceEligibility.CUSTOMER_ELIGIBLE
    ):
        raise VerificationError("bundle is not customer-eligible")
    _json_output(
        {
            "artifact_count": report.artifact_count,
            "bundle_digest": report.bundle_digest,
            "evidence_eligibility": report.descriptor.evidence_eligibility.value,
            "integrity_status": report.descriptor.integrity_status,
            "run_id": report.run_id,
            "total_bytes": report.total_bytes,
            "valid": True,
        }
    )
    return 0


def _command_reduce(namespace: argparse.Namespace) -> int:
    analysis = recalculate_bundle(
        _path(namespace, "bundle"),
        expected_bundle_digest=_optional_text(namespace, "expected_digest"),
    )
    sys.stdout.buffer.write(analysis.reduction.measurements_bytes + b"\n")
    return 0


def _command_summarize(namespace: argparse.Namespace) -> int:
    analysis = recalculate_bundle(
        _path(namespace, "bundle"),
        expected_bundle_digest=_optional_text(namespace, "expected_digest"),
    )
    descriptor = analysis.verification.descriptor
    measurements = analysis.reduction.measurements
    _json_output(
        {
            "bundle_digest": analysis.verification.bundle_digest,
            "environment_completeness": descriptor.environment_completeness.value,
            "evidence_eligibility": descriptor.evidence_eligibility.value,
            "measurements": [
                {
                    "aggregation": item.aggregation.value,
                    "metric": item.metric.value,
                    "sample_count": item.sample_count,
                    "unit": item.unit.value,
                    "value": item.value,
                }
                for item in measurements.measurements
            ],
            "run_id": analysis.verification.run_id,
            "unavailable": [item.metric.value for item in measurements.unavailable],
        }
    )
    return 0


def _command_trial_set_create(namespace: argparse.Namespace) -> int:
    from inferdrome.trials import create_trial_set

    verified = create_trial_set(
        runs_root=_path(namespace, "runs_root"),
        trial_sets_root=_path(namespace, "trial_sets_root"),
        run_ids=cast(list[str], namespace.member_runs),
        title=cast(str, namespace.title),
        hypothesis=_optional_text(namespace, "hypothesis"),
        trial_set_id=_optional_text(namespace, "trial_set_id"),
    )
    _json_output(
        {
            "controlled_comparison_authority_issues": list(
                verified.comparison_authority.issues
            ),
            "controlled_comparison_scope": (
                verified.comparison_authority.scope
            ),
            "member_count": len(verified.descriptor.members),
            "path": str(verified.path),
            "trial_set_digest": verified.trial_set_digest,
            "trial_set_id": verified.descriptor.trial_set_id,
            "valid": True,
        }
    )
    return 0


def _command_trial_set_verify(namespace: argparse.Namespace) -> int:
    from inferdrome.trials import verify_trial_set

    verified = verify_trial_set(
        _path(namespace, "trial_set"),
        runs_root=_path(namespace, "runs_root"),
        expected_trial_set_digest=_optional_text(namespace, "expected_digest"),
    )
    _json_output(
        {
            "controlled_comparison_authority_issues": list(
                verified.comparison_authority.issues
            ),
            "controlled_comparison_scope": (
                verified.comparison_authority.scope
            ),
            "execution_fingerprint": (verified.descriptor.execution_fingerprint),
            "member_count": len(verified.members),
            "trial_set_digest": verified.trial_set_digest,
            "trial_set_id": verified.descriptor.trial_set_id,
            "valid": True,
        }
    )
    return 0


def _command_trial_set_summarize(namespace: argparse.Namespace) -> int:
    from inferdrome.trials import trial_metric_variations, verify_trial_set

    verified = verify_trial_set(
        _path(namespace, "trial_set"),
        runs_root=_path(namespace, "runs_root"),
        expected_trial_set_digest=_optional_text(namespace, "expected_digest"),
    )
    variations = trial_metric_variations(verified)
    _json_output(
        {
            "controlled_comparison_authority_issues": list(
                verified.comparison_authority.issues
            ),
            "controlled_comparison_scope": (
                verified.comparison_authority.scope
            ),
            "inference": "DESCRIPTIVE_ONLY",
            "member_count": len(verified.members),
            "request_population_policy": "separate_per_run_v1",
            "summary_method": "per_run_scalar_sample_variation_v1",
            "trial_set_digest": verified.trial_set_digest,
            "trial_set_id": verified.descriptor.trial_set_id,
            "variations": [
                {
                    "aggregation": item.aggregation,
                    "available_run_count": item.available_run_count,
                    "maximum": item.maximum,
                    "mean": item.mean,
                    "median": item.median,
                    "metric": item.metric,
                    "minimum": item.minimum,
                    "sample_standard_deviation": (item.sample_standard_deviation),
                    "span": item.span,
                    "unit": item.unit,
                    "values": [
                        {
                            "run_id": point.run_id,
                            "sample_count": point.sample_count,
                            "value": point.value,
                        }
                        for point in item.values
                    ],
                }
                for item in variations
            ],
            "weighting": "EQUAL_PER_RUN",
        }
    )
    return 0


def _command_comparison_plan_create(namespace: argparse.Namespace) -> int:
    from inferdrome.comparisons import create_comparison_plan

    baseline = resolve_experiment(
        _path(namespace, "baseline_source"),
        run_id="run-11111111111111111111111111111111",
    )
    candidate = resolve_experiment(
        _path(namespace, "candidate_source"),
        run_id="run-22222222222222222222222222222222",
    )
    baseline_traffic = baseline.resolved_spec.traffic
    candidate_traffic = candidate.resolved_spec.traffic
    if not isinstance(baseline_traffic, ConcurrentTraffic) or not isinstance(
        candidate_traffic,
        ConcurrentTraffic,
    ):
        raise AdapterError(
            "controlled-comparison v1 requires concurrent traffic in both sources"
        )
    verified = create_comparison_plan(
        runs_root=_path(namespace, "runs_root"),
        comparison_plans_root=_path(namespace, "comparison_plans_root"),
        experiment_id=baseline.resolved_spec.experiment.id,
        title=cast(str, namespace.title),
        hypothesis=cast(str, namespace.hypothesis),
        planned_repetitions_per_arm=cast(int, namespace.repetitions),
        independent_variable=ConcurrencyIndependentVariable(
            value_type="integer",
            path="traffic.concurrency",
            baseline_value=baseline_traffic.concurrency,
            candidate_value=candidate_traffic.concurrency,
        ),
        primary_outcome=cast(OutcomeSelector, namespace.primary_outcome),
        baseline_resolved_experiment=baseline.resolved_spec,
        baseline_source_spec_digest=baseline.source_spec_digest,
        baseline_execution_fingerprint=baseline.execution_fingerprint,
        candidate_resolved_experiment=candidate.resolved_spec,
        candidate_source_spec_digest=candidate.source_spec_digest,
        candidate_execution_fingerprint=candidate.execution_fingerprint,
        comparison_plan_id=_optional_text(namespace, "comparison_plan_id"),
        baseline_trial_set_id=_optional_text(namespace, "baseline_trial_set_id"),
        candidate_trial_set_id=_optional_text(namespace, "candidate_trial_set_id"),
        schedule_seed=_optional_text(namespace, "schedule_seed"),
    )
    descriptor = verified.descriptor
    _json_output(
        {
            "arms": {
                "baseline": {
                    "concurrency": descriptor.independent_variable.baseline_value,
                    "planned_trial_set_id": (
                        descriptor.baseline_arm.planned_trial_set_id
                    ),
                    "run_ids": descriptor.baseline_arm.run_ids,
                    "source": str(_path(namespace, "baseline_source")),
                },
                "candidate": {
                    "concurrency": descriptor.independent_variable.candidate_value,
                    "planned_trial_set_id": (
                        descriptor.candidate_arm.planned_trial_set_id
                    ),
                    "run_ids": descriptor.candidate_arm.run_ids,
                    "source": str(_path(namespace, "candidate_source")),
                },
            },
            "comparison_plan_digest": verified.comparison_plan_digest,
            "comparison_plan_id": descriptor.comparison_plan_id,
            "path": str(verified.path),
            "predeclaration_assurance": descriptor.predeclaration_assurance,
            "schedule": [
                {
                    "arm": slot.arm.value,
                    "block_index": slot.block_index,
                    "run_id": slot.run_id,
                    "sequence_index": slot.sequence_index,
                }
                for slot in descriptor.ordered_schedule
            ],
            "valid": True,
        }
    )
    return 0


def _command_comparison_plan_verify(namespace: argparse.Namespace) -> int:
    from inferdrome.comparisons import verify_comparison_plan

    verified = verify_comparison_plan(
        _path(namespace, "comparison_plan"),
        expected_comparison_plan_digest=_optional_text(
            namespace,
            "expected_digest",
        ),
    )
    descriptor = verified.descriptor
    _json_output(
        {
            "comparison_plan_digest": verified.comparison_plan_digest,
            "comparison_plan_id": descriptor.comparison_plan_id,
            "planned_repetitions_per_arm": (descriptor.planned_repetitions_per_arm),
            "predeclaration_assurance": descriptor.predeclaration_assurance,
            "primary_outcome": descriptor.primary_outcome.model_dump(mode="json"),
            "valid": True,
        }
    )
    return 0


def _command_comparison_plan_execute(namespace: argparse.Namespace) -> int:
    from inferdrome.comparisons import execute_comparison_plan

    cancellation = CancellationToken()
    with _signal_cancellation(cancellation):
        executed = execute_comparison_plan(
            _path(namespace, "comparison_plan"),
            expected_comparison_plan_digest=cast(
                str,
                namespace.expected_digest,
            ),
            baseline_source=_path(namespace, "baseline_source"),
            candidate_source=_path(namespace, "candidate_source"),
            runs_root=_path(namespace, "runs_root"),
            trial_sets_root=_path(namespace, "trial_sets_root"),
            comparison_results_root=_path(namespace, "comparison_results_root"),
            tokenizer_path=_optional_path(namespace, "tokenizer_path"),
            managed_vllm=_managed_vllm_config(namespace),
            cancellation=cancellation,
        )
    value = _comparison_result_json(executed.result)
    value.update(
        {
            "baseline_trial_set_digest": (
                executed.baseline_trial_set.trial_set_digest
            ),
            "baseline_trial_set_id": (
                executed.baseline_trial_set.descriptor.trial_set_id
            ),
            "candidate_trial_set_digest": (
                executed.candidate_trial_set.trial_set_digest
            ),
            "candidate_trial_set_id": (
                executed.candidate_trial_set.descriptor.trial_set_id
            ),
            "comparison_plan_digest": executed.plan.comparison_plan_digest,
            "executed_run_ids": executed.executed_run_ids,
            "planned_run_count": len(
                executed.plan.descriptor.ordered_schedule
            ),
            "reused_run_ids": executed.reused_run_ids,
        }
    )
    _json_output(value)
    return 0


def _comparison_result_json(verified: object) -> dict[str, object]:
    from inferdrome.comparisons import VerifiedComparisonResult

    if not isinstance(verified, VerifiedComparisonResult):
        raise TypeError("comparison result has an unexpected type")
    descriptor = verified.descriptor
    return {
        "comparison_plan_id": descriptor.comparison_plan_id,
        "comparison_result_digest": verified.comparison_result_digest,
        "comparison_result_id": descriptor.comparison_result_id,
        "environment_control_scope": descriptor.environment_control_scope,
        "inference_scope": descriptor.inference_scope,
        "outcomes": [
            outcome.model_dump(mode="json", by_alias=True, exclude_none=False)
            for outcome in descriptor.outcomes
        ],
        "path": str(verified.path),
        "predeclaration_assurance": descriptor.predeclaration_assurance,
        "status": descriptor.status.value,
        "unsatisfied_controls": [
            control.value for control in descriptor.unsatisfied_controls
        ],
        "valid": True,
    }


def _command_comparison_result_create(namespace: argparse.Namespace) -> int:
    from inferdrome.comparisons import create_comparison_result

    verified = create_comparison_result(
        runs_root=_path(namespace, "runs_root"),
        trial_sets_root=_path(namespace, "trial_sets_root"),
        comparison_plans_root=_path(namespace, "comparison_plans_root"),
        comparison_results_root=_path(namespace, "comparison_results_root"),
        comparison_plan_id=cast(str, namespace.comparison_plan_id),
        expected_comparison_plan_digest=cast(
            str,
            namespace.expected_plan_digest,
        ),
        baseline_trial_set_id=cast(str, namespace.baseline_trial_set_id),
        expected_baseline_trial_set_digest=cast(
            str,
            namespace.expected_baseline_digest,
        ),
        candidate_trial_set_id=cast(str, namespace.candidate_trial_set_id),
        expected_candidate_trial_set_digest=cast(
            str,
            namespace.expected_candidate_digest,
        ),
        comparison_result_id=_optional_text(namespace, "comparison_result_id"),
    )
    _json_output(_comparison_result_json(verified))
    return 0


def _command_comparison_result_verify(namespace: argparse.Namespace) -> int:
    from inferdrome.comparisons import verify_comparison_result

    verified = verify_comparison_result(
        _path(namespace, "comparison_result"),
        runs_root=_path(namespace, "runs_root"),
        trial_sets_root=_path(namespace, "trial_sets_root"),
        comparison_plans_root=_path(namespace, "comparison_plans_root"),
        expected_comparison_result_digest=_optional_text(
            namespace,
            "expected_digest",
        ),
    )
    _json_output(_comparison_result_json(verified))
    return 0


def _command_dashboard(namespace: argparse.Namespace) -> int:
    from inferdrome.dashboard.server import run_dashboard

    run_dashboard(
        _path(namespace, "runs_root"),
        trial_sets_root=_path(namespace, "trial_sets_root"),
        comparison_plans_root=_path(namespace, "comparison_plans_root"),
        comparison_results_root=_path(namespace, "comparison_results_root"),
        port=cast(int, namespace.port),
        open_browser=cast(bool, namespace.open_browser),
        keyring_path=_optional_path(namespace, "keyring"),
    )
    return 0


def _keyring_path(namespace: argparse.Namespace) -> Path:
    return _path(namespace, "keyring")


def _command_dashboard_keyring_create(namespace: argparse.Namespace) -> int:
    from inferdrome.dashboard.auth import DashboardKeyringStore

    token, record = DashboardKeyringStore(_keyring_path(namespace)).create(
        cast(str, namespace.label)
    )
    _json_output(
        {
            "created_at": record.created_at,
            "key_id": record.key_id,
            "label": record.label,
            "scope": record.scope,
            "status": record.status,
            "token": token,
        }
    )
    return 0


def _command_dashboard_keyring_list(namespace: argparse.Namespace) -> int:
    from inferdrome.dashboard.auth import DashboardKeyringStore

    _json_output(
        {
            "keys": DashboardKeyringStore(_keyring_path(namespace)).list_public()
        }
    )
    return 0


def _command_dashboard_keyring_revoke(namespace: argparse.Namespace) -> int:
    from inferdrome.dashboard.auth import DashboardKeyringStore

    record = DashboardKeyringStore(_keyring_path(namespace)).revoke(
        cast(str, namespace.key_id)
    )
    _json_output(
        {
            "key_id": record.key_id,
            "status": "revoked",
            "revoked": True,
        }
    )
    return 0


def _command_dashboard_keyring_rotate(namespace: argparse.Namespace) -> int:
    from inferdrome.dashboard.auth import DashboardKeyringStore

    token, record = DashboardKeyringStore(_keyring_path(namespace)).rotate(
        cast(str, namespace.revoke_key_id),
        cast(str, namespace.label),
    )
    _json_output(
        {
            "created_at": record.created_at,
            "key_id": record.key_id,
            "label": record.label,
            "revoked_key_id": namespace.revoke_key_id,
            "scope": record.scope,
            "status": record.status,
            "token": token,
        }
    )
    return 0


def _add_bundle_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bundle", help="sealed Inferdrome bundle directory")
    parser.add_argument(
        "--expected-digest",
        help="externally retained bundle digest to require",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferdrome",
        description="Reproducible evidence pipeline for LLM-serving experiments",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate without executing")
    _add_resolution_options(validate)
    validate.set_defaults(handler=_command_validate)

    resolve = commands.add_parser("resolve", help="resolve defaults and hashes")
    _add_resolution_options(resolve)
    resolve.add_argument("--run-id", help="explicit run-<32 hex> identity")
    resolve.set_defaults(handler=_command_resolve)

    run = commands.add_parser("run", help="execute and seal one evidence bundle")
    _add_resolution_options(run)
    run.add_argument("--runs-root", default="runs", help="run workspace root")
    run.add_argument("--run-id", help="explicit run-<32 hex> identity")
    run.add_argument(
        "--tokenizer-path",
        help="local tokenizer directory required by attached vLLM execution",
    )
    run.add_argument(
        "--managed-local-vllm",
        action="store_true",
        help="launch and capture proof for one local pinned-vLLM NVIDIA server",
    )
    run.add_argument(
        "--managed-model-path",
        help="absolute local model snapshot used by managed vLLM",
    )
    run.add_argument(
        "--managed-gpu-index",
        type=_gpu_index,
        help="physical NVIDIA GPU index (default: 0)",
    )
    run.add_argument(
        "--managed-startup-timeout-seconds",
        type=float,
        help="bounded model-load and server-readiness timeout (default: 900)",
    )
    run.add_argument(
        "--managed-capability-profile",
        choices=(QWEN3_8B_PROFILE_ID,),
        help="opt into one exact operational model/workload profile",
    )
    run.add_argument(
        "--expected-exitspec-contract-digest",
        help=(
            "require the resolved experiment to carry this exact external "
            "ExitSpec contract digest before execution"
        ),
    )
    run.set_defaults(handler=_command_run)

    inspect = commands.add_parser("inspect", help="inspect one run workspace")
    inspect.add_argument("run", help="run ID or workspace path")
    inspect.add_argument("--runs-root", default="runs", help="run workspace root")
    inspect.set_defaults(handler=_command_inspect)

    bundle = commands.add_parser("bundle", help="bundle operations")
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_verify = bundle_commands.add_parser(
        "verify", help="verify structure, hashes, and semantic consistency"
    )
    _add_bundle_input(bundle_verify)
    bundle_verify.add_argument(
        "--require-customer-eligible",
        action="store_true",
        help="reject valid bundles not eligible for customer-evidence flows",
    )
    bundle_verify.set_defaults(handler=_command_bundle_verify)

    reduce_parser = commands.add_parser(
        "reduce", help="independently recompute measurements"
    )
    _add_bundle_input(reduce_parser)
    reduce_parser.set_defaults(handler=_command_reduce)

    summarize = commands.add_parser(
        "summarize", help="show a deterministic evidence summary"
    )
    _add_bundle_input(summarize)
    summarize.set_defaults(handler=_command_summarize)

    trial_set = commands.add_parser(
        "trial-set",
        help="create and verify descriptive repeated-trial groupings",
    )
    trial_set_commands = trial_set.add_subparsers(
        dest="trial_set_command",
        required=True,
    )
    trial_set_create = trial_set_commands.add_parser(
        "create",
        help="group same-configuration completed runs",
    )
    trial_set_create.add_argument(
        "--run",
        dest="member_runs",
        action="append",
        required=True,
        help="member run ID; repeat for each repetition",
    )
    trial_set_create.add_argument("--title", required=True)
    trial_set_create.add_argument("--hypothesis")
    trial_set_create.add_argument("--trial-set-id")
    trial_set_create.add_argument(
        "--runs-root",
        default="runs",
        help="run workspace root",
    )
    trial_set_create.add_argument(
        "--trial-sets-root",
        default="trial-sets",
        help="trial-set artifact root",
    )
    trial_set_create.set_defaults(handler=_command_trial_set_create)

    for name, help_text, handler in (
        (
            "verify",
            "verify membership and recalculate every run",
            _command_trial_set_verify,
        ),
        (
            "summarize",
            "show descriptive run-level variation",
            _command_trial_set_summarize,
        ),
    ):
        operation = trial_set_commands.add_parser(name, help=help_text)
        operation.add_argument("trial_set", help="immutable trial-set directory")
        operation.add_argument(
            "--runs-root",
            default="runs",
            help="run workspace root",
        )
        operation.add_argument(
            "--expected-digest",
            help="externally retained trial-set digest to require",
        )
        operation.set_defaults(handler=handler)

    comparison_plan = commands.add_parser(
        "comparison-plan",
        help="create and verify operator-attested controlled-comparison designs",
    )
    comparison_plan_commands = comparison_plan.add_subparsers(
        dest="comparison_plan_command",
        required=True,
    )
    comparison_plan_create = comparison_plan_commands.add_parser(
        "create",
        help="freeze two concurrent-traffic arms before execution",
    )
    comparison_plan_create.add_argument("--baseline-source", required=True)
    comparison_plan_create.add_argument("--candidate-source", required=True)
    comparison_plan_create.add_argument("--title", required=True)
    comparison_plan_create.add_argument("--hypothesis", required=True)
    comparison_plan_create.add_argument(
        "--repetitions",
        required=True,
        type=_comparison_repetitions,
        help="planned run pairs (2-100 per arm)",
    )
    comparison_plan_create.add_argument(
        "--primary-outcome",
        required=True,
        type=_outcome_selector,
        help="frozen METRIC:AGGREGATION selector",
    )
    comparison_plan_create.add_argument("--comparison-plan-id")
    comparison_plan_create.add_argument("--baseline-trial-set-id")
    comparison_plan_create.add_argument("--candidate-trial-set-id")
    comparison_plan_create.add_argument(
        "--schedule-seed",
        help="optional 64-character lowercase hex seed",
    )
    comparison_plan_create.add_argument(
        "--runs-root",
        default="runs",
        help="future run workspace root",
    )
    comparison_plan_create.add_argument(
        "--comparison-plans-root",
        default="comparison-plans",
        help="comparison-plan artifact root",
    )
    comparison_plan_create.set_defaults(handler=_command_comparison_plan_create)

    comparison_plan_verify = comparison_plan_commands.add_parser(
        "verify",
        help="verify immutable design bytes and an optional retained digest",
    )
    comparison_plan_verify.add_argument(
        "comparison_plan",
        help="immutable comparison-plan directory",
    )
    comparison_plan_verify.add_argument(
        "--expected-digest",
        help="externally retained comparison-plan digest to require",
    )
    comparison_plan_verify.set_defaults(handler=_command_comparison_plan_verify)

    comparison_plan_execute = comparison_plan_commands.add_parser(
        "execute",
        help="run the exact frozen schedule and finalize its evidence",
    )
    comparison_plan_execute.add_argument(
        "comparison_plan",
        help="immutable comparison-plan directory",
    )
    comparison_plan_execute.add_argument(
        "--expected-digest",
        required=True,
        help="externally retained comparison-plan digest to require",
    )
    comparison_plan_execute.add_argument("--baseline-source", required=True)
    comparison_plan_execute.add_argument("--candidate-source", required=True)
    comparison_plan_execute.add_argument(
        "--runs-root",
        default="runs",
        help="run workspace root",
    )
    comparison_plan_execute.add_argument(
        "--trial-sets-root",
        default="trial-sets",
        help="trial-set artifact root",
    )
    comparison_plan_execute.add_argument(
        "--comparison-results-root",
        default="comparison-results",
        help="comparison-result artifact root",
    )
    comparison_plan_execute.add_argument(
        "--tokenizer-path",
        help="local tokenizer directory required by attached vLLM execution",
    )
    comparison_plan_execute.add_argument(
        "--managed-local-vllm",
        action="store_true",
        help="launch one pinned local-vLLM server for each planned run",
    )
    comparison_plan_execute.add_argument(
        "--managed-model-path",
        help="absolute local model snapshot used by managed vLLM",
    )
    comparison_plan_execute.add_argument(
        "--managed-gpu-index",
        type=_gpu_index,
        help="physical NVIDIA GPU index (default: 0)",
    )
    comparison_plan_execute.add_argument(
        "--managed-startup-timeout-seconds",
        type=float,
        help="bounded model-load and server-readiness timeout (default: 900)",
    )
    comparison_plan_execute.add_argument(
        "--managed-capability-profile",
        choices=(QWEN3_8B_PROFILE_ID,),
        help="opt into one exact operational model/workload profile",
    )
    comparison_plan_execute.set_defaults(
        handler=_command_comparison_plan_execute
    )

    comparison_result = commands.add_parser(
        "comparison-result",
        help="evaluate and verify controlled-comparison results",
    )
    comparison_result_commands = comparison_result.add_subparsers(
        dest="comparison_result_command",
        required=True,
    )
    comparison_result_create = comparison_result_commands.add_parser(
        "create",
        help="publish a point estimate or explicit INCOMPARABLE result",
    )
    comparison_result_create.add_argument("--comparison-plan-id", required=True)
    comparison_result_create.add_argument(
        "--expected-plan-digest",
        required=True,
    )
    comparison_result_create.add_argument(
        "--baseline-trial-set-id",
        required=True,
    )
    comparison_result_create.add_argument(
        "--expected-baseline-digest",
        required=True,
    )
    comparison_result_create.add_argument(
        "--candidate-trial-set-id",
        required=True,
    )
    comparison_result_create.add_argument(
        "--expected-candidate-digest",
        required=True,
    )
    comparison_result_create.add_argument("--comparison-result-id")
    comparison_result_create.add_argument("--runs-root", default="runs")
    comparison_result_create.add_argument(
        "--trial-sets-root",
        default="trial-sets",
    )
    comparison_result_create.add_argument(
        "--comparison-plans-root",
        default="comparison-plans",
    )
    comparison_result_create.add_argument(
        "--comparison-results-root",
        default="comparison-results",
    )
    comparison_result_create.set_defaults(handler=_command_comparison_result_create)

    comparison_result_verify = comparison_result_commands.add_parser(
        "verify",
        help="recalculate a stored result from every referenced artifact",
    )
    comparison_result_verify.add_argument(
        "comparison_result",
        help="immutable comparison-result directory",
    )
    comparison_result_verify.add_argument("--runs-root", default="runs")
    comparison_result_verify.add_argument(
        "--trial-sets-root",
        default="trial-sets",
    )
    comparison_result_verify.add_argument(
        "--comparison-plans-root",
        default="comparison-plans",
    )
    comparison_result_verify.add_argument(
        "--expected-digest",
        help="externally retained comparison-result digest to require",
    )
    comparison_result_verify.set_defaults(handler=_command_comparison_result_verify)

    dashboard = commands.add_parser(
        "dashboard",
        help="serve the local read-only evidence dashboard",
    )
    dashboard.add_argument("--runs-root", default="runs", help="run workspace root")
    dashboard.add_argument(
        "--trial-sets-root",
        default="trial-sets",
        help="trial-set artifact root",
    )
    dashboard.add_argument(
        "--comparison-plans-root",
        default="comparison-plans",
        help="controlled-comparison plan root",
    )
    dashboard.add_argument(
        "--comparison-results-root",
        default="comparison-results",
        help="controlled-comparison result root",
    )
    dashboard.add_argument(
        "--port",
        type=_port,
        default=8787,
        help="loopback port (default: 8787)",
    )
    dashboard.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="open the dashboard in the default browser",
    )
    dashboard.add_argument(
        "--keyring",
        help="opt into local bearer auth using this 0600 keyring file",
    )
    dashboard.set_defaults(handler=_command_dashboard)

    keyring = commands.add_parser(
        "dashboard-keyring",
        help="manage local dashboard bearer keys without an HTTP endpoint",
    )
    keyring_commands = keyring.add_subparsers(
        dest="dashboard_keyring_command",
        required=True,
    )
    keyring_create = keyring_commands.add_parser(
        "create", help="create a read-only dashboard key"
    )
    keyring_create.add_argument("--keyring", required=True)
    keyring_create.add_argument("--label", default="local-dashboard")
    keyring_create.set_defaults(handler=_command_dashboard_keyring_create)

    keyring_list = keyring_commands.add_parser(
        "list", help="list public dashboard key metadata"
    )
    keyring_list.add_argument("--keyring", required=True)
    keyring_list.set_defaults(handler=_command_dashboard_keyring_list)

    keyring_revoke = keyring_commands.add_parser(
        "revoke", help="revoke one dashboard key id"
    )
    keyring_revoke.add_argument("--keyring", required=True)
    keyring_revoke.add_argument("key_id")
    keyring_revoke.set_defaults(handler=_command_dashboard_keyring_revoke)

    keyring_rotate = keyring_commands.add_parser(
        "rotate", help="atomically revoke one key and create its replacement"
    )
    keyring_rotate.add_argument("--keyring", required=True)
    keyring_rotate.add_argument("--revoke-key-id", required=True)
    keyring_rotate.add_argument("--label", default="local-dashboard")
    keyring_rotate.set_defaults(handler=_command_dashboard_keyring_rotate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    handler = cast(_Command, namespace.handler)
    try:
        return handler(namespace)
    except CancellationRequested as error:
        print(f"inferdrome: interrupted: {error}", file=sys.stderr)
        return 130
    except InferdromeError as error:
        print(f"inferdrome: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("inferdrome: interrupted", file=sys.stderr)
        return 130


def entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
