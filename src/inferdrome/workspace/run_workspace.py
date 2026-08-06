"""Atomic run reservation, frozen inputs, and crash-readable state events."""

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from inferdrome.domain.base import FrozenModel
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.ids import RelativeArtifactPath, RunId, Sha256Digest
from inferdrome.domain.states import (
    IntegrityStatus,
    RunState,
    validate_run_transition,
)
from inferdrome.errors import WorkspaceError
from inferdrome.resolution.resolver import ResolutionResult

_CONTROL_DIRECTORY = "control"
_INPUT_DIRECTORY = "inputs"
_MAX_CONTROL_FILE_BYTES = 1_048_576


class FrozenInputDescriptor(FrozenModel):
    path: RelativeArtifactPath
    size_bytes: Annotated[int, Field(strict=True, ge=0)]
    sha256: Sha256Digest


class ResolutionMetadata(FrozenModel):
    schema_version: Literal["inferdrome.resolution-metadata.v1"]
    run_id: RunId
    source_spec_digest: Sha256Digest
    execution_fingerprint: Sha256Digest
    request_plan_digest: Sha256Digest
    frozen_inputs: Annotated[
        tuple[FrozenInputDescriptor, ...], Field(min_length=4, max_length=4)
    ]

    @model_validator(mode="after")
    def frozen_input_paths_are_exact(self) -> "ResolutionMetadata":
        expected = (
            "inputs/experiment.original.yaml",
            "inputs/workload.source.jsonl",
            "inputs/experiment.resolved.json",
            "inputs/request-plan.json",
        )
        if tuple(item.path for item in self.frozen_inputs) != expected:
            raise ValueError("resolution metadata has an unexpected input set")
        return self


class WorkspaceStateRecord(FrozenModel):
    schema_version: Literal["inferdrome.workspace-state.v1"]
    run_id: RunId
    sequence_index: Annotated[int, Field(strict=True, ge=0)]
    previous_state: RunState | None
    state: RunState
    integrity_status: IntegrityStatus
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_initial_state(self) -> "WorkspaceStateRecord":
        if self.sequence_index == 0:
            if self.previous_state is not None or self.state is not RunState.CREATED:
                raise ValueError("initial workspace state must be CREATED")
        elif self.previous_state is None:
            raise ValueError("non-initial workspace state requires a predecessor")
        if self.state is RunState.COMPLETE:
            if self.integrity_status is not IntegrityStatus.VALID:
                raise ValueError("complete state requires valid integrity")
        elif self.integrity_status is IntegrityStatus.VALID:
            raise ValueError("valid integrity is reserved for complete state")
        return self


