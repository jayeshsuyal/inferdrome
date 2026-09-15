"""Explicit CPU image qualification; import performs no external operations.

Only a separately authorized caller may invoke qualify. The image must already
exist locally by exact ID. Docker, disposable test keys, real stock loopback
SSH and public pinned model staging are executed only through the injected
runner. No build, pull, registry publication, provider API or GPU is invoked.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from inferdrome.deployment.vast_ssh_trust import (
    HostKeyAnnouncement,
    SshTrustFailure,
    parse_log_announcement,
)
from inferdrome.qwen3_campaign import (
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
    qwen3_model_manifest,
)
from scripts.vast_cpu_common import Deadline, Failure, Result, Runner, write_json

_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CID = re.compile(r"[0-9a-f]{64}\Z")
_PYTHON = "/opt/inferdrome-runtime/bin/python"
_PROBE = "/run/inferdrome-cpu-qualify.py"
_CLIENT = "/run/inferdrome-cpu-client"
_ENTRYPOINT = [_PYTHON, "-I", "-m", "inferdrome.deployment.vast_guest_ssh"]
MANAGEMENT_CHECKS = frozenset(
    (
        "pid1_root_supervisor",
        "accounts_and_permissions",
        "daemon_config",
        "host_key_binding",
        "client_key_binding",
        "experiment_identity",
        "experiment_dac",
        "transfer_dac",
        "sftp_before",
        "wrong_host_key",
        "wrong_client_key",
        "root_login",
        "password_auth",
        "shell_exec",
        "pty",
        "direct_forward",
        "remote_forward",
        "chroot_escape",
        "private_path",
        "download_write",
        "download_remove",
        "download_rename",
        "sftp_after",
        "upload_ownership",
    )
)
MODEL_CHECKS = frozenset(
    (
        "experiment_identity",
        "pinned_download",
        "snapshot_copy",
        "frozen_inventory",
        "file_ownership",
        "shared_stage_budget",
        "disk_budget",
    )
)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Failure("CPU_JSON_INVALID")
        result[key] = value
    return result


def _json(content: bytes) -> Any:
    if not content or len(content) > 131_072:
        raise Failure("CPU_JSON_INVALID")
    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, ValueError):
        raise Failure("CPU_JSON_INVALID") from None


def _one(content: bytes) -> dict[str, Any]:
    value = _json(content)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise Failure("CPU_INSPECTION_INVALID")
    return value[0]


def _read(path: Path, maximum: int, *, private: bool = False) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size > maximum
            or before.st_mode & (0o077 if private else 0o022)
        ):
            raise Failure("CPU_LOCAL_FILE_INVALID")
        content = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        if len(content) != before.st_size or any(
            getattr(before, key) != getattr(after, key)
            for key in ("st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise Failure("CPU_LOCAL_FILE_CHANGED")
        return content
    finally:
        os.close(fd)


def _public(path: Path, nonce: str) -> str:
    try:
        content = _read(path, 256).decode("ascii").strip()
        fields = content.split()
        if len(fields) != 2 or "\n" in content or "\r" in content:
            raise ValueError
        return HostKeyAnnouncement(101, nonce, " ".join(fields)).host_public_key
    except (OSError, UnicodeError, ValueError):
        raise Failure("CPU_CLIENT_PUBLIC_KEY_INVALID") from None


def _image(runner: Runner, image_id: str, deadline: Deadline) -> None:
    value = _one(
        runner.run(("docker", "image", "inspect", image_id), deadline=deadline).stdout
    )
    config = value.get("Config")
    if (
        value.get("Id") != image_id
        or value.get("Os") != "linux"
        or value.get("Architecture") != "amd64"
        or not isinstance(config, dict)
        or config.get("User") != "0:0"
        or config.get("Entrypoint") != _ENTRYPOINT
        or config.get("Cmd") not in (None, [])
        or config.get("ExposedPorts") not in (None, {})
        or config.get("Volumes") not in (None, {})
        or config.get("Healthcheck") not in (None, {})
    ):
        raise Failure("CPU_IMAGE_CONTRACT_INVALID")


def _container(
    runner: Runner, cid: str, image_id: str, network: str, deadline: Deadline
) -> dict[str, Any]:
    value = _one(
        runner.run(("docker", "container", "inspect", cid), deadline=deadline).stdout
    )
    host = value.get("HostConfig")
    if (
        value.get("Id") != cid
        or value.get("Image") != image_id
        or not isinstance(host, dict)
        or host.get("NetworkMode") != network
        or host.get("Runtime") != "runc"
        or host.get("Privileged") is not False
        or host.get("DeviceRequests") not in (None, [])
        or host.get("Devices") not in (None, [])
        or host.get("PortBindings") not in (None, {})
        or host.get("PidMode") not in (None, "")
        or host.get("Binds") not in (None, [])
        or value.get("Mounts") not in (None, [])
    ):
        raise Failure("CPU_CONTAINER_CONTRACT_INVALID")
    return value


def _cidfile(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    try:
        value = _read(path, 65).decode("ascii").strip()
    except (OSError, UnicodeError):
        raise Failure("CPU_CONTAINER_ID_INVALID") from None
    if _CID.fullmatch(value) is None:
        raise Failure("CPU_CONTAINER_ID_INVALID")
    return value


def _absent(result: Result, cid: str) -> bool:
    messages = {
        f"Error: No such object: {cid}".encode(),
        f"Error: No such container: {cid}".encode(),
        f"Error response from daemon: No such container: {cid}".encode(),
    }
    return (
        result.returncode == 1
        and result.stdout.strip() in (b"", b"[]")
        and result.stderr.strip() in messages
    )


def _remove(runner: Runner, cid: str, cleanup: Deadline) -> None:
    result = runner.run(("docker", "rm", "--force", cid), deadline=cleanup, check=False)
    removed = (
        result.returncode == 0 and result.stdout.strip() == cid.encode()
    ) or _absent(result, cid)
    observed = runner.run(
        ("docker", "container", "inspect", cid), deadline=cleanup, check=False
    )
    if not removed or not _absent(observed, cid):
        raise Failure("CPU_CONTAINER_CLEANUP_UNCONFIRMED")
    runner.forget_container(cid)


def _guest(content: bytes, mode: str, checks: frozenset[str]) -> dict[str, Any]:
    value = _json(content)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "inferdrome.vast-cpu-guest-probe.v1"
        or value.get("mode") != mode
        or value.get("status") != "PASS"
        or not isinstance(value.get("checks"), dict)
        or set(value["checks"]) != checks
        or any(item is not True for item in value["checks"].values())
    ):
        raise Failure("CPU_GUEST_PROBE_FAILED")
    return value


def _model_report(value: dict[str, Any]) -> dict[str, Any]:
    manifest = qwen3_model_manifest()
    total = sum(item["size_bytes"] for item in manifest["files"])
    disk = value.get("disk")
    names = {
        "required_free_bytes",
        "free_before_bytes",
        "free_after_bytes",
        "minimum_free_observed_bytes",
        "peak_observed_consumption_bytes",
        "peak_observed_tree_bytes",
    }
    if (
        value.get("frozen_revision") != QWEN3_8B_REVISION
        or value.get("snapshot_sha256") != qwen3_expected_snapshot_sha256()
        or value.get("inventory_file_count") != len(manifest["files"])
        or value.get("inventory_total_bytes") != total
        or not isinstance(disk, dict)
        or set(disk) != names
        or any(type(number) is not int or number < 0 for number in disk.values())
        or disk["required_free_bytes"]
        != 2 * total
        + max(item["size_bytes"] for item in manifest["files"])
        + 1_073_741_824
        or disk["free_before_bytes"] < disk["required_free_bytes"]
        or disk["minimum_free_observed_bytes"]
        > min(disk["free_before_bytes"], disk["free_after_bytes"])
        or disk["peak_observed_consumption_bytes"]
        != disk["free_before_bytes"] - disk["minimum_free_observed_bytes"]
    ):
        raise Failure("CPU_MODEL_REPORT_INVALID")
    expected_files = [
        {**item, "uid": 2000, "gid": 0, "mode": "0600", "nlink": 1}
        for item in manifest["files"]
    ]
    for name in ("stage_files", "snapshot_files"):
        receipts = value.get(name)
        if (
            not isinstance(receipts, list)
            or receipts != expected_files
            or any(
                type(item[key]) is not type(expected[key])
                for item, expected in zip(receipts, expected_files, strict=True)
                for key in expected
            )
        ):
            raise Failure("CPU_MODEL_FILE_RECEIPTS_INVALID")
    return {
        "checks": value["checks"],
        "frozen_revision": QWEN3_8B_REVISION,
        "snapshot_sha256": value["snapshot_sha256"],
        "inventory_file_count": len(manifest["files"]),
        "inventory_total_bytes": total,
        "disk": disk,
        "experiment_identity": _identity_receipt(
            value.get("experiment_identity"), 2000
        ),
        "stage_files": value["stage_files"],
        "snapshot_files": value["snapshot_files"],
        "peak_measurement": "SAMPLED_NOT_EXACT_PEAK",
    }


def _identity_receipt(value: Any, uid: int) -> dict[str, Any]:
    expected = {
        "uids": [uid] * 3,
        "gids": [0] * 3,
        "groups": [],
        "no_new_privs": 1,
        "capabilities": dict.fromkeys(
            ("CapInh", "CapPrm", "CapEff", "CapAmb"), "0000000000000000"
        ),
    }
    if (
        not isinstance(value, dict)
        or value != expected
        or type(value["no_new_privs"]) is not int
        or any(type(item) is not int for key in ("uids", "gids") for item in value[key])
    ):
        raise Failure("CPU_IDENTITY_RECEIPT_INVALID")
    return expected


def qualify(
    image_id: str, runner: Runner, root: Path, execution: Deadline, cleanup: Deadline
) -> dict[str, Any]:
    """Execute one bounded CPU attempt; failures never authorize publication."""
    if _IMAGE.fullmatch(image_id) is None or not root.is_absolute():
        raise Failure("CPU_QUALIFICATION_INPUT_INVALID")
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    report: dict[str, Any] = {
        "schema_version": "inferdrome.vast-cpu-qualification.v1",
        "image_id": image_id,
        "status": "FAILED",
        "gpu": "NOT_RUN",
        "campaign": "NOT_RUN",
        "execution_deadline": execution.original_timestamp,
        "cleanup_deadline": cleanup.timestamp,
        "containers": [],
    }
    owned: list[str] = []
    active_cidfiles: list[Path] = []
    cleanup_failed = False
    failed = False
    private_files: list[Path] = []
    probe = Path(__file__).with_name("vast_cpu_guest_probe.py")

    def retain(cid: str) -> None:
        if cid not in owned:
            owned.append(cid)
            report["containers"].append(cid)
            runner.record_container(cid)

    def create(args: tuple[str, ...], name: str, deadline: Deadline) -> str:
        cidfile = root / (name + ".cid")
        active_cidfiles.append(cidfile)
        result: Result | None = None
        try:
            result = runner.run(
                (
                    "docker",
                    "create",
                    "--cidfile",
                    str(cidfile),
                    "--pull=never",
                    "--runtime=runc",
                    *args,
                ),
                deadline=deadline,
            )
        finally:
            value = _cidfile(cidfile)
            if value is not None:
                retain(value)
        if result is None:
            raise Failure("CPU_CONTAINER_CREATE_FAILED")
        raw = result.stdout.decode("ascii").strip()
        if _CID.fullmatch(raw) is None:
            raise Failure("CPU_CONTAINER_ID_INVALID")
        if value is None:
            retain(raw)
            raise Failure("CPU_CONTAINER_ID_NOT_DURABLE")
        if raw != value:
            raise Failure("CPU_CONTAINER_ID_MISMATCH")
        return value

    def retire(cid: str) -> None:
        _remove(runner, cid, cleanup)
        owned.remove(cid)

    try:
        _image(runner, image_id, execution.child(20))
        phase = execution.child(600)
        if phase.remaining() <= 60:
            raise Failure("CPU_SSH_PHASE_TOO_SHORT")
        nonce = secrets.token_hex(16)
        for filename in ("identity", "wrong_identity"):
            path = root / filename
            private_files.extend((path, path.with_suffix(".pub")))
            runner.run(
                (
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "",
                    "-f",
                    str(path),
                ),
                deadline=phase.child(10),
                limit=4096,
            )
            if not _read(path, 16_384, private=True):
                raise Failure("CPU_CLIENT_KEY_INVALID")
        public = _public(root / "identity.pub", nonce)
        wrong_public = _public(root / "wrong_identity.pub", nonce)
        if wrong_public == public:
            raise Failure("CPU_CLIENT_KEYS_NOT_DISTINCT")
        end = datetime.strptime(phase.timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        guest_end = (end - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        guest_cleanup = (end - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cid = create(
            (
                "--network",
                "none",
                "--env",
                "NVIDIA_VISIBLE_DEVICES=void",
                "--env",
                "CONTAINER_ID=101",
                image_id,
                "serve",
                "--run-nonce",
                nonce,
                "--intent-sha256",
                "sha256:" + "0" * 64,
                "--execution-deadline",
                guest_end,
                "--cleanup-deadline",
                guest_cleanup,
                "--client-public-key",
                public,
            ),
            "management",
            phase,
        )
        _container(runner, cid, image_id, "none", phase)
        runner.run(("docker", "start", cid), deadline=phase)
        wait = phase.child(20)
        while True:
            logs = runner.run(
                ("docker", "logs", "--tail", "20", cid),
                deadline=wait,
                limit=131_072,
            ).stdout
            try:
                announcement = parse_log_announcement(
                    logs, instance_id=101, run_nonce=nonce
                )
                break
            except SshTrustFailure as error:
                if str(error) != "VAST_SSH_MARKER_MISSING":
                    raise Failure("CPU_HOST_PIN_INVALID") from None
                time.sleep(min(0.1, wait.remaining()))
        keys = root / "known_hosts"
        private_files.append(keys)
        keys.write_text("[127.0.0.1]:2222 " + announcement.host_public_key + "\n")
        keys.chmod(0o600)
        runner.run(
            (
                "docker",
                "exec",
                cid,
                "/usr/bin/install",
                "-d",
                "-o",
                "0",
                "-g",
                "0",
                "-m",
                "0700",
                _CLIENT,
            ),
            deadline=phase,
        )
        for path, remote in (
            (probe, _PROBE),
            (root / "identity", _CLIENT + "/identity"),
            (root / "wrong_identity", _CLIENT + "/wrong_identity"),
            (keys, _CLIENT + "/known_hosts"),
        ):
            runner.run(("docker", "cp", str(path), cid + ":" + remote), deadline=phase)
        runner.run(
            (
                "docker",
                "exec",
                cid,
                "/bin/chmod",
                "0600",
                _CLIENT + "/identity",
                _CLIENT + "/wrong_identity",
                _CLIENT + "/known_hosts",
            ),
            deadline=phase,
        )
        value = _guest(
            runner.run(
                (
                    "docker",
                    "exec",
                    cid,
                    _PYTHON,
                    "-I",
                    _PROBE,
                    "management",
                    "--nonce",
                    nonce,
                    "--host-public-key",
                    announcement.host_public_key,
                    "--client-root",
                    _CLIENT,
                    "--deadline",
                    guest_end,
                ),
                deadline=phase,
                limit=131_072,
            ).stdout,
            "management",
            MANAGEMENT_CHECKS,
        )
        if (
            value.get("run_nonce") != nonce
            or value.get("host_key_sha256") != announcement.host_key_sha256
        ):
            raise Failure("CPU_HOST_PIN_MISMATCH")
        allocated = value.get("root_filesystem_allocated_bytes")
        if type(allocated) is not int or allocated <= 0:
            raise Failure("CPU_ROOT_DISK_MEASUREMENT_INVALID")
        report["ssh_management"] = {
            "checks": value["checks"],
            "host_key_sha256": announcement.host_key_sha256,
            "endpoint": "CONTAINER_LOOPBACK_ONLY",
            "phase_deadline": phase.timestamp,
            "gpu_bootstrap": "NOT_RUN_NO_GPU",
            "experiment_identity": _identity_receipt(
                value.get("experiment_identity"), 2000
            ),
            "transfer_identity": _identity_receipt(
                value.get("transfer_identity"), 2001
            ),
            "root_filesystem_allocated_bytes": allocated,
        }
        retire(cid)
        phase = execution.child(2100)
        cid = create(
            (
                "--network",
                "bridge",
                "--env",
                "NVIDIA_VISIBLE_DEVICES=void",
                "--entrypoint",
                _PYTHON,
                image_id,
                "-I",
                _PROBE,
                "model",
                "--deadline",
                phase.timestamp,
                "--seconds",
                str(phase.remaining()),
            ),
            "model",
            phase,
        )
        _container(runner, cid, image_id, "bridge", phase)
        runner.run(("docker", "cp", str(probe), cid + ":" + _PROBE), deadline=phase)
        value = _guest(
            runner.run(
                ("docker", "start", "--attach", cid),
                deadline=phase,
                limit=131_072,
            ).stdout,
            "model",
            MODEL_CHECKS,
        )
        state = _container(runner, cid, image_id, "bridge", phase).get("State", {})
        if (
            state.get("Running") is not False
            or type(state.get("ExitCode")) is not int
            or state["ExitCode"] != 0
        ):
            raise Failure("CPU_MODEL_CONTAINER_FAILED")
        report["model_staging"] = {
            **_model_report(value),
            "phase_deadline": phase.timestamp,
        }
        retire(cid)
        execution.remaining()
    except BaseException:
        failed = True
    finally:
        for cidfile in active_cidfiles:
            try:
                pending_cid = _cidfile(cidfile)
                if pending_cid is not None and pending_cid not in report["containers"]:
                    retain(pending_cid)
            except BaseException:
                cleanup_failed = True
        for cid in list(reversed(owned)):
            try:
                retire(cid)
            except BaseException:
                cleanup_failed = True
        for path in private_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        report["cleanup"] = "UNCONFIRMED" if cleanup_failed or owned else "CONFIRMED"
        if not failed and report["cleanup"] == "CONFIRMED":
            report["status"] = "PASS"
        write_json(root / "qualification.json", report)
    if report["status"] != "PASS":
        raise Failure("CPU_QUALIFICATION_FAILED")
    return report
