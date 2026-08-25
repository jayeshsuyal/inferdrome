"""Explicit synthetic endpoint probe used by the reproducible runner image.

This module is deliberately a client boundary, not a serving runtime.  It
performs one explicitly configured HTTP request and writes a bounded metadata
result to an explicitly mounted directory.  It does not launch vLLM/SGLang,
implement benchmark methodology, or produce a frozen evidence bundle. The
container's default entrypoint is the canonical ``inferdrome`` CLI; this
module is only the separately named ``inferdrome-runner-probe`` command.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from inferdrome import __version__
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import sha256_digest
from inferdrome.errors import AdapterError

RUNNER_OUTPUT_SCHEMA_VERSION = "inferdrome.runner-probe-output.v1"
DEFAULT_OUTPUT_NAME = "runner-output.json"
MAX_ENDPOINT_BYTES = 2048
MAX_MODEL_LENGTH = 256
MAX_PROMPT_LENGTH = 8192
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_NAME_LENGTH = 128

_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}$")
_SAFE_OUTPUT_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$"
)


class RunnerError(AdapterError):
    """Bounded runner configuration, endpoint, or output failure."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _endpoint(value: str) -> SplitResult:
    if not value or len(value.encode("utf-8")) > MAX_ENDPOINT_BYTES:
        raise RunnerError("endpoint is empty or exceeds its byte bound")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise RunnerError("endpoint scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise RunnerError("endpoint credentials are forbidden")
    if parsed.query or parsed.fragment:
        raise RunnerError("endpoint query and fragment are forbidden")
    if not parsed.hostname or not parsed.hostname.isascii():
        raise RunnerError("endpoint host is invalid")
    try:
        port = parsed.port
    except ValueError:
        raise RunnerError("endpoint port is invalid") from None
    if port is not None and not 1 <= port <= 65_535:
        raise RunnerError("endpoint port is invalid")
    if parsed.hostname in {"0.0.0.0", "::"}:
        raise RunnerError("endpoint must not use an unspecified host")
    if not parsed.path.startswith("/"):
        raise RunnerError("endpoint path is invalid")
    return parsed


def _safe_model(value: str) -> str:
    if not 1 <= len(value) <= MAX_MODEL_LENGTH or _SAFE_MODEL.fullmatch(value) is None:
        raise RunnerError("model identifier is invalid")
    return value


def _safe_prompt(value: str) -> str:
    if not 1 <= len(value.encode("utf-8")) <= MAX_PROMPT_LENGTH:
        raise RunnerError("prompt exceeds its byte bound")
    return value


def _safe_output_name(value: str) -> str:
    if (
        not 1 <= len(value) <= MAX_OUTPUT_NAME_LENGTH
        or _SAFE_OUTPUT_NAME.fullmatch(value) is None
    ):
        raise RunnerError("output name is invalid")
    return value


def _real_directory(path: Path) -> Path:
    selected = path.absolute()
    try:
        metadata = os.lstat(selected)
    except OSError:
        raise RunnerError("evidence directory is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RunnerError("evidence directory must be a real directory")
    return selected


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


class _OutputReservation:
    """Reserve a destination before network activity and publish atomically."""

    def __init__(
        self,
        directory: Path,
        output_name: str,
        reservation_name: str,
        reservation_fd: int,
        reservation_stat: os.stat_result,
    ) -> None:
        self.directory = directory
        self.output_path = directory / output_name
        self.reservation_path = directory / reservation_name
        self.reservation_fd = reservation_fd
        self.reservation_stat = reservation_stat
        self.temp_path: Path | None = None
        self.temp_stat: os.stat_result | None = None
        self.final_stat: os.stat_result | None = None
        self.published = False

    @classmethod
    def reserve(cls, directory: Path, output_name: str) -> _OutputReservation:
        output_path = directory / output_name
        try:
            os.lstat(output_path)
        except FileNotFoundError:
            pass
        except OSError:
            raise RunnerError("runner output destination is unsafe") from None
        else:
            raise RunnerError("runner output already exists or is unsafe")

        reservation_name = f".{output_name}.inferdrome-reservation"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        reservation_path = directory / reservation_name
        reservation_fd: int | None = None
        try:
            reservation_fd = os.open(reservation_path, flags, 0o600)
            reservation_stat = os.fstat(reservation_fd)
            try:
                os.lstat(output_path)
            except FileNotFoundError:
                pass
            except OSError:
                raise RunnerError("runner output destination is unsafe") from None
            else:
                raise RunnerError("runner output already exists or is unsafe")
        except RunnerError:
            if reservation_fd is not None:
                os.close(reservation_fd)
            with contextlib.suppress(OSError):
                os.unlink(reservation_path)
            raise
        except OSError:
            if reservation_fd is not None:
                os.close(reservation_fd)
            raise RunnerError("runner output is already reserved or unsafe") from None

        assert reservation_fd is not None
        return cls(
            directory,
            output_name,
            reservation_name,
            reservation_fd,
            reservation_stat,
        )

    def _unlink_owned(self, path: Path | None, expected: os.stat_result | None) -> None:
        if path is None or expected is None:
            return
        try:
            current = os.lstat(path)
        except OSError:
            return
        if _same_file(current, expected):
            with contextlib.suppress(OSError):
                os.unlink(path)

    def _close_reservation(self) -> None:
        if self.reservation_fd >= 0:
            os.close(self.reservation_fd)
            self.reservation_fd = -1

    def _fsync_directory(self) -> None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self.directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def abort(self) -> None:
        """Remove only this invocation's private reservation and temp file."""

        self._close_reservation()
        self._unlink_owned(self.temp_path, self.temp_stat)
        if not self.published:
            self._unlink_owned(self.output_path, self.final_stat)
        self._unlink_owned(self.reservation_path, self.reservation_stat)

    def publish(self, content: bytes) -> None:
        temp_name = f".{self.output_path.name}.inferdrome-temp-{secrets.token_hex(16)}"
        self.temp_path = self.directory / temp_name
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.temp_path, flags, 0o600)
            self.temp_stat = os.fstat(descriptor)
            try:
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise RunnerError("runner output write was incomplete")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.final_stat = self.temp_stat
            os.link(self.temp_path, self.output_path)
            self._fsync_directory()
            self.published = True
            self._unlink_owned(self.temp_path, self.temp_stat)
            self.temp_path = None
            self.temp_stat = None
            self._unlink_owned(self.reservation_path, self.reservation_stat)
            self._close_reservation()
        except RunnerError:
            raise
        except OSError:
            raise RunnerError("runner output could not be published") from None


def _read_response(response: Any) -> tuple[int, bytes]:
    status = int(getattr(response, "status", 200))
    if not 200 <= status < 300:
        raise RunnerError("endpoint returned a non-success response")
    content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise RunnerError("endpoint response exceeds its byte bound")
    return status, content


def run_endpoint_request(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    evidence_dir: Path,
    output_name: str = DEFAULT_OUTPUT_NAME,
    max_output_tokens: int = 16,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Call one configured endpoint and write one synthetic runner result."""

    _endpoint(endpoint)
    selected_model = _safe_model(model)
    selected_prompt = _safe_prompt(prompt)
    selected_output_name = _safe_output_name(output_name)
    selected_dir = _real_directory(evidence_dir)
    if not 1 <= max_output_tokens <= 4096:
        raise RunnerError("max output tokens is outside its bound")
    if not 0.1 <= timeout_seconds <= 300:
        raise RunnerError("timeout is outside its bound")

    reservation = _OutputReservation.reserve(selected_dir, selected_output_name)
    try:
        request_value = {
            "messages": [{"content": selected_prompt, "role": "user"}],
            "max_tokens": max_output_tokens,
            "model": selected_model,
            "temperature": 0,
        }
        request_bytes = canonical_json_bytes(request_value)
        request = Request(
            endpoint,
            data=request_bytes,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            status, response_bytes = _read_response(response)

        output_value: dict[str, object] = {
            "schema_version": RUNNER_OUTPUT_SCHEMA_VERSION,
            "runner_version": __version__,
            "execution_mode": "synthetic_endpoint_probe",
            "synthetic_only": True,
            "evidence_eligible": False,
            "status": "SUCCEEDED",
            "endpoint_sha256": sha256_digest(endpoint.encode("utf-8")),
            "model": selected_model,
            "request_sha256": sha256_digest(request_bytes),
            "response_sha256": sha256_digest(response_bytes),
            "response_status": status,
            "response_bytes": len(response_bytes),
        }
        reservation.publish(canonical_json_bytes(output_value))
        return output_value
    except RunnerError:
        reservation.abort()
        raise
    except (HTTPError, URLError, TimeoutError, OSError):
        reservation.abort()
        raise RunnerError("inference endpoint request failed") from None
    except Exception:
        reservation.abort()
        raise RunnerError("inference endpoint request failed") from None
    except BaseException:
        reservation.abort()
        raise


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "max output tokens must be an integer"
        ) from None
    if not 1 <= parsed <= 4096:
        raise argparse.ArgumentTypeError("max output tokens must be between 1 and 4096")
    return parsed


def _bounded_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number") from None
    if not 0.1 <= parsed <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 0.1 and 300")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferdrome-runner-probe",
        description="Synthetic one-request probe against an existing endpoint",
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output-name", default=DEFAULT_OUTPUT_NAME)
    parser.add_argument("--max-output-tokens", default=16, type=_positive_int)
    parser.add_argument("--timeout-seconds", default=30.0, type=_bounded_timeout)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        output = run_endpoint_request(
            endpoint=arguments.endpoint,
            model=arguments.model,
            prompt=arguments.prompt,
            evidence_dir=arguments.evidence_dir,
            output_name=arguments.output_name,
            max_output_tokens=arguments.max_output_tokens,
            timeout_seconds=arguments.timeout_seconds,
        )
    except RunnerError as error:
        print(f"runner failed: {error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(output) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_OUTPUT_NAME",
    "RUNNER_OUTPUT_SCHEMA_VERSION",
    "RunnerError",
    "build_parser",
    "main",
    "run_endpoint_request",
]
