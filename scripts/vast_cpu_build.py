"""One manually approved Vast CPU build, qualification and fixed-target publication.

Import and validation perform no Docker, registry, model or cloud operation.
The executable subcommands are intended only for the separately approved worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.vast_cpu_common import (
    CID,
    IMAGE_BASE,
    PYTHON,
    TARGET,
    Deadline,
    Failure,
    Runner,
    network_bytes,
    parse_time,
    read_json,
    sha256_file,
    write_json,
)

REPOSITORY = "jayeshsuyal/inferdrome"
REFS = {"refs/heads/main", "refs/heads/codex/add-vast-process-adapter"}
MODES = {"QUALIFY_ONLY", "PUBLISH_QUALIFIED_VAST"}
SOURCE = Path(__file__).resolve().parents[1]
PACKAGE_API = "https://api.github.com/users/jayeshsuyal/packages/container/inferdrome-vast-process"
ARCHIVE_PATHS = (
    "Dockerfile.vast-process",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "requirements-vast-downloader.txt",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "LICENSES",
    "src",
)


def require(condition: object, code: str) -> None:
    if not condition:
        raise Failure(code)


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(SOURCE), *arguments],
        capture_output=True,
        timeout=10,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C",
        },
    )
    require(result.returncode == 0, "SOURCE_INSPECTION_FAILED")
    return result.stdout.decode("utf-8").strip()


def identity(args: argparse.Namespace, *, cleaning: bool = False) -> dict[str, Any]:
    env = os.environ
    require(
        args.mode in MODES
        and env.get("EXECUTION_MODE") == args.mode
        and env.get("MODE_CONFIRMATION") == args.mode,
        "MODE_CONFIRMATION_MISMATCH",
    )
    require(
        env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and args.expected_ref in REFS
        and env.get("GITHUB_REF") == args.expected_ref,
        "DISPATCH_SCOPE_MISMATCH",
    )
    require(
        re.fullmatch(r"[0-9a-f]{40}", args.source_commit)
        and env.get("GITHUB_SHA") == args.source_commit
        and git("rev-parse", "HEAD") == args.source_commit
        and (cleaning or not git("status", "--porcelain")),
        "SOURCE_MISMATCH_OR_DIRTY",
    )
    require(
        re.fullmatch(r"[1-9][0-9]{0,19}", args.attempt_id)
        and env.get("GITHUB_RUN_ID") == args.attempt_id
        and env.get("GITHUB_RUN_ATTEMPT") == "1",
        "RETRY_OR_ATTEMPT_MISMATCH",
    )
    require(
        re.fullmatch(r"[a-z][a-z0-9-]{2,62}", args.expected_runner_name)
        and env.get("RUNNER_NAME") == args.expected_runner_name
        and env.get("RUNNER_OS") == "Linux"
        and env.get("RUNNER_ARCH") == "X64"
        and env.get("RUNNER_ENVIRONMENT") == "self-hosted"
        and platform.system() == "Linux"
        and platform.machine() == "x86_64",
        "CPU_WORKER_MISMATCH",
    )
    require(not list(Path("/dev").glob("nvidia*")), "GPU_WORKER_REFUSED")
    start, execution, cleanup = map(
        parse_time, (args.attempt_start, args.execution_deadline, args.cleanup_deadline)
    )
    require(
        execution == start + timedelta(minutes=105)
        and cleanup == start + timedelta(minutes=120),
        "ATTEMPT_BOUNDS_MISMATCH",
    )
    now = datetime.now(UTC)
    require(
        start <= now < (cleanup if cleaning else execution), "ATTEMPT_EXPIRED_OR_FUTURE"
    )
    require(0 < args.max_egress_bytes <= 40 * 1024**3, "EGRESS_ALLOWANCE_INVALID")
    temporary = Path(env.get("RUNNER_TEMP", ""))
    require(
        temporary.is_absolute()
        and temporary.is_dir()
        and temporary.resolve() == temporary,
        "RUNNER_TEMP_INVALID",
    )
    expected = temporary / f"inferdrome-vast-{args.attempt_id}"
    require(
        args.work_root == expected and expected.resolve() == expected,
        "WORK_ROOT_MISMATCH",
    )
    return {
        "schema_version": "vast-cpu-attempt-v1",
        "source_commit": args.source_commit,
        "source_tree": git("rev-parse", "HEAD^{tree}"),
        "ref": args.expected_ref,
        "attempt_id": args.attempt_id,
        "mode": args.mode,
        "runner_name": args.expected_runner_name,
        "attempt_start": args.attempt_start,
        "execution_deadline": args.execution_deadline,
        "cleanup_deadline": args.cleanup_deadline,
        "max_egress_bytes": args.max_egress_bytes,
        "target": TARGET,
        "base_image": IMAGE_BASE,
        "platform": "linux/amd64",
    }


def initialize(args: argparse.Namespace) -> None:
    binding = identity(args)
    phase(args, 15).remaining()
    args.work_root.mkdir(mode=0o700)
    (args.work_root / "evidence").mkdir(mode=0o700)
    write_json(args.work_root / "attempt.json", binding)
    write_json(args.work_root / "evidence" / "attempt.json", binding)
    write_json(
        args.work_root / "network-start.json",
        {
            "interfaces": network_bytes(),
            "max_egress_bytes": args.max_egress_bytes,
        },
    )


def load_attempt(args: argparse.Namespace, *, cleaning: bool = False) -> dict[str, Any]:
    expected = identity(args, cleaning=cleaning)
    require(
        read_json(args.work_root / "attempt.json") == expected,
        "ATTEMPT_RECORD_MISMATCH",
    )
    metadata = args.work_root.stat()
    require(
        metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
        "WORK_ROOT_PERMISSIONS",
    )
    return expected


def phase(args: argparse.Namespace, minutes: int) -> Deadline:
    end = parse_time(args.attempt_start) + timedelta(minutes=minutes)
    return Deadline(end.strftime("%Y-%m-%dT%H:%M:%SZ"))


def disk(path: Path) -> dict[str, int]:
    usage = os.statvfs(path)
    return {
        "capacity_bytes": usage.f_blocks * usage.f_frsize,
        "free_bytes": usage.f_bavail * usage.f_frsize,
        "free_inodes": usage.f_favail,
    }


def inspect_image(runner: Runner, image: str, deadline: Deadline) -> dict[str, Any]:
    result = runner.run(["docker", "image", "inspect", image], deadline=deadline)
    value = json.loads(result.stdout)
    require(isinstance(value, list) and len(value) == 1, "IMAGE_INSPECTION_INVALID")
    row = value[0]
    if not isinstance(row, dict):
        raise Failure("IMAGE_INSPECTION_INVALID")
    return row


def validate_image(value: dict[str, Any], source: str, lock: str) -> str:
    config = value.get("Config", {})
    labels = config.get("Labels", {})
    require(
        value.get("Os") == "linux" and value.get("Architecture") == "amd64",
        "IMAGE_PLATFORM_MISMATCH",
    )
    require(type(value.get("Size")) is int and value["Size"] > 0, "IMAGE_SIZE_INVALID")
    require(
        config.get("User") == "0:0"
        and config.get("Entrypoint")
        == [PYTHON, "-I", "-m", "inferdrome.deployment.vast_guest_ssh"],
        "IMAGE_ENTRYPOINT_MISMATCH",
    )
    require(
        not any(
            config.get(key) for key in ("Cmd", "ExposedPorts", "Volumes", "Healthcheck")
        ),
        "IMAGE_INHERITED_CONFIGURATION",
    )
    require(
        labels.get("org.opencontainers.image.revision") == source
        and labels.get("org.opencontainers.image.source")
        == "https://github.com/" + REPOSITORY
        and labels.get("com.inferdrome.os-package-lock-sha256") == lock,
        "IMAGE_SOURCE_OR_OS_LOCK_MISMATCH",
    )
    image_id = value.get("Id", "")
    if not isinstance(image_id, str):
        raise Failure("IMAGE_ID_INVALID")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id), "IMAGE_ID_INVALID")
    return image_id


def archive_source(
    runner: Runner, root: Path, source: str, deadline: Deadline
) -> dict[str, str]:
    archive = root / "source.tar"
    runner.run(
        [
            "git",
            "-C",
            str(SOURCE),
            "archive",
            "--format=tar",
            f"--output={archive}",
            source,
            "--",
            *ARCHIVE_PATHS,
        ],
        deadline=deadline,
    )
    context = root / "context"
    context.mkdir(mode=0o700)
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        require(
            len({member.name for member in members}) == len(members),
            "SOURCE_ARCHIVE_DUPLICATE_PATH",
        )
        require(
            all(member.isfile() or member.isdir() for member in members),
            "SOURCE_ARCHIVE_LINK_OR_SPECIAL_FILE",
        )
        tar.extractall(context, filter="data")
    require(not (context / "os-input").exists(), "SOURCE_HAS_GENERATED_OS_INPUT")
    return {"context": str(context), "archive_sha256": sha256_file(archive)}


STATIC_PROBE = r"""
import hashlib,json,os,subprocess
from pathlib import Path
from inferdrome.deployment.vast_process import module_digests
from inferdrome.deployment.gcp_private_engine_adapter import observed_vllm_version
markers = {name: json.loads(Path(path).read_text()) for name,path in (
    ('v1','/opt/inferdrome-vast-build.json'),('v2','/opt/inferdrome-vast-build-v2.json'))}
