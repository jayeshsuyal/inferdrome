"""Namespaced command line for the additive PR-B real-endpoint bridge."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from inferdrome.qwen3_campaign import qwen3_workload_prompts, qwen3_workload_sha256
from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    canonical_jsonl_bytes,
    sha256_digest,
)
from inferdrome.routing_execution.contracts import (
    RoutingExecutionConfig,
    fixed_r1_input_digests,
    fixed_selected_workload_sha256,
)
from inferdrome.routing_execution.executor import (
    ExecutionError,
    ManualMonotonicClock,
    SealedRoutingExecution,
    declared_input_transfer_digest,
    load_config,
    load_config_bytes,
    run_execution,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.loopback import LoopbackPair
from inferdrome.routing_execution.stdin_bundle import (
    MAX_RUNTIME_INPUT_BUNDLE_BYTES,
    RuntimeInputBundle,
)
from inferdrome.routing_execution.transport import (
    EndpointTransport,
    UrllibEndpointTransport,
)
from inferdrome.routing_execution.tunnel import (
    IapTunnelMappedTransport,
    IapTunnelTransportMap,
)
from inferdrome.routing_execution.verifier import verify_execution_package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m inferdrome.routing_execution")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one admitted two-endpoint execution")
    run.add_argument("--deployment-config", type=Path)
    run.add_argument("--workload", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--iap-transport-map",
        type=Path,
        help="ephemeral exact IAP tunnel map; admitted GCP_PRIVATE origins stay sealed",
    )
    run.add_argument(
        "--input-bundle-stdin",
        action="store_true",
        help=(
            "accept the bounded canonical GCP_PRIVATE config, workload, and IAP "
            "map only through standard input"
        ),
    )
    verify = commands.add_parser(
        "verify", help="offline verify a sealed execution package"
    )
    verify.add_argument("package", type=Path)
    verify.add_argument("--expected-digest")
    inspect = commands.add_parser(
        "inspect", help="show a bounded verified package summary"
    )
    inspect.add_argument("package", type=Path)
    demo = commands.add_parser(
        "demo", help="run the actual two-socket local loopback demo"
    )
    demo.add_argument("--output", type=Path, default=Path("routing-execution-package"))
    demo.add_argument("--source-commit")
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _source_commit(value: str | None) -> str:
    if value is not None:
        return value
    try:
        root = Path(__file__).resolve().parents[3]
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        result = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ExecutionError("demo source commit is unavailable") from None
    if len(result) != 40 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ExecutionError("demo source commit is invalid")
    return result


def _demo_config(
    *, origin_a: str, origin_b: str, selected_sha256: str, source_commit: str
) -> dict[str, object]:
    image = "local/inferdrome-vllm@sha256:" + ("1" * 64)
    runner = "local/inferdrome-runner@sha256:" + ("2" * 64)
    model = {
        "model_id": "Qwen/Qwen3-8B",
        "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "tokenizer_revision": "b968826d9c46dd6066d109eabc6255188de91218",
    }
    runtime = {
        "runtime_name": "vllm",
        "runtime_version": "0.26.0",
        "adapter_id": "openai-compatible-routing-execution-v1",
        "adapter_version": "1.0.0",
    }
    capabilities = {
        "health": "HTTP_HEALTH_V1",
        "load": "VLLM_PROMETHEUS_V1",
        "gpu_dcgm": "UNAVAILABLE",
        "kv_cache": "UNAVAILABLE",
    }
    workload_sha = qwen3_workload_sha256()
    r1_digests = fixed_r1_input_digests()
    endpoints = [
        {
            "endpoint_id": endpoint_id,
            "origin": origin,
            "model": model,
            "runtime": runtime,
            "serving_image": {"reference": image},
            "workload_sha256": workload_sha,
            "capabilities": capabilities,
        }
        for endpoint_id, origin in (("endpoint-a", origin_a), ("endpoint-b", origin_b))
    ]
    config: dict[str, object] = {
        "schema_version": "inferdrome.routing-execution-config.v1",
        "execution_id": "routing-execution-v1",
        "mode": "LOCAL_LOOPBACK",
        "source_commit": source_commit,
        "runner_image": {"reference": runner},
        "serving_image": {"reference": image},
        "model": model,
        "runtime": runtime,
        "routing_inputs": {
            "campaign_id": "routing-campaign-v1",
            "plan_sha256": r1_digests["plan"],
            "trace_sha256": r1_digests["trace"],
            "fault_schedule_sha256": r1_digests["fault"],
            "trial_plan_sha256": r1_digests["trial"],
            "policies": [
                "fail_closed_required_load_v1",
                "explicit_fail_open_stale_load_v1",
                "typed_admissible_state_only_v1",
            ],
        },
        "workload": {
            "workload_id": "inferdrome.qwen-text-mixed-length.v1",
            "workload_sha256": workload_sha,
            "selected_workload_sha256": selected_sha256,
            "selected_request_ids": [f"request-{index:03d}" for index in range(6)],
            "request_denominator": 6,
        },
        "endpoints": endpoints,
        "telemetry": {
            "clock_domain": "RUNNER_MONOTONIC_NS",
            "health_freshness_ms": 5,
            "load_freshness_ms": 5,
            "gpu_freshness_ms": 5,
            "load_metric_name": "vllm:num_requests_running",
        },
        "fault": {
            "fault_id": "stale-load-fresh-health-v1",
            "load_collection_pause_after_sequence_index": 1,
            "health_collection_continues": True,
            "inter_request_interval_ms": 10,
        },
        "topology": {
            "runner_separate_from_serving": True,
            "serving_engine_count": 2,
            "one_engine_per_endpoint": True,
            "accelerator_model": "NONE_LOCAL",
            "accelerator_count": 0,
        },
        "evidence_destination": {
            "destination_sha256": sha256_digest(b"routing-execution-local-evidence-v1"),
            "declared_input_transfer_sha256": "sha256:" + ("0" * 64),
            "publication_mode": "LOCAL_CREATE_NO_REPLACE_V1",
        },
        "request_timeout_ms": 1000,
        "no_retry": True,
    }
    if selected_sha256 != fixed_selected_workload_sha256():
        raise ExecutionError("demo workload selection is not the fixed Qwen3 trace")
    parsed = RoutingExecutionConfig.model_validate_json(canonical_json_bytes(config))
    destination = config["evidence_destination"]
    assert isinstance(destination, dict)
    destination["declared_input_transfer_sha256"] = declared_input_transfer_digest(
        parsed
    )
    return config


def _demo(output: Path, source_commit: str | None) -> None:
    parent = output.absolute().parent
    if not parent.exists():
        raise ExecutionError("demo evidence parent must already exist")
    prompts = qwen3_workload_prompts()[:6]
    workload_bytes = canonical_jsonl_bytes({"prompt": prompt} for prompt in prompts)
    with (
        LoopbackPair() as endpoints,
        tempfile.TemporaryDirectory(
            prefix=".routing-execution-demo-", dir=parent
        ) as temporary,
    ):
        root = Path(temporary)
        workload_path = root / "workload.jsonl"
        config_path = root / "deployment-config.json"
        workload_path.write_bytes(workload_bytes)
        config = _demo_config(
            origin_a=endpoints.endpoint_a.origin,
            origin_b=endpoints.endpoint_b.origin,
            selected_sha256=sha256_digest(workload_bytes),
            source_commit=_source_commit(source_commit),
        )
        config_path.write_bytes(canonical_json_bytes(config))
        sealed = run_execution(
            config_path,
            workload_path,
            output,
            clock=ManualMonotonicClock(),
        )
    _emit({"package_path": str(sealed.path), "retained_digest": sealed.retained_digest})


def _tunnel_transport_factory(
    config: RoutingExecutionConfig, transport_map: IapTunnelTransportMap
) -> Callable[[], EndpointTransport]:
    origins = transport_map.logical_to_tunnel(config)

    def tunnel_transport_factory() -> EndpointTransport:
        return IapTunnelMappedTransport(
            inner=UrllibEndpointTransport(), origins=origins
        )

    return tunnel_transport_factory


def _read_runtime_input_bundle() -> bytes:
    """Read at most one canonical stdin envelope without echoing its values."""

    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise ExecutionError("runtime input bundle is unavailable")
    try:
        content = stream.read(MAX_RUNTIME_INPUT_BUNDLE_BYTES + 1)
    except (OSError, ValueError):
        raise ExecutionError("runtime input bundle is unavailable") from None
    if type(content) is not bytes or len(content) > MAX_RUNTIME_INPUT_BUNDLE_BYTES:
        raise ExecutionError("runtime input bundle is unavailable")
    return content


def _run_from_stdin(arguments: argparse.Namespace) -> SealedRoutingExecution:
    """Execute the GCP runner-only bounded envelope after local validation."""

    if (
        arguments.deployment_config is not None
        or arguments.workload is not None
        or arguments.iap_transport_map is not None
    ):
        raise ExecutionError("runtime input bundle arguments are exclusive")
    bundle = RuntimeInputBundle.decode(_read_runtime_input_bundle())
    config = load_config_bytes(bundle.config_bytes)
    if not isinstance(config, RoutingExecutionConfig) or (
        config.mode != "GCP_PRIVATE" or bundle.iap_transport_map_bytes is None
    ):
        raise ExecutionError("runtime input bundle is not an admitted GCP input")
    transport_map = IapTunnelTransportMap.load_bytes(
        bundle.iap_transport_map_bytes, config=config
    )
    return run_execution_from_bytes(
        bundle.config_bytes,
        bundle.workload_bytes,
        arguments.output,
        transport_factory=_tunnel_transport_factory(config, transport_map),
    )


def _run_from_files(arguments: argparse.Namespace) -> SealedRoutingExecution:
    """Admit local or manual-host files; GCP still requires its stdin boundary."""

    if arguments.deployment_config is None or arguments.workload is None:
        raise ExecutionError("execution config and workload are required")
    config, _ = load_config(arguments.deployment_config)
    if config.mode == "GCP_PRIVATE" or arguments.iap_transport_map is not None:
        raise ExecutionError("GCP_PRIVATE execution requires the runner stdin input")
    return run_execution(
        arguments.deployment_config,
        arguments.workload,
        arguments.output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch namespaced bridge commands without printing sensitive inputs."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            sealed = (
                _run_from_stdin(arguments)
                if arguments.input_bundle_stdin
                else _run_from_files(arguments)
            )
            _emit(
                {
                    "package_path": str(sealed.path),
                    "retained_digest": sealed.retained_digest,
                }
            )
            return 0
        if arguments.command == "verify":
            report = verify_execution_package(
                arguments.package, expected_digest=arguments.expected_digest
            ).report
            _emit({"retained_digest": report.retained_digest, "valid": True})
            return 0
        if arguments.command == "inspect":
            report = verify_execution_package(arguments.package).report
            _emit(
                {
                    "execution_id": report.execution_id,
                    "retained_digest": report.retained_digest,
                    "terminal_population": {
                        trial_id: dict(population)
                        for trial_id, population in report.terminal_population.items()
                    },
                    "verified": True,
                }
            )
            return 0
        if arguments.command == "demo":
            _demo(arguments.output, arguments.source_commit)
            return 0
    except (ExecutionError, OSError, ValueError):
        parser.error("routing execution command rejected")
    raise AssertionError("routing execution command dispatch is incomplete")
