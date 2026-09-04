"""Capture and independently verify the additive v0.3 qualification overlay.

The sealed R1 routing package remains the only producer of request-level
observations, decisions, terminals, faults, and reset receipts. This module
adds one immutable descriptor that names exactly the qualified source package
without copying or pooling those records.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from inferdrome.errors import VerificationError
from inferdrome.routing_campaign import (
    RoutingCampaignError,
    SealedCampaign,
    VerifiedCampaign,
    load_verified_campaign,
    run_campaign,
)
from inferdrome.routing_campaign.canonical import (
    canonical_json_bytes,
    canonical_jsonl_bytes,
    sha256_digest,
)
from inferdrome.routing_qualification.contracts import (
    QualifiedTrial,
    SourceInputDigests,
    StaleTelemetryFaultTimeline,
    StaleTelemetryQualification,
    TerminalPopulation,
)

QUALIFICATION_ARTIFACT_ID = "stale-telemetry-qualification-v1"
QUALIFICATION_FILENAME = "qualification.json"
_QUALIFICATION_DIGEST_PREFIX = b"inferdrome:stale-telemetry-qualification-v1\0"
MAX_QUALIFICATION_BYTES = 524_288
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 1
_QUALIFICATION_STAGE_PREFIX = ".inferdrome-qualification-stage-"


class StaleTelemetryQualificationError(ValueError):
    """A qualification descriptor cannot be bound to a verified source package."""


@dataclass(frozen=True)
class CapturedQualification:
    """One canonical descriptor plus its externally retainable digest."""

    descriptor: StaleTelemetryQualification
    canonical_bytes: bytes
    retained_digest: str


@dataclass(frozen=True)
class SealedQualification:
    """One immutable descriptor directory published without replacement."""

    path: Path
    descriptor_path: Path
    retained_digest: str


@dataclass(frozen=True)
class SealedQualificationCampaign:
    """The separately sealed R1 source package and its qualification overlay."""

    campaign: SealedCampaign
    qualification: SealedQualification


def qualification_digest(content: bytes) -> str:
    """Return the domain-separated digest retained for one descriptor."""

    digest = hashlib.sha256(_QUALIFICATION_DIGEST_PREFIX + content).hexdigest()
    return f"sha256:{digest}"


def _strict_json_value(content: bytes) -> Any:
    if not 1 <= len(content) <= MAX_QUALIFICATION_BYTES:
        raise StaleTelemetryQualificationError(
            "qualification descriptor size is invalid"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise StaleTelemetryQualificationError(
            "qualification descriptor is not UTF-8"
        ) from None

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StaleTelemetryQualificationError(
                    "qualification descriptor contains duplicate JSON keys"
                )
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise StaleTelemetryQualificationError(
            "qualification descriptor contains a non-finite number"
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_pairs,
            parse_constant=reject_constant,
        )
    except StaleTelemetryQualificationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise StaleTelemetryQualificationError(
            "qualification descriptor is not valid JSON"
        ) from None


def _required_safe_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if type(value) is not int or value <= 0:
        raise StaleTelemetryQualificationError(
            f"qualification descriptor reader requires {name}"
        )
    return value


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


def _safe_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | _required_safe_open_flag("O_DIRECTORY")
        | _required_safe_open_flag("O_NOFOLLOW")
        | _required_safe_open_flag("O_NONBLOCK")
    )


def _assert_owner_private_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise StaleTelemetryQualificationError(
            "qualification output root is not owner-private"
        )


@dataclass
class _HeldDirectory:
    """One owner-private directory retained across all publication operations."""

    fd: int
    device: int
    inode: int

    @classmethod
    def from_descriptor(cls, descriptor: int) -> _HeldDirectory:
        metadata = os.fstat(descriptor)
        _assert_owner_private_directory(metadata)
        return cls(
            fd=descriptor,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )

    def assert_open(self) -> None:
        if self.fd < 0:
            raise StaleTelemetryQualificationError(
                "qualification output root descriptor is closed"
            )
        metadata = os.fstat(self.fd)
        _assert_owner_private_directory(metadata)
        if metadata.st_dev != self.device or metadata.st_ino != self.inode:
            raise StaleTelemetryQualificationError(
                "qualification output root descriptor changed"
            )

    def fsync(self) -> None:
        self.assert_open()
        os.fsync(self.fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _absolute_directory_path(path: Path) -> Path:
    if ".." in path.parts:
        raise StaleTelemetryQualificationError(
            "qualification directory path must not contain parent traversal"
        )
    return path if path.is_absolute() else Path.cwd() / path


def _open_held_directory(path: Path, *, create: bool) -> _HeldDirectory:
    """Walk from `/` using held no-follow descriptors and retain the final root.

    Ancestors can be system-owned (for example `/private/tmp`); they are never
    trusted for evidence writes. Each child is opened relative to the held
    parent. Missing components are created `0700`, then the final directory is
    required to be owned by the current user and non-writable by group/other.
    """

    selected = _absolute_directory_path(path)
    flags = _safe_directory_flags()
    descriptor: int | None = None
    try:
        descriptor = os.open(selected.anchor, flags)
        for component in selected.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        held = _HeldDirectory.from_descriptor(descriptor)
        descriptor = None
        return held
    except OSError as error:
        raise StaleTelemetryQualificationError(
            "qualification output root is unavailable"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_directory_chain(path: Path) -> int:
    """Return a duplicate of one held, owner-private root descriptor."""

    held = _open_held_directory(path, create=False)
    try:
        return os.dup(held.fd)
    finally:
        held.close()


def _open_or_create_output_root(output_root: Path) -> _HeldDirectory:
    """Hold one owner-private output root without reopening it by pathname.

    Descriptor publication must not preflight a path and then create or rename
    through that path: an ancestor could be swapped to a symlink in between.
    This creates missing components one at a time through a held, no-follow
    directory descriptor and returns the final root still held open.
    """

    return _open_held_directory(output_root, create=True)


def _open_artifact_directory(root_descriptor: int) -> int:
    try:
        artifact = os.open(
            QUALIFICATION_ARTIFACT_ID,
            _safe_directory_flags(),
            dir_fd=root_descriptor,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StaleTelemetryQualificationError(
                "qualification descriptor directory is not immutable"
            ) from error
        raise StaleTelemetryQualificationError(
            "qualification descriptor directory is unavailable"
        ) from error
    try:
        metadata = os.fstat(artifact)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o500
        ):
            raise StaleTelemetryQualificationError(
                "qualification descriptor directory is not immutable"
            )
    except Exception:
        os.close(artifact)
        raise
    return artifact


def _assert_closed_artifact_inventory(artifact_descriptor: int) -> None:
    try:
        entries = tuple(sorted(os.listdir(artifact_descriptor)))
    except OSError as error:
        raise StaleTelemetryQualificationError(
            "qualification descriptor inventory is unavailable"
        ) from error
    if entries != (QUALIFICATION_FILENAME,):
        raise StaleTelemetryQualificationError(
            "qualification descriptor inventory is not closed"
        )


def _assert_open_path_unchanged(
    qualification_root: Path,
    *,
    root_identity: tuple[int, ...],
    artifact_identity: tuple[int, ...],
) -> None:
    root_descriptor = _open_directory_chain(qualification_root)
    try:
        if _file_identity(os.fstat(root_descriptor)) != root_identity:
            raise StaleTelemetryQualificationError(
                "qualification output root changed during reading"
            )
        artifact_descriptor = _open_artifact_directory(root_descriptor)
        try:
            if _file_identity(os.fstat(artifact_descriptor)) != artifact_identity:
                raise StaleTelemetryQualificationError(
                    "qualification descriptor directory changed during reading"
                )
            _assert_closed_artifact_inventory(artifact_descriptor)
        finally:
            os.close(artifact_descriptor)
    finally:
        os.close(root_descriptor)


def _read_published_descriptor(qualification_root: Path) -> bytes:
    """Read exactly one immutable descriptor without following a replacement."""

    root_descriptor = _open_directory_chain(qualification_root)
    artifact_descriptor: int | None = None
    try:
        root_identity = _file_identity(os.fstat(root_descriptor))
        artifact_descriptor = _open_artifact_directory(root_descriptor)
        artifact_identity = _file_identity(os.fstat(artifact_descriptor))
        _assert_closed_artifact_inventory(artifact_descriptor)
        try:
            initial = os.stat(
                QUALIFICATION_FILENAME,
                dir_fd=artifact_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise StaleTelemetryQualificationError(
                "qualification descriptor is unavailable"
            ) from error
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) != 0o400
            or not 1 <= initial.st_size <= MAX_QUALIFICATION_BYTES
        ):
            raise StaleTelemetryQualificationError("qualification descriptor is unsafe")
        try:
            descriptor = os.open(
                QUALIFICATION_FILENAME,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | _required_safe_open_flag("O_NOFOLLOW")
                | _required_safe_open_flag("O_NONBLOCK"),
                dir_fd=artifact_descriptor,
            )
        except OSError as error:
            raise StaleTelemetryQualificationError(
                "qualification descriptor is unavailable"
            ) from error
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(initial):
                raise StaleTelemetryQualificationError(
                    "qualification descriptor changed before reading"
                )
            chunks: list[bytes] = []
            remaining = initial.st_size + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        except OSError as error:
            raise StaleTelemetryQualificationError(
                "qualification descriptor could not be read"
            ) from error
        finally:
            os.close(descriptor)
        if len(content) != initial.st_size:
            raise StaleTelemetryQualificationError(
                "qualification descriptor size changed"
            )
        try:
            final = os.stat(
                QUALIFICATION_FILENAME,
                dir_fd=artifact_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise StaleTelemetryQualificationError(
                "qualification descriptor disappeared during reading"
            ) from None
        if _file_identity(final) != _file_identity(initial):
            raise StaleTelemetryQualificationError(
                "qualification descriptor changed during reading"
            )
        _assert_closed_artifact_inventory(artifact_descriptor)
        _assert_open_path_unchanged(
            qualification_root,
            root_identity=root_identity,
            artifact_identity=artifact_identity,
        )
        return content
    finally:
        if artifact_descriptor is not None:
            os.close(artifact_descriptor)
        os.close(root_descriptor)


def _raise_no_replace_error() -> None:
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def _rename_no_replace_at(
    parent: _HeldDirectory, source: str, destination: str
) -> None:
    """Atomically publish two child names through one held parent descriptor."""

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
            _raise_no_replace_error()
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
            _raise_no_replace_error()
        return
    raise OSError(errno.ENOTSUP, "no-replace rename is unsupported")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short qualification descriptor write")
        view = view[written:]


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _assert_owner_private_regular(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
    ):
        raise StaleTelemetryQualificationError(
            "qualification staged descriptor is unsafe"
        )


def _validate_regular_child(
    parent: _HeldDirectory,
    name: str,
    *,
    descriptor: int,
) -> os.stat_result:
    parent.assert_open()
    named = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    opened = os.fstat(descriptor)
    _assert_owner_private_regular(named)
    _assert_owner_private_regular(opened)
    if not _same_inode(named, opened):
        raise StaleTelemetryQualificationError(
            "qualification staged descriptor changed"
        )
    return opened


def _open_regular_child_for_write(parent: _HeldDirectory, name: str) -> int:
    parent.assert_open()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | _required_safe_open_flag("O_NOFOLLOW")
            | _required_safe_open_flag("O_NONBLOCK"),
            0o600,
            dir_fd=parent.fd,
        )
        _validate_regular_child(parent, name, descriptor=descriptor)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _assert_artifact_absent(root: _HeldDirectory) -> None:
    root.assert_open()
    try:
        os.stat(
            QUALIFICATION_ARTIFACT_ID,
            dir_fd=root.fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    raise FileExistsError("qualification artifact already exists")


def _publish_descriptor_no_replace(
    *,
    output_root: Path,
    content: bytes,
) -> Path:
    """Publish through held directory descriptors, never a re-opened root path."""

    selected_root = _absolute_directory_path(output_root)
    root = _open_or_create_output_root(selected_root)
    stage: _HeldDirectory | None = None
    try:
        _assert_artifact_absent(root)
        stage_name = f"{_QUALIFICATION_STAGE_PREFIX}{secrets.token_hex(16)}"
        os.mkdir(stage_name, 0o700, dir_fd=root.fd)
        stage_descriptor = os.open(
            stage_name,
            _safe_directory_flags(),
            dir_fd=root.fd,
        )
        try:
            stage = _HeldDirectory.from_descriptor(stage_descriptor)
        except BaseException:
            os.close(stage_descriptor)
            raise

        descriptor: int | None = None
        try:
            descriptor = _open_regular_child_for_write(
                stage,
                QUALIFICATION_FILENAME,
            )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            _validate_regular_child(
                stage,
                QUALIFICATION_FILENAME,
                descriptor=descriptor,
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)

        os.fchmod(stage.fd, 0o500)
        stage.fsync()
        _assert_artifact_absent(root)
        stage_metadata = os.stat(stage_name, dir_fd=root.fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(stage_metadata.st_mode)
            or stage_metadata.st_dev != stage.device
            or stage_metadata.st_ino != stage.inode
        ):
            raise StaleTelemetryQualificationError(
                "qualification staging identity changed"
            )
        _rename_no_replace_at(root, stage_name, QUALIFICATION_ARTIFACT_ID)
        root.fsync()

        visible = _open_held_directory(selected_root, create=False)
        try:
            if visible.device != root.device or visible.inode != root.inode:
                raise StaleTelemetryQualificationError(
                    "qualification output root changed during publication"
                )
        finally:
            visible.close()
        return selected_root / QUALIFICATION_ARTIFACT_ID
    except FileExistsError:
        raise
    except StaleTelemetryQualificationError:
        raise
    except OSError as error:
        raise StaleTelemetryQualificationError(
            "qualification publication failed closed"
        ) from error
    finally:
        if stage is not None:
            stage.close()
        root.close()


def _source_input_digests(verified: VerifiedCampaign) -> SourceInputDigests:
    return SourceInputDigests(
        campaign_plan_sha256=sha256_digest(
            canonical_json_bytes(verified.plan.model_dump(mode="json"))
        ),
        request_trace_sha256=sha256_digest(
            canonical_jsonl_bytes(
                record.model_dump(mode="json") for record in verified.trace
            )
        ),
        fault_schedule_sha256=sha256_digest(
            canonical_json_bytes(verified.fault_schedule.model_dump(mode="json"))
        ),
        trial_plan_sha256=sha256_digest(
            canonical_json_bytes(verified.trial_plan.model_dump(mode="json"))
        ),
    )


def _assert_falsifiable_scenario(verified: VerifiedCampaign) -> None:
    """Require the particular stale-load/fresh-health observation, not a label."""

    fault = verified.fault_schedule
    if fault.load_observer_pause_at_ms != 15 or not fault.health_continues:
        raise StaleTelemetryQualificationError("source fault schedule is not qualified")
    expected_outcomes = {
        "fail_closed_required_load_v1": (
            None,
            "REQUIRED_LOAD_STALE",
            "NO_SAFE_ROUTE",
        ),
        "explicit_fail_open_stale_load_v1": (
            "endpoint-b",
            "STALE_LOAD_FAIL_OPEN",
            "TIMED_OUT",
        ),
        "typed_admissible_state_only_v1": (
            "endpoint-a",
            "HEALTH_ONLY_TIE_BREAK",
            "SUCCEEDED",
        ),
    }
    resets = []
    for trial in verified.trial_plan.trials:
        reset = verified.resets[trial.trial_id]
        resets.append(reset)
        if (
            reset.virtual_time_ms != 0
            or not reset.queue_cleared
            or not reset.load_state_cleared
            or not reset.kv_state_cleared
        ):
            raise StaleTelemetryQualificationError(
                "source trial does not begin from a cold reset"
            )
        decisions = verified.decisions[trial.trial_id]
        terminals = verified.terminals[trial.trial_id]
        observations = verified.observations[trial.trial_id]
        if len(decisions) != 6 or len(terminals) != 6 or len(observations) != 36:
            raise StaleTelemetryQualificationError(
                "source trial does not close the fixed request denominator"
            )
        decision = decisions[2]
        terminal = terminals[2]
        if (
            decision.decision_time_ms != 20
            or terminal.request_id != decision.request_id
        ):
            raise StaleTelemetryQualificationError(
                "source stale-telemetry request is not the fixed vector"
            )
        for candidate in decision.candidates:
            if (
                candidate.health.age_ms != 0
                or candidate.health.admissibility != "ADMISSIBLE"
                or candidate.health.freshness_bound_ms != 5
                or candidate.load.age_ms != 10
                or candidate.load.admissibility != "INADMISSIBLE"
                or candidate.load.freshness_bound_ms != 5
            ):
                raise StaleTelemetryQualificationError(
                    "source does not observe fresh health with stale load"
                )
        selected, fallback, status = expected_outcomes[trial.policy_id]
        if (
            decision.selected_endpoint_id != selected
            or decision.fallback_reason != fallback
            or terminal.status != status
        ):
            raise StaleTelemetryQualificationError(
                "source policy outcome is not the declared qualified outcome"
            )
    if (
        len({reset.endpoint_instance_ids["endpoint-a"] for reset in resets}) != 3
        or len({reset.endpoint_instance_ids["endpoint-b"] for reset in resets}) != 3
    ):
        raise StaleTelemetryQualificationError(
            "source reset identities are not trial-scoped"
        )


def _captured_trials(
    verified: VerifiedCampaign,
) -> tuple[QualifiedTrial, QualifiedTrial, QualifiedTrial]:
    rows: list[QualifiedTrial] = []
    for trial in verified.trial_plan.trials:
        trial_id = trial.trial_id
        summary = verified.summaries[trial_id]
        rows.append(
            QualifiedTrial(
                policy_id=trial.policy_id,
                repetition_index=0,
                trial_id=trial_id,
                request_denominator=6,
                reset_receipt_sha256=sha256_digest(
                    canonical_json_bytes(
                        verified.resets[trial_id].model_dump(mode="json")
                    )
                ),
                state_observations_sha256=sha256_digest(
                    canonical_jsonl_bytes(
                        record.model_dump(mode="json")
                        for record in verified.observations[trial_id]
                    )
                ),
                state_observation_count=36,
                route_decisions_sha256=sha256_digest(
                    canonical_jsonl_bytes(
                        record.model_dump(mode="json")
                        for record in verified.decisions[trial_id]
                    )
                ),
                route_decision_count=6,
                terminal_outcomes_sha256=sha256_digest(
                    canonical_jsonl_bytes(
                        record.model_dump(mode="json")
                        for record in verified.terminals[trial_id]
                    )
                ),
                terminal_outcome_count=6,
                terminal_population=TerminalPopulation(
                    values=dict(summary.terminal_population)
                ),
            )
        )
    if len(rows) != 3:
        raise StaleTelemetryQualificationError(
            "source policy inventory is not the fixed three-trial vector"
        )
    return rows[0], rows[1], rows[2]


def capture_qualification(
    campaign_package: Path,
    *,
    expected_source_digest: str | None = None,
) -> CapturedQualification:
    """Bind the fixed policy-by-repetition declaration to a verified R1 package."""

    try:
        verified = load_verified_campaign(
            campaign_package,
            expected_digest=expected_source_digest,
            require_immutable=True,
        )
    except (RoutingCampaignError, VerificationError) as error:
        raise StaleTelemetryQualificationError(
            "source routing campaign is not independently verified"
        ) from error
    _assert_falsifiable_scenario(verified)
    descriptor = StaleTelemetryQualification(
        schema_version="inferdrome.stale-telemetry-qualification.v1",
        qualification_id="stale-telemetry-qualification-v1",
        source_campaign_id=verified.plan.campaign_id,
        source_execution_mode=verified.plan.execution_mode,
        source_package_retained_digest=verified.report.retained_digest,
        source_inputs=_source_input_digests(verified),
        fault_timeline=StaleTelemetryFaultTimeline(
            load_observer_pause_at_ms=15,
            health_continues=True,
            first_stale_load_fresh_health_decision_time_ms=20,
            health_age_ms=0,
            load_age_ms=10,
            freshness_bound_ms=5,
        ),
        repetitions_per_mode=1,
        population_accounting="SEPARATE_PER_TRIAL_NO_POOLING",
        trials=_captured_trials(verified),
    )
    canonical = canonical_json_bytes(descriptor.model_dump(mode="json"))
    return CapturedQualification(
        descriptor=descriptor,
        canonical_bytes=canonical,
        retained_digest=qualification_digest(canonical),
    )


def publish_qualification(
    captured: CapturedQualification,
    *,
    campaign_package: Path,
    output_root: Path,
) -> SealedQualification:
    """Verify then publish one immutable descriptor without replacement.

    The no-replace identity is deliberately consumed only after the caller's
    in-memory capture has been re-parsed, canonicalized, digested, and bound
    to the supplied independently verified source package.
    """

    prepared = verify_qualification_descriptor(
        captured.canonical_bytes,
        campaign_package=campaign_package,
        expected_descriptor_digest=captured.retained_digest,
    )
    if prepared != captured:
        raise StaleTelemetryQualificationError(
            "qualification capture disagrees with its canonical descriptor"
        )
    destination = _publish_descriptor_no_replace(
        output_root=output_root,
        content=captured.canonical_bytes,
    )
    descriptor_path = destination / QUALIFICATION_FILENAME
    readback = _read_published_descriptor(output_root)
    if readback != captured.canonical_bytes or qualification_digest(readback) != (
        captured.retained_digest
    ):
        raise StaleTelemetryQualificationError(
            "published qualification descriptor disagrees with capture"
        )
    return SealedQualification(
        path=destination,
        descriptor_path=descriptor_path,
        retained_digest=captured.retained_digest,
    )


def verify_qualification_descriptor(
    content: bytes,
    *,
    campaign_package: Path,
    expected_descriptor_digest: str,
) -> CapturedQualification:
    """Fail closed unless canonical bytes exactly rebind the source package."""

    _strict_json_value(content)
    try:
        descriptor = StaleTelemetryQualification.model_validate_json(content)
    except ValidationError as error:
        raise StaleTelemetryQualificationError(
            "qualification descriptor violates its strict contract"
        ) from error
    canonical = canonical_json_bytes(descriptor.model_dump(mode="json"))
    if canonical != content:
        raise StaleTelemetryQualificationError(
            "qualification descriptor is not canonical JSON"
        )
    retained_digest = qualification_digest(canonical)
    if expected_descriptor_digest != retained_digest:
        raise VerificationError("qualification descriptor digest disagrees")
    captured = capture_qualification(
        campaign_package,
        expected_source_digest=descriptor.source_package_retained_digest,
    )
    if captured.descriptor != descriptor:
        raise VerificationError(
            "qualification descriptor disagrees with source campaign"
        )
    return CapturedQualification(
        descriptor=descriptor,
        canonical_bytes=canonical,
        retained_digest=retained_digest,
    )


def verify_qualification(
    qualification_root: Path,
    *,
    campaign_package: Path,
    expected_descriptor_digest: str,
) -> CapturedQualification:
    """Verify a published descriptor and independently replay its source package."""

    return verify_qualification_descriptor(
        _read_published_descriptor(qualification_root),
        campaign_package=campaign_package,
        expected_descriptor_digest=expected_descriptor_digest,
    )


def _qualification_root_is_inside_campaign(
    campaign_output: Path, qualification_output_root: Path
) -> bool:
    campaign = campaign_output.absolute()
    root = qualification_output_root.absolute()
    return root == campaign or campaign in root.parents


def run_qualification(
    *,
    campaign_plan: Path,
    request_trace: Path,
    fault_schedule: Path,
    trial_plan: Path,
    campaign_output: Path,
    qualification_output_root: Path,
) -> SealedQualificationCampaign:
    """Run the fixed R1 campaign, then publish and reread its qualification."""

    if _qualification_root_is_inside_campaign(
        campaign_output, qualification_output_root
    ):
        raise StaleTelemetryQualificationError(
            "qualification output root must not be inside the sealed campaign package"
        )
    campaign = run_campaign(
        campaign_plan,
        request_trace,
        fault_schedule,
        trial_plan,
        campaign_output,
    )
    captured = capture_qualification(
        campaign.path,
        expected_source_digest=campaign.retained_digest,
    )
    qualification = publish_qualification(
        captured,
        campaign_package=campaign.path,
        output_root=qualification_output_root,
    )
    verified = verify_qualification(
        qualification_output_root,
        campaign_package=campaign.path,
        expected_descriptor_digest=qualification.retained_digest,
    )
    if verified != captured:
        raise StaleTelemetryQualificationError(
            "published qualification did not survive independent verification"
        )
    return SealedQualificationCampaign(
        campaign=campaign,
        qualification=qualification,
    )
