"""Private vLLM adapter for the narrow two-engine GCP pre-campaign path.

The pinned Inferdrome runtime image uses this module as an explicit Docker
entrypoint override.  It launches ``vllm serve`` exactly once on loopback,
verifies the frozen preloaded Qwen3 snapshot before doing so, proxies only the
four PR-B HTTP paths, and serves a local engine-attestation response.  It is
not a general reverse proxy or production serving framework.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Literal, cast

from inferdrome.deployment.gcp_private_campaign_v2 import (
    GcpPrivateCampaignEngineAttestation,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest,
    qwen3_model_manifest_sha256,
)
from inferdrome.routing_execution.canonical import sha256_digest
from inferdrome.routing_execution.contracts import (
    ImageIdentity,
    ModelIdentity,
    RuntimeIdentity,
)

_MAX_PROXY_BODY_BYTES: Final = 1_048_576
_PROXIED_GET_PATHS: Final = frozenset({"/health", "/metrics", "/v1/models"})
_PROXIED_POST_PATHS: Final = frozenset({"/v1/chat/completions"})
_ATTESTATION_PATH: Final = "/inferdrome/v2/engine-attestation"


class EngineAdapterError(ValueError):
    """Sanitized startup, artifact, or local proxy contract failure."""


@dataclass(frozen=True)
class EngineAdapterArguments:
    endpoint_id: str
    container_name: str
    gpu_ordinal: int
    private_port: int
    upstream_port: int
    serving_image_reference: str
    model_snapshot_path: Path
    model_id: str
    model_revision: str
    tokenizer_revision: str
    startup_payload_digest: str
    adapter_source_sha256: str
    model_manifest_sha256: str
    model_snapshot_sha256: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inferdrome-gcp-private-engine-adapter")
    parser.add_argument(
        "--endpoint-id", choices=("endpoint-a", "endpoint-b"), required=True
    )
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--gpu-ordinal", choices=(0, 1), type=int, required=True)
    parser.add_argument("--private-port", choices=(8000, 8001), type=int, required=True)
    parser.add_argument(
        "--upstream-port", choices=(18000, 18001), type=int, required=True
    )
    parser.add_argument("--serving-image-reference", required=True)
    parser.add_argument("--model-snapshot-path", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--startup-payload-digest", required=True)
    parser.add_argument("--adapter-source-sha256", required=True)
    parser.add_argument("--model-manifest-sha256", required=True)
    parser.add_argument("--model-snapshot-sha256", required=True)
    return parser


def _arguments(argv: Sequence[str] | None) -> EngineAdapterArguments:
    parsed = _parser().parse_args(argv)
    result = EngineAdapterArguments(
        endpoint_id=parsed.endpoint_id,
        container_name=parsed.container_name,
        gpu_ordinal=parsed.gpu_ordinal,
        private_port=parsed.private_port,
        upstream_port=parsed.upstream_port,
        serving_image_reference=parsed.serving_image_reference,
        model_snapshot_path=parsed.model_snapshot_path,
        model_id=parsed.model_id,
        model_revision=parsed.model_revision,
        tokenizer_revision=parsed.tokenizer_revision,
        startup_payload_digest=parsed.startup_payload_digest,
        adapter_source_sha256=parsed.adapter_source_sha256,
        model_manifest_sha256=parsed.model_manifest_sha256,
        model_snapshot_sha256=parsed.model_snapshot_sha256,
    )
    expected_slots = {
        "endpoint-a": ("inferdrome-engine-a", 0, 8000, 18000),
        "endpoint-b": ("inferdrome-engine-b", 1, 8001, 18001),
    }
    if (
        result.container_name,
        result.gpu_ordinal,
        result.private_port,
        result.upstream_port,
    ) != expected_slots[result.endpoint_id]:
        raise EngineAdapterError("ENGINE_ADAPTER_SLOT_MISMATCH")
    if (
        result.model_id != QWEN3_8B_MODEL_ID
        or result.model_revision != QWEN3_8B_REVISION
        or result.tokenizer_revision != QWEN3_8B_REVISION
        or result.model_manifest_sha256 != qwen3_model_manifest_sha256()
        or result.model_snapshot_sha256 != qwen3_expected_snapshot_sha256()
    ):
        raise EngineAdapterError("ENGINE_ADAPTER_MODEL_IDENTITY_MISMATCH")
    if result.model_snapshot_path != Path("/opt/inferdrome/qwen3-8b"):
        raise EngineAdapterError("ENGINE_ADAPTER_MODEL_PATH_MISMATCH")
    return result


def adapter_source_sha256() -> str:
    """Return the installed source identity used by the startup contract."""

    try:
        return sha256_digest(Path(__file__).read_bytes())
    except OSError:
        raise EngineAdapterError("ENGINE_ADAPTER_SOURCE_UNAVAILABLE") from None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1_048_576), b""):
                digest.update(block)
    except OSError:
        raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID") from None
    return "sha256:" + digest.hexdigest()


def verify_preloaded_snapshot(arguments: EngineAdapterArguments) -> None:
    """Verify every frozen Qwen3 model file without network fallback."""

    root = arguments.model_snapshot_path
    try:
        root_stat = os.lstat(root)
    except OSError:
        raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID") from None
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID")
    manifest = qwen3_model_manifest()
    files = manifest.get("files")
    if not isinstance(files, list):
        raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID")
    for item in files:
        if not isinstance(item, dict):
            raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID")
        relative = item.get("path")
        expected_size = item.get("size_bytes")
        expected_digest = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_size, int)
            or not isinstance(expected_digest, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID")
        candidate = root / relative
        try:
            metadata = os.lstat(candidate)
        except OSError:
            raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != expected_size
            or _file_sha256(candidate) != expected_digest
        ):
            raise EngineAdapterError("ENGINE_ADAPTER_MODEL_SNAPSHOT_INVALID")


def vllm_child_argv(arguments: EngineAdapterArguments) -> tuple[str, ...]:
    """Render the one and only vLLM command owned by this adapter."""

    return (
        "vllm",
        "serve",
        str(arguments.model_snapshot_path),
        "--served-model-name",
        arguments.model_id,
        "--tokenizer",
        str(arguments.model_snapshot_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(arguments.upstream_port),
        "--disable-log-requests",
    )


def _attestation(arguments: EngineAdapterArguments) -> bytes:
    value = GcpPrivateCampaignEngineAttestation(
        schema_version="inferdrome.gcp-private-engine-attestation.v2",
        endpoint_id=cast(Literal["endpoint-a", "endpoint-b"], arguments.endpoint_id),
        container_name=cast(
            Literal["inferdrome-engine-a", "inferdrome-engine-b"],
            arguments.container_name,
        ),
        gpu_ordinal=cast(Literal[0, 1], arguments.gpu_ordinal),
        private_port=cast(Literal[8000, 8001], arguments.private_port),
        serving_image=ImageIdentity(reference=arguments.serving_image_reference),
        model=ModelIdentity(
            model_id=cast(Literal["Qwen/Qwen3-8B"], arguments.model_id),
            model_revision=cast(
                Literal["b968826d9c46dd6066d109eabc6255188de91218"],
                arguments.model_revision,
            ),
            tokenizer_revision=cast(
                Literal["b968826d9c46dd6066d109eabc6255188de91218"],
                arguments.tokenizer_revision,
            ),
        ),
        runtime=RuntimeIdentity(
            runtime_name="vllm",
            runtime_version="0.26.0",
            adapter_id="openai-compatible-routing-execution-v1",
            adapter_version="1.0.0",
        ),
        startup_payload_digest=arguments.startup_payload_digest,
        adapter_source_sha256=arguments.adapter_source_sha256,
        model_snapshot_sha256=arguments.model_snapshot_sha256,
        listener_scope="PRIVATE_VPC_ONLY",
    )
    return canonical_json_bytes(value.model_dump(mode="json"))


class _PrivateHandler(BaseHTTPRequestHandler):
    server: _PrivateServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else 0
        except ValueError:
            raise EngineAdapterError("ENGINE_ADAPTER_REQUEST_INVALID") from None
        if not 0 <= length <= _MAX_PROXY_BODY_BYTES:
            raise EngineAdapterError("ENGINE_ADAPTER_REQUEST_INVALID")
        return self.rfile.read(length)

    def _send(self, status: int, body: bytes, *, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, *, body: bytes | None) -> None:
        if self.server.child.poll() is not None:
            self._send(503, b"", content_type="application/json")
            return
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.upstream_port, timeout=5
        )
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(_MAX_PROXY_BODY_BYTES + 1)
            if len(payload) > _MAX_PROXY_BODY_BYTES:
                raise EngineAdapterError("ENGINE_ADAPTER_RESPONSE_TOO_LARGE")
            content_type = (
                response.getheader("Content-Type") or "application/octet-stream"
            )
            self._send(response.status, payload, content_type=content_type)
        except (OSError, http.client.HTTPException, EngineAdapterError):
            self._send(503, b"", content_type="application/json")
        finally:
            connection.close()

    def do_GET(self) -> None:
        if self.path == _ATTESTATION_PATH:
            self._send(200, self.server.attestation, content_type="application/json")
            return
        if self.path not in _PROXIED_GET_PATHS:
            self._send(404, b"", content_type="application/json")
            return
        self._proxy(body=None)

    def do_POST(self) -> None:
        if self.path not in _PROXIED_POST_PATHS:
            self._send(404, b"", content_type="application/json")
            return
        try:
            body = self._body()
        except EngineAdapterError:
            self._send(400, b"", content_type="application/json")
            return
        self._proxy(body=body)


class _PrivateServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        *,
        child: subprocess.Popen[bytes],
        upstream_port: int,
        attestation: bytes,
    ) -> None:
        self.child = child
        self.upstream_port = upstream_port
        self.attestation = attestation
        super().__init__(address, _PrivateHandler)


def serve(arguments: EngineAdapterArguments) -> None:
    if adapter_source_sha256() != arguments.adapter_source_sha256:
        raise EngineAdapterError("ENGINE_ADAPTER_SOURCE_MISMATCH")
    verify_preloaded_snapshot(arguments)
    if (
        os.environ.get("HF_HUB_OFFLINE") != "1"
        or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
    ):
        raise EngineAdapterError("ENGINE_ADAPTER_OFFLINE_MODE_REQUIRED")
    child = subprocess.Popen(vllm_child_argv(arguments))
    server = _PrivateServer(
        ("0.0.0.0", arguments.private_port),
        child=child,
        upstream_port=arguments.upstream_port,
        attestation=_attestation(arguments),
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _arguments(argv)
        serve(arguments)
        return 0
    except (EngineAdapterError, OSError, ValueError):
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
