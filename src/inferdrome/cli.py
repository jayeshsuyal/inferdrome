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
from inferdrome.domain.digests import canonical_json_bytes
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


def _managed_vllm_config(
    namespace: argparse.Namespace,
) -> ManagedVllmConfig | None:
    enabled = cast(bool, namespace.managed_local_vllm)
    model_path = _optional_path(namespace, "managed_model_path")
    gpu_index = cast(int | None, namespace.managed_gpu_index)
    startup_timeout = cast(float | None, namespace.managed_startup_timeout_seconds)
    if not enabled:
        if (
            model_path is not None
            or gpu_index is not None
            or startup_timeout is not None
        ):
            raise AdapterError(
                "managed vLLM options require --managed-local-vllm"
            )
        return None
    if model_path is None:
        raise AdapterError(
            "--managed-local-vllm requires --managed-model-path"
        )
    return ManagedVllmConfig(
        model_path=model_path.absolute(),
        gpu_indices=(gpu_index if gpu_index is not None else 0,),
        startup_timeout_seconds=(
            startup_timeout if startup_timeout is not None else 900.0
        ),
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


def _command_dashboard(namespace: argparse.Namespace) -> int:
    from inferdrome.dashboard.server import run_dashboard

    run_dashboard(
        _path(namespace, "runs_root"),
        port=cast(int, namespace.port),
        open_browser=cast(bool, namespace.open_browser),
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

    dashboard = commands.add_parser(
        "dashboard",
        help="serve the local read-only evidence dashboard",
    )
    dashboard.add_argument("--runs-root", default="runs", help="run workspace root")
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
    dashboard.set_defaults(handler=_command_dashboard)
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
