#!/usr/bin/env python3
"""Deterministic OpenAI-compatible endpoint for the vLLM client spike."""

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional


MODEL_ID = "inferdrome/mock-model"
TRACE_LOCK = threading.Lock()
TRACE_PATH: Optional[Path] = None
FAIL_REQUEST_ID = "inferdrome-spike-2"
EVENT_DELAY_SECONDS = 0.01


def trace(event: str, **fields: Any) -> None:
    record: Dict[str, Any] = {
        "event": event,
        "wall_time_ns": time.time_ns(),
        **fields,
    }
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if TRACE_PATH is None:
        return
    with TRACE_LOCK:
        with TRACE_PATH.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.write("\n")


class SpikeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "InferdromeSpikeMock/1"

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format_string % args)
        )

    def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "root": MODEL_ID,
                        }
                    ],
                },
            )
            return
        self._write_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/chat/completions":
            self._write_json(404, {"error": {"message": "not found"}})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            request_body = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(400, {"error": {"message": "invalid JSON"}})
            return

        request_id = self.headers.get("x-request-id")
        trace(
            "request_received",
            request_id=request_id,
            model=request_body.get("model"),
            stream=request_body.get("stream"),
        )

        if request_id == FAIL_REQUEST_ID:
            trace("response_error", request_id=request_id, http_status=503)
            self._write_json(
                503,
                {"error": {"message": "intentional capability-spike failure"}},
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        response_id = request_id or "preflight-or-warmup"
        events = [
            (
                "role_only",
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": None},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                "content_alpha",
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "alpha"},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                "content_beta",
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": " beta"},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                "finish_reason",
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                },
            ),
            (
                "usage",
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "model": MODEL_ID,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                },
            ),
        ]

        try:
            for event_name, payload in events:
                time.sleep(EVENT_DELAY_SECONDS)
                wire = "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"
                self.wfile.write(wire.encode("utf-8"))
                self.wfile.flush()
                trace("stream_event_sent", request_id=request_id, kind=event_name)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            trace("stream_done_sent", request_id=request_id)
        except (BrokenPipeError, ConnectionResetError):
            trace("client_disconnected", request_id=request_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--trace-path", type=Path)
    parser.add_argument("--fail-request-id", default=FAIL_REQUEST_ID)
    parser.add_argument("--event-delay-seconds", type=float, default=0.01)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    global TRACE_PATH, FAIL_REQUEST_ID, EVENT_DELAY_SECONDS
    TRACE_PATH = args.trace_path
    FAIL_REQUEST_ID = args.fail_request_id
    EVENT_DELAY_SECONDS = args.event_delay_seconds

    if TRACE_PATH is not None:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if TRACE_PATH.exists():
            raise FileExistsError(f"trace path already exists: {TRACE_PATH}")

    server = ThreadingHTTPServer((args.host, args.port), SpikeHandler)

    def stop_server(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    trace("server_started", host=args.host, port=args.port)
    print(f"ready http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        trace("server_stopped")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
