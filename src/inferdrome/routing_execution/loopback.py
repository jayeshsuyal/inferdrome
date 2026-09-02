"""Actual socket-level local harness for the PR-B bridge acceptance tests/demo."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def vllm_0_26_metrics(*, running: int, model_name: str = "Qwen/Qwen3-8B") -> bytes:
    """Return a bounded vLLM-0.26-style exposition with repeated buckets."""

    labels = f'model_name="{model_name}"'
    return (
        "# HELP vllm:num_requests_running Number of running requests.\n"
        "# TYPE vllm:num_requests_running gauge\n"
        f"vllm:num_requests_running{{{labels}}} {running}\n"
        "# HELP vllm:time_to_first_token_seconds Time to first token.\n"
        "# TYPE vllm:time_to_first_token_seconds histogram\n"
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="0.01"}} 0\n'
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="0.10"}} 1\n'
        f'vllm:time_to_first_token_seconds_bucket{{{labels},le="+Inf"}} 1\n'
        f"vllm:time_to_first_token_seconds_count{{{labels}}} 1\n"
        f"vllm:time_to_first_token_seconds_sum{{{labels}}} 0.02\n"
    ).encode()


_VALID_COMPLETION = {
    "id": "local-completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "local completion"},
            "finish_reason": "stop",
        }
    ],
}


@dataclass
class LoopbackEndpoint:
    """One deliberately tiny OpenAI-compatible, metrics-bearing local endpoint."""

    name: str
    load: int
    health_ok: bool = True
    request_count: int = 0
    health_count: int = 0
    metrics_count: int = 0
    model_count: int = 0
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    @property
    def origin(self) -> str:
        if self._server is None:
            raise RuntimeError("loopback endpoint is not started")
        host, port = self._server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else host
        return f"http://{host_text}:{port}"

    def start(self) -> None:
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "InferdromeLoopback/1"
            sys_version = ""

            def log_message(self, _: str, *args: object) -> None:
                # Never mirror prompts, headers, or HTTP payloads to a log.
                return

            def _json(self, status: int, value: object) -> None:
                content = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self) -> None:
                if self.path == "/health":
                    endpoint.health_count += 1
                    if endpoint.health_ok:
                        # Pinned vLLM health is an empty HTTP 200 response.
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable"},
                        )
                    return
                if self.path == "/metrics":
                    endpoint.metrics_count += 1
                    content = vllm_0_26_metrics(running=endpoint.load)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                if self.path == "/v1/models":
                    endpoint.model_count += 1
                    self._json(HTTPStatus.OK, {"data": [{"id": "Qwen/Qwen3-8B"}]})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request"})
                    return
                if not 0 < size <= 65_536:
                    self._json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "bad_request"}
                    )
                    return
                # Consume exactly one request body but do not parse/store/echo it.
                if len(self.rfile.read(size)) != size:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request"})
                    return
                endpoint.request_count += 1
                self._json(HTTPStatus.OK, _VALID_COMPLETION)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


@dataclass
class LoopbackPair:
    """Two independent local processes-in-spirit with distinct actual origins."""

    endpoint_a: LoopbackEndpoint = field(
        default_factory=lambda: LoopbackEndpoint(name="endpoint-a", load=4)
    )
    endpoint_b: LoopbackEndpoint = field(
        default_factory=lambda: LoopbackEndpoint(name="endpoint-b", load=1)
    )

    def __enter__(self) -> LoopbackPair:
        self.endpoint_a.start()
        self.endpoint_b.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.endpoint_b.close()
        self.endpoint_a.close()