assert markers['v1']['module_sha256'] == module_digests()
assert markers['v2']['module_sha256'] == module_digests(profile='v2')
assert observed_vllm_version() == '0.26.0'
lock=Path('/opt/inferdrome-vast-os-lock.json').read_bytes()
measured=subprocess.run(['/usr/bin/du','-sx','-B1','/'],capture_output=True,timeout=100,check=True)
allocated=int(measured.stdout.split()[0])
print(json.dumps({'markers':markers,'vllm_version':'0.26.0','allocated_rootfs_bytes':allocated,
 'os_lock_sha256':hashlib.sha256(lock).hexdigest()}))
"""


def static_probe(
    runner: Runner, image: str, root: Path, deadline: Deadline, cleanup: Deadline
) -> dict[str, Any]:
    cidfile = root / "static.cid"
    cid = None
    try:
        created = runner.run(
            [
                "docker",
                "create",
                "--cidfile",
                str(cidfile),
                "--pull=never",
                "--runtime=runc",
                "--network=none",
                "--read-only",
                "--interactive",
                "--env",
                "NVIDIA_VISIBLE_DEVICES=void",
                "--entrypoint",
                PYTHON,
                image,
                "-I",
                "-",
            ],
            deadline=deadline,
        )
        cid = cidfile.read_text().strip()
        runner.record_container(cid)
        require(created.stdout.strip() == cid.encode(), "STATIC_CONTAINER_ID_MISMATCH")
        result = runner.run(
            ["docker", "start", "--attach", "--interactive", cid],
            deadline=deadline.child(120),
            input=STATIC_PROBE.encode(),
        )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise Failure("STATIC_PROBE_INVALID")
        observed = runner.run(
            ["docker", "container", "inspect", cid], deadline=deadline
        )
        state = json.loads(observed.stdout)[0]
        require(
            state.get("Image") == image
            and state.get("State", {}).get("ExitCode") == 0
            and state.get("State", {}).get("Running") is False,
            "STATIC_CONTAINER_OUTCOME_UNCONFIRMED",
        )
        require(value.get("allocated_rootfs_bytes", 0) > 0, "ROOTFS_MEASUREMENT_FAILED")
        return value
    finally:
        try:
            if cid is None and cidfile.exists():
                cid = cidfile.read_text().strip()
                runner.record_container(cid)
        finally:
            if cid:
                remove_container(runner, cid, cleanup)


def remove_container(runner: Runner, cid: str, cleanup: Deadline) -> None:
    require(CID.fullmatch(cid), "INVALID_CLEANUP_CONTAINER")
    runner.run(
        ["docker", "rm", "--force", cid], deadline=cleanup.child(15), check=False
    )
    result = runner.run(
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={cid}",
            "--format",
            "{{.ID}}",
        ],
        deadline=cleanup.child(10),
    )
    require(not result.stdout.strip(), "CONTAINER_CLEANUP_UNCONFIRMED")
    runner.forget_container(cid)


def build_and_qualify(args: argparse.Namespace) -> None:
    from scripts.vast_cpu_os import resolve_os
    from scripts.vast_cpu_qualify import qualify

    binding = load_attempt(args)
    root, cleanup = args.work_root, Deadline(args.cleanup_deadline)
    runner = Runner(root, cleanup)
    write_json(root / "qualification-started.json", {"attempt_id": args.attempt_id})
    setup = phase(args, 15)
    before = disk(root)
    versions = {}
    for name, command in (
        ("docker", ["docker", "version", "--format", "{{.Server.Version}}"]),
        ("buildx", ["docker", "buildx", "version"]),
    ):
        versions[name] = (
            runner.run(command, deadline=setup.child(10)).stdout.decode().strip()
        )
    require(
        not runner.run(["docker", "ps", "-q"], deadline=setup.child(10)).stdout.strip(),
        "WORKER_HAS_OTHER_CONTAINERS",
    )
    runner.run(["docker", "pull", "--platform=linux/amd64", IMAGE_BASE], deadline=setup)
    resolved = resolve_os(runner, root, setup, cleanup)
    lock = resolved["lock_sha256"]
    require(re.fullmatch(r"[0-9a-f]{64}", lock), "OS_LOCK_ID_INVALID")
    require(sha256_file(Path(resolved["lock_path"])) == lock, "OS_LOCK_CHANGED")
    archived = archive_source(runner, root, args.source_commit, setup)
    context = Path(archived["context"])
    shutil.copytree(Path(resolved["input_dir"]), context / "os-input")
    write_json(
        root / "evidence" / "os-lock.json", read_json(Path(resolved["lock_path"]))
    )
    test_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit/test_vast_end_to_end.py",
        "tests/unit/test_vast_sftp_end_to_end.py",
        "tests/unit/test_vast_image_source.py",
        "tests/unit/test_vast_model_stage.py",
    ]
    environment = dict(runner.environment, PYTHONPATH=str(SOURCE / "src"))
    runner.run(test_command, deadline=setup, env=environment)
    tag = f"{TARGET}:candidate-{args.source_commit[:12]}-{args.attempt_id}"
    build = phase(args, 55).child(2400)
    runner.run(
        [
            "docker",
            "buildx",
            "build",
            "--platform=linux/amd64",
            "--load",
            "--file",
            str(context / "Dockerfile.vast-process"),
            "--build-arg",
            f"SOURCE_REPOSITORY_COMMIT={args.source_commit}",
            "--build-arg",
            f"OS_PACKAGE_LOCK_SHA256={lock}",
            "--tag",
            tag,
            "--metadata-file",
            str(root / "build-metadata.json"),
            str(context),
        ],
        deadline=build,
        limit=8 * 1024 * 1024,
    )
    inspection = inspect_image(runner, tag, build)
    image_id = validate_image(inspection, args.source_commit, lock)
    probed = static_probe(runner, image_id, root, build, cleanup)
    require(probed["os_lock_sha256"] == lock, "IMAGE_OS_LOCK_BYTES_MISMATCH")
    require(
        all(
            marker["source_commit"] == args.source_commit
            for marker in probed["markers"].values()
        ),
        "IMAGE_MARKER_SOURCE_MISMATCH",
    )
    # The installed module digest values must agree with this exact archived source.
    from inferdrome.deployment.vast_process import module_digests

    require(
        probed["markers"]["v1"]["module_sha256"] == module_digests()
        and probed["markers"]["v2"]["module_sha256"] == module_digests(profile="v2"),
        "IMAGE_PAYLOAD_DIFFERS_FROM_SOURCE",
    )
    qualified = qualify(
        image_id, runner, root / "qualification", phase(args, 100), cleanup
    )
    require(
        qualified.get("status") == "PASS"
        and qualified.get("image_id") == image_id
        and qualified.get("gpu") == "NOT_RUN"
        and qualified.get("campaign") == "NOT_RUN"
        and qualified.get("cleanup") == "CONFIRMED"
        and isinstance(qualified.get("ssh_management"), dict)
        and isinstance(qualified.get("model_staging"), dict),
        "CPU_QUALIFICATION_FAILED",
    )
    require(not read_json(root / "owned-containers.json"), "OWNED_CONTAINERS_REMAIN")
    record = {
        **binding,
        "schema_version": "vast-cpu-qualified-image-v1",
        "status": "QUALIFIED",
        "tag": tag,
        "image_id": image_id,
        "os_lock_sha256": lock,
        "archive_sha256": archived["archive_sha256"],
        "tools": versions,
        "image_logical_bytes": inspection["Size"],
        "image_probe": probed,
        "host_disk_before": before,
        "host_disk_after": disk(root),
        "qualification": qualified,
        "egress_observation": runner.check_egress(),
        "provider_cleanup": "OPERATOR_MUST_VERIFY",
        "gpu_campaign": "NOT_RUN",
    }
    write_json(root / "evidence" / "qualified.json", record)
    write_json(
        root / "qualification-digest.json",
        {
            "sha256": sha256_file(root / "evidence" / "qualified.json"),
        },
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise Failure("REGISTRY_METADATA_REDIRECT_REFUSED")


@contextmanager
def registry_call_bound(deadline: Deadline) -> Iterator[None]:
    """Interrupt the entire HTTPS call, including a slow-dripping response body."""
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "ACTIVE_TIMER_REFUSED")
    seconds = min(10, deadline.remaining())
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_number: int, _frame: Any) -> None:
        raise Failure("REGISTRY_METADATA_DEADLINE")

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
        deadline.remaining()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def package_metadata(token: str, deadline: Deadline, *, versions: bool = False) -> Any:
    url = PACKAGE_API + ("/versions?per_page=100" if versions else "")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "inferdrome-vast-cpu",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with (
            registry_call_bound(deadline),
            opener.open(request, timeout=min(10, deadline.remaining())) as response,
        ):
            data = response.read(2 * 1024 * 1024 + 1)
            require(len(data) <= 2 * 1024 * 1024, "PACKAGE_RESPONSE_LIMIT")
            deadline.remaining()
            return json.loads(data)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise Failure("PACKAGE_ACCESS_UNCONFIRMED") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise Failure("PACKAGE_ACCESS_UNCONFIRMED") from None


def private_package(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("name") == "inferdrome-vast-process"
        and value.get("package_type") == "container"
        and value.get("visibility") == "private"
    )


def publish(args: argparse.Namespace) -> None:
    binding = load_attempt(args)
    require(args.mode == "PUBLISH_QUALIFIED_VAST", "PUBLICATION_MODE_REQUIRED")
    root, cleanup = args.work_root, Deadline(args.cleanup_deadline)
    runner, budget = Runner(root, cleanup), phase(args, 105).child(300)
    path = root / "evidence" / "qualified.json"
    receipt = read_json(path)
    require(
        sha256_file(path) == read_json(root / "qualification-digest.json")["sha256"],
        "QUALIFICATION_RECORD_CHANGED",
    )
    require(
        receipt.get("status") == "QUALIFIED"
        and all(
            receipt.get(name) == value
            for name, value in binding.items()
            if name != "schema_version"
        ),
        "QUALIFICATION_BINDING_MISMATCH",
    )
    expected_tag = f"{TARGET}:candidate-{args.source_commit[:12]}-{args.attempt_id}"
    require(receipt["tag"] == expected_tag, "PUBLICATION_TARGET_MISMATCH")
    inspection = inspect_image(runner, expected_tag, budget)
    image_id = validate_image(inspection, args.source_commit, receipt["os_lock_sha256"])
    require(image_id == receipt["image_id"], "QUALIFIED_IMAGE_CHANGED")
    require(
        not read_json(root / "owned-containers.json"), "QUALIFICATION_CLEANUP_REQUIRED"
    )
    # A conservative uncompressed-size estimate is separate from measured host TX.
    consumed = runner.check_egress()
    require(consumed.get("observed") is True, "EGRESS_BASELINE_REQUIRED")
    require(
        inspection["Size"] * 1.02 + 1024**2 + consumed["conservative_tx_bytes"]
        < args.max_egress_bytes,
        "IMAGE_EXCEEDS_EGRESS_ALLOWANCE",
    )
    write_json(
        root / "publication-started.json", {"tag": expected_tag, "image_id": image_id}
    )
    token = os.environ.pop("GHCR_TOKEN", "")
    actor = os.environ.get("GITHUB_ACTOR", "")
    require(
        20 <= len(token) <= 4096
        and not any(ch.isspace() for ch in token)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?", actor),
        "PUBLICATION_CREDENTIAL_REQUIRED",
    )
    config = runner.home / "docker-config"
    config.mkdir(mode=0o700)
    try:
        runner.check_egress()
        existing = package_metadata(token, budget)
        require(existing is None or private_package(existing), "TARGET_NOT_PRIVATE")
        runner.run(
            ["docker", "login", "ghcr.io", "--username", actor, "--password-stdin"],
            deadline=budget,
            input=token.encode(),
        )
        runner.run(
            ["docker", "push", expected_tag], deadline=budget, limit=2 * 1024 * 1024
        )
        pushed = inspect_image(runner, expected_tag, budget)
        digests = {
            value
            for value in pushed.get("RepoDigests", [])
            if value.startswith(TARGET + "@")
        }
        require(len(digests) == 1, "PUSH_DIGEST_UNCONFIRMED")
        immutable = next(iter(digests))
        digest = immutable.removeprefix(TARGET + "@")
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "REGISTRY_DIGEST_INVALID")
        raw = runner.run(
            ["docker", "buildx", "imagetools", "inspect", immutable, "--raw"],
            deadline=budget,
        ).stdout
        require(
            digest
            in {
                "sha256:" + hashlib.sha256(data).hexdigest()
                for data in (raw, raw.removesuffix(b"\n"))
            },
            "REMOTE_MANIFEST_HASH_MISMATCH",
        )
        manifest = json.loads(raw)
        require(
            manifest.get("config", {}).get("digest") == image_id,
            "REMOTE_IMAGE_CONFIG_MISMATCH",
        )
        runner.run(
            ["docker", "pull", "--platform=linux/amd64", immutable], deadline=budget
        )
        require(
            inspect_image(runner, immutable, budget)["Id"] == image_id,
            "REMOTE_IMAGE_CHANGED",
        )
        while True:
            runner.check_egress()
            metadata = package_metadata(token, budget)
            versions = package_metadata(token, budget, versions=True)
            runner.check_egress()
            if private_package(metadata) and isinstance(versions, list):
                matches = [
                    value
                    for value in versions
                    if value.get("name") == digest
                    and expected_tag.rsplit(":", 1)[1]
                    in value.get("metadata", {}).get("container", {}).get("tags", [])
                ]
                if len(matches) == 1:
                    break
            require(
                metadata is None or private_package(metadata),
                "PUBLISHED_TARGET_NOT_PRIVATE",
            )
            time.sleep(min(2, budget.remaining()))
        write_json(
            root / "evidence" / "publication.json",
            {
                "schema_version": "vast-cpu-publication-v1",
                "source_commit": args.source_commit,
                "attempt_id": args.attempt_id,
                "immutable_image": immutable,
                "image_id": image_id,
                "visibility": "private",
                "package_version_id": matches[0]["id"],
                "qualification_sha256": sha256_file(path),
                "egress_observation": runner.check_egress(),
                "provider_cleanup": "OPERATOR_MUST_VERIFY",
            },
        )
    finally:
        try:
            runner.run(
                ["docker", "logout", "ghcr.io"], deadline=cleanup.child(10), check=False
            )
        finally:
            shutil.rmtree(config)


def cleanup_owned(args: argparse.Namespace) -> None:
    load_attempt(args, cleaning=True)
    _cleanup_validated(args)


def _cleanup_validated(args: argparse.Namespace) -> None:
    runner = Runner(args.work_root, Deadline(args.cleanup_deadline))
    ids = runner._owned()
    failed = False
    for cid in ids:
        try:
            remove_container(runner, cid, runner.cleanup)
        except (Failure, OSError):
            failed = True
    config = runner.home / "docker-config"
    if config.exists():
        shutil.rmtree(config)
    output = args.work_root / "evidence" / "cleanup.json"
    if not output.exists():
        write_json(
            output,
            {
                "owned_containers_absent": not failed,
                "provider_cleanup": "OPERATOR_MUST_VERIFY",
                "docker_daemon_work": "NOT_A_PROVIDER_CLEANUP_PROOF",
            },
        )
    require(not failed, "OWNED_CLEANUP_UNCONFIRMED")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command", choices=("validate", "qualify", "publish", "cleanup")
    )
    for name in (
        "mode",
        "source-commit",
        "expected-ref",
        "expected-runner-name",
        "attempt-start",
        "execution-deadline",
        "cleanup-deadline",
        "attempt-id",
    ):
        result.add_argument("--" + name, required=True)
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--max-egress-bytes", type=int, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    def interrupted(_number: int, _frame: Any) -> None:
        raise Failure("CPU_ATTEMPT_INTERRUPTED")

    previous = {
        number: signal.signal(number, interrupted)
        for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        {
            "validate": initialize,
            "qualify": build_and_qualify,
            "publish": publish,
            "cleanup": cleanup_owned,
        }[args.command](args)
        return 0
    except Exception:
        # Exception messages can contain downstream output or credentials.
        failure = {
            "status": "FAILED",
            "phase": args.command,
            "provider_cleanup": "OPERATOR_MUST_VERIFY",
        }
        print(json.dumps(failure))
        if args.command in {"qualify", "publish"}:
            # A dirty checkout must block productive work, but must not prevent
            # deletion of IDs already owned by this exact validated attempt.
            with suppress(Exception):
                load_attempt(args, cleaning=True)
                with suppress(Exception):
                    write_json(
                        args.work_root / "evidence" / f"failure-{args.command}.json",
                        failure,
                    )
                _cleanup_validated(args)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
