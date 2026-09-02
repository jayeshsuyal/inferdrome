"""Sealing and independent offline verification for routing-campaign-v1."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from inferdrome.errors import VerificationError
from inferdrome.immutable import _rename_no_replace
from inferdrome.routing_campaign.canonical import (
    canonical_json_bytes,
    canonical_jsonl_bytes,
    package_digest,
    sha256_digest,
)
from inferdrome.routing_campaign.contracts import (
    ArtifactHashEntry,
    CampaignSummary,
    FaultSchedule,
    IntegrityManifest,
    RequestTraceRecord,
    ResetReceipt,
    RouteDecisionReceipt,
    RoutingCampaignPlan,
    StateObservationRecord,
    TerminalOutcomeReceipt,
    TerminalStatus,
    TrialPlan,
    TrialSummary,
)
from inferdrome.routing_campaign.engine import CampaignExecution, execute_campaign
from inferdrome.routing_campaign.replay import (
    ReplayVerificationError,
    verify_replay,
)

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_FILES = 64
_MAX_DIRECTORIES = 64
_MAX_ENTRIES = 128
_MAX_FILE_BYTES = 8_388_608
_MAX_TOTAL_BYTES = 67_108_864
_MAX_DEPTH = 5


class RoutingCampaignError(ValueError):
    """An R1 source input or sealed-package invariant failed closed."""


@dataclass(frozen=True)
class SealedCampaign:
    """Published package identity returned by the writer."""

    path: Path
    retained_digest: str


@dataclass(frozen=True)
class VerificationReport:
    """Bounded facts established by the offline R1 verifier."""

    path: Path
    retained_digest: str
    trial_count: int
    planned_request_count: int
    trial_ids: tuple[str, ...]
    terminal_populations: tuple[dict[TerminalStatus, int], ...]


@dataclass(frozen=True)
class VerifiedCampaign:
    """Typed R1 records bound to one verified immutable package snapshot.

    This is intentionally a read-only view over the bytes that were parsed,
    hash-checked, replayed, and followed by the verifier's final identity
    scan. Consumers must not re-open package paths after calling this API.
    """

    report: VerificationReport
    plan: RoutingCampaignPlan
    trace: tuple[RequestTraceRecord, ...]
    fault_schedule: FaultSchedule
    trial_plan: TrialPlan
    resets: Mapping[str, ResetReceipt]
    observations: Mapping[str, tuple[StateObservationRecord, ...]]
    decisions: Mapping[str, tuple[RouteDecisionReceipt, ...]]
    terminals: Mapping[str, tuple[TerminalOutcomeReceipt, ...]]
    summaries: Mapping[str, TrialSummary]
    campaign_summary: CampaignSummary


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short routing-campaign write")
        view = view[written:]


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("routing-campaign directory changed type")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_read_only(root: Path) -> None:
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            os.chmod(current / filename, 0o400, follow_symlinks=False)
        for directory_name in directory_names:
            os.chmod(current / directory_name, 0o500, follow_symlinks=False)
    os.chmod(root, 0o500, follow_symlinks=False)


def _strict_json_value(content: bytes, *, label: str) -> Any:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise RoutingCampaignError(f"{label} is not UTF-8") from None

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RoutingCampaignError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise RoutingCampaignError(f"{label} contains a non-finite number")

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_pairs,
            parse_constant=reject_constant,
        )
    except RoutingCampaignError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise RoutingCampaignError(f"{label} is not valid JSON") from None


def _parse_model[ModelT: BaseModel](
    content: bytes, model_type: type[ModelT], *, label: str
) -> ModelT:
    _strict_json_value(content, label=label)
    try:
        model = model_type.model_validate_json(content)
    except ValidationError as error:
        raise RoutingCampaignError(f"{label} violates its strict contract") from error
    if canonical_json_bytes(model.model_dump(mode="json")) != content:
        raise RoutingCampaignError(f"{label} is not canonical JSON")
    return model


def _parse_source_model[ModelT: BaseModel](
    path: Path, model_type: type[ModelT], *, label: str
) -> ModelT:
    content = _read_source_input(path, label=label)
    _strict_json_value(content, label=label)
    try:
        return model_type.model_validate_json(content)
    except ValidationError as error:
        raise RoutingCampaignError(f"{label} violates its strict contract") from error


def _read_source_input(path: Path, *, label: str) -> bytes:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise RoutingCampaignError(f"{label} is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RoutingCampaignError(f"{label} must be one real regular input file")
    if metadata.st_size > _MAX_FILE_BYTES:
        raise RoutingCampaignError(f"{label} exceeds its input size limit")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        content = b""
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise RoutingCampaignError(f"{label} was truncated during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RoutingCampaignError(f"{label} changed during read")
        return content
    finally:
        os.close(descriptor)


def _parse_source_trace(path: Path) -> tuple[RequestTraceRecord, ...]:
    content = _read_source_input(path, label="routing request trace")
    if not content.endswith(b"\n"):
        raise RoutingCampaignError("routing request trace must end with one newline")
    lines = content.splitlines()
    if len(lines) != 6 or any(not line for line in lines):
        raise RoutingCampaignError(
            "routing request trace must contain six non-empty rows"
        )
    records: list[RequestTraceRecord] = []
    for index, line in enumerate(lines):
        _strict_json_value(line, label=f"routing request trace row {index}")
        try:
            records.append(RequestTraceRecord.model_validate_json(line))
        except ValidationError as error:
            raise RoutingCampaignError(
                "routing request trace row violates contract"
            ) from error
    return tuple(records)


def load_campaign_inputs(
    plan_path: Path,
    trace_path: Path,
    fault_schedule_path: Path,
    trial_plan_path: Path,
) -> tuple[
    RoutingCampaignPlan, tuple[RequestTraceRecord, ...], FaultSchedule, TrialPlan
]:
    """Load bounded frozen source inputs without invoking any external service."""

    plan = _parse_source_model(plan_path, RoutingCampaignPlan, label="routing plan")
    trace = _parse_source_trace(trace_path)
    fault_schedule = _parse_source_model(
        fault_schedule_path,
        FaultSchedule,
        label="routing fault schedule",
    )
    trial_plan = _parse_source_model(
        trial_plan_path, TrialPlan, label="routing trial plan"
    )
    return plan, trace, fault_schedule, trial_plan


def _payloads(
    plan: RoutingCampaignPlan,
    trace: tuple[RequestTraceRecord, ...],
    fault_schedule: FaultSchedule,
    trial_plan: TrialPlan,
    execution: CampaignExecution,
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {
        "campaign-plan.json": canonical_json_bytes(plan.model_dump(mode="json")),
        "request-trace.jsonl": canonical_jsonl_bytes(
            record.model_dump(mode="json") for record in trace
        ),
        "fault-schedule.json": canonical_json_bytes(
            fault_schedule.model_dump(mode="json")
        ),
        "trial-plan.json": canonical_json_bytes(trial_plan.model_dump(mode="json")),
        "derived/campaign-summary.json": canonical_json_bytes(
            execution.summary.model_dump(mode="json")
        ),
    }
    for trial in execution.trials:
        root = f"trials/{trial.reset.trial_id}"
        payloads[f"{root}/reset-receipt.json"] = canonical_json_bytes(
            trial.reset.model_dump(mode="json")
        )
        payloads[f"{root}/state-observations.jsonl"] = canonical_jsonl_bytes(
            record.model_dump(mode="json") for record in trial.observations
        )
        payloads[f"{root}/route-decisions.jsonl"] = canonical_jsonl_bytes(
            record.model_dump(mode="json") for record in trial.decisions
        )
        payloads[f"{root}/terminal-outcomes.jsonl"] = canonical_jsonl_bytes(
            record.model_dump(mode="json") for record in trial.terminals
        )
        payloads[f"{root}/summary.json"] = canonical_json_bytes(
            trial.summary.model_dump(mode="json")
        )
    return payloads


def _manifest(
    plan: RoutingCampaignPlan, payloads: dict[str, bytes]
) -> tuple[IntegrityManifest, bytes]:
    entries = tuple(
        ArtifactHashEntry(
            path=path,
            sha256=sha256_digest(content),
            size_bytes=len(content),
        )
        for path, content in sorted(payloads.items())
    )
    manifest = IntegrityManifest(
        schema_version="inferdrome.routing-integrity-manifest.v1",
        campaign_id=plan.campaign_id,
        hash_algorithm="sha256",
        path_ordering="normalized_posix_ascending_v1",
        entries=entries,
    )
    return manifest, canonical_json_bytes(manifest.model_dump(mode="json"))


def _published_path(output_root: Path) -> Path:
    destination = output_root.absolute()
    try:
        destination_stat = destination.lstat()
    except FileNotFoundError:
        return destination
    if stat.S_ISLNK(destination_stat.st_mode):
        raise RoutingCampaignError("routing output path must not be a symlink")
    raise RoutingCampaignError("routing output path already exists")


def _seal_payloads(
    destination: Path, payloads: dict[str, bytes], manifest_bytes: bytes
) -> None:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent_metadata = parent.stat(follow_symlinks=False)
    except OSError:
        raise RoutingCampaignError("routing output parent is unavailable") from None
    if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise RoutingCampaignError("routing output parent must be a real directory")
    staging = parent / f".routing-campaign-stage-{secrets.token_hex(16)}"
    staging.mkdir(mode=0o700)
    try:
        for path, content in payloads.items():
            _write_new(staging / path, content)
        _write_new(staging / "integrity/artifact-hashes.json", manifest_bytes)
        _make_read_only(staging)
        _fsync_directory(staging)
        _fsync_directory(parent)
        _rename_no_replace(staging, destination)
        _fsync_directory(parent)
    except OSError as error:
        raise RoutingCampaignError("routing campaign sealing failed closed") from error


def run_campaign(
    plan_path: Path,
    trace_path: Path,
    fault_schedule_path: Path,
    trial_plan_path: Path,
    output_root: Path,
) -> SealedCampaign:
    """Execute, seal, and independently re-verify one deterministic R1 package."""

    plan, trace, fault_schedule, trial_plan = load_campaign_inputs(
        plan_path,
        trace_path,
        fault_schedule_path,
        trial_plan_path,
    )
    execution = execute_campaign(plan, trace, fault_schedule, trial_plan)
    payloads = _payloads(plan, trace, fault_schedule, trial_plan, execution)
    _, manifest_bytes = _manifest(plan, payloads)
    retained_digest = package_digest(manifest_bytes)
    destination = _published_path(output_root)
    _seal_payloads(destination, payloads, manifest_bytes)
    verify_campaign(
        destination, expected_digest=retained_digest, require_immutable=True
    )
    return SealedCampaign(path=destination, retained_digest=retained_digest)


@dataclass(frozen=True)
class _ScannedFile:
    relative_path: str
    path: Path
    identity: tuple[int, ...]


@dataclass(frozen=True)
class _ScannedDirectory:
    relative_path: str
    path: Path
    identity: tuple[int, ...]


@dataclass(frozen=True)
class _PackageScan:
    root: _ScannedDirectory
    files: dict[str, _ScannedFile]
    directories: dict[str, _ScannedDirectory]
    total_bytes: int


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _safe_relative_path(relative_path: str) -> None:
    parts = relative_path.split("/")
    if (
        not relative_path
        or len(parts) > _MAX_DEPTH
        or any(not _SAFE_COMPONENT.fullmatch(part) for part in parts)
    ):
        raise VerificationError("routing package has an unsafe relative path")


def _scan_package(root: Path, *, require_immutable: bool) -> _PackageScan:
    root = root.absolute()
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError:
        raise VerificationError("routing package root is unavailable") from None
    if root.is_symlink() or not stat.S_ISDIR(root_stat.st_mode):
        raise VerificationError("routing package root must be a real directory")
    if require_immutable and stat.S_IMODE(root_stat.st_mode) & 0o222:
        raise VerificationError("routing package root is writable")
    scan_root = _ScannedDirectory(
        relative_path="",
        path=root,
        identity=_directory_identity(root_stat),
    )
    files: dict[str, _ScannedFile] = {}
    directories: dict[str, _ScannedDirectory] = {}
    total_bytes = 0
    scanned_entries = 0
    pending = [(root, "", 0)]
    casefold_paths: set[str] = set()
    while pending:
        directory, relative_directory, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise VerificationError("routing package exceeds directory-depth limit")
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if scanned_entries >= _MAX_ENTRIES:
                        raise VerificationError(
                            "routing package exceeds its bounded inventory"
                        )
                    entries.append(entry)
                    scanned_entries += 1
                entries.sort(key=lambda entry: entry.name)
        except OSError:
            raise VerificationError(
                "routing package directory cannot be scanned"
            ) from None
        for entry in entries:
            relative_path = (
                f"{relative_directory}/{entry.name}"
                if relative_directory
                else entry.name
            )
            _safe_relative_path(relative_path)
            if relative_path.casefold() in casefold_paths:
                raise VerificationError(
                    "routing package paths collide case-insensitively"
                )
            casefold_paths.add(relative_path.casefold())
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise VerificationError(
                    "routing package entry cannot be inspected"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise VerificationError("routing package symlinks are forbidden")
            if stat.S_ISDIR(metadata.st_mode):
                if require_immutable and stat.S_IMODE(metadata.st_mode) & 0o222:
                    raise VerificationError("routing package directory is writable")
                if len(directories) >= _MAX_DIRECTORIES:
                    raise VerificationError(
                        "routing package exceeds its bounded inventory"
                    )
                directories[relative_path] = _ScannedDirectory(
                    relative_path=relative_path,
                    path=Path(entry.path),
                    identity=_directory_identity(metadata),
                )
                pending.append((Path(entry.path), relative_path, depth + 1))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise VerificationError("routing package entries must be regular files")
            if metadata.st_nlink != 1:
                raise VerificationError("routing package hard links are forbidden")
            if require_immutable and stat.S_IMODE(metadata.st_mode) & 0o222:
                raise VerificationError("routing package file is writable")
            if metadata.st_size > _MAX_FILE_BYTES:
                raise VerificationError("routing package file exceeds size limit")
            total_bytes += metadata.st_size
            if total_bytes > _MAX_TOTAL_BYTES or len(files) >= _MAX_FILES:
                raise VerificationError("routing package exceeds its bounded inventory")
            files[relative_path] = _ScannedFile(
                relative_path,
                Path(entry.path),
                _file_identity(metadata),
            )
    return _PackageScan(
        root=scan_root,
        files=files,
        directories=directories,
        total_bytes=total_bytes,
    )


def _read_scanned_file(scanned: _ScannedFile) -> bytes:
    descriptor = os.open(
        scanned.path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if _file_identity(before) != scanned.identity:
            raise VerificationError("routing package file changed during verification")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise VerificationError("routing package file was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise VerificationError("routing package file grew during verification")
        after = os.fstat(descriptor)
        if _file_identity(after) != scanned.identity:
            raise VerificationError("routing package file changed during verification")
        try:
            path_metadata = scanned.path.lstat()
        except OSError:
            raise VerificationError(
                "routing package file changed during verification"
            ) from None
        if _file_identity(path_metadata) != scanned.identity:
            raise VerificationError("routing package file changed during verification")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_jsonl[ModelT: BaseModel](
    content: bytes, model_type: type[ModelT], *, label: str, expected_count: int
) -> tuple[ModelT, ...]:
    if not content.endswith(b"\n"):
        raise VerificationError(f"{label} must end with one newline")
    rows = content[:-1].split(b"\n")
    if len(rows) != expected_count or any(not row for row in rows):
        raise VerificationError(f"{label} has an unexpected record count")
    parsed: list[ModelT] = []
    for index, row in enumerate(rows):
        _strict_json_value(row, label=f"{label} row {index}")
        try:
            model = model_type.model_validate_json(row)
        except ValidationError as error:
            raise VerificationError(f"{label} row violates its contract") from error
        if canonical_json_bytes(model.model_dump(mode="json")) != row:
            raise VerificationError(f"{label} row is not canonical JSON")
        parsed.append(model)
    return tuple(parsed)


def _required_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return directories


def _assert_scan_unchanged(initial: _PackageScan, final: _PackageScan) -> None:
    """Reject a same-path replacement after the first no-follow package scan."""

    if (
        final.root != initial.root
        or final.files != initial.files
        or final.directories != initial.directories
        or final.total_bytes != initial.total_bytes
    ):
        raise VerificationError("routing package changed during verification")


def load_verified_campaign(
    package_path: Path,
    *,
    expected_digest: str | None = None,
    require_immutable: bool = True,
) -> VerifiedCampaign:
    """Return records from one canonical, replay-verified immutable snapshot.

    The returned models originate from the same no-follow file reads whose
    hashes and receipt relationships were checked below.  A final inventory
    identity scan happens before this function returns, so a consumer can
    safely project these in-memory values without reopening package paths.
    """

    initial_scan = _scan_package(package_path, require_immutable=require_immutable)
    files = initial_scan.files
    directories = initial_scan.directories
    required_top_level = {
        "campaign-plan.json",
        "request-trace.jsonl",
        "fault-schedule.json",
        "trial-plan.json",
        "integrity/artifact-hashes.json",
        "derived/campaign-summary.json",
    }
    if not required_top_level.issubset(files):
        raise VerificationError("routing package misses a required top-level artifact")
    content = {path: _read_scanned_file(scanned) for path, scanned in files.items()}
    try:
        plan = _parse_model(
            content["campaign-plan.json"], RoutingCampaignPlan, label="routing plan"
        )
        fault_schedule = _parse_model(
            content["fault-schedule.json"],
            FaultSchedule,
            label="routing fault schedule",
        )
        trial_plan = _parse_model(
            content["trial-plan.json"], TrialPlan, label="routing trial plan"
        )
        manifest = _parse_model(
            content["integrity/artifact-hashes.json"],
            IntegrityManifest,
            label="routing integrity manifest",
        )
    except RoutingCampaignError as error:
        raise VerificationError(str(error)) from error
    trace = _parse_jsonl(
        content["request-trace.jsonl"],
        RequestTraceRecord,
        label="routing request trace",
        expected_count=6,
    )
    expected_paths = set(required_top_level)
    for trial in trial_plan.trials:
        root = f"trials/{trial.trial_id}"
        expected_paths.update(
            {
                f"{root}/reset-receipt.json",
                f"{root}/state-observations.jsonl",
                f"{root}/route-decisions.jsonl",
                f"{root}/terminal-outcomes.jsonl",
                f"{root}/summary.json",
            }
        )
    if set(files) != expected_paths:
        raise VerificationError("routing package inventory is not closed")
    if set(directories) != _required_directories(expected_paths):
        raise VerificationError("routing package directory inventory is not closed")
    hashed_paths = tuple(entry.path for entry in manifest.entries)
    expected_hashed_paths = tuple(
        sorted(expected_paths - {"integrity/artifact-hashes.json"})
    )
    if hashed_paths != expected_hashed_paths:
        raise VerificationError(
            "routing integrity manifest inventory disagrees with package"
        )
    for entry in manifest.entries:
        artifact = content.get(entry.path)
        if (
            artifact is None
            or entry.size_bytes != len(artifact)
            or entry.sha256 != sha256_digest(artifact)
        ):
            raise VerificationError(
                "routing integrity manifest hash disagrees with artifact"
            )
    retained_digest = package_digest(content["integrity/artifact-hashes.json"])
    if expected_digest is not None and expected_digest != retained_digest:
        raise VerificationError(
            "routing package retained digest disagrees with expectation"
        )

    resets: dict[str, ResetReceipt] = {}
    observations: dict[str, tuple[StateObservationRecord, ...]] = {}
    decisions: dict[str, tuple[RouteDecisionReceipt, ...]] = {}
    terminals: dict[str, tuple[TerminalOutcomeReceipt, ...]] = {}
    summaries: dict[str, TrialSummary] = {}
    for trial in trial_plan.trials:
        root = f"trials/{trial.trial_id}"
        try:
            resets[trial.trial_id] = _parse_model(
                content[f"{root}/reset-receipt.json"],
                ResetReceipt,
                label="routing reset receipt",
            )
            observations[trial.trial_id] = _parse_jsonl(
                content[f"{root}/state-observations.jsonl"],
                StateObservationRecord,
                label="routing state observations",
                expected_count=36,
            )
            decisions[trial.trial_id] = _parse_jsonl(
                content[f"{root}/route-decisions.jsonl"],
                RouteDecisionReceipt,
                label="routing route decisions",
                expected_count=6,
            )
            terminals[trial.trial_id] = _parse_jsonl(
                content[f"{root}/terminal-outcomes.jsonl"],
                TerminalOutcomeReceipt,
                label="routing terminal outcomes",
                expected_count=6,
            )
            summaries[trial.trial_id] = _parse_model(
                content[f"{root}/summary.json"],
                TrialSummary,
                label="routing trial summary",
            )
        except RoutingCampaignError as error:
            raise VerificationError(str(error)) from error
    try:
        campaign_summary = _parse_model(
            content["derived/campaign-summary.json"],
            CampaignSummary,
            label="routing campaign summary",
        )
        verify_replay(
            plan=plan,
            trace=trace,
            fault_schedule=fault_schedule,
            trial_plan=trial_plan,
            resets=resets,
            observations=observations,
            decisions=decisions,
            terminals=terminals,
            summaries=summaries,
            campaign_summary=campaign_summary,
        )
    except (ReplayVerificationError, RoutingCampaignError) as error:
        raise VerificationError(str(error)) from error
    final_scan = _scan_package(package_path, require_immutable=require_immutable)
    _assert_scan_unchanged(initial_scan, final_scan)
    report = VerificationReport(
        path=package_path.absolute(),
        retained_digest=retained_digest,
        trial_count=len(trial_plan.trials),
        planned_request_count=len(trace) * len(trial_plan.trials),
        trial_ids=tuple(
            summary.trial_id for summary in campaign_summary.trial_summaries
        ),
        terminal_populations=tuple(
            dict(summary.terminal_population)
            for summary in campaign_summary.trial_summaries
        ),
    )
    return VerifiedCampaign(
        report=report,
        plan=plan,
        trace=trace,
        fault_schedule=fault_schedule,
        trial_plan=trial_plan,
        resets=MappingProxyType(dict(resets)),
        observations=MappingProxyType(dict(observations)),
        decisions=MappingProxyType(dict(decisions)),
        terminals=MappingProxyType(dict(terminals)),
        summaries=MappingProxyType(dict(summaries)),
        campaign_summary=campaign_summary,
    )


def verify_campaign(
    package_path: Path,
    *,
    expected_digest: str | None = None,
    require_immutable: bool = True,
) -> VerificationReport:
    """Verify a closed R1 package by canonical re-parse and fresh replay only."""

    return load_verified_campaign(
        package_path,
        expected_digest=expected_digest,
        require_immutable=require_immutable,
    ).report
