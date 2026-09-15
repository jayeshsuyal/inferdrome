"""Guest-only, pinned model staging; construction/import performs no download."""

from __future__ import annotations

import os
import shutil
import tempfile
from functools import partial
from pathlib import Path

from inferdrome.deployment.gcp_securefs import SafeDirFD
from inferdrome.deployment.vast_control import _bounded_call
from inferdrome.deployment.vast_transfer import (
    FileSpec,
    TransferCommand,
    TransferRunner,
    _copy_verified,
    _deadline,
    _remaining,
    _run_bounded,
)
from inferdrome.qwen3_campaign import QWEN3_8B_REVISION, qwen3_model_manifest

DOWNLOADER = "/opt/inferdrome-downloader/bin/python"
HEADROOM_BYTES = 1_073_741_824
# This interpreter has its own hash-locked requirements, not the serving/app env.
_WORKER = """import importlib.metadata,sys
from pathlib import Path
import httpx
from huggingface_hub import hf_hub_download,set_client_factory
assert importlib.metadata.version('huggingface-hub') == '1.3.2'
set_client_factory(lambda: httpx.Client(
    trust_env=False,follow_redirects=True,timeout=20))
name,revision,directory = sys.argv[1:]
result=hf_hub_download(repo_id='Qwen/Qwen3-8B',filename=name,revision=revision,
    endpoint='https://huggingface.co',token=False,local_dir=directory,
    force_download=True,etag_timeout=20)
assert Path(result) == Path(directory)/name
"""


class ModelStageFailure(ValueError):
    """Sanitized failures; no token, URL or downloader diagnostic retention."""


def required_free_bytes(headroom_bytes: int = HEADROOM_BYTES) -> int:
    files = qwen3_model_manifest()["files"]
    sizes = [int(item["size_bytes"]) for item in files]
    if type(headroom_bytes) is not int or headroom_bytes < HEADROOM_BYTES:
        raise ModelStageFailure("VAST_MODEL_HEADROOM_INVALID")
    return 2 * sum(sizes) + max(sizes) + headroom_bytes


def stage_pinned_model(
    destination: Path,
    *,
    seconds: float,
    runner: TransferRunner | None = None,
    headroom_bytes: int = HEADROOM_BYTES,
) -> None:
    """Publish verified inventory only; the caller then validates its snapshot.

    The fresh directory is within the guest's private workspace, outside SFTP.
    A fake runner is the sole local test seam. The production path requires the
    approved nonroot guest identity and explicitly installed downloader.
    """
    end = _deadline(seconds)
    if runner is None and (os.getuid(), os.getgid()) != (2000, 0):
        raise ModelStageFailure("VAST_MODEL_GUEST_IDENTITY_REQUIRED")
    selected = _run_bounded if runner is None else runner
    root = SafeDirFD.open(destination)
    try:
        if os.listdir(root.fd):
            raise ModelStageFailure("VAST_MODEL_DESTINATION_NOT_EMPTY")
        if shutil.disk_usage(destination).free < required_free_bytes(headroom_bytes):
            raise ModelStageFailure("VAST_MODEL_INSUFFICIENT_FREE_DISK")
        for item in qwen3_model_manifest()["files"]:
            name = item["path"]
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or name in {".", ".."}
            ):
                raise ModelStageFailure("VAST_MODEL_INVENTORY_INVALID")
            size, digest = item["size_bytes"], item["sha256"]
            spec = FileSpec(name, "send", size, size, digest)
            _remaining(end)
            # One file's HF cache/scratch at a time, never a third model copy.
            with tempfile.TemporaryDirectory(
                prefix="download-", dir=destination.parent
            ) as temporary:
                scratch = Path(temporary)
                command = TransferCommand(
                    (
                        DOWNLOADER,
                        "-I",
                        "-c",
                        _WORKER,
                        name,
                        QWEN3_8B_REVISION,
                        str(scratch),
                    ),
                    {
                        "PATH": "/usr/bin:/bin",
                        "HOME": str(scratch),
                        "TMPDIR": str(scratch),
                        "LANG": "C.UTF-8",
                        "HF_HOME": str(scratch / ".hf"),
                        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1",
                        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
                        "HF_HUB_DISABLE_XET": "1",
                        "HF_HUB_ETAG_TIMEOUT": "20",
                        "HF_HUB_DOWNLOAD_TIMEOUT": "20",
                    },
                    scratch,
                    scratch / name,
                    max(size, 131_072),
                    "send",
                )
                remaining = _remaining(end)
                _bounded_call(partial(selected, command, remaining), remaining)
                _copy_verified(scratch / name, spec, end)
                # Re-read into a new owner-created file and verify during copy.
                output = root.open_child(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    _copy_verified(scratch / name, spec, end, output)
                    os.fsync(output)
                finally:
                    os.close(output)
                root.validated_regular_child(name)
                root.fsync()
        if set(os.listdir(root.fd)) != {
            f["path"] for f in qwen3_model_manifest()["files"]
        }:
            raise ModelStageFailure("VAST_MODEL_INVENTORY_INVALID")
        _remaining(end)
    except Exception:
        raise ModelStageFailure("VAST_MODEL_STAGE_FAILED") from None
    finally:
        root.close()
