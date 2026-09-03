"""CPU-only, co-located runner adapter for the bounded GCP private campaign.

The adapter is intentionally small.  It is not an inference server, provider
client, router, or general artifact API.  It accepts one canonical bounded
handoff through the VM's approval-bound IAP path, samples the two engine
containers over their shared internal Docker network, seals the existing PR-B
package on the VM's boot disk, and exposes that sealed package for one bounded
IAP retrieval.  It never receives a GPU, Docker socket, cloud credential, or
provider mutation capability.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import stat
import tarfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GCP_PRIVATE_CAMPAIGN_RUNNER_ATTESTATION_SCHEMA_VERSION,
    GcpPrivateCampaignHandoffReceipt,
    GcpPrivateCampaignRunnerAttestation,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.errors import VerificationError
from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.contracts import ImageIdentity, RoutingExecutionConfig
from inferdrome.routing_execution.executor import (
    ExecutionError,
    SealedRoutingExecution,
    load_config_bytes,
    run_execution_from_bytes,
)
from inferdrome.routing_execution.package import verify_execution_package
from inferdrome.routing_execution.stdin_bundle import (
    MAX_RUNTIME_INPUT_BUNDLE_BYTES,
    RuntimeInputBundle,
    RuntimeInputBundleError,
)
from inferdrome.routing_execution.topology import TopologyAdmissionError, admit_topology
from inferdrome.routing_execution.transport import (
    TransportError,
    TransportResponse,
    UrllibEndpointTransport,
)

_ATTESTATION_PATH: Final = "/inferdrome/v2/runner/attestation"
_HANDOFF_PATH: Final = "/inferdrome/v2/runner/handoff"
_EVIDENCE_PATH: Final = "/inferdrome/v2/runner/evidence"
_HEALTH_PATH: Final = "/health"
_MAX_ARCHIVE_BYTES: Final = 33_554_432
_MAX_ARTIFACT_BYTES: Final = 8_388_608
_PACKAGE_FILES: Final = (
    "executed-manifest.json",
    "input-transfer-receipt.json",
    "integrity-manifest.json",
    "producer-receipt.json",
)
_PROPOSAL_RE: Final = re.compile(r"^sha256:[a-f0-9]{64}$")
_IMAGE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,255}@sha256:[a-f0-9]{64}$")


class RunnerAdapterError(ValueError):
    """Sanitized runner startup, handoff, or retrieval rejection."""


@dataclass(frozen=True)
class RunnerAdapterArguments:
    container_name: str
    private_port: int
    runner_image_reference: str
    runner_command_sha256: str
    adapter_source_sha256: str
    startup_payload_digest: str
    proposal_id: str
    evidence_root: Path
    endpoint_a_local_origin: str
    endpoint_b_local_origin: str

    @property
    def package_name(self) -> str:
        return f"routing-execution-{self.proposal_id[7:39]}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inferdrome-gcp-private-runner-adapter")
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--private-port", type=int, required=True)
    parser.add_argument("--runner-image-reference", required=True)
    parser.add_argument("--runner-command-sha256", required=True)
    parser.add_argument("--adapter-source-sha256", required=True)
    parser.add_argument("--startup-payload-digest", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--endpoint-a-local-origin", required=True)
    parser.add_argument("--endpoint-b-local-origin", required=True)
    return parser


def _arguments(argv: Sequence[str] | None) -> RunnerAdapterArguments:
    parsed = _parser().parse_args(argv)
    result = RunnerAdapterArguments(
        container_name=parsed.container_name,
        private_port=parsed.private_port,
        runner_image_reference=parsed.runner_image_reference,
        runner_command_sha256=parsed.runner_command_sha256,
        adapter_source_sha256=parsed.adapter_source_sha256,
        startup_payload_digest=parsed.startup_payload_digest,
        proposal_id=parsed.proposal_id,
        evidence_root=parsed.evidence_root,
        endpoint_a_local_origin=parsed.endpoint_a_local_origin,
        endpoint_b_local_origin=parsed.endpoint_b_local_origin,
    )
    if (
        result.container_name != "inferdrome-runner-observer"
        or result.private_port != 8002
        or not _IMAGE_RE.fullmatch(result.runner_image_reference)
        or not _PROPOSAL_RE.fullmatch(result.runner_command_sha256)
        or not _PROPOSAL_RE.fullmatch(result.adapter_source_sha256)
        or not _PROPOSAL_RE.fullmatch(result.startup_payload_digest)
        or not _PROPOSAL_RE.fullmatch(result.proposal_id)
        or result.evidence_root != Path("/evidence")
        or result.endpoint_a_local_origin != "http://inferdrome-engine-a:8000"
        or result.endpoint_b_local_origin != "http://inferdrome-engine-b:8001"
    ):
        raise RunnerAdapterError("RUNNER_ADAPTER_ARGUMENTS_INVALID")
    return result


def adapter_source_sha256() -> str:
    """Return the installed source identity before accepting any request."""

    try:
        return sha256_digest(Path(__file__).read_bytes())
    except OSError:
        raise RunnerAdapterError("RUNNER_ADAPTER_SOURCE_UNAVAILABLE") from None


def _attestation(arguments: RunnerAdapterArguments) -> bytes:
    value = GcpPrivateCampaignRunnerAttestation(
        schema_version=GCP_PRIVATE_CAMPAIGN_RUNNER_ATTESTATION_SCHEMA_VERSION,
        container_name="inferdrome-runner-observer",
        private_port=8002,
        runner_image=ImageIdentity(reference=arguments.runner_image_reference),
        container_command_module="inferdrome.deployment.gcp_private_runner_adapter",
        adapter_source_sha256=arguments.adapter_source_sha256,
        runner_command_sha256=arguments.runner_command_sha256,
        startup_payload_digest=arguments.startup_payload_digest,
        docker_network="inferdrome-private-campaign",
        gpu_access="NONE",
        cloud_credentials="NONE",
        docker_socket="ABSENT",
        serving_role="OBSERVER_ONLY",
        provider_mutation_authority="NONE",
    )
    return canonical_json_bytes(value.model_dump(mode="json"))


class _CoLocatedTransport:
    """Map only admitted logical targets to the runner's internal network.

    The sealed configuration retains the observed RFC1918 endpoint identity.
    This in-memory map is intentionally absent from evidence and rejects every
    origin other than the two topology-admitted endpoints.
    """

    def __init__(
        self, *, config: RoutingExecutionConfig, arguments: RunnerAdapterArguments
    ) -> None:
        try:
            topology = admit_topology(config)
        except TopologyAdmissionError as error:
            raise RunnerAdapterError("RUNNER_TOPOLOGY_REJECTED") from error
        if config.mode != "GCP_PRIVATE" or tuple(
            endpoint.endpoint_id for endpoint in topology.endpoints
        ) != ("endpoint-a", "endpoint-b"):
            raise RunnerAdapterError("RUNNER_TOPOLOGY_REJECTED")
        self._origins = {
            topology.endpoints[0].canonical_origin: arguments.endpoint_a_local_origin,
            topology.endpoints[1].canonical_origin: arguments.endpoint_b_local_origin,
        }
        if len(self._origins) != 2 or len(set(self._origins.values())) != 2:
            raise RunnerAdapterError("RUNNER_TOPOLOGY_REJECTED")
        self._inner = UrllibEndpointTransport()

    def _origin(self, value: str) -> str:
        try:
            return self._origins[value]
        except KeyError:
            raise TransportError("runner rejected an undeclared endpoint") from None

    def get(self, origin: str, path: str, *, timeout_ms: int) -> TransportResponse:
        return self._inner.get(self._origin(origin), path, timeout_ms=timeout_ms)

    def post_json(
        self, origin: str, path: str, body: bytes, *, timeout_ms: int
    ) -> TransportResponse:
        return self._inner.post_json(
            self._origin(origin), path, body, timeout_ms=timeout_ms
        )

    def close(self) -> None:
        self._inner.close()


def _pre_campaign_freshness_admission(
    config: RoutingExecutionConfig,
    *,
    arguments: RunnerAdapterArguments,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> None:
    """Observe the frozen 5 ms bound at the co-located decision point.

    This is intentionally an admission condition, not a latency promise.  The
    runner makes no request when any health/load observation is unavailable or
    older than the exact five-millisecond bound when the admission decision is
    made.  The PR-B executor repeats the same fail-closed logic per request.
    """

    if (
        config.telemetry.health_freshness_ms != 5
        or config.telemetry.load_freshness_ms != 5
        or config.telemetry.gpu_freshness_ms != 5
    ):
        raise RunnerAdapterError("RUNNER_FRESHNESS_CONTRACT_MISMATCH")
    transport = _CoLocatedTransport(config=config, arguments=arguments)
    observed: list[int] = []
    try:
        topology = admit_topology(config)
        for endpoint in topology.endpoints:
            for path in (_HEALTH_PATH, "/metrics"):
                response = transport.get(endpoint.canonical_origin, path, timeout_ms=5)
                sampled_at = monotonic_ns()
                if response.status != 200:
                    raise RunnerAdapterError("RUNNER_FRESHNESS_UNAVAILABLE")
                observed.append(sampled_at)
        decision_at = monotonic_ns()
    except (TopologyAdmissionError, TransportError):
        raise RunnerAdapterError("RUNNER_FRESHNESS_UNAVAILABLE") from None
    finally:
        transport.close()
    if not observed or any(
        decision_at < sampled or decision_at - sampled > 5_000_000
        for sampled in observed
    ):
        raise RunnerAdapterError("RUNNER_FRESHNESS_STALE")


def _read_package_files(path: Path) -> tuple[dict[str, bytes], str]:
    """Read the fixed immutable inventory only after the offline verifier."""

    try:
        verified = verify_execution_package(path)
    except (VerificationError, ValueError, OSError):
        raise RunnerAdapterError("RUNNER_EVIDENCE_UNAVAILABLE") from None
    output: dict[str, bytes] = {}
    for name in _PACKAGE_FILES:
        try:
            content = (path / name).read_bytes()
        except OSError:
            raise RunnerAdapterError("RUNNER_EVIDENCE_UNAVAILABLE") from None
        if not 1 <= len(content) <= _MAX_ARTIFACT_BYTES:
            raise RunnerAdapterError("RUNNER_EVIDENCE_UNAVAILABLE")
        output[name] = content
    return output, verified.report.retained_digest


def _archive_package(path: Path) -> tuple[bytes, str]:
    files, retained_digest = _read_package_files(path)
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:") as archive:
        for name in _PACKAGE_FILES:
            payload = files[name]
            info = tarfile.TarInfo(name)
            info.mode = 0o400
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    value = content.getvalue()
    if len(value) > _MAX_ARCHIVE_BYTES:
        raise RunnerAdapterError("RUNNER_EVIDENCE_UNAVAILABLE")
    return value, retained_digest


class _RunnerServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(
        self, address: tuple[str, int], arguments: RunnerAdapterArguments
    ) -> None:
        self.arguments = arguments
        self.attestation = _attestation(arguments)
        self.lock = threading.Lock()
        self.handoff_started = False
        self.sealed: SealedRoutingExecution | None = None
        super().__init__(address, _RunnerHandler)

    def _package_path(self) -> Path:
        return self.arguments.evidence_root / self.arguments.package_name

    def _recover_sealed(self) -> SealedRoutingExecution:
        if self.sealed is not None:
            return self.sealed
        verified = verify_execution_package(self._package_path())
        self.sealed = SealedRoutingExecution(
            path=self._package_path(), retained_digest=verified.report.retained_digest
        )
        return self.sealed

    def handoff(self, content: bytes) -> GcpPrivateCampaignHandoffReceipt:
        with self.lock:
            if self.handoff_started:
                raise RunnerAdapterError("RUNNER_HANDOFF_ALREADY_CONSUMED")
            self.handoff_started = True
            try:
                bundle = RuntimeInputBundle.decode(content)
                if bundle.iap_transport_map_bytes is not None:
                    raise RunnerAdapterError("RUNNER_INPUT_TRANSPORT_INVALID")
                config = load_config_bytes(bundle.config_bytes)
                if (
                    config.mode != "GCP_PRIVATE"
                    or config.runner_image.reference
                    != self.arguments.runner_image_reference
                ):
                    raise RunnerAdapterError("RUNNER_INPUT_BINDING_MISMATCH")
                _pre_campaign_freshness_admission(config, arguments=self.arguments)
                self.sealed = run_execution_from_bytes(
                    bundle.config_bytes,
                    bundle.workload_bytes,
                    self._package_path(),
                    transport_factory=lambda: _CoLocatedTransport(
                        config=config, arguments=self.arguments
                    ),
                )
                verified = verify_execution_package(self.sealed.path)
                endpoints = {
                    endpoint.endpoint_id: endpoint.origin_sha256
                    for endpoint in verified.executed_manifest.endpoints
                }
                if set(endpoints) != {"endpoint-a", "endpoint-b"}:
                    raise RunnerAdapterError("RUNNER_HANDOFF_INVALID")
                return GcpPrivateCampaignHandoffReceipt(
                    proposal_id=self.arguments.proposal_id,
                    routing_config_sha256=verified.executed_manifest.config_sha256,
                    selected_workload_sha256=(
                        verified.executed_manifest.workload.selected_workload_sha256
                    ),
                    endpoint_a_origin_sha256=endpoints["endpoint-a"],
                    endpoint_b_origin_sha256=endpoints["endpoint-b"],
                    routed_after_verified_readiness=True,
                    co_located_freshness_admitted=True,
                    runner_command_sha256=self.arguments.runner_command_sha256,
                )
            except (
                ExecutionError,
                RuntimeInputBundleError,
                VerificationError,
                ValueError,
            ):
                raise RunnerAdapterError("RUNNER_HANDOFF_REJECTED") from None

    def archive(self) -> tuple[bytes, str]:
        with self.lock:
            try:
                sealed = self._recover_sealed()
                return _archive_package(sealed.path)
            except (RunnerAdapterError, ValueError):
                raise
            except Exception:
                raise RunnerAdapterError("RUNNER_EVIDENCE_UNAVAILABLE") from None


class _RunnerHandler(BaseHTTPRequestHandler):
    server: _RunnerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send(
        self,
        status: int,
        content: bytes = b"",
        *,
        content_type: str = "application/json",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if headers is not None:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        if content:
            self.wfile.write(content)

    def _body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else -1
        except ValueError:
            raise RunnerAdapterError("RUNNER_REQUEST_INVALID") from None
        if not 1 <= length <= MAX_RUNTIME_INPUT_BUNDLE_BYTES:
            raise RunnerAdapterError("RUNNER_REQUEST_INVALID")
        body = self.rfile.read(length)
        if len(body) != length:
            raise RunnerAdapterError("RUNNER_REQUEST_INVALID")
        return body

    def do_GET(self) -> None:
        if self.path == _HEALTH_PATH:
            self._send(200, b"{}")
            return
        if self.path == _ATTESTATION_PATH:
            self._send(200, self.server.attestation)
            return
        if self.path != _EVIDENCE_PATH:
            self._send(404)
            return
        try:
            content, retained_digest = self.server.archive()
        except RunnerAdapterError:
            self._send(409)
            return
        self._send(
            200,
            content,
            content_type="application/x-tar",
            headers={"X-Inferdrome-Retained-Digest": retained_digest},
        )

    def do_POST(self) -> None:
        if self.path != _HANDOFF_PATH:
            self._send(404)
            return
        try:
            body = self._body()
            receipt = self.server.handoff(body)
        except RunnerAdapterError:
            self._send(409)
            return
        self._send(200, canonical_json_bytes(receipt.model_dump(mode="json")))


def serve(arguments: RunnerAdapterArguments) -> None:
    if adapter_source_sha256() != arguments.adapter_source_sha256:
        raise RunnerAdapterError("RUNNER_ADAPTER_SOURCE_MISMATCH")
    try:
        metadata = os.lstat(arguments.evidence_root)
    except OSError:
        raise RunnerAdapterError("RUNNER_EVIDENCE_ROOT_INVALID") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o777 != 0o700
    ):
        raise RunnerAdapterError("RUNNER_EVIDENCE_ROOT_INVALID")
    server = _RunnerServer(("0.0.0.0", arguments.private_port), arguments)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        serve(_arguments(argv))
        return 0
    except (RunnerAdapterError, OSError, ValueError):
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
