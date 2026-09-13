"""File-driven, bounded Vast args bootstrap and operator workflow.

All incoming files are untrusted. No provider client, credential, SSH daemon,
model download, or implicit approval is supplied by the guest. The transport
and external deadline guard are separate, explicitly gated operator interfaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import stat
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, model_validator

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_process import (
    GPUUUID,
    MODULES,
    ProviderId,
    SafeOperator,
    VastLaunchReadback,
    VastProcessInput,
    export_package,
    module_digests,
    parse_deadline,
    prepare,
    read_private,
)
from inferdrome.deployment.vast_transfer import BrokerGate
from inferdrome.errors import InferdromeError
from inferdrome.qwen3_campaign import qwen3_model_manifest
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from inferdrome.routing_execution.contracts import (
    Commit,
    Digest,
    ExecutionModel,
    ImageIdentity,
)
from inferdrome.routing_execution.package import verify_execution_package

ROOT = Path("/workspace/vast-bootstrap")
Nonce = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Seconds = Annotated[int, Field(ge=1, le=1800)]
EXPORT_FILES = (
    "executed-manifest.json",
    "input-transfer-receipt.json",
    "producer-receipt.json",
    "integrity-manifest.json",
)


class BootstrapFailure(ValueError):
    """Fixed failure messages only; never echo untrusted or private records."""


class CommandBinding(ExecutionModel):
    run_nonce: Nonce
    intent_sha256: Digest
    execution_deadline_utc: str

    @model_validator(mode="after")
    def deadline(self) -> CommandBinding:
        parse_deadline(self.execution_deadline_utc)
        return self


class TransferPrerequisites(ExecutionModel):
    """Required before create; missing live evidence is an enforced launch gate."""

    broker_contract_evidence_sha256: Digest
    trusted_key_source: Literal[
        "PROVIDER_AUTHENTICATED_CHANNEL", "OPERATOR_OUT_OF_BAND"
    ]
    trusted_key_evidence_sha256: Digest
    uid2000_gid0_compatibility: Literal["OPERATOR_VERIFIED"]
    assertion: Literal["OPERATOR_DECLARED_NOT_PROVIDER_ATTESTED"]

    @model_validator(mode="after")
    def independent_key_evidence(self) -> TransferPrerequisites:
        if self.broker_contract_evidence_sha256 == self.trusted_key_evidence_sha256:
            raise ValueError("separate authenticated key evidence required")
        return self

    def accepts(self, gate: BrokerGate) -> bool:
        return (
            gate.broker_contract_verified is True
            and gate.broker_contract_evidence_sha256
            == self.broker_contract_evidence_sha256
            and gate.trusted_key_source == self.trusted_key_source
            and gate.trusted_key_evidence_sha256 == self.trusted_key_evidence_sha256
            and gate.remote_uid == 2000
            and gate.remote_gid == 0
        )


class LaunchIntent(ExecutionModel):
    schema_version: Literal["inferdrome.vast-launch-intent.v1"]
    run_nonce: Nonce
    source_commit: Commit
    container_image: ImageIdentity
    offer_id: ProviderId
    accountable_operator: SafeOperator
    module_sha256: dict[str, Digest]
    transfer_prerequisites: TransferPrerequisites
    execution_deadline_utc: str
    cleanup_deadline_utc: str
    stage_timeout_seconds: Seconds
    approval_timeout_seconds: Seconds
    retrieval_timeout_seconds: Seconds
    request_timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    readiness_timeout_seconds: Annotated[int, Field(ge=1, le=600)]
    campaign_timeout_seconds: Seconds

    @model_validator(mode="after")
    def bindings(self) -> LaunchIntent:
        execution = parse_deadline(self.execution_deadline_utc)
        cleanup = parse_deadline(self.cleanup_deadline_utc)
        if not 1 <= (cleanup - execution).total_seconds() <= 600:
            raise ValueError("separate finite cleanup reserve required")
        if set(self.module_sha256) != set(MODULES):
            raise ValueError("installed artifact inventory differs")
        return self


class Ready(ExecutionModel):
    schema_version: Literal["inferdrome.vast-bootstrap-ready.v1"]
    run_nonce: Nonce
    intent_sha256: Digest
    execution_deadline_utc: str
    instance_id: ProviderId
    source_commit: Commit
    module_sha256: dict[str, Digest]
    gpu_uuids: tuple[GPUUUID, GPUUUID]
    uid: Literal[2000]
    gid: Literal[0]
    assertion: Literal["LOCAL_OBSERVATIONS_NOT_PROVIDER_ATTESTATION"]

    @model_validator(mode="after")
    def bindings(self) -> Ready:
        parse_deadline(self.execution_deadline_utc)
        if len(set(self.gpu_uuids)) != 2 or set(self.module_sha256) != set(MODULES):
            raise ValueError("bootstrap observation inventory differs")
        return self


class Stage(ExecutionModel):
    schema_version: Literal["inferdrome.vast-bootstrap-stage.v1"]
    run_nonce: Nonce
    intent_sha256: Digest
    launch_readback: VastLaunchReadback


class Approval(ExecutionModel):
    schema_version: Literal["inferdrome.vast-bootstrap-approval.v1"]
    run_nonce: Nonce
    intent_sha256: Digest
    instance_id: ProviderId
    plan_sha256: Digest
    accountable_operator: SafeOperator
    execution_deadline_utc: str
    accept_manual_cleanup_risk: Literal[True]


class Result(ExecutionModel):
    schema_version: Literal["inferdrome.vast-bootstrap-result.v1"]
    run_nonce: Nonce
    intent_sha256: Digest
    instance_id: ProviderId
    plan_sha256: Digest
    retained_digest: Digest
    status: Literal["GUEST_EXPORT_VERIFIED_NOT_YET_RETRIEVED"]
    provider_cleanup: Literal["CLEANUP_UNCONFIRMED"]


def record_bytes(record: ExecutionModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json"))


def record_digest(record: ExecutionModel) -> str:
    return sha256_digest(record_bytes(record))


def write_record(directory: Path, name: str, content: bytes) -> None:
    """Publish fsynced bytes without replacing any prior record."""
    root = SafeDirFD.open(directory)
    temporary = name + ".pending"
    try:
        descriptor = root.open_child(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        # link is atomic and fails if the final name already exists. A reader
        # observing the brief two-link state refuses it rather than trusting it.
        os.link(
            temporary,
            name,
            src_dir_fd=root.fd,
            dst_dir_fd=root.fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=root.fd)
        root.validated_regular_child(name)
        root.fsync()
    finally:
        root.close()


def incoming_bytes(directory: Path, name: str, maximum: int = 131_072) -> bytes:
    """Anchored read of a foreign-owned transfer file; never adopt its ownership."""
    root = SafeDirFD.open(directory)
    descriptor = -1
    try:
        if not name or "/" in name or name in {".", ".."}:
            raise BootstrapFailure("VAST_INBOX_NAME_INVALID")
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root.fd
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
        ):
            raise BootstrapFailure("VAST_INBOX_FILE_INVALID")
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        after = os.fstat(descriptor)
        named = root.stat_child(name)
        fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
        )
        if len(content) != before.st_size or any(
            getattr(before, key) != getattr(after, key)
            or getattr(after, key) != getattr(named, key)
            for key in fields
        ):
            raise BootstrapFailure("VAST_INBOX_CHANGED")
        return bytes(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        root.close()


def read_record[Record: ExecutionModel](
    directory: Path, name: str, model: type[Record]
) -> Record:
    content = incoming_bytes(directory, name)
    parsed = model.model_validate_json(content)
    if record_bytes(parsed) != content:
        raise BootstrapFailure("VAST_RECORD_NOT_CANONICAL")
    return parsed


class Budget:
    """Neither wall clock rollback nor phase completion renews the run budget."""

    def __init__(
        self,
        deadline: str,
        *,
        maximum: float = 7200,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.monotonic, self.now = monotonic, now
        self.utc = parse_deadline(deadline)
        seconds = (self.utc - now()).total_seconds()
        if not 0 < seconds <= maximum:
            raise BootstrapFailure("VAST_BOOTSTRAP_DEADLINE")
        self.end = monotonic() + seconds

    def remaining(self) -> float:
        seconds = min(
            self.end - self.monotonic(), (self.utc - self.now()).total_seconds()
        )
        if seconds <= 0:
            raise BootstrapFailure("VAST_BOOTSTRAP_DEADLINE")
        return seconds

    def child(self, seconds: float) -> Budget:
        if seconds <= 0:
            raise BootstrapFailure("VAST_BOOTSTRAP_PHASE_TIMEOUT")
        child = object.__new__(Budget)
        child.monotonic, child.now, child.utc = self.monotonic, self.now, self.utc
        child.end = min(self.end, self.monotonic() + seconds)
        return child

    def wait_for(self, directory: Path, name: str, seconds: float) -> None:
        end = self.monotonic() + min(seconds, self.remaining())
        while True:
            remaining = min(end - self.monotonic(), self.remaining())
            if remaining <= 0:
                raise BootstrapFailure("VAST_BOOTSTRAP_PHASE_TIMEOUT")
            root = SafeDirFD.open(directory)
            try:
                root.stat_child(name)
                return
            except FileNotFoundError:
                pass
            finally:
                root.close()
            time.sleep(min(0.1, remaining))


def _private_directory(parent: Path, name: str) -> Path:
    root = SafeDirFD.open(parent)
    try:
        os.mkdir(name, mode=0o700, dir_fd=root.fd)
        root.fsync()
    finally:
        root.close()
    return parent / name


def observe_guest(root: Path, budget: Budget) -> dict[str, Any]:
    """Only local identity declarations; never access CONTAINER_API_KEY."""
    from inferdrome.deployment import vast_process_runtime as runtime

    if (
        platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or sys.version_info[:2] != (3, 12)
        or os.getuid() != 2000
        or os.getgid() != 0
    ):
        raise BootstrapFailure("VAST_BOOTSTRAP_HOST_IDENTITY")
    marker = json.loads(read_private(runtime._BUILD_MARKER))
    installed = module_digests()
    if (
        set(marker) != {"source_commit", "module_sha256"}
        or marker["module_sha256"] != installed
    ):
        raise BootstrapFailure("VAST_BOOTSTRAP_BUILD_IDENTITY")
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "utility",
    }
    content = runtime._gpu_probe_bounded(root, environment, min(20, budget.remaining()))
    rows = [line.split(",") for line in content.decode("ascii").strip().splitlines()]
    if len(rows) != 2 or any(len(row) != 5 for row in rows):
        raise BootstrapFailure("VAST_BOOTSTRAP_GPU_IDENTITY")
    uuids = []
    drivers = set()
    for row in rows:
        uuid, name, memory, mig, driver = (value.strip() for value in row)
        if name != "NVIDIA H100 80GB HBM3" or memory != "81559" or mig != "Disabled":
            raise BootstrapFailure("VAST_BOOTSTRAP_GPU_IDENTITY")
        uuids.append(uuid)
        drivers.add(driver)
    if len(drivers) != 1:
        raise BootstrapFailure("VAST_BOOTSTRAP_GPU_IDENTITY")
    raw_id = os.environ.get("CONTAINER_ID", "")
    if not raw_id.isascii() or not raw_id.isdigit() or str(int(raw_id)) != raw_id:
        raise BootstrapFailure("VAST_BOOTSTRAP_INSTANCE_IDENTITY")
    return {
        "instance_id": int(raw_id),
        "source_commit": marker["source_commit"],
        "module_sha256": installed,
        "gpu_uuids": uuids,
        "uid": 2000,
        "gid": 0,
    }


def stage_snapshot(inbox: Path, destination: Path, budget: Budget) -> None:
    """Stream only the frozen file inventory into new private owner-created files."""
    manifest = qwen3_model_manifest()
    source = SafeDirFD.open(inbox)
    target = SafeDirFD.open(destination)
    try:
        if set(os.listdir(source.fd)) != {item["path"] for item in manifest["files"]}:
            raise BootstrapFailure("VAST_MODEL_INVENTORY_MISMATCH")
        for item in manifest["files"]:
            budget.remaining()
            name = item["path"]
            if "/" in name or name in {".", ".."}:
                raise BootstrapFailure("VAST_MODEL_INVENTORY_MISMATCH")
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source.fd
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size != item["size_bytes"]
                ):
                    raise BootstrapFailure("VAST_MODEL_FILE_INVALID")
                output = target.open_child(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                digest, total = hashlib.sha256(), 0
                with os.fdopen(output, "wb") as file:
                    while True:
                        budget.remaining()
                        chunk = os.read(
                            descriptor, min(1048576, item["size_bytes"] + 1 - total)
                        )
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > item["size_bytes"]:
                            raise BootstrapFailure("VAST_MODEL_FILE_INVALID")
                        digest.update(chunk)
                        file.write(chunk)
                    file.flush()
                    os.fsync(file.fileno())
                after, named = os.fstat(descriptor), source.stat_child(name)
                fields = (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_nlink",
                )
                if (
                    total != item["size_bytes"]
                    or "sha256:" + digest.hexdigest() != item["sha256"]
                    or any(
                        getattr(before, key) != getattr(after, key)
                        or getattr(after, key) != getattr(named, key)
                        for key in fields
                    )
                ):
                    raise BootstrapFailure("VAST_MODEL_DIGEST_MISMATCH")
            finally:
                os.close(descriptor)
        target.fsync()
        budget.remaining()
    finally:
        source.close()
        target.close()


def make_plan(
    intent: LaunchIntent, ready: Ready, launch: VastLaunchReadback, root: Path = ROOT
) -> VastProcessInput:
    if (
        ready.run_nonce != intent.run_nonce
        or ready.intent_sha256 != record_digest(intent)
        or ready.execution_deadline_utc != intent.execution_deadline_utc
        or ready.source_commit != intent.source_commit
        or ready.module_sha256 != intent.module_sha256
        or ready.instance_id != launch.instance_id
        or launch.requested_image != intent.container_image
    ):
        raise BootstrapFailure("VAST_BOOTSTRAP_BINDING_MISMATCH")
    return VastProcessInput.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.vast-process-input.v1",
                "provider": "VAST_AI",
                "source_commit": intent.source_commit,
                "instance_id": ready.instance_id,
                "offer_id": intent.offer_id,
                "container_image": intent.container_image.model_dump(mode="json"),
                "launch_readback": launch.model_dump(mode="json"),
                "gpu_uuids": ready.gpu_uuids,
                "uid": 2000,
                "gid": 0,
                "module_sha256": intent.module_sha256,
                **{
                    name + "_path": str(root / "private" / name)
                    for name in ("model", "preparation", "evidence", "cache")
                },
                "request_timeout_ms": intent.request_timeout_ms,
                "readiness_timeout_seconds": intent.readiness_timeout_seconds,
                "campaign_timeout_seconds": intent.campaign_timeout_seconds,
                "cleanup": {
                    "provider": "VAST_AI",
                    "instance_id": ready.instance_id,
                    "accountable_operator": intent.accountable_operator,
                    "terminate_by_utc": intent.execution_deadline_utc,
                    "method": "EXTERNAL_EXACT_ID_DESTROY_AND_ABSENCE_READBACK",
                    "state": "NOT_REQUESTED_NOT_VERIFIED",
                },
            }
        )
    )


class GuestBootstrap:
    def __init__(
        self,
        run_nonce: str,
        intent_sha256: str,
        deadline: str,
        *,
        root: Path = ROOT,
        budget: Budget | None = None,
    ) -> None:
        # Validate the command binding before making any filesystem changes.
        self.binding = CommandBinding.model_validate_json(
            canonical_json_bytes(
                {
                    "run_nonce": run_nonce,
                    "intent_sha256": intent_sha256,
                    "execution_deadline_utc": deadline,
                }
            )
        )
        self.root, self.deadline = root, deadline
        self.budget = budget or Budget(deadline)

    def initialize(self) -> Ready:
        self.budget.remaining()
        _private_directory(self.root.parent, self.root.name)
        for name in ("inbox", "outbox", "private"):
            _private_directory(self.root, name)
        _private_directory(self.root / "inbox", "model")
        for name in ("model", "evidence", "cache"):
            _private_directory(self.root / "private", name)
        observed = observe_guest(self.root, self.budget)
        ready = Ready.model_validate_json(
            canonical_json_bytes(
                {
                    **observed,
                    "schema_version": "inferdrome.vast-bootstrap-ready.v1",
                    "run_nonce": self.binding.run_nonce,
                    "intent_sha256": self.binding.intent_sha256,
                    "execution_deadline_utc": self.deadline,
                    "assertion": "LOCAL_OBSERVATIONS_NOT_PROVIDER_ATTESTATION",
                }
            )
        )
        self.budget.remaining()
        write_record(self.root / "outbox", "ready.json", record_bytes(ready))
        return ready

    def stage(self) -> VastProcessInput:
        self.budget.remaining()
        inbox = self.root / "inbox"
        intent = read_record(inbox, "intent.json", LaunchIntent)
        stage = read_record(inbox, "stage.json", Stage)
        if (
            record_digest(intent) != self.binding.intent_sha256
            or intent.run_nonce != self.binding.run_nonce
            or intent.execution_deadline_utc != self.deadline
            or stage.intent_sha256 != self.binding.intent_sha256
            or stage.run_nonce != self.binding.run_nonce
        ):
            raise BootstrapFailure("VAST_BOOTSTRAP_INTENT_MISMATCH")
        ready = read_record(self.root / "outbox", "ready.json", Ready)
        spec = make_plan(intent, ready, stage.launch_readback, self.root)
        write_record(self.root / "private", "stage-attempt.json", record_bytes(stage))
        stage_snapshot(
            inbox / "model",
            self.root / "private" / "model",
            self.budget.child(intent.stage_timeout_seconds),
        )
        prepare(spec, Path(spec.preparation_path))
        self.budget.remaining()
        write_record(self.root / "outbox", "plan.json", record_bytes(spec))
        return spec

    def execute_approved(self) -> Result:
        from inferdrome.deployment.vast_process_runtime import execute

        self.budget.remaining()
        intent = read_record(self.root / "inbox", "intent.json", LaunchIntent)
        ready = read_record(self.root / "outbox", "ready.json", Ready)
        stage = read_record(self.root / "inbox", "stage.json", Stage)
        expected = make_plan(intent, ready, stage.launch_readback, self.root)
        spec = read_record(self.root / "outbox", "plan.json", VastProcessInput)
        approval = read_record(self.root / "inbox", "approval.json", Approval)
        if (
            record_digest(intent) != self.binding.intent_sha256
            or spec != expected
            or intent.run_nonce != self.binding.run_nonce
            or approval != make_approval(intent, spec)
        ):
            raise BootstrapFailure("VAST_BOOTSTRAP_EXACT_APPROVAL_REQUIRED")
        write_record(
            self.root / "private", "approval-consumed.json", record_bytes(approval)
        )
        result = execute(
            Path(spec.preparation_path),
            record_digest(spec),
            intent.accountable_operator,
            True,
        )
        self.budget.remaining()
        digest = export_package(
            Path(spec.evidence_path) / "routing-execution-package",
            self.root / "outbox" / "export",
            result["retained_digest"],
        )
        self.budget.remaining()
        retained = Result.model_validate_json(
            canonical_json_bytes(
                {
                    "schema_version": "inferdrome.vast-bootstrap-result.v1",
                    "run_nonce": intent.run_nonce,
                    "intent_sha256": record_digest(intent),
                    "instance_id": spec.instance_id,
                    "plan_sha256": record_digest(spec),
                    "retained_digest": digest,
                    "status": "GUEST_EXPORT_VERIFIED_NOT_YET_RETRIEVED",
                    "provider_cleanup": "CLEANUP_UNCONFIRMED",
                }
            )
        )
        write_record(self.root / "outbox", "result.json", record_bytes(retained))
        return retained


def make_approval(intent: LaunchIntent, spec: VastProcessInput) -> Approval:
    return Approval.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "inferdrome.vast-bootstrap-approval.v1",
                "run_nonce": intent.run_nonce,
                "intent_sha256": record_digest(intent),
                "instance_id": spec.instance_id,
                "plan_sha256": record_digest(spec),
                "accountable_operator": intent.accountable_operator,
                "execution_deadline_utc": intent.execution_deadline_utc,
                "accept_manual_cleanup_risk": True,
            }
        )
    )


class Transfer(Protocol):
    @property
    def gate(self) -> BrokerGate: ...

    @property
    def instance_id(self) -> int: ...

    def send(self, local: Path, remote_relative: str, seconds: float) -> Any: ...
    def receive(self, remote_relative: str, local: Path, seconds: float) -> Any: ...


class BootstrapWorkflow:
    """Operator phases for vast_control; approval callback must review full bytes.

    Read-only control retrieval may poll within its phase deadline. A received
    malformed record fails immediately; creation, approval and execution never
    retry. Transfer configuration/trust failures are never polled.
    """

    def __init__(
        self,
        intent: LaunchIntent,
        transfer_for: Callable[[int], Transfer],
        local: Path,
        model: Path,
        launch_readback: Callable[[int], VastLaunchReadback],
        approve_plan: Callable[[bytes, float], str],
        *,
        guest_root: Path = ROOT,
    ) -> None:
        if intent.module_sha256 != module_digests():
            raise BootstrapFailure("VAST_OPERATOR_ARTIFACT_MISMATCH")
        self.intent, self.transfer_for, self.local, self.model = (
            intent,
            transfer_for,
            local,
            model,
        )
        self.launch_readback, self.approve_plan = launch_readback, approve_plan
        self.guest_root = guest_root
        self.budget = Budget(intent.execution_deadline_utc)
        self.ready_record: Ready | None = None
        self.plan: VastProcessInput | None = None
        self.result: Result | None = None
        self.retained_digest: str | None = None
        self._instance_id: int | None = None
        self._transfer: Transfer | None = None
        _private_directory(local.parent, local.name)

    @property
    def launch_request_sha256(self) -> str:
        return record_digest(self.intent)

    @property
    def execution_deadline_utc(self) -> str:
        return self.intent.execution_deadline_utc

    @property
    def cleanup_deadline_utc(self) -> str:
        return self.intent.cleanup_deadline_utc

    def _seconds(self, seconds: float) -> float:
        return min(seconds, self.budget.remaining())

    def _bind(self, instance_id: int) -> None:
        if self._instance_id is None:
            transfer = self.transfer_for(instance_id)
            if (
                transfer.instance_id != instance_id
                or not self.intent.transfer_prerequisites.accepts(transfer.gate)
            ):
                raise BootstrapFailure("VAST_WORKFLOW_TRANSPORT_ID_MISMATCH")
            self._transfer = transfer
            self._instance_id = instance_id
        if self._instance_id != instance_id:
            raise BootstrapFailure("VAST_WORKFLOW_INSTANCE_MISMATCH")

    @property
    def transfer(self) -> Transfer:
        if self._transfer is None:
            raise BootstrapFailure("VAST_WORKFLOW_NOT_READY")
        return self._transfer

    def _receive_control(self, name: str, seconds: float) -> None:
        from inferdrome.deployment.vast_transfer import (
            TransferConfigurationFailure,
            TransferFailure,
        )

        end = time.monotonic() + self._seconds(seconds)
        destination = self.local / name
        while True:
            remaining = min(end - time.monotonic(), self.budget.remaining())
            if remaining <= 0:
                raise BootstrapFailure("VAST_CONTROL_RECORD_TIMEOUT")
            try:
                self.transfer.receive("outbox/" + name, destination, min(10, remaining))
                return
            except TransferConfigurationFailure:
                raise
            except TransferFailure:
                # Never erase/retry a visible partial, prior, or replaced file.
                if os.path.lexists(destination):
                    raise
                time.sleep(min(0.1, max(0, end - time.monotonic())))

    def readiness(self, instance_id: int, *, seconds: float) -> None:
        self._bind(instance_id)
        self._receive_control("ready.json", seconds)
        ready = read_record(self.local, "ready.json", Ready)
        make_plan(
            self.intent, ready, self.launch_readback(instance_id), self.guest_root
        )
        if ready.instance_id != instance_id:
            raise BootstrapFailure("VAST_WORKFLOW_INSTANCE_MISMATCH")
        self.budget.remaining()
        self.ready_record = ready

    def stage(self, instance_id: int, *, seconds: float) -> None:
        self._bind(instance_id)
        if self.ready_record is None:
            raise BootstrapFailure("VAST_WORKFLOW_NOT_READY")
        end = time.monotonic() + min(
            self.intent.stage_timeout_seconds, self._seconds(seconds)
        )

        def remaining() -> float:
            value = min(end - time.monotonic(), self.budget.remaining())
            if value <= 0:
                raise BootstrapFailure("VAST_STAGE_TIMEOUT")
            return value

        write_record(self.local, "intent.json", record_bytes(self.intent))
        self.transfer.send(self.local / "intent.json", "inbox/intent.json", remaining())
        for item in qwen3_model_manifest()["files"]:
            self.transfer.send(
                self.model / item["path"], "inbox/model/" + item["path"], remaining()
            )
        stage = Stage.model_validate_json(
            canonical_json_bytes(
                {
                    "schema_version": "inferdrome.vast-bootstrap-stage.v1",
                    "run_nonce": self.intent.run_nonce,
                    "intent_sha256": record_digest(self.intent),
                    "launch_readback": self.launch_readback(instance_id).model_dump(
                        mode="json"
                    ),
                }
            )
        )
        write_record(self.local, "stage.json", record_bytes(stage))
        self.transfer.send(self.local / "stage.json", "inbox/stage.json", remaining())
        remaining()

    def approve(self, instance_id: int, *, seconds: float) -> None:
        self._bind(instance_id)
        if self.ready_record is None:
            raise BootstrapFailure("VAST_WORKFLOW_NOT_READY")
        end = time.monotonic() + min(
            self.intent.approval_timeout_seconds, self._seconds(seconds)
        )
        self._receive_control("plan.json", end - time.monotonic())
        plan = read_record(self.local, "plan.json", VastProcessInput)
        expected = make_plan(
            self.intent,
            self.ready_record,
            self.launch_readback(instance_id),
            self.guest_root,
        )
        if plan != expected:
            raise BootstrapFailure("VAST_WORKFLOW_PLAN_MISMATCH")
        from inferdrome.deployment.vast_control import _bounded_call

        remaining = min(end - time.monotonic(), self.budget.remaining())
        if remaining <= 0:
            raise BootstrapFailure("VAST_WORKFLOW_APPROVAL_REQUIRED")
        approved = _bounded_call(
            lambda: self.approve_plan(record_bytes(plan), remaining), remaining
        )
        remaining = min(end - time.monotonic(), self.budget.remaining())
        if remaining <= 0 or approved != record_digest(plan):
            raise BootstrapFailure("VAST_WORKFLOW_APPROVAL_REQUIRED")
        write_record(
            self.local, "approval.json", record_bytes(make_approval(self.intent, plan))
        )
        self.transfer.send(
            self.local / "approval.json", "inbox/approval.json", remaining
        )
        self.budget.remaining()
        self.plan = plan

    def run(self, instance_id: int, *, seconds: float) -> None:
        self._bind(instance_id)
        if self.plan is None:
            raise BootstrapFailure("VAST_WORKFLOW_APPROVAL_REQUIRED")
        self._receive_control("result.json", seconds)
        result = read_record(self.local, "result.json", Result)
        if (
            result.instance_id != instance_id
            or result.run_nonce != self.intent.run_nonce
            or result.intent_sha256 != record_digest(self.intent)
            or result.plan_sha256 != record_digest(self.plan)
        ):
            raise BootstrapFailure("VAST_WORKFLOW_RESULT_MISMATCH")
        self.budget.remaining()
        self.result = result

    def retrieve(self, instance_id: int, *, seconds: float) -> None:
        self._bind(instance_id)
        if self.result is None or self.plan is None:
            raise BootstrapFailure("VAST_WORKFLOW_NO_RESULT")
        end = time.monotonic() + min(
            self.intent.retrieval_timeout_seconds, self._seconds(seconds)
        )
        destination = _private_directory(self.local, "retrieved-package")
        for name in EXPORT_FILES:
            remaining = min(end - time.monotonic(), self.budget.remaining())
            if remaining <= 0:
                raise BootstrapFailure("VAST_RETRIEVAL_TIMEOUT")
            self.transfer.receive(
                "outbox/export/" + name, destination / name, remaining
            )
        held = SafeDirFD.open(destination)
        try:
            if set(os.listdir(held.fd)) != set(EXPORT_FILES):
                raise BootstrapFailure("VAST_RETRIEVAL_INVENTORY_MISMATCH")
            for name in EXPORT_FILES:
                descriptor = held.open_child(name, os.O_RDONLY)
                try:
                    os.fchmod(descriptor, 0o400)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            os.fchmod(held.fd, 0o500)
            held.fsync()
        finally:
            held.close()
        verified = verify_execution_package(
            destination, expected_digest=self.result.retained_digest
        )
        manifest = verified.executed_manifest.model_dump(mode="json")
        if manifest["source_commit"] != self.plan.source_commit or manifest["topology"][
            "declaration_sha256"
        ] != record_digest(self.plan):
            raise BootstrapFailure("VAST_RETRIEVAL_PLAN_MISMATCH")
        if time.monotonic() >= end:
            raise BootstrapFailure("VAST_RETRIEVAL_TIMEOUT")
        self.budget.remaining()
        write_record(self.local, "retrieval-verified.json", record_bytes(self.result))
        self.retained_digest = verified.report.retained_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--intent-sha256", required=True)
    parser.add_argument("--deadline", required=True)
    args = parser.parse_args(argv)

    def interrupt(signum: int, frame: Any) -> None:
        del signum, frame
        raise BootstrapFailure("VAST_BOOTSTRAP_INTERRUPTED")

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGALRM, interrupt)
    try:
        guest = GuestBootstrap(args.run_nonce, args.intent_sha256, args.deadline)
        signal.setitimer(signal.ITIMER_REAL, guest.budget.remaining())
        guest.initialize()
        guest.budget.wait_for(ROOT / "inbox", "intent.json", guest.budget.remaining())
        intent = read_record(ROOT / "inbox", "intent.json", LaunchIntent)
        if record_digest(intent) != args.intent_sha256:
            raise BootstrapFailure("VAST_BOOTSTRAP_INTENT_MISMATCH")
        guest.budget.wait_for(
            ROOT / "inbox", "stage.json", intent.stage_timeout_seconds
        )
        guest.stage()
        guest.budget.wait_for(
            ROOT / "inbox", "approval.json", intent.approval_timeout_seconds
        )
        guest.execute_approved()
        return 0
    except (ValueError, OSError, InferdromeError):
        print('{"status":"BOOTSTRAP_FAILED","provider_cleanup":"CLEANUP_UNCONFIRMED"}')
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
