#!/usr/bin/env python3
"""Mac-safe synthetic probe smoke; never claims benchmark or GPU proof."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length)
        if self.path != "/v1/chat/completions" or not request_body:
            self.send_error(400)
            return
        response = b'{"id":"synthetic","choices":[{"message":{"content":"ok"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args: object) -> None:
        return


def _host_smoke() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="inferdrome-runner-smoke-") as raw:
            evidence_dir = Path(raw) / "evidence"
            evidence_dir.mkdir()
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
            command = [
                sys.executable,
                "-m",
                "inferdrome.runner",
                "--endpoint",
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "--model",
                "inferdrome/mock-model",
                "--prompt",
                "runner smoke prompt",
                "--evidence-dir",
                str(evidence_dir),
            ]
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                check=False,
                text=True,
            )
            if completed.returncode != 0:
                raise RuntimeError("runner host smoke command failed")
            output = json.loads(completed.stdout)
            stored = json.loads(
                (evidence_dir / "runner-output.json").read_text(encoding="utf-8")
            )
            if output != stored:
                raise RuntimeError("runner stdout and mounted output differ")
            if output.get("synthetic_only") is not True:
                raise RuntimeError("runner smoke lost synthetic-only marking")
            if output.get("evidence_eligible") is not False:
                raise RuntimeError("runner smoke became evidence eligible")
            if "runner smoke prompt" in completed.stdout or '"ok"' in completed.stdout:
                raise RuntimeError("runner smoke exposed request or response content")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the local smoke path without starting a server",
    )
    args = parser.parse_args()
    if args.check:
        docker_status = "available" if shutil.which("docker") else "unavailable"
        print(
            f"runner host smoke path: available; Docker execution gate: {docker_status}"
        )
        return 0
    _host_smoke()
    print(
        "runner probe smoke: PASS (synthetic CLI/config/output only; "
        "no CUDA, GPU, vLLM, or SGLang claim)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
