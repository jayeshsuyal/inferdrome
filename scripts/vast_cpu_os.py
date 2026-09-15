"""Exact-base, disposable CPU container orchestration for the OS input lock."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from scripts.vast_cpu_common import IMAGE_BASE, Deadline, Failure, Runner
from scripts.vast_cpu_os_guest import (
    BASE,
    SNAPSHOT,
    Refusal,
    digest,
    publish,
    regular,
    validate_inputs,
)

CID = re.compile(r"[0-9a-f]{64}")


def _retained_id(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = regular(path, 128).decode("ascii").strip()
    except (OSError, Refusal, UnicodeError):
        raise Failure("OS_RESOLVER_CREATE_OUTCOME_UNRESOLVED") from None
    if not CID.fullmatch(value):
        raise Failure("INVALID_OS_RESOLVER_CONTAINER_ID")
    return value


def _remove(runner: Runner, cid: str, cleanup: Deadline) -> None:
    phase = cleanup.child(30)
    runner.run(["docker", "rm", "--force", cid], deadline=phase, check=False)
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
        deadline=phase,
    )
    if result.stdout.strip():
        raise Failure("OS_RESOLVER_ABSENCE_UNCONFIRMED")
    runner.forget_container(cid)


def resolve_os(
    runner: Runner, root: Path, execution: Deadline, cleanup: Deadline
) -> dict[str, Any]:
    """Return generated lock/deb inputs; never pull, build, publish or run a GPU.

    The caller must have separately pulled the exact base and authorized public
    snapshot egress. Missing/malformed create identity is not inferred or retried.
    """
    if (
        BASE != IMAGE_BASE
        or not root.is_absolute()
        or any(c in str(root) for c in ",\n\r")
    ):
        raise Failure("INVALID_OS_RESOLUTION_ROOT")
    phase = execution.child(900)
    inputs = root / "os-input"
    inputs.mkdir(mode=0o700)
    helper = regular(Path(__file__).with_name("vast_cpu_os_guest.py"), 1024 * 1024)
    publish(inputs / "install.py", helper)
    cidfile = root / "os-resolver.cid"
    if cidfile.exists() or cidfile.is_symlink():
        raise Failure("OS_RESOLVER_ID_ALREADY_EXISTS")
    cid = None
    attempted = False
    record_attempted = False
    try:
        seconds = min(900, phase.remaining())
        argv = [
            "docker",
            "create",
            "--pull=never",
            "--platform=linux/amd64",
            "--runtime=runc",
            "--network=bridge",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=268435456",
            "--mount",
            f"type=bind,src={inputs},dst=/os-input",
            "--cidfile",
            str(cidfile),
            "--entrypoint",
            "/usr/bin/env",
            IMAGE_BASE,
            "-i",
            "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=C",
            "LC_ALL=C",
            "/usr/bin/python3.12",
            "-I",
            "/os-input/install.py",
            "resolve",
            "/os-input",
            "--seconds",
            str(seconds),
        ]
        attempted = True
        created = runner.run(argv, deadline=phase, limit=4096)
        cid = _retained_id(cidfile)
        if cid is None or created.stdout.decode("ascii").strip() != cid:
            raise Failure("OS_RESOLVER_CREATE_OUTCOME_UNRESOLVED")
        record_attempted = True
        runner.record_container(cid)
        runner.run(
            ["docker", "start", "--attach", cid], deadline=phase, limit=1024 * 1024
        )
        lock_sha = digest(regular(inputs / "lock.json", 8 * 1024 * 1024))
        lock = validate_inputs(inputs, lock_sha)
        if lock["helper_sha256"] != digest(helper):
            raise Failure("OS_RESOLVER_HELPER_CHANGED")
        phase.remaining()
        return {
            "schema_version": "vast-cpu-os-resolution-v1",
            "input_dir": str(inputs),
            "lock_path": str(inputs / "lock.json"),
            "lock_sha256": lock_sha,
            "debs_dir": str(inputs / "debs"),
            "base_image": IMAGE_BASE,
            "snapshot": SNAPSHOT,
        }
    except (Refusal, OSError, ValueError, KeyError, TypeError):
        raise Failure("OS_RESOLUTION_REFUSED") from None
    finally:
        if cid is None and attempted:
            cid = _retained_id(cidfile)
        if cid is not None:
            try:
                if not record_attempted:
                    # Retain a known create result even when the Docker client
                    # timed out, so outer cleanup can recover if removal fails.
                    runner.record_container(cid)
            finally:
                _remove(runner, cid, cleanup)
        elif attempted:
            raise Failure("OS_RESOLVER_CREATE_OUTCOME_UNRESOLVED")
