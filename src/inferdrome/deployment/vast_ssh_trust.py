"""Prospective SSH pins authenticated through Vast's exact-instance log API.

This is an explicit trust in the Vast control plane and its delivery of guest
logs, not hardware attestation. The startup integration must announce the key
actually used by its SSH daemon. Nothing here starts SSH or activates a launch.

Official transport: https://docs.vast.ai/api-reference/instances/show-logs
The authenticated API returns an S3 URL; the public log fetch is a separate,
credential-free HTTPS connection. No TOFU, key scan, or key replacement exists.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import ssl
import struct
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_control import ControlFailure, _bounded_call

ANNOUNCEMENT_PREFIX = b"INFERDROME_VAST_SSH_HOST_KEY_V1 "
MAX_LOG_BYTES = 131_072
MAX_ANNOUNCEMENT_BYTES = 1024
_SCHEMA = "inferdrome.vast-ssh-host-key.v1"
_PIN_SCHEMA = "inferdrome.vast-ssh-pin.v1"
_TRUST = "VAST_CONTROL_PLANE_LOG_BINDING_NOT_HARDWARE_ATTESTATION"
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_URL = re.compile(
    r"https://s3\.amazonaws\.com/vast\.ai/instance_logs/"
    r"[A-Za-z0-9_-]{1,128}\.log\Z"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SshTrustFailure(ValueError):
    """Fixed failure codes; never include log bodies, URLs or credentials."""


class LogRequester(Protocol):
    """The provider must already have durably retained this exact create ID."""

    def request_logs(self, instance_id: int, *, seconds: float) -> str: ...


class LogFetcher(Protocol):
    def __call__(self, result_url: str, *, seconds: float) -> bytes: ...


class LogResponse(Protocol):
    status: int

    def getheaders(self) -> list[tuple[str, str]]: ...
    def read(self, amt: int) -> bytes: ...
    def close(self) -> None: ...


class LogConnection(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str]
    ) -> None: ...
    def getresponse(self) -> LogResponse: ...
    def close(self) -> None: ...


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SshTrustFailure("VAST_SSH_DUPLICATE_FIELD")
        result[key] = value
    return result


def _json(content: bytes) -> dict[str, object]:
    try:
        value = json.loads(content, object_pairs_hook=_object)
        if not isinstance(value, dict) or _canonical(value) != content:
            raise SshTrustFailure("VAST_SSH_RECORD_INVALID")
        return value
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise SshTrustFailure("VAST_SSH_RECORD_INVALID") from None


def _binding(instance_id: int, run_nonce: str) -> None:
    if (
        type(instance_id) is not int
        or not 1 <= instance_id <= 9_007_199_254_740_991
        or not isinstance(run_nonce, str)
        or _NONCE.fullmatch(run_nonce) is None
    ):
        raise SshTrustFailure("VAST_SSH_BINDING_INVALID")


def _public_key(value: str) -> bytes:
    """Accept one canonical OpenSSH Ed25519 public key, without comments."""
    if not isinstance(value, str) or len(value) > 128:
        raise SshTrustFailure("VAST_SSH_KEY_INVALID")
    parts = value.split(" ")
    if len(parts) != 2 or parts[0] != "ssh-ed25519":
        raise SshTrustFailure("VAST_SSH_KEY_INVALID")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (ValueError, UnicodeError):
        raise SshTrustFailure("VAST_SSH_KEY_INVALID") from None
    if (
        len(decoded) != 51
        or decoded[:19]
        != struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        or base64.b64encode(decoded).decode("ascii") != parts[1]
    ):
        raise SshTrustFailure("VAST_SSH_KEY_INVALID")
    return decoded


@dataclass(frozen=True)
class HostKeyAnnouncement:
    instance_id: int
    run_nonce: str
    host_public_key: str

    def __post_init__(self) -> None:
        _binding(self.instance_id, self.run_nonce)
        _public_key(self.host_public_key)

    def value(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA,
            "instance_id": self.instance_id,
            "run_nonce": self.run_nonce,
            "host_public_key": self.host_public_key,
        }

    @property
    def host_key_sha256(self) -> str:
        return _digest(_public_key(self.host_public_key))


def format_announcement(
    instance_id: int, run_nonce: str, host_public_key: str
) -> bytes:
    """One public, newline-terminated startup marker; never include private keys."""
    record = HostKeyAnnouncement(instance_id, run_nonce, host_public_key)
    return ANNOUNCEMENT_PREFIX + _canonical(record.value()) + b"\n"


def _announcement(value: dict[str, object]) -> HostKeyAnnouncement:
    if (
        set(value) != {"schema_version", "instance_id", "run_nonce", "host_public_key"}
        or value["schema_version"] != _SCHEMA
        or type(value["instance_id"]) is not int
        or not isinstance(value["run_nonce"], str)
        or not isinstance(value["host_public_key"], str)
    ):
        raise SshTrustFailure("VAST_SSH_RECORD_INVALID")
    return HostKeyAnnouncement(
        value["instance_id"], value["run_nonce"], value["host_public_key"]
    )


def parse_log_announcement(
    content: bytes, *, instance_id: int, run_nonce: str
) -> HostKeyAnnouncement:
    """Ignore ordinary logs; reject malformed, stale or conflicting markers.

    Identical repeated markers are idempotent. A marker from a different run or
    instance is a replay failure, even when a matching marker is also present.
    A missing/truncated marker never produces a pin; callers can poll again
    within their original phase deadline, without accepting partial records.
    """
    _binding(instance_id, run_nonce)
    if not isinstance(content, bytes) or len(content) > MAX_LOG_BYTES:
        raise SshTrustFailure("VAST_SSH_LOG_LIMIT")
    selected: HostKeyAnnouncement | None = None
    for line in content.splitlines(keepends=True):
        if ANNOUNCEMENT_PREFIX not in line:
            continue
        if (
            not line.startswith(ANNOUNCEMENT_PREFIX)
            or not line.endswith(b"\n")
            or len(line) > MAX_ANNOUNCEMENT_BYTES
        ):
            raise SshTrustFailure("VAST_SSH_MARKER_INVALID")
        record = _announcement(_json(line[len(ANNOUNCEMENT_PREFIX) : -1]))
        if record.instance_id != instance_id or record.run_nonce != run_nonce:
            raise SshTrustFailure("VAST_SSH_STALE_OR_REPLAYED_MARKER")
        if selected is not None and selected != record:
            raise SshTrustFailure("VAST_SSH_CONFLICTING_KEYS")
        selected = record
    if selected is None:
        raise SshTrustFailure("VAST_SSH_MARKER_MISSING")
    return selected


def validate_result_url(result_url: str) -> str:
    """Accept only the documented S3 log object namespace, without redirects."""
    if not isinstance(result_url, str) or _URL.fullmatch(result_url) is None:
        raise SshTrustFailure("VAST_SSH_LOG_URL_FORBIDDEN")
    return result_url


def _seconds(seconds: float) -> None:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (float, int))
        or not 0 < seconds <= 600
        or not math.isfinite(seconds)
    ):
        raise SshTrustFailure("VAST_SSH_BUDGET_INVALID")


def _connection(host: str, seconds: float) -> LogConnection:
    # HTTPSConnection has no requests session, .netrc, proxy or ambient headers.
    return http.client.HTTPSConnection(
        host, timeout=seconds, context=ssl.create_default_context()
    )


def fetch_log_bytes(
    result_url: str,
    *,
    seconds: float,
    connection_factory: Callable[[str, float], LogConnection] | None = None,
) -> bytes:
    """One bounded GET, never an authenticated provider request or a redirect.

    The shared POSIX main-thread alarm bounds DNS, TLS, headers and body reads,
    including a peer that trickles bytes. It preserves the earlier controller
    deadline. A fresh connection and exact public headers prevent API credential
    forwarding; no cookies, proxy, authentication callback or redirect is used.
    """
    validate_result_url(result_url)
    _seconds(seconds)

    def retrieve() -> bytes:
        connection: LogConnection | None = None
        response: LogResponse | None = None
        try:
            connection = (connection_factory or _connection)(
                "s3.amazonaws.com", seconds
            )
            connection.request(
                "GET",
                result_url.removeprefix("https://s3.amazonaws.com"),
                headers={"Accept": "text/plain", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise SshTrustFailure("VAST_SSH_LOG_HTTP_STATUS")
            headers = response.getheaders()
            lengths = [v for k, v in headers if k.lower() == "content-length"]
            encodings = [v for k, v in headers if k.lower() == "content-encoding"]
            if encodings and encodings != ["identity"]:
                raise SshTrustFailure("VAST_SSH_LOG_ENCODING_FORBIDDEN")
            if lengths and (
                len(lengths) != 1
                or re.fullmatch(r"[0-9]{1,9}", lengths[0]) is None
                or int(lengths[0]) > MAX_LOG_BYTES
            ):
                raise SshTrustFailure("VAST_SSH_LOG_LIMIT")
            content = bytearray()
            while True:
                chunk = response.read(min(8192, MAX_LOG_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_LOG_BYTES:
                    raise SshTrustFailure("VAST_SSH_LOG_LIMIT")
            if lengths and len(content) != int(lengths[0]):
                raise SshTrustFailure("VAST_SSH_LOG_TRUNCATED")
            return bytes(content)
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                if connection is not None:
                    connection.close()

    try:
        return _bounded_call(retrieve, seconds)
    except SshTrustFailure:
        raise
    except ControlFailure:
        raise SshTrustFailure("VAST_SSH_LOG_DEADLINE_OR_PLATFORM") from None
    except Exception:
        raise SshTrustFailure("VAST_SSH_LOG_FETCH_FAILED") from None


@dataclass(frozen=True)
class HostKeyPin:
    announcement: HostKeyAnnouncement
    log_sha256: str
    result_url_sha256: str

    def value(self) -> dict[str, object]:
        return {
            "schema_version": _PIN_SCHEMA,
            "trust_model": _TRUST,
            "announcement": self.announcement.value(),
            "host_key_sha256": self.announcement.host_key_sha256,
            "log_sha256": self.log_sha256,
            "result_url_sha256": self.result_url_sha256,
        }


class SshPinJournal:
    """One immutable private record per instance; never automatically rotate.

    The directory must already exist with mode 0700. It is a trusted local
    journal, not protection against its owner deliberately rewriting evidence.
    Atomic no-replace publication prevents competing enrollments from replacing
    an earlier pin. A new nonce for an already pinned instance is rejected.
    """

    def __init__(self, directory: Path) -> None:
        self._root = SafeDirFD.open(directory)
        if os.fstat(self._root.fd).st_mode & 0o777 != 0o700:
            self._root.close()
            raise SshTrustFailure("VAST_SSH_JOURNAL_NOT_PRIVATE")
        self._observed: dict[int, bytes] = {}

    def close(self) -> None:
        self._root.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, *, instance_id: int, run_nonce: str) -> HostKeyPin | None:
        _binding(instance_id, run_nonce)
        name = f"instance-{instance_id}.json"
        try:
            descriptor = self._root.open_child(name, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            if instance_id in self._observed:
                raise SshTrustFailure("VAST_SSH_PIN_REPLACED") from None
            return None
        try:
            before = os.fstat(descriptor)
            if before.st_mode & 0o777 != 0o600 or before.st_size > 4096:
                raise SshTrustFailure("VAST_SSH_PIN_INVALID")
            content = os.read(descriptor, 4097)
            after = self._root.validated_regular_child(name, descriptor=descriptor)
            if len(content) != before.st_size or any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise SshTrustFailure("VAST_SSH_PIN_REPLACED")
        finally:
            os.close(descriptor)
        if instance_id in self._observed and self._observed[instance_id] != content:
            raise SshTrustFailure("VAST_SSH_PIN_REPLACED")
        value = _json(content)
        if (
            set(value)
            != {
                "schema_version", "trust_model", "announcement", "host_key_sha256",
                "log_sha256", "result_url_sha256",
            }
            or value["schema_version"] != _PIN_SCHEMA
            or value["trust_model"] != _TRUST
            or not isinstance(value["announcement"], dict)
            or not isinstance(value["log_sha256"], str)
            or not isinstance(value["result_url_sha256"], str)
            or _DIGEST.fullmatch(value["log_sha256"]) is None
            or _DIGEST.fullmatch(value["result_url_sha256"]) is None
        ):
            raise SshTrustFailure("VAST_SSH_PIN_INVALID")
        announcement = _announcement(value["announcement"])
        if (
            announcement.instance_id != instance_id
            or announcement.run_nonce != run_nonce
            or value["host_key_sha256"] != announcement.host_key_sha256
        ):
            raise SshTrustFailure("VAST_SSH_PIN_BINDING_MISMATCH")
        self._observed[instance_id] = content
        return HostKeyPin(announcement, value["log_sha256"], value["result_url_sha256"])

    def enroll(
        self, content: bytes, *, instance_id: int, run_nonce: str, result_url: str
    ) -> HostKeyPin:
        validate_result_url(result_url)
        announcement = parse_log_announcement(
            content, instance_id=instance_id, run_nonce=run_nonce
        )
        previous = self.read(instance_id=instance_id, run_nonce=run_nonce)
        if previous is not None:
            if previous.announcement != announcement:
                raise SshTrustFailure("VAST_SSH_KEY_REPLACEMENT_FORBIDDEN")
            return previous
        pin = HostKeyPin(announcement, _digest(content), _digest(result_url.encode()))
        encoded = _canonical(pin.value())
        name = f"instance-{instance_id}.json"
        temporary = f".pending-{secrets.token_hex(16)}"
        descriptor = self._root.open_child(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        )
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            self._root.validated_regular_child(temporary)
            try:
                os.link(
                    temporary, name, src_dir_fd=self._root.fd,
                    dst_dir_fd=self._root.fd, follow_symlinks=False,
                )
            except FileExistsError:
                concurrent = self.read(instance_id=instance_id, run_nonce=run_nonce)
                if concurrent is None or concurrent.announcement != announcement:
                    raise SshTrustFailure(
                        "VAST_SSH_KEY_REPLACEMENT_FORBIDDEN"
                    ) from None
                return concurrent
            os.unlink(temporary, dir_fd=self._root.fd)
            published = self._root.validated_regular_child(name)
            try:
                self._root.fsync()
            except BaseException:
                # A failed durable enrollment must not become a usable pin on
                # retry. Preserve an independently replaced name during rollback.
                with suppress(OSError):
                    self._root.unlink_child(name, expected=published)
                    self._root.fsync()
                raise
            self._observed[instance_id] = encoded
            return pin
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root.fd)


def enroll_from_provider(
    provider: LogRequester,
    journal: SshPinJournal,
    *,
    instance_id: int,
    run_nonce: str,
    seconds: float,
    fetcher: LogFetcher | None = None,
) -> HostKeyPin:
    """One exact-ID log attempt within one budget; callers own bounded polling."""
    _binding(instance_id, run_nonce)
    _seconds(seconds)
    deadline = time.monotonic() + seconds

    def remaining() -> float:
        result = deadline - time.monotonic()
        if result <= 0:
            raise SshTrustFailure("VAST_SSH_LOG_DEADLINE_OR_PLATFORM")
        return result

    def enroll() -> HostKeyPin:
        result_url = provider.request_logs(instance_id, seconds=min(60, remaining()))
        validate_result_url(result_url)
        content = (fetcher or fetch_log_bytes)(result_url, seconds=remaining())
        remaining()
        return journal.enroll(
            content, instance_id=instance_id, run_nonce=run_nonce, result_url=result_url
        )

    try:
        return _bounded_call(enroll, seconds)
    except SshTrustFailure:
        raise
    except ControlFailure as error:
        if str(error) in {
            "VAST_CONTROL_CALL_TIMEOUT", "VAST_CONTROL_MAIN_THREAD_ALARM_REQUIRED",
            "VAST_CONTROL_DEADLINE",
        }:
            raise SshTrustFailure("VAST_SSH_LOG_DEADLINE_OR_PLATFORM") from None
        raise SshTrustFailure("VAST_SSH_ENROLLMENT_FAILED") from None
    except Exception:
        raise SshTrustFailure("VAST_SSH_ENROLLMENT_FAILED") from None
