"""Create/no-replace sealing and offline verification for PR-B execution evidence."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.errors import VerificationError
from inferdrome.routing_execution.canonical import (
    canonical_json_bytes,
    package_digest,
    sha256_digest,
)
from inferdrome.routing_execution.contracts import (
    ArtifactHashEntry,
    ExecutionId,
    InputTransferReceipt,
    IntegrityManifest,
    ProducerReceipt,
    TerminalStatus,
)
from inferdrome.routing_execution.manual_host_contracts import (
    MANIFEST_ADAPTER,
    ExecutionManifest,
)
from inferdrome.routing_execution.replay import ReplayVerificationError, verify_replay

_PAYLOAD_FILES = (
    "executed-manifest.json",
    "input-transfer-receipt.json",
    "producer-receipt.json",
)
_INTEGRITY_FILE = "integrity-manifest.json"
_EXPECTED_FILES = frozenset((*_PAYLOAD_FILES, _INTEGRITY_FILE))
_MAX_FILE_BYTES = 8_388_608
_MAX_TOTAL_BYTES = 33_554_432
_FORBIDDEN_KEYS = frozenset(
    {
        "origin",
        "url",
        "endpoint_url",
        "authorization",
        "headers",
        "prompt",
        "messages",
        "content",
        "response",
        "response_body",
        "provider_payload",
        "instance_id",
        "raw_log",
        "invoice",
    }
)
_FORBIDDEN_BYTE_MARKERS = (
    b"://",
    b"authorization",
    b"provider_payload",
    b"raw_log",
    b"invoice",
)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 1


class ExecutionPackageError(ValueError):
    """An execution package cannot be safely sealed or interpreted."""


def _raise_rename_error() -> None:
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def _rename_no_replace_at(parent: SafeDirFD, source: str, destination: str) -> None:
    """Atomically publish two child names through one held parent descriptor.

    A pathname rename would reopen an attacker-replaceable parent after
    reservation.  Both supported platforms expose a no-replace rename-at
    primitive; unsupported platforms deliberately fail closed.
    """

    parent.assert_open()
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError:
            raise OSError(errno.ENOTSUP, "no-replace rename is unavailable") from None
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        if (
            rename(
                parent.fd,
                source_bytes,
                parent.fd,
                destination_bytes,
                _LINUX_RENAME_NOREPLACE,
            )
            != 0
        ):
            _raise_rename_error()
        return
    if sys.platform == "darwin":
        try:
            rename = library.renameatx_np
        except AttributeError:
            raise OSError(errno.ENOTSUP, "no-replace rename is unavailable") from None
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        if (
            rename(
                parent.fd,
                source_bytes,
                parent.fd,
                destination_bytes,
                _DARWIN_RENAME_EXCL,
            )
            != 0
        ):
            _raise_rename_error()
        return
    raise OSError(errno.ENOTSUP, "no-replace rename is unsupported")


@dataclass(frozen=True)
class SealedPackage:
    """A published package path and retained identity."""

    path: Path
    retained_digest: str


@dataclass(frozen=True)
class VerificationReport:
    """Bounded facts established by an offline snapshot verifier."""

    path: Path
    retained_digest: str
    execution_id: str
    terminal_population: Mapping[str, Mapping[TerminalStatus, int]]


@dataclass(frozen=True)
class VerifiedExecutionPackage:
    """In-memory, revalidated package records safe for read-only projection."""

    report: VerificationReport
    input_transfer: InputTransferReceipt
    executed_manifest: ExecutionManifest
    producer_receipt: ProducerReceipt


@dataclass
class EvidenceReservation:
    """A held safe output root and unpublished staging directory.

    The reservation is made before network use.  A final package therefore
    cannot replace an existing evidence directory after requests have run.
    """

    destination: Path
    parent: SafeDirFD
    stage: SafeDirFD
    stage_name: str
    published: bool = False

    @classmethod
    def reserve(cls, destination: Path) -> EvidenceReservation:
        selected = destination.absolute()
        if not _SAFE_NAME.fullmatch(selected.name):
            raise ExecutionPackageError("evidence destination name is invalid")
        try:
            parent = SafeDirFD.open(selected.parent)
        except (OSError, SafeDirFSError):
            raise ExecutionPackageError(
                "evidence parent is unavailable or unsafe"
            ) from None
        return cls._reserve_in_held_parent(
            destination=selected, parent=parent, destination_name=selected.name
        )

    @classmethod
    def reserve_in_parent(
        cls, parent: SafeDirFD, destination_name: str
    ) -> EvidenceReservation:
        """Reserve under an already-held root without reopening its pathname.

        The reservation owns a duplicate descriptor, so its caller can retain
        the original descriptor for an independent visible-path race check.
        All staging and publication operations remain relative to the held
        directory rather than a same-UID-replaceable pathname.
        """

        if not _SAFE_NAME.fullmatch(destination_name):
            raise ExecutionPackageError("evidence destination name is invalid")
        try:
            parent.assert_open()
            duplicate = SafeDirFD.from_inherited_fd(parent.fd)
        except (OSError, SafeDirFSError):
            raise ExecutionPackageError(
                "evidence parent is unavailable or unsafe"
            ) from None
        return cls._reserve_in_held_parent(
            destination=parent.path / destination_name,
            parent=duplicate,
            destination_name=destination_name,
        )

    @classmethod
    def _reserve_in_held_parent(
        cls,
        *,
        destination: Path,
        parent: SafeDirFD,
        destination_name: str,
    ) -> EvidenceReservation:
        stage_name = f".routing-execution-stage-{secrets.token_hex(16)}"
        stage_descriptor: int | None = None
        stage: SafeDirFD | None = None
        try:
            try:
                os.stat(destination_name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ExecutionPackageError("evidence destination already exists")
            os.mkdir(stage_name, 0o700, dir_fd=parent.fd)
            stage_descriptor = os.open(
                stage_name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent.fd,
            )
            stage = SafeDirFD.from_inherited_fd(stage_descriptor)
            os.close(stage_descriptor)
            stage_descriptor = None
            try:
                os.stat(destination_name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                return cls(destination, parent, stage, stage_name)
            raise ExecutionPackageError("evidence destination already exists")
        except BaseException:
            if stage_descriptor is not None:
                os.close(stage_descriptor)
            if stage is not None:
                stage.close()
            parent.close()
            raise

    def close(self) -> None:
        self.stage.close()
        self.parent.close()

    def _write(self, filename: str, content: bytes) -> None:
        if filename not in _EXPECTED_FILES or not isinstance(content, bytes):
            raise ExecutionPackageError("evidence artifact inventory is invalid")
        descriptor: int | None = None
        try:
            descriptor = self.stage.open_child(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode=0o600,
            )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            self.stage.validated_regular_child(filename, descriptor=descriptor)
        except (OSError, SafeDirFSError):
            raise ExecutionPackageError("evidence staging write failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def publish(
        self, payloads: Mapping[str, bytes], manifest: IntegrityManifest
    ) -> SealedPackage:
        if set(payloads) != set(_PAYLOAD_FILES):
            raise ExecutionPackageError("evidence payload inventory is invalid")
        manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
        for filename in _PAYLOAD_FILES:
            self._write(filename, payloads[filename])
        self._write(_INTEGRITY_FILE, manifest_bytes)
        retained = package_digest(manifest_bytes)
        try:
            self.stage.assert_open()
            os.fchmod(self.stage.fd, 0o500)
            self.stage.fsync()
            self.parent.assert_open()
            try:
                os.stat(
                    self.destination.name, dir_fd=self.parent.fd, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                raise ExecutionPackageError("evidence destination already exists")
            stage_stat = os.stat(
                self.stage_name, dir_fd=self.parent.fd, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(stage_stat.st_mode)
                or stage_stat.st_dev != self.stage.device
                or stage_stat.st_ino != self.stage.inode
            ):
                raise ExecutionPackageError("evidence staging identity changed")
            _rename_no_replace_at(self.parent, self.stage_name, self.destination.name)
            self.parent.fsync()
            visible_parent = SafeDirFD.open(self.destination.parent)
            try:
                if (
                    visible_parent.device != self.parent.device
                    or visible_parent.inode != self.parent.inode
                ):
                    raise ExecutionPackageError("evidence parent identity changed")
            finally:
                visible_parent.close()
            self.published = True
        except ExecutionPackageError:
            raise
        except (OSError, SafeDirFSError):
            raise ExecutionPackageError("evidence publication failed closed") from None
        finally:
            self.close()
        verify_execution_package(
            self.destination, expected_digest=retained, require_immutable=True
        )
        return SealedPackage(path=self.destination, retained_digest=retained)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short evidence write")
        view = view[written:]


def make_integrity_manifest(
    execution_id: ExecutionId, payloads: Mapping[str, bytes]
) -> IntegrityManifest:
    """Make the fixed outer hash list without a digest cycle."""

    return IntegrityManifest(
        schema_version="inferdrome.routing-execution-integrity-manifest.v1",
        execution_id=execution_id,
        hash_algorithm="sha256",
        path_ordering="normalized_posix_ascending_v1",
        entries=tuple(
            ArtifactHashEntry(
                path=filename,  # type: ignore[arg-type]
                sha256=sha256_digest(payloads[filename]),
                size_bytes=len(payloads[filename]),
            )
            for filename in _PAYLOAD_FILES
        ),
    )


@dataclass(frozen=True)
class _ScannedFile:
    name: str
    identity: tuple[int, ...]


@dataclass
class _PackageScan:
    """A package directory held by descriptor throughout one verification pass."""

    root: SafeDirFD
    files: dict[str, _ScannedFile]


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _scan(root: Path, *, require_immutable: bool) -> _PackageScan:
    selected = root.absolute()
    held: SafeDirFD | None = None
    try:
        held = SafeDirFD.open(selected)
        root_stat = os.fstat(held.fd)
        if require_immutable and stat.S_IMODE(root_stat.st_mode) & 0o222:
            raise VerificationError("routing execution package root is writable")
        entries = sorted(os.listdir(held.fd))
        if len(entries) != len(_EXPECTED_FILES) or set(entries) != _EXPECTED_FILES:
            raise VerificationError("routing execution package inventory is not closed")
        total = 0
        scanned: dict[str, _ScannedFile] = {}
        for name in entries:
            metadata = held.validated_regular_child(name)
            if metadata.st_size > _MAX_FILE_BYTES or (
                require_immutable and stat.S_IMODE(metadata.st_mode) & 0o222
            ):
                raise VerificationError("routing execution package entry is unsafe")
            total += metadata.st_size
            if total > _MAX_TOTAL_BYTES:
                raise VerificationError("routing execution package exceeds its bound")
            scanned[name] = _ScannedFile(name, _identity(metadata))
        return _PackageScan(root=held, files=scanned)
    except VerificationError:
        if held is not None:
            held.close()
        raise
    except (OSError, SafeDirFSError):
        if held is not None:
            held.close()
        raise VerificationError("routing execution package is unavailable") from None


def _read(scan: _PackageScan, scanned: _ScannedFile) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = scan.root.open_child(scanned.name, os.O_RDONLY)
        before = scan.root.validated_regular_child(scanned.name, descriptor=descriptor)
        if _identity(before) != scanned.identity:
            raise VerificationError("routing execution package changed during read")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise VerificationError("routing execution package was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise VerificationError("routing execution package grew during read")
        after = scan.root.validated_regular_child(scanned.name, descriptor=descriptor)
        if _identity(after) != scanned.identity:
            raise VerificationError("routing execution package changed during read")
        return b"".join(chunks)
    except (OSError, SafeDirFSError):
        raise VerificationError("routing execution package cannot be read") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _json_value(content: bytes, *, label: str) -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise VerificationError(f"{label} is not UTF-8") from None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise VerificationError(f"{label} has duplicate JSON keys")
            result[key] = value
        return result

    def constants(_: str) -> None:
        raise VerificationError(f"{label} has a non-finite number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constants)
    except VerificationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise VerificationError(f"{label} is invalid JSON") from None


def _parse[ModelT: BaseModel](
    content: bytes, model_type: type[ModelT], *, label: str
) -> ModelT:
    _json_value(content, label=label)
    try:
        model = model_type.model_validate_json(content)
    except ValidationError:
        raise VerificationError(f"{label} violates its contract") from None
    if canonical_json_bytes(model.model_dump(mode="json")) != content:
        raise VerificationError(f"{label} is not canonical JSON")
    return model


def _assert_redacted(value: object) -> None:
    if isinstance(value, dict):
        if any(key in _FORBIDDEN_KEYS for key in value):
            raise VerificationError(
                "routing execution package contains a forbidden field"
            )
        for child in value.values():
            _assert_redacted(child)
    elif isinstance(value, list):
        for child in value:
            _assert_redacted(child)


def _terminal_population(
    receipt: ProducerReceipt,
) -> dict[str, Mapping[TerminalStatus, int]]:
    terminals_by_trial: dict[str, list[TerminalStatus]] = {}
    for terminal in receipt.terminal_outcomes:
        terminals_by_trial.setdefault(terminal.trial_id, []).append(terminal.status)
    statuses: tuple[TerminalStatus, ...] = (
        "SUCCEEDED",
        "TIMED_OUT",
        "FAILED",
        "CANCELLED",
        "NO_SAFE_ROUTE",
    )
    population: dict[str, Mapping[TerminalStatus, int]] = {}
    for summary in receipt.trial_summaries:
        counts: dict[TerminalStatus, int] = {
            status: terminals_by_trial.get(summary.trial_id, []).count(status)
            for status in statuses
        }
        if counts != summary.terminal_population:
            raise VerificationError("routing execution terminal population disagrees")
        population[summary.trial_id] = MappingProxyType(counts)
    return population


def _verify_semantics(
    input_transfer: InputTransferReceipt,
    manifest: ExecutionManifest,
    receipt: ProducerReceipt,
    *,
    manifest_bytes: bytes,
    transfer_bytes: bytes,
) -> Mapping[str, Mapping[TerminalStatus, int]]:
    if input_transfer.config_sha256 != manifest.config_sha256:
        raise VerificationError("input transfer config binding disagrees")
    if manifest.input_transfer_receipt_sha256 != sha256_digest(transfer_bytes):
        raise VerificationError("executed manifest input transfer binding disagrees")
    if receipt.executed_manifest_sha256 != sha256_digest(manifest_bytes):
        raise VerificationError("producer receipt manifest binding disagrees")
    if receipt.input_transfer_receipt_sha256 != sha256_digest(transfer_bytes):
        raise VerificationError("producer receipt transfer binding disagrees")
    if (
        input_transfer.selected_workload_sha256
        != manifest.workload.selected_workload_sha256
    ):
        raise VerificationError("workload binding disagrees")
    if len(receipt.route_decisions) != manifest.planned_terminal_denominator:
        raise VerificationError("route decision denominator disagrees")
    if len(receipt.terminal_outcomes) != manifest.planned_terminal_denominator:
        raise VerificationError("terminal denominator disagrees")
    summaries = {summary.trial_id: summary for summary in receipt.trial_summaries}
    if len(summaries) != 3:
        raise VerificationError("trial summary inventory is invalid")
    if {row.trial_id for row in receipt.reset_receipts} != set(summaries):
        raise VerificationError("reset receipt inventory is invalid")
    if {row.trial_id for row in receipt.fault_receipts} != set(summaries):
        raise VerificationError("fault receipt inventory is invalid")
    decision_keys = {
        (row.trial_id, row.request_id, row.sequence_index): row
        for row in receipt.route_decisions
    }
    if len(decision_keys) != 18:
        raise VerificationError("route decision identities are not unique")
    terminal_keys = {
        (row.trial_id, row.request_id, row.sequence_index): row
        for row in receipt.terminal_outcomes
    }
    if len(terminal_keys) != 18 or set(terminal_keys) != set(decision_keys):
        raise VerificationError("decision and terminal populations are not bijective")
    for key, decision in decision_keys.items():
        terminal = terminal_keys[key]
        if (
            decision.decision_id != terminal.decision_id
            or decision.terminal_outcome_id != terminal.terminal_outcome_id
            or decision.selected_endpoint_id != terminal.selected_endpoint_id
        ):
            raise VerificationError("route decision and terminal receipt disagree")
        if decision.selected_endpoint_id is None and terminal.status != "NO_SAFE_ROUTE":
            raise VerificationError("undispatched decision has an invalid terminal")
        if (
            decision.selected_endpoint_id is not None
            and terminal.status == "NO_SAFE_ROUTE"
        ):
            raise VerificationError("dispatched decision has an invalid terminal")
    expected_telemetry = 3 * 6 * 2 * 4
    if len(receipt.telemetry_observations) != expected_telemetry:
        raise VerificationError("telemetry population is incomplete")
    telemetry_keys = {
        (row.trial_id, row.request_id, row.sequence_index, row.endpoint_id, row.signal)
        for row in receipt.telemetry_observations
    }
    if len(telemetry_keys) != expected_telemetry:
        raise VerificationError("telemetry identities are not unique")
    for trial_id, request_id, sequence_index in decision_keys:
        for endpoint_id in ("endpoint-a", "endpoint-b"):
            for signal in ("HEALTH", "LOAD", "GPU_DCGM", "KV_CACHE"):
                if (
                    trial_id,
                    request_id,
                    sequence_index,
                    endpoint_id,
                    signal,
                ) not in telemetry_keys:
                    raise VerificationError("decision lacks a complete telemetry state")
    return _terminal_population(receipt)


def verify_execution_package(
    package_path: Path,
    *,
    expected_digest: str | None = None,
    require_immutable: bool = True,
) -> VerifiedExecutionPackage:
    """Offline verify an immutable, closed PR-B evidence snapshot.

    This function opens only package files.  It neither imports the executor
    nor constructs a transport, so callers can use it as an independent replay
    boundary before displaying or relying on a package.
    """

    initial = _scan(package_path, require_immutable=require_immutable)
    try:
        content = {
            name: _read(initial, scanned) for name, scanned in initial.files.items()
        }
        for marker in _FORBIDDEN_BYTE_MARKERS:
            if any(marker in value.lower() for value in content.values()):
                raise VerificationError(
                    "routing execution package contains sensitive bytes"
                )
        input_transfer = _parse(
            content["input-transfer-receipt.json"],
            InputTransferReceipt,
            label="routing execution input transfer receipt",
        )
        manifest_content = content["executed-manifest.json"]
        _json_value(manifest_content, label="routing execution executed manifest")
        try:
            manifest = MANIFEST_ADAPTER.validate_json(manifest_content)
        except ValidationError:
            raise VerificationError("executed manifest violates its contract") from None
        if canonical_json_bytes(manifest.model_dump(mode="json")) != manifest_content:
            raise VerificationError("executed manifest is not canonical JSON")
        receipt = _parse(
            content["producer-receipt.json"],
            ProducerReceipt,
            label="routing execution producer receipt",
        )
        integrity = _parse(
            content[_INTEGRITY_FILE],
            IntegrityManifest,
            label="routing execution integrity manifest",
        )
        for value in (
            input_transfer.model_dump(mode="json"),
            manifest.model_dump(mode="json"),
            receipt.model_dump(mode="json"),
            integrity.model_dump(mode="json"),
        ):
            _assert_redacted(value)
        if integrity.execution_id != manifest.execution_id:
            raise VerificationError("integrity manifest execution binding disagrees")
        for entry in integrity.entries:
            content_bytes = content[entry.path]
            if entry.size_bytes != len(content_bytes) or entry.sha256 != sha256_digest(
                content_bytes
            ):
                raise VerificationError("integrity manifest artifact hash disagrees")
        retained = package_digest(content[_INTEGRITY_FILE])
        if expected_digest is not None and expected_digest != retained:
            raise VerificationError("routing execution package digest disagrees")
        population = _verify_semantics(
            input_transfer,
            manifest,
            receipt,
            manifest_bytes=content["executed-manifest.json"],
            transfer_bytes=content["input-transfer-receipt.json"],
        )
        try:
            verify_replay(manifest, receipt)
        except ReplayVerificationError:
            raise VerificationError(
                "routing execution semantic replay disagrees"
            ) from None
        final = _scan(package_path, require_immutable=require_immutable)
        try:
            if (
                final.root.device != initial.root.device
                or final.root.inode != initial.root.inode
                or final.files != initial.files
            ):
                raise VerificationError(
                    "routing execution package changed during verification"
                )
        finally:
            final.root.close()
        return VerifiedExecutionPackage(
            report=VerificationReport(
                path=package_path.absolute(),
                retained_digest=retained,
                execution_id=manifest.execution_id,
                terminal_population=MappingProxyType(population),
            ),
            input_transfer=input_transfer,
            executed_manifest=manifest,
            producer_receipt=receipt,
        )
    finally:
        initial.root.close()
