"""Bounded deterministic OpenAI-compatible mock engine for Compose smoke only."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

MOCK_MODEL_ID: Final = "inferdrome/mock-model"
MOCK_MAX_REQUEST_BYTES: Final = 16_384
MOCK_RESPONSE_TEXT: Final = "synthetic mock response"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def mock_response_json_bytes() -> bytes:
    """Return the exact bounded response body served by the mock."""

    return _json_bytes(
        {
            "id": "inferdrome-compose-mock-response",
            "object": "chat.completion",
            "model": MOCK_MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": MOCK_RESPONSE_TEXT,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "completion_tokens": 4,
                "prompt_tokens": 1,
                "total_tokens": 5,
            },
            "synthetic_only": True,
        }
    )


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "InferdromeComposeMock/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write(self, status: int, value: object) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Inferdrome-Synthetic", "true")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write(200, {"status": "ok", "synthetic_only": True})
            return
        if self.path == "/v1/models":
            self._write(
                200,
                {
                    "object": "list",
                    "data": [{"id": MOCK_MODEL_ID, "object": "model"}],
                    "synthetic_only": True,
                },
            )
            return
        self._write(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._write(404, {"error": {"message": "not found"}})
            return
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "0")
        except ValueError:
            self._write(400, {"error": {"message": "invalid content length"}})
            return
        if content_length < 0 or content_length > MOCK_MAX_REQUEST_BYTES:
            self._write(413, {"error": {"message": "request is too large"}})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": {"message": "invalid JSON"}})
            return
        if not isinstance(payload, dict) or payload.get("model") != MOCK_MODEL_ID:
            self._write(400, {"error": {"message": "unsupported mock request"}})
            return
        self._write(200, json.loads(mock_response_json_bytes()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inferdrome-compose-mock")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args(argv)
    if arguments.port < 1 or arguments.port > 65_535:
        parser.error("port is outside its bounded range")
    server = ThreadingHTTPServer((arguments.host, arguments.port), _MockHandler)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


__all__ = [
    "MOCK_MODEL_ID",
    "MOCK_RESPONSE_TEXT",
    "main",
    "mock_response_json_bytes",
]


if __name__ == "__main__":
    raise SystemExit(main())