def _tagged_sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _canonical_model_bytes(model: FrozenModel) -> bytes:
    return canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_new(path: Path, content: bytes, *, mode: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_file(path: Path, content: bytes) -> None:
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.next")
    try:
        _write_new(temporary_path, content, mode=0o600)
        os.replace(temporary_path, path)
    finally:
        with suppress(FileNotFoundError):
            temporary_path.unlink()


def _read_regular(path: Path, *, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise WorkspaceError("workspace file is missing or unsafe") from None
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > limit:
            raise WorkspaceError("workspace file is not a bounded regular file")
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(1_048_576, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > limit:
            raise WorkspaceError("workspace file exceeds its expected bound")
        return bytes(content)
    finally:
        os.close(descriptor)


class RunWorkspace:
    """Handle for one exclusively reserved run directory."""

    def __init__(self, path: Path, metadata: ResolutionMetadata) -> None:
        self.path = path
        self.metadata = metadata

    @property
    def run_id(self) -> str:
        return self.metadata.run_id

    @property
    def control_directory(self) -> Path:
        return self.path / _CONTROL_DIRECTORY

    @property
    def input_directory(self) -> Path:
        return self.path / _INPUT_DIRECTORY

    @classmethod
    def reserve(
        cls,
        runs_root: Path,
        resolution: ResolutionResult,
        *,
        created_at: datetime | None = None,
    ) -> "RunWorkspace":
        root = runs_root.absolute()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceError("runs root must be a real directory")

        run_path = root / resolution.run_id
        try:
            run_path.mkdir(mode=0o700)
            input_directory = run_path / _INPUT_DIRECTORY
            control_directory = run_path / _CONTROL_DIRECTORY
            events_directory = control_directory / "events"
            input_directory.mkdir(mode=0o700)
            control_directory.mkdir(mode=0o700)
            events_directory.mkdir(mode=0o700)

            input_payloads = (
                ("inputs/experiment.original.yaml", resolution.source_bytes),
                ("inputs/workload.source.jsonl", resolution.workload_bytes),
                ("inputs/experiment.resolved.json", resolution.resolved_spec_bytes),
                ("inputs/request-plan.json", resolution.request_plan_bytes),
            )
            descriptors = []
            for relative_path, content in input_payloads:
                destination = run_path / relative_path
                _write_new(destination, content, mode=0o400)
                descriptors.append(
                    FrozenInputDescriptor(
                        path=relative_path,
                        size_bytes=len(content),
                        sha256=_tagged_sha256(content),
                    )
                )

            metadata = ResolutionMetadata(
                schema_version="inferdrome.resolution-metadata.v1",
                run_id=resolution.run_id,
                source_spec_digest=resolution.source_spec_digest,
                execution_fingerprint=resolution.execution_fingerprint,
                request_plan_digest=resolution.request_plan_digest,
                frozen_inputs=tuple(descriptors),
            )
            _write_new(
                control_directory / "resolution.json",
                _canonical_model_bytes(metadata),
                mode=0o400,
            )
            _write_new(control_directory / "state.lock", b"", mode=0o600)

            initial = WorkspaceStateRecord(
                schema_version="inferdrome.workspace-state.v1",
                run_id=resolution.run_id,
                sequence_index=0,
                previous_state=None,
                state=RunState.CREATED,
                integrity_status=IntegrityStatus.NOT_CHECKED,
                occurred_at=created_at or datetime.now(UTC),
            )
            initial_bytes = _canonical_model_bytes(initial)
            _write_new(
                events_directory / "00000000-CREATED.json",
                initial_bytes,
                mode=0o400,
            )
            _write_new(control_directory / "state.json", initial_bytes, mode=0o600)
        except FileExistsError:
            raise WorkspaceError("run ID is already reserved") from None
        except (OSError, ValidationError) as error:
            raise WorkspaceError("run workspace reservation failed closed") from error

        workspace = cls(run_path, metadata)
        workspace.verify_frozen_inputs()
        return workspace

    @classmethod
    def open(cls, run_path: Path) -> "RunWorkspace":
        absolute_path = run_path.absolute()
        if absolute_path.is_symlink() or not absolute_path.is_dir():
            raise WorkspaceError("run workspace is unavailable or unsafe")
        metadata_bytes = _read_regular(
            absolute_path / _CONTROL_DIRECTORY / "resolution.json",
            limit=_MAX_CONTROL_FILE_BYTES,
        )
        try:
            metadata = ResolutionMetadata.model_validate_json(metadata_bytes)
        except ValidationError:
            raise WorkspaceError("resolution metadata is invalid") from None
        if absolute_path.name != metadata.run_id:
            raise WorkspaceError("workspace directory and run ID do not match")
        workspace = cls(absolute_path, metadata)
        workspace.verify_frozen_inputs()
        workspace.current_state()
        return workspace

    def verify_frozen_inputs(self) -> None:
        for expected in self.metadata.frozen_inputs:
            path = self.path / expected.path
            try:
                file_stat = path.stat(follow_symlinks=False)
            except OSError:
                raise WorkspaceError("frozen input is missing or unsafe") from None
            if not stat.S_ISREG(file_stat.st_mode):
                raise WorkspaceError("frozen input must remain a regular file")
            if stat.S_IMODE(file_stat.st_mode) & 0o222:
                raise WorkspaceError("frozen input became writable")
            content = _read_regular(
                path,
                limit=expected.size_bytes + 1,
            )
            if len(content) != expected.size_bytes:
                raise WorkspaceError("frozen input size changed")
            if _tagged_sha256(content) != expected.sha256:
                raise WorkspaceError("frozen input digest changed")

    def _state_path(self) -> Path:
        return self.control_directory / "state.json"

    def current_state(self) -> WorkspaceStateRecord:
        content = _read_regular(self._state_path(), limit=_MAX_CONTROL_FILE_BYTES)
        try:
            state = WorkspaceStateRecord.model_validate_json(content)
        except ValidationError:
            raise WorkspaceError("workspace state is invalid") from None
        if state.run_id != self.run_id:
            raise WorkspaceError("workspace state belongs to a different run")
        events_directory = self.control_directory / "events"
        try:
            event_paths = sorted(events_directory.iterdir())
        except OSError:
            raise WorkspaceError("workspace state history is unavailable") from None
        if len(event_paths) != state.sequence_index + 1:
            raise WorkspaceError("workspace state history is incomplete")

        previous: WorkspaceStateRecord | None = None
        latest_content = b""
        for expected_index, event_path in enumerate(event_paths):
            event_content = _read_regular(event_path, limit=_MAX_CONTROL_FILE_BYTES)
            try:
                event = WorkspaceStateRecord.model_validate_json(event_content)
            except ValidationError:
                raise WorkspaceError("workspace state event is invalid") from None
            expected_name = f"{expected_index:08d}-{event.state.value}.json"
            invalid_identity = (
                event_path.name != expected_name
                or event.sequence_index != expected_index
            )
            if invalid_identity:
                raise WorkspaceError("workspace state event ordering is invalid")
            if event.run_id != self.run_id:
                raise WorkspaceError("workspace state event belongs to another run")
            if event_content != _canonical_model_bytes(event):
                raise WorkspaceError("workspace state event is not canonical")

            if previous is not None:
                if event.previous_state is not previous.state:
                    raise WorkspaceError("workspace state predecessor is invalid")
                try:
                    validate_run_transition(previous.state, event.state)
                except ValueError:
                    raise WorkspaceError("workspace state history is invalid") from None
                if event.occurred_at < previous.occurred_at:
                    raise WorkspaceError("workspace state history moved backward")
            previous = event
            latest_content = event_content

        if previous != state or latest_content != content:
            raise WorkspaceError("workspace state snapshot disagrees with its event")
        return state

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        lock_path = self.control_directory / "state.lock"
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            raise WorkspaceError("workspace state lock is unavailable") from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def transition(
        self,
        target: RunState,
        *,
        occurred_at: datetime | None = None,
        integrity_status: IntegrityStatus = IntegrityStatus.NOT_CHECKED,
    ) -> WorkspaceStateRecord:
        with self._state_lock():
            current = self.current_state()
            self.verify_frozen_inputs()
            try:
                validate_run_transition(current.state, target)
            except ValueError as error:
                raise WorkspaceError(str(error)) from None

            if target is RunState.COMPLETE:
                if integrity_status is not IntegrityStatus.VALID:
                    raise WorkspaceError(
                        "COMPLETE requires successful offline integrity verification"
                    )
            elif integrity_status is IntegrityStatus.VALID:
                raise WorkspaceError("VALID integrity is reserved for COMPLETE")

            event_time = occurred_at or datetime.now(UTC)
            if event_time.tzinfo is None or event_time.utcoffset() is None:
                raise WorkspaceError("state timestamp must include a UTC offset")
            if event_time < current.occurred_at:
                raise WorkspaceError("state timestamp cannot move backward")

            next_state = WorkspaceStateRecord(
                schema_version="inferdrome.workspace-state.v1",
                run_id=self.run_id,
                sequence_index=current.sequence_index + 1,
                previous_state=current.state,
                state=target,
                integrity_status=integrity_status,
                occurred_at=event_time,
            )
            content = _canonical_model_bytes(next_state)
            event_path = (
                self.control_directory
                / "events"
                / f"{next_state.sequence_index:08d}-{target.value}.json"
            )
            try:
                _write_new(event_path, content, mode=0o400)
                _replace_file(self._state_path(), content)
            except (FileExistsError, OSError) as error:
                raise WorkspaceError("state transition persistence failed") from error

            if target is RunState.COMPLETE:
                self._state_path().chmod(0o400)
            return next_state
