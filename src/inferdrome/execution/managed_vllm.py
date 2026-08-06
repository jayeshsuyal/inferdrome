"""Managed local vLLM server and allowlisted NVIDIA-host proof collection."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from inferdrome.adapters.vllm_bench import (
    EndpointPreflightCapture,
    preflight_attached_endpoint,
)
from inferdrome.domain.digests import canonical_json_bytes
from inferdrome.domain.experiment import ExperimentSpec, VllmExecution
from inferdrome.domain.ids import sha256_digest
from inferdrome.errors import AdapterError
from inferdrome.execution.cancellation import (
    CancellationReason,
    CancellationToken,
)
from inferdrome.execution.subprocess_runner import (
    ProcessCapture,
    ProcessTermination,
    run_captured_process,
)
from inferdrome.gpu_proof import (
    GpuComputeProcessEvidence,
    GpuDeviceEvidence,
    LocalGpuProof,
    ManagedServerEvidence,
    ManagedVllmConfig,
    SnapshotIdentity,
    VllmDistributionIdentity,
    build_managed_server_argv,
    expected_vllm_source_wheel,
    gpu_compute_process_argv,
    gpu_inventory_argv,
    parse_gpu_compute_process_output,
    parse_gpu_inventory_output,
    validate_local_gpu_proof,
    validate_managed_vllm_target,
)

_MAX_CAPTURE_BYTES = 67_108_864
_MAX_QUERY_BYTES = 262_144
_MAX_SNAPSHOT_FILES = 100_000
_MAX_SNAPSHOT_BYTES = 137_438_953_472
_MAX_DISTRIBUTION_BYTES = 17_179_869_184
_EXCLUDED_SNAPSHOT_DIRECTORY = ".cache"

ProcessRunner = Callable[..., ProcessCapture]
PreflightProbe = Callable[..., EndpointPreflightCapture]


class _GpuProcessNotReady(AdapterError):
    """The managed server has not yet engaged every selected GPU."""


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _DiscoveredFile:
    relative_path: str
    absolute_path: Path
    identity: _FileIdentity


def _file_identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        links=value.st_nlink,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _require_real_directory(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        path_stat = os.lstat(absolute)
    except OSError:
        raise AdapterError(f"{label} is unavailable") from None
    if absolute.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
        raise AdapterError(f"{label} must be a real directory")
    return absolute


def _safe_relative_path(relative: Path, *, label: str) -> str:
    value = relative.as_posix()
    pure = PurePosixPath(value)
    if (
        not value
        or value == "."
        or len(value.encode("utf-8")) > 4096
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(ord(character) < 32 for character in value)
    ):
        raise AdapterError(f"{label} contains an unsafe path")
    return value


def _discover_snapshot_files(root: Path) -> tuple[_DiscoveredFile, ...]:
    discovered: list[_DiscoveredFile] = []
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for directory, directory_names, filenames in walker:
            current = Path(directory)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                child = current / name
                child_stat = os.lstat(child)
                if child.is_symlink() or not stat.S_ISDIR(child_stat.st_mode):
                    raise AdapterError("snapshot contains a non-directory entry")
                if name != _EXCLUDED_SNAPSHOT_DIRECTORY:
                    retained_directories.append(name)
            directory_names[:] = retained_directories
            for name in sorted(filenames):
                child = current / name
                child_stat = os.lstat(child)
                if child.is_symlink() or not stat.S_ISREG(child_stat.st_mode):
                    raise AdapterError("snapshot contains a non-regular file")
                relative = _safe_relative_path(
                    child.relative_to(root),
                    label="snapshot",
                )
                discovered.append(
                    _DiscoveredFile(
                        relative_path=relative,
                        absolute_path=child,
                        identity=_file_identity(child_stat),
                    )
                )
                if len(discovered) > _MAX_SNAPSHOT_FILES:
                    raise AdapterError("snapshot file count exceeds its limit")
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise AdapterError("snapshot tree cannot be inspected safely") from None
    return tuple(discovered)


def _hash_regular_file(
    path: Path,
    expected: _FileIdentity,
    *,
    label: str,
) -> tuple[str, int]:
    if (
        not stat.S_ISREG(expected.mode)
        or expected.links != 1
        or expected.size < 0
    ):
        raise AdapterError(f"{label} is not an independent regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AdapterError(f"{label} cannot be opened safely") from None
    try:
        if _file_identity(os.fstat(descriptor)) != expected:
            raise AdapterError(f"{label} changed before hashing")
        digest = hashlib.sha256()
        remaining = expected.size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise AdapterError(f"{label} was truncated during hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AdapterError(f"{label} grew during hashing")
        if _file_identity(os.fstat(descriptor)) != expected:
            raise AdapterError(f"{label} changed during hashing")
        try:
            final_path_identity = _file_identity(os.lstat(path))
        except OSError:
            raise AdapterError(f"{label} changed during hashing") from None
        if final_path_identity != expected:
            raise AdapterError(f"{label} changed during hashing")
        return f"sha256:{digest.hexdigest()}", expected.size
    finally:
        os.close(descriptor)


def _read_small_regular_file(
    path: Path,
    expected: _FileIdentity,
    *,
    label: str,
    limit: int,
) -> bytes:
    if (
        not stat.S_ISREG(expected.mode)
        or expected.links != 1
        or not 0 <= expected.size <= limit
    ):
        raise AdapterError(f"{label} is not a bounded independent regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AdapterError(f"{label} cannot be opened safely") from None
    try:
        if _file_identity(os.fstat(descriptor)) != expected:
            raise AdapterError(f"{label} changed before reading")
        remaining = expected.size
        content = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise AdapterError(f"{label} was truncated during reading")
            content.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AdapterError(f"{label} grew during reading")
        if _file_identity(os.fstat(descriptor)) != expected:
            raise AdapterError(f"{label} changed during reading")
        if _file_identity(os.lstat(path)) != expected:
            raise AdapterError(f"{label} changed during reading")
        return bytes(content)
    except OSError:
        raise AdapterError(f"{label} changed during reading") from None
    finally:
        os.close(descriptor)


def snapshot_directory_identity(
    path: Path,
    *,
    kind: Literal["model", "tokenizer"],
    revision: str,
) -> SnapshotIdentity:
    """Hash one immutable logical snapshot while refusing links and special files."""

    if kind not in {"model", "tokenizer"}:
        raise AdapterError("snapshot kind is invalid")
    root = _require_real_directory(path, label=f"managed vLLM {kind} snapshot")
    before = _discover_snapshot_files(root)
    if not before:
        raise AdapterError(f"managed vLLM {kind} snapshot is empty")
    entries: list[dict[str, str | int]] = []
    total_bytes = 0
    for item in before:
        digest, size = _hash_regular_file(
            item.absolute_path,
            item.identity,
            label=f"managed vLLM {kind} snapshot file",
        )
        total_bytes += size
        if total_bytes > _MAX_SNAPSHOT_BYTES:
            raise AdapterError(f"managed vLLM {kind} snapshot exceeds its size limit")
        entries.append(
            {
                "path": item.relative_path,
                "sha256": digest,
                "size_bytes": size,
            }
        )
    after = _discover_snapshot_files(root)
    if before != after:
        raise AdapterError(f"managed vLLM {kind} snapshot changed during hashing")
    manifest_bytes = canonical_json_bytes(
        {
            "files": entries,
            "hash_policy": "regular-files-excluding-dot-cache-v1",
        }
    )
    try:
        return SnapshotIdentity(
            kind=kind,
            root=str(root),
            revision=revision,
            sha256=sha256_digest(manifest_bytes),
            file_count=len(entries),
            total_bytes=total_bytes,
            hash_policy="regular-files-excluding-dot-cache-v1",
        )
    except ValidationError:
        raise AdapterError(
            f"managed vLLM {kind} snapshot identity is invalid"
        ) from None


def _distribution_files(
    distribution: importlib.metadata.Distribution,
) -> tuple[_DiscoveredFile, ...]:
    package_files = distribution.files
    if package_files is None:
        raise AdapterError("installed vLLM distribution has no file inventory")
    base = Path(str(distribution.locate_file(""))).absolute()
    discovered: list[_DiscoveredFile] = []
    for package_file in sorted(package_files, key=str):
        relative = PurePosixPath(str(package_file))
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            continue
        relative_text = relative.as_posix()
        if len(relative_text.encode("utf-8")) > 4096:
            raise AdapterError("installed vLLM distribution path exceeds its limit")
        candidate = Path(str(distribution.locate_file(package_file))).absolute()
        try:
            candidate.relative_to(base)
            candidate_stat = os.lstat(candidate)
        except (OSError, ValueError):
            raise AdapterError(
                "installed vLLM distribution file cannot be inspected"
            ) from None
        if candidate.is_symlink() or not stat.S_ISREG(candidate_stat.st_mode):
            raise AdapterError(
                "installed vLLM distribution contains a non-regular file"
            )
        discovered.append(
            _DiscoveredFile(
                relative_path=relative_text,
                absolute_path=candidate,
                identity=_file_identity(candidate_stat),
            )
        )
    if (
        not discovered
        or len(discovered) > _MAX_SNAPSHOT_FILES
        or not any(item.relative_path.startswith("vllm/") for item in discovered)
    ):
        raise AdapterError("installed vLLM distribution inventory is invalid")
    relative_paths = [item.relative_path for item in discovered]
    absolute_paths = [item.absolute_path for item in discovered]
    if (
        len(relative_paths) != len(set(relative_paths))
        or len(absolute_paths) != len(set(absolute_paths))
    ):
        raise AdapterError("installed vLLM distribution inventory is duplicated")
    return tuple(discovered)


def _distribution_source_wheel(
    files: tuple[_DiscoveredFile, ...],
) -> tuple[str, str, str]:
    try:
        expected_filename, expected_sha256 = expected_vllm_source_wheel(
            platform.machine()
        )
    except ValueError:
        raise AdapterError("managed vLLM host architecture is unsupported") from None
    direct_url_files = tuple(
        item
        for item in files
        if PurePosixPath(item.relative_path).name == "direct_url.json"
        and PurePosixPath(item.relative_path).parent.name.endswith(".dist-info")
    )
    if len(direct_url_files) != 1:
        raise AdapterError("installed vLLM source-wheel metadata is unavailable")
    content = _read_small_regular_file(
        direct_url_files[0].absolute_path,
        direct_url_files[0].identity,
        label="installed vLLM source-wheel metadata",
        limit=65_536,
    )
    try:
        text = content.decode("utf-8")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise AdapterError("installed vLLM source-wheel metadata is invalid") from None
    if not isinstance(value, dict) or set(value) != {"archive_info", "url"}:
        raise AdapterError("installed vLLM source-wheel metadata is invalid")
    archive_info = value["archive_info"]
    source_url = value["url"]
    if not isinstance(archive_info, dict) or not isinstance(source_url, str):
        raise AdapterError("installed vLLM source-wheel metadata is invalid")
    parsed_url = urlsplit(source_url)
    source_wheel_path = Path(unquote(parsed_url.path))
    if (
        parsed_url.scheme != "file"
        or parsed_url.netloc not in {"", "localhost"}
        or parsed_url.query
        or parsed_url.fragment
        or not source_wheel_path.is_absolute()
        or source_wheel_path.name != expected_filename
    ):
        raise AdapterError("installed vLLM source wheel differs from its pin")
    expected_hex = expected_sha256.removeprefix("sha256:")
    observed_hashes: set[str] = set()
    legacy_hash = archive_info.get("hash")
    hashes = archive_info.get("hashes")
    if isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
        observed_hashes.add(legacy_hash.removeprefix("sha256="))
    if isinstance(hashes, dict) and isinstance(hashes.get("sha256"), str):
        observed_hashes.add(hashes["sha256"])
    if observed_hashes != {expected_hex}:
        raise AdapterError("installed vLLM source wheel hash differs from its pin")
    try:
        source_wheel_stat = os.lstat(source_wheel_path)
    except OSError:
        raise AdapterError("installed vLLM source wheel is unavailable") from None
    source_wheel_digest, _ = _hash_regular_file(
        source_wheel_path,
        _file_identity(source_wheel_stat),
        label="installed vLLM source wheel",
    )
    if source_wheel_digest != expected_sha256:
        raise AdapterError("installed vLLM source wheel bytes differ from its pin")
    return expected_filename, expected_sha256, str(source_wheel_path)


def collect_vllm_distribution_identity() -> VllmDistributionIdentity:
    executable = Path(sys.executable).absolute().parent / "vllm"
    try:
        executable_stat = os.lstat(executable)
    except OSError:
        raise AdapterError(
            "managed vLLM executable is unavailable in the Inferdrome environment"
        ) from None
    executable_digest, _ = _hash_regular_file(
        executable,
        _file_identity(executable_stat),
        label="managed vLLM executable",
    )
    try:
        distribution = importlib.metadata.distribution("vllm")
    except importlib.metadata.PackageNotFoundError:
        raise AdapterError("managed vLLM distribution is unavailable") from None
    if distribution.version != "0.26.0":
        raise AdapterError("managed vLLM distribution is not pinned to 0.26.0")
    before = _distribution_files(distribution)
    (
        source_wheel_filename,
        source_wheel_sha256,
        source_wheel_path,
    ) = _distribution_source_wheel(before)
    entries: list[dict[str, str | int]] = []
    total_bytes = 0
    for item in before:
        digest, size = _hash_regular_file(
            item.absolute_path,
            item.identity,
            label="installed vLLM distribution file",
        )
        total_bytes += size
        if total_bytes > _MAX_DISTRIBUTION_BYTES:
            raise AdapterError("installed vLLM distribution exceeds its size limit")
        entries.append(
            {
                "path": item.relative_path,
                "sha256": digest,
                "size_bytes": size,
            }
        )
    if before != _distribution_files(distribution):
        raise AdapterError("installed vLLM distribution changed during hashing")
    manifest_bytes = canonical_json_bytes(
        {
            "files": entries,
            "hash_policy": "installed-wheel-files-v1",
        }
    )
    try:
        return VllmDistributionIdentity(
            name="vllm",
            version="0.26.0",
            sha256=sha256_digest(manifest_bytes),
            file_count=len(entries),
            total_bytes=total_bytes,
            hash_policy="installed-wheel-files-v1",
            executable_path=str(executable),
            executable_sha256=executable_digest,
            source_wheel_filename=source_wheel_filename,
            source_wheel_path=source_wheel_path,
            source_wheel_sha256=source_wheel_sha256,
        )
    except ValidationError:
        raise AdapterError("installed vLLM distribution identity is invalid") from None


def _decode_query_output(content: bytes, *, label: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError(f"{label} is not valid UTF-8") from None


def _successful_query(process: ProcessCapture, *, label: str) -> None:
    if (
        process.termination is not ProcessTermination.EXITED
        or process.exit_status != 0
    ):
        raise AdapterError(f"{label} did not complete successfully")


def _resolve_nvidia_smi() -> tuple[str, str]:
    executable_text = shutil.which("nvidia-smi")
    if executable_text is None:
        raise AdapterError("managed vLLM requires nvidia-smi")
    try:
        executable = Path(executable_text).resolve(strict=True)
        executable_stat = os.lstat(executable)
    except (OSError, RuntimeError):
        raise AdapterError("nvidia-smi cannot be inspected") from None
    digest, _ = _hash_regular_file(
        executable,
        _file_identity(executable_stat),
        label="nvidia-smi executable",
    )
    return str(executable), digest


def _collect_gpu_inventory(
    nvidia_smi_path: str,
    indices: tuple[int, ...],
    *,
    cwd: Path,
    process_runner: ProcessRunner,
) -> tuple[tuple[GpuDeviceEvidence, ...], tuple[str, ...], str]:
    argv = gpu_inventory_argv(nvidia_smi_path, indices)
    process = process_runner(
        argv,
        cwd=cwd,
        max_runtime_seconds=30,
        output_limit_bytes=_MAX_QUERY_BYTES,
        merge_stderr=True,
    )
    _successful_query(process, label="GPU inventory query")
    output = _decode_query_output(process.stdout, label="GPU inventory output")
    try:
        parsed = parse_gpu_inventory_output(output)
        gpus = tuple(
            GpuDeviceEvidence(
                index=index,
                model=model,
                uuid=uuid,
                driver_version=driver,
            )
            for index, model, uuid, driver in parsed
        )
    except (ValidationError, ValueError):
        raise AdapterError("GPU inventory output is invalid") from None
    if tuple(gpu.index for gpu in gpus) != indices:
        raise AdapterError("GPU inventory does not match selected indices")
    return gpus, argv, output


def _collect_torch_cuda_runtime() -> tuple[str, str, int]:
    try:
        torch: Any = importlib.import_module("torch")
        torch_version = torch.__version__
        cuda_version = torch.version.cuda
        cuda_available = torch.cuda.is_available()
        device_count = torch.cuda.device_count()
    except (AttributeError, ImportError, RuntimeError):
        raise AdapterError("managed vLLM CUDA runtime cannot be inspected") from None
    if (
        not isinstance(torch_version, str)
        or not isinstance(cuda_version, str)
        or cuda_available is not True
        or isinstance(device_count, bool)
        or not isinstance(device_count, int)
        or not 1 <= device_count <= 256
    ):
        raise AdapterError("managed vLLM requires an available CUDA runtime")
    return torch_version, cuda_version, device_count


class ManagedVllmServer:
    """One isolated server process supervised for the duration of one run."""

    def __init__(
        self,
        spec: ExperimentSpec,
        *,
        run_id: str,
        config: ManagedVllmConfig,
        tokenizer_path: Path,
        cwd: Path,
        cancellation: CancellationToken,
        process_runner: ProcessRunner,
        preflight_probe: PreflightProbe,
    ) -> None:
        self._spec = spec
        self._target = validate_managed_vllm_target(spec)
        self._run_id = run_id
        self._config = config
        self._tokenizer_path = tokenizer_path.absolute()
        self._cwd = _require_real_directory(cwd, label="managed vLLM workspace")
        self._cancellation = cancellation
        self._process_runner = process_runner
        self._preflight_probe = preflight_probe
        self._server_cancellation = CancellationToken()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._started_at: datetime | None = None
        self._capture: ProcessCapture | None = None
        self._error: BaseException | None = None

        if not isinstance(spec.execution, VllmExecution):
            raise AdapterError("managed vLLM requires pinned vLLM execution")
        if config.startup_timeout_seconds + spec.execution.max_runtime_seconds > 86_340:
            raise AdapterError("managed vLLM total runtime exceeds its limit")
        if platform.system() != "Linux":
            raise AdapterError("managed vLLM real-GPU proof requires Linux")
        if any(
            name in os.environ
            for name in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES")
        ):
            raise AdapterError(
                "managed vLLM requires unset GPU visibility remapping"
            )
        client_arch = platform.machine()
        if client_arch not in {"aarch64", "x86_64"}:
            raise AdapterError("managed vLLM host architecture is unsupported")
        self._client_arch = cast(Literal["aarch64", "x86_64"], client_arch)

        model_revision = self._target.model_revision
        tokenizer_revision = self._target.tokenizer_revision
        if model_revision is None or tokenizer_revision is None:
            raise AssertionError
        self._model_snapshot = snapshot_directory_identity(
            config.model_path,
            kind="model",
            revision=model_revision,
        )
        if config.model_path.absolute() == self._tokenizer_path:
            self._tokenizer_snapshot = SnapshotIdentity(
                kind="tokenizer",
                root=self._model_snapshot.root,
                revision=tokenizer_revision,
                sha256=self._model_snapshot.sha256,
                file_count=self._model_snapshot.file_count,
                total_bytes=self._model_snapshot.total_bytes,
                hash_policy=self._model_snapshot.hash_policy,
            )
        else:
            self._tokenizer_snapshot = snapshot_directory_identity(
                self._tokenizer_path,
                kind="tokenizer",
                revision=tokenizer_revision,
            )
        self._distribution = collect_vllm_distribution_identity()
        self._nvidia_smi_path, self._nvidia_smi_sha256 = _resolve_nvidia_smi()
        self._gpus, self._gpu_query_argv, self._gpu_query_stdout = (
            _collect_gpu_inventory(
                self._nvidia_smi_path,
                config.gpu_indices,
                cwd=self._cwd,
                process_runner=process_runner,
            )
        )
        (
            self._torch_version,
            self._cuda_runtime_version,
            self._torch_cuda_device_count,
        ) = _collect_torch_cuda_runtime()
        if self._torch_cuda_device_count <= max(config.gpu_indices):
            raise AdapterError("selected GPU is absent from the CUDA runtime")
        self._argv = build_managed_server_argv(
            spec,
            executable_path=self._distribution.executable_path,
            model_path=self._model_snapshot.root,
            tokenizer_path=self._tokenizer_snapshot.root,
            gpu_indices=config.gpu_indices,
        )
        self._thread = threading.Thread(
            target=self._run_server,
            daemon=True,
            name="inferdrome-managed-vllm",
        )

    @classmethod
    def start(
        cls,
        spec: ExperimentSpec,
        *,
        run_id: str,
        config: ManagedVllmConfig,
        tokenizer_path: Path,
        cwd: Path,
        cancellation: CancellationToken,
        process_runner: ProcessRunner = run_captured_process,
        preflight_probe: PreflightProbe = preflight_attached_endpoint,
    ) -> ManagedVllmServer:
        server = cls(
            spec,
            run_id=run_id,
            config=config,
            tokenizer_path=tokenizer_path,
            cwd=cwd,
            cancellation=cancellation,
            process_runner=process_runner,
            preflight_probe=preflight_probe,
        )
        server._thread.start()
        deadline = monotonic() + 10
        while not server._started.wait(0.05):
            if server._finished.is_set():
                server._raise_background_failure(
                    "managed vLLM server exited during process start"
                )
            if monotonic() >= deadline:
                server._server_cancellation.request(CancellationReason.DEADLINE)
                server._finished.wait(15)
                raise AdapterError("managed vLLM server process start timed out")
        return server

    def _observe_start(self, pid: int, started_at: datetime) -> None:
        with self._lock:
            self._pid = pid
            self._started_at = started_at
        self._started.set()

    def _run_server(self) -> None:
        if not isinstance(self._spec.execution, VllmExecution):
            self._error = AssertionError()
            self._finished.set()
            return
        try:
            capture = self._process_runner(
                self._argv,
                cwd=self._cwd,
                max_runtime_seconds=(
                    self._config.startup_timeout_seconds
                    + self._spec.execution.max_runtime_seconds
                    + 60
                ),
                output_limit_bytes=_MAX_CAPTURE_BYTES,
                cancellation=self._server_cancellation,
                merge_stderr=False,
                on_start=self._observe_start,
            )
            with self._lock:
                self._capture = capture
        except BaseException as error:
            with self._lock:
                self._error = error
        finally:
            self._finished.set()

    def _raise_background_failure(self, message: str) -> None:
        with self._lock:
            error = self._error
            capture = self._capture
        if error is not None:
            raise AdapterError(message) from error
        if capture is not None:
            raise AdapterError(message)
        raise AdapterError(message)

    def assert_running(self) -> None:
        if self._finished.is_set():
            self._raise_background_failure("managed vLLM server exited unexpectedly")

    def _server_identity(self) -> tuple[int, datetime]:
        with self._lock:
            pid = self._pid
            started_at = self._started_at
        if pid is None or started_at is None:
            raise AdapterError("managed vLLM server process identity is unavailable")
        return pid, started_at

    def _matching_gpu_processes(
        self,
    ) -> tuple[
        tuple[GpuComputeProcessEvidence, ...],
        tuple[str, ...],
        str,
    ]:
        pid, _ = self._server_identity()
        argv = gpu_compute_process_argv(
            self._nvidia_smi_path,
            self._config.gpu_indices,
        )
        process = self._process_runner(
            argv,
            cwd=self._cwd,
            max_runtime_seconds=30,
            output_limit_bytes=_MAX_QUERY_BYTES,
            merge_stderr=True,
        )
        _successful_query(process, label="GPU compute-process query")
        output = _decode_query_output(
            process.stdout,
            label="GPU compute-process output",
        )
        try:
            parsed = parse_gpu_compute_process_output(output)
        except ValueError:
            raise AdapterError("GPU compute-process output is invalid") from None
        selected_uuids = {gpu.uuid for gpu in self._gpus}
        matched: list[GpuComputeProcessEvidence] = []
        for process_pid, gpu_uuid in parsed:
            if gpu_uuid not in selected_uuids:
                raise AdapterError(
                    "GPU process query returned an unselected device"
                )
            try:
                process_group_id = os.getpgid(process_pid)
            except (OSError, ProcessLookupError):
                raise _GpuProcessNotReady(
                    "selected GPU process identity changed during capture"
                ) from None
            if process_group_id != pid:
                raise AdapterError(
                    "selected GPU is shared with an unmanaged compute process"
                )
            try:
                matched.append(
                    GpuComputeProcessEvidence(
                        pid=process_pid,
                        process_group_id=process_group_id,
                        gpu_uuid=gpu_uuid,
                    )
                )
            except ValidationError:
                raise AdapterError("GPU compute-process evidence is invalid") from None
        if {item.gpu_uuid for item in matched} != selected_uuids:
            raise _GpuProcessNotReady(
                "managed vLLM has not engaged every selected GPU"
            )
        return tuple(matched), argv, output

    def wait_until_ready(self) -> tuple[EndpointPreflightCapture, LocalGpuProof]:
        deadline = monotonic() + self._config.startup_timeout_seconds
        while True:
            self._cancellation.raise_if_requested()
            self.assert_running()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AdapterError("managed vLLM server readiness timed out")
            try:
                preflight = self._preflight_probe(
                    self._target,
                    timeout_seconds=min(1.0, remaining),
                )
            except AdapterError:
                self._cancellation.wait(min(0.25, remaining))
                continue
            try:
                gpu_processes, compute_argv, compute_stdout = (
                    self._matching_gpu_processes()
                )
            except _GpuProcessNotReady:
                self._cancellation.wait(min(0.25, remaining))
                continue
            pid, started_at = self._server_identity()
            ready_at = datetime.now(UTC)
            try:
                proof = LocalGpuProof(
                    schema_version="inferdrome.local-gpu-proof.v1",
                    run_id=self._run_id,
                    capture_mode="managed_local_vllm",
                    captured_at=ready_at,
                    client_os="Linux",
                    client_arch=self._client_arch,
                    client_python_version=platform.python_version(),
                    torch_version=self._torch_version,
                    cuda_runtime_version=self._cuda_runtime_version,
                    torch_cuda_device_count=self._torch_cuda_device_count,
                    nvidia_smi_path=self._nvidia_smi_path,
                    nvidia_smi_sha256=self._nvidia_smi_sha256,
                    gpu_query_argv=self._gpu_query_argv,
                    gpu_query_stdout=self._gpu_query_stdout,
                    selected_gpu_indices=self._config.gpu_indices,
                    gpus=self._gpus,
                    producer_distribution=self._distribution,
                    model_snapshot=self._model_snapshot,
                    tokenizer_snapshot=self._tokenizer_snapshot,
                    server=ManagedServerEvidence(
                        argv=self._argv,
                        endpoint=str(self._target.endpoint).rstrip("/"),
                        pid=pid,
                        process_group_id=pid,
                        started_at=started_at,
                        ready_at=ready_at,
                        compute_query_argv=compute_argv,
                        compute_query_stdout=compute_stdout,
                        gpu_processes=gpu_processes,
                    ),
                )
            except ValidationError:
                raise AdapterError("managed vLLM local GPU proof is invalid") from None
            return preflight, validate_local_gpu_proof(
                self._spec,
                proof,
                run_id=self._run_id,
            )

    def assert_inputs_unchanged(self) -> None:
        model = snapshot_directory_identity(
            Path(self._model_snapshot.root),
            kind="model",
            revision=self._model_snapshot.revision,
        )
        if model != self._model_snapshot:
            raise AdapterError("managed vLLM model snapshot changed during execution")
        if self._tokenizer_snapshot.root == self._model_snapshot.root:
            tokenizer = SnapshotIdentity(
                kind="tokenizer",
                root=model.root,
                revision=self._tokenizer_snapshot.revision,
                sha256=model.sha256,
                file_count=model.file_count,
                total_bytes=model.total_bytes,
                hash_policy=model.hash_policy,
            )
        else:
            tokenizer = snapshot_directory_identity(
                Path(self._tokenizer_snapshot.root),
                kind="tokenizer",
                revision=self._tokenizer_snapshot.revision,
            )
        if tokenizer != self._tokenizer_snapshot:
            raise AdapterError(
                "managed vLLM tokenizer snapshot changed during execution"
            )
        if collect_vllm_distribution_identity() != self._distribution:
            raise AdapterError("managed vLLM distribution changed during execution")
        try:
            nvidia_smi_path = Path(self._nvidia_smi_path)
            nvidia_smi_stat = os.lstat(nvidia_smi_path)
        except OSError:
            raise AdapterError("nvidia-smi changed during execution") from None
        nvidia_smi_digest, _ = _hash_regular_file(
            nvidia_smi_path,
            _file_identity(nvidia_smi_stat),
            label="nvidia-smi executable",
        )
        if nvidia_smi_digest != self._nvidia_smi_sha256:
            raise AdapterError("nvidia-smi changed during execution")

    def stop(self) -> ProcessCapture:
        self._server_cancellation.request(CancellationReason.USER)
        if not self._finished.wait(20):
            raise AdapterError("managed vLLM server did not stop within limits")
        self._thread.join(1)
        if self._thread.is_alive():
            raise AdapterError("managed vLLM supervisor did not finish")
        with self._lock:
            error = self._error
            capture = self._capture
        if error is not None:
            raise AdapterError("managed vLLM server supervision failed") from error
        if capture is None:
            raise AdapterError("managed vLLM server capture is unavailable")
        return capture
