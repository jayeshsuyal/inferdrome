"""Fail-closed local transport preparation for the external P1 handoff.

This module deliberately treats ExitSpec contracts as opaque JSON.  It only
checks the identities needed to transport the handoff and to bind each source
file to its case.  It never evaluates an ExitSpec criterion or emits a
verdict.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

CASE_IDS = (
    "native-p95-under-20ms",
    "native-p95-under-10ms",
    "semantic-first-nonempty-under-20ms",
)
_DIGEST_PATTERN = r"sha256:[0-9a-f]{64}"
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_CONTRACT_BYTES = 4 * 1024 * 1024
_MAX_CONFIRMATION_BYTES = 512 * 1024
_MAX_SOURCE_BYTES = 512 * 1024
_MAX_WORKLOAD_BYTES = 64 * 1024 * 1024
_MAX_HANDOFF_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_HANDOFF_FILES = 12
_MAX_ARCHIVE_MEMBERS = 64


class ProspectiveHandoffError(RuntimeError):
    """Expected, user-facing handoff rejection."""


@dataclass(frozen=True)
class HandoffFile:
    relative_path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class HandoffCase:
    case_id: str
    contract: HandoffFile
    confirmation: HandoffFile
    source: HandoffFile
    contract_digest: str


@dataclass(frozen=True)
class HandoffSnapshot:
    """The one-read byte snapshot used for every subsequent handoff action."""

    root: Path
    manifest: HandoffFile
    complete: HandoffFile
    workload: HandoffFile
    cases: tuple[HandoffCase, ...]
    files: tuple[HandoffFile, ...]
    archive_path: Path | None = None
    archive_sha256: str | None = None
    archive_size_bytes: int | None = None

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    @property
    def workload_sha256(self) -> str:
        return self.workload.sha256

    @property
    def expected_contract_digests(self) -> tuple[str, ...]:
        return tuple(case.contract_digest for case in self.cases)


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


def _strict_json(content: bytes, *, label: str) -> dict[str, Any]:
    if not content or len(content) > _MAX_MANIFEST_BYTES:
        raise ProspectiveHandoffError(f"{label} is empty or exceeds its size limit")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProspectiveHandoffError(f"{label} is not strict JSON") from None
    if not isinstance(value, dict):
        raise ProspectiveHandoffError(f"{label} must be a JSON object")
    return value


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ProspectiveHandoffError(f"{label} has an invalid SHA-256 shape")
    if value.startswith("sha256:"):
        selected = value
    elif len(value) == 64:
        selected = "sha256:" + value
    else:
        selected = ""
    if len(selected) != 71 or any(
        character not in "0123456789abcdef" for character in selected[7:]
    ):
        raise ProspectiveHandoffError(f"{label} has an invalid SHA-256 shape")
    return selected


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ProspectiveHandoffError(f"{label} has an unsafe relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProspectiveHandoffError(f"{label} has an unsafe relative path")
    return value


def _read_regular_once(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[bytes, tuple[int, ...]]:
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        identity = _identity(before)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("not a single-link regular file")
        if before.st_size > maximum_bytes:
            raise ValueError("file is too large")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(content) > maximum_bytes or _identity(after) != identity:
            raise ValueError("file changed while it was read")
        path_identity = _identity(os.lstat(path))
        if path_identity != identity:
            raise ValueError("file changed after it was read")
        return content, identity
    except (OSError, ValueError):
        raise ProspectiveHandoffError(f"{label} is unavailable or unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _walk_inventory(root: Path) -> dict[str, tuple[int, ...]]:
    try:
        root_metadata = os.lstat(root)
    except OSError:
        raise ProspectiveHandoffError(
            "prospective handoff root is unavailable"
        ) from None
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise ProspectiveHandoffError(
            "prospective handoff root must be a real directory"
        )
    result: dict[str, tuple[int, ...]] = {"": _identity(root_metadata)}
    regular_file_count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise ProspectiveHandoffError(
                "prospective handoff cannot be enumerated"
            ) from None
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise ProspectiveHandoffError(
                    "prospective handoff entry cannot be inspected"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ProspectiveHandoffError(
                    f"prospective handoff contains a symlink: {relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                result[relative] = _identity(metadata)
                pending.append(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                regular_file_count += 1
                if regular_file_count > _MAX_HANDOFF_FILES:
                    raise ProspectiveHandoffError(
                        "prospective handoff has too many files"
                    )
                result[relative] = _identity(metadata)
            else:
                raise ProspectiveHandoffError(
                    f"prospective handoff contains an unsafe entry: {relative}"
                )
    return result


def _file_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    return path


def _entry_digest(value: object, *, label: str) -> tuple[str, int | None]:
    if isinstance(value, str):
        return _digest(value, label=label), None
    if not isinstance(value, dict):
        raise ProspectiveHandoffError(f"{label} is invalid")
    digest_value = value.get("sha256", value.get("digest"))
    digest = _digest(digest_value, label=label)
    size_value = value.get("size_bytes", value.get("size"))
    if size_value is None:
        return digest, None
    if (
        isinstance(size_value, bool)
        or not isinstance(size_value, int)
        or not 1 <= size_value <= _MAX_WORKLOAD_BYTES
    ):
        raise ProspectiveHandoffError(f"{label} has an invalid byte count")
    return digest, size_value


def _manifest_file_entries(
    manifest: dict[str, Any],
) -> dict[str, tuple[str, int | None]]:
    raw = manifest.get("files", manifest.get("artifacts"))
    if raw is None and isinstance(manifest.get("cases"), list):
        result: dict[str, tuple[str, int | None]] = {}
        for item in manifest["cases"]:
            if not isinstance(item, dict):
                raise ProspectiveHandoffError("handoff case entry is invalid")
            for path_key, digest_key in (
                ("contract_artifact_path", "contract_artifact_sha256"),
                ("confirmation_artifact_path", "confirmation_record_sha256"),
                ("source_yaml_artifact_path", "source_yaml_artifact_sha256"),
            ):
                path = _safe_relative(item.get(path_key), label="handoff file")
                if path in result:
                    raise ProspectiveHandoffError(
                        "handoff file manifest repeats a path"
                    )
                result[path] = _entry_digest(
                    item.get(digest_key),
                    label=f"handoff file {path}",
                )
        workload_path = _safe_relative(
            manifest.get("workload_artifact_path"),
            label="handoff workload file",
        )
        result[workload_path] = _entry_digest(
            manifest.get("workload_artifact_sha256"),
            label="handoff workload file",
        )
        return result
    if isinstance(raw, dict):
        entries: list[tuple[str, Any]] = list(raw.items())
    elif isinstance(raw, list):
        entries = []
        for item in raw:
            if not isinstance(item, dict):
                raise ProspectiveHandoffError("handoff file manifest entry is invalid")
            path = item.get("path", item.get("relative_path"))
            entries.append((path, item))
    else:
        raise ProspectiveHandoffError("handoff manifest omits its file hashes")
    result: dict[str, tuple[str, int | None]] = {}
    for raw_path, raw_entry in entries:
        path = _safe_relative(raw_path, label="handoff file")
        if path in result:
            raise ProspectiveHandoffError("handoff file manifest repeats a path")
        result[path] = _entry_digest(raw_entry, label=f"handoff file {path}")
    return result


def _case_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = manifest.get("cases")
    if isinstance(raw, dict):
        values = []
        for case_id, value in raw.items():
            if not isinstance(value, dict):
                raise ProspectiveHandoffError("handoff case entry is invalid")
            values.append({"case_id": case_id, **value})
    elif isinstance(raw, list):
        values = raw
    else:
        raise ProspectiveHandoffError("handoff manifest omits its cases")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("case_id"), str):
            raise ProspectiveHandoffError("handoff case entry is invalid")
        case_id = value["case_id"]
        if case_id in result:
            raise ProspectiveHandoffError("handoff case IDs are duplicated")
        result[case_id] = value
    if tuple(result) != CASE_IDS:
        raise ProspectiveHandoffError(
            "handoff cases are not the three canonical case IDs"
        )
    return result


def _case_path(value: dict[str, Any], names: tuple[str, ...], *, case_id: str) -> str:
    for name in names:
        candidate = value.get(name)
        if candidate is not None:
            return _safe_relative(candidate, label=f"{case_id} {name}")
    raise ProspectiveHandoffError(f"handoff case omits its {names[0]}: {case_id}")


def _contract_digest(
    contract: dict[str, Any], case: dict[str, Any], *, case_id: str
) -> str:
    for value in (
        case.get("producer_contract_link"),
        contract.get("canonical_hash"),
        contract.get("contract_digest"),
        contract.get("contract_sha256"),
        case.get("contract_digest"),
        case.get("contract_sha256"),
    ):
        if value is not None:
            return _digest(value, label=f"{case_id} contract digest")
    raise ProspectiveHandoffError(f"{case_id} frozen contract omits its digest")


def _affirmative_confirmation(
    contract: dict[str, Any], confirmation: dict[str, Any], *, case_id: str
) -> None:
    decision = confirmation.get("decision", confirmation.get("status"))
    if decision != "CONFIRM" or confirmation.get("agreement_acknowledged") is not True:
        raise ProspectiveHandoffError(f"{case_id} confirmation is not affirmative")
    contract_id = contract.get("id", contract.get("contract_id"))
    contract_version = contract.get("version", contract.get("contract_version"))
    if (
        confirmation.get("contract_id") != contract_id
        or confirmation.get("contract_version") != contract_version
        or (
            contract.get("confirmation_id") is not None
            and confirmation.get("confirmation_id") != contract.get("confirmation_id")
        )
    ):
        raise ProspectiveHandoffError(
            f"{case_id} confirmation is bound to another contract"
        )
    if confirmation.get("contract_fingerprint") is None:
        raise ProspectiveHandoffError(f"{case_id} confirmation omits its fingerprint")


def _workload_digest(
    manifest: dict[str, Any], files: dict[str, tuple[str, int | None]]
) -> str:
    candidates: list[object] = [
        manifest.get("workload_sha256"),
        manifest.get("workload_digest"),
        manifest.get("workload_artifact_sha256"),
    ]
    workload = manifest.get("workload")
    if isinstance(workload, dict):
        candidates.extend([workload.get("sha256"), workload.get("digest")])
    candidates.extend(
        digest
        for path, (digest, _size) in files.items()
        if path == "sources/real-gpu/workload.jsonl"
    )
    for candidate in candidates:
        if candidate is not None:
            return _digest(candidate, label="handoff workload digest")
    raise ProspectiveHandoffError("handoff manifest omits the workload digest")


def snapshot_handoff(
    handoff_root: Path,
    *,
    expected_manifest_sha256: str | None = None,
    expected_workload_sha256: str | None = None,
) -> HandoffSnapshot:
    """Read the complete handoff exactly once and validate its byte snapshot."""

    root = handoff_root.expanduser().absolute()
    before = _walk_inventory(root)
    allowed_root = {".complete", "handoff-manifest.json"}
    nested = set(before) - {""} - allowed_root
    file_paths = {
        path for path, identity in before.items() if path and stat.S_ISREG(identity[2])
    }
    if len(file_paths) != _MAX_HANDOFF_FILES:
        raise ProspectiveHandoffError(
            "prospective handoff must contain exactly the bounded P1 file set"
        )
    if set(before) & {".complete", "handoff-manifest.json"} != allowed_root:
        raise ProspectiveHandoffError(
            "prospective handoff is missing its marker or manifest"
        )
    allowed_directories = {
        "contracts",
        "confirmations",
        "sources",
        "sources/real-gpu",
    }
    if any(
        path not in allowed_directories
        and path
        not in {
            ".complete",
            "handoff-manifest.json",
            "sources/real-gpu/workload.jsonl",
        }
        and not path.startswith("contracts/")
        and not path.startswith("confirmations/")
        and not path.startswith("sources/")
        for path in nested
    ):
        raise ProspectiveHandoffError("prospective handoff contains an extra path")

    maximums = {
        ".complete": 1_024,
        "handoff-manifest.json": _MAX_MANIFEST_BYTES,
        "sources/real-gpu/workload.jsonl": _MAX_WORKLOAD_BYTES,
    }
    contents: dict[str, bytes] = {}
    identities: dict[str, tuple[int, ...]] = {}
    for relative, identity in before.items():
        if not relative or not stat.S_ISREG(identity[2]):
            continue
        maximum = maximums.get(relative)
        if maximum is None:
            if relative.startswith("contracts/"):
                maximum = _MAX_CONTRACT_BYTES
            elif relative.startswith("confirmations/"):
                maximum = _MAX_CONFIRMATION_BYTES
            elif relative.startswith("sources/"):
                maximum = _MAX_SOURCE_BYTES
            else:
                raise ProspectiveHandoffError(
                    f"prospective handoff contains an extra path: {relative}"
                )
        contents[relative], identities[relative] = _read_regular_once(
            _file_path(root, relative),
            label=f"handoff file {relative}",
            maximum_bytes=maximum,
        )
    after = _walk_inventory(root)
    if after != before:
        raise ProspectiveHandoffError("prospective handoff changed while it was staged")

    manifest_value = _strict_json(
        contents["handoff-manifest.json"], label="handoff manifest"
    )
    schema = manifest_value.get("schema_version")
    if schema != "exitspec.inferdrome-prospective-handoff.v1":
        raise ProspectiveHandoffError("handoff manifest schema version is unsupported")
    if (
        manifest_value.get("acceptance_verdict") is not None
        or manifest_value.get("authority_boundary")
        != "EXIT_SPEC_CUSTOMER_CONFIRMED_HANDOFF_ONLY"
        or manifest_value.get("completion_marker") != ".complete"
        or manifest_value.get("workload_artifact_path")
        != "sources/real-gpu/workload.jsonl"
    ):
        raise ProspectiveHandoffError("handoff manifest acceptance boundary is invalid")
    file_entries = _manifest_file_entries(manifest_value)
    expected_paths = set(contents) - {"handoff-manifest.json", ".complete"}
    if set(file_entries) != expected_paths:
        raise ProspectiveHandoffError(
            "handoff manifest file allowlist disagrees with the snapshot"
        )
    for relative, (digest, size) in file_entries.items():
        actual = _digest_bytes(contents[relative])
        if actual != digest or (size is not None and size != len(contents[relative])):
            raise ProspectiveHandoffError(
                f"handoff file hash or size disagrees: {relative}"
            )
    workload_digest = _workload_digest(manifest_value, file_entries)
    if workload_digest != _digest_bytes(contents["sources/real-gpu/workload.jsonl"]):
        raise ProspectiveHandoffError("handoff workload digest disagrees")
    if expected_manifest_sha256 is not None and _digest(
        expected_manifest_sha256, label="expected handoff manifest digest"
    ) != _digest_bytes(contents["handoff-manifest.json"]):
        raise ProspectiveHandoffError(
            "handoff manifest digest disagrees with its operator pin"
        )
    if (
        expected_workload_sha256 is not None
        and _digest(expected_workload_sha256, label="expected workload digest")
        != workload_digest
    ):
        raise ProspectiveHandoffError("workload digest disagrees with its operator pin")

    case_values = _case_entries(manifest_value)
    cases: list[HandoffCase] = []
    contract_digests: set[str] = set()
    selected_paths: set[str] = set()
    for case_id in CASE_IDS:
        selected = case_values[case_id]
        contract_path = _case_path(
            selected,
            (
                "contract_artifact_path",
                "contract_path",
                "contract",
                "contract_file",
            ),
            case_id=case_id,
        )
        confirmation_path = _case_path(
            selected,
            (
                "confirmation_artifact_path",
                "confirmation_path",
                "confirmation",
                "confirmation_file",
            ),
            case_id=case_id,
        )
        source_path = _case_path(
            selected,
            (
                "source_yaml_artifact_path",
                "source_path",
                "source",
                "source_file",
            ),
            case_id=case_id,
        )
        if (
            not contract_path.startswith("contracts/")
            or not confirmation_path.startswith("confirmations/")
            or not source_path.startswith("sources/")
            or source_path == "sources/real-gpu/workload.jsonl"
        ):
            raise ProspectiveHandoffError(
                f"{case_id} paths are outside the P1 allowlist"
            )
        selected_paths.update({contract_path, confirmation_path, source_path})
        contract_bytes = contents.get(contract_path)
        confirmation_bytes = contents.get(confirmation_path)
        source_bytes = contents.get(source_path)
        if contract_bytes is None or confirmation_bytes is None or source_bytes is None:
            raise ProspectiveHandoffError(f"{case_id} handoff files are missing")
        contract_value = _strict_json(
            contract_bytes, label=f"{case_id} frozen contract"
        )
        confirmation_value = _strict_json(
            confirmation_bytes, label=f"{case_id} confirmation"
        )
        contract_id = contract_value.get("id", contract_value.get("contract_id"))
        if not isinstance(contract_id, str) or case_id not in contract_id:
            raise ProspectiveHandoffError(
                f"{case_id} contract is not tagged with its case ID"
            )
        _affirmative_confirmation(contract_value, confirmation_value, case_id=case_id)
        if (
            selected.get("contract_id") is not None
            and selected.get("contract_id") != contract_id
        ) or (
            selected.get("contract_version") is not None
            and selected.get("contract_version")
            != contract_value.get("version", contract_value.get("contract_version"))
        ):
            raise ProspectiveHandoffError(
                f"{case_id} manifest contract identity disagrees"
            )
        if (
            selected.get("confirmation_id") is not None
            and selected.get("confirmation_id")
            != confirmation_value.get("confirmation_id")
        ) or (
            selected.get("contract_confirmation_fingerprint") is not None
            and selected.get("contract_confirmation_fingerprint")
            != confirmation_value.get("contract_fingerprint")
        ):
            raise ProspectiveHandoffError(
                f"{case_id} manifest confirmation identity disagrees"
            )
        selected_digest = _contract_digest(contract_value, selected, case_id=case_id)
        if "contract_canonical_hash" in selected:
            canonical = _digest(
                selected["contract_canonical_hash"],
                label=f"{case_id} manifest contract hash",
            )
            if canonical != _digest(
                contract_value.get("canonical_hash"),
                label=f"{case_id} contract canonical hash",
            ):
                raise ProspectiveHandoffError(
                    f"{case_id} contract canonical hash disagrees"
                )
        if (
            "producer_contract_link" in selected
            and _digest(
                selected["producer_contract_link"],
                label=f"{case_id} producer contract link",
            )
            != selected_digest
        ):
            raise ProspectiveHandoffError(f"{case_id} producer contract link disagrees")
        if selected_digest in contract_digests:
            raise ProspectiveHandoffError(
                "the three frozen contract links must be distinct"
            )
        contract_digests.add(selected_digest)
        cases.append(
            HandoffCase(
                case_id=case_id,
                contract=HandoffFile(
                    contract_path, contract_bytes, _digest_bytes(contract_bytes)
                ),
                confirmation=HandoffFile(
                    confirmation_path,
                    confirmation_bytes,
                    _digest_bytes(confirmation_bytes),
                ),
                source=HandoffFile(
                    source_path, source_bytes, _digest_bytes(source_bytes)
                ),
                contract_digest=selected_digest,
            )
        )
    contract_paths = {path for path in expected_paths if path.startswith("contracts/")}
    confirmation_paths = {
        path for path in expected_paths if path.startswith("confirmations/")
    }
    source_paths = {
        path
        for path in expected_paths
        if path.startswith("sources/") and path != "sources/real-gpu/workload.jsonl"
    }
    if (
        contract_paths != {case.contract.relative_path for case in cases}
        or confirmation_paths != {case.confirmation.relative_path for case in cases}
        or source_paths != {case.source.relative_path for case in cases}
        or len(source_paths) != 3
        or any(
            Path(path).suffix.lower() not in {".yaml", ".yml"} for path in source_paths
        )
        or selected_paths != contract_paths | confirmation_paths | source_paths
    ):
        raise ProspectiveHandoffError(
            "handoff must contain exactly three contracts, confirmations, "
            "and source YAMLs"
        )
    complete = HandoffFile(
        ".complete", contents[".complete"], _digest_bytes(contents[".complete"])
    )
    if complete.content.strip().lower() not in {
        b"complete",
        b"p1-complete",
        b"true",
        b"exitspec.inferdrome-prospective-handoff.complete.v1",
    }:
        raise ProspectiveHandoffError("handoff completion marker is not affirmative")
    all_files = tuple(
        HandoffFile(relative, contents[relative], _digest_bytes(contents[relative]))
        for relative in sorted(contents)
    )
    return HandoffSnapshot(
        root=root,
        manifest=HandoffFile(
            "handoff-manifest.json",
            contents["handoff-manifest.json"],
            _digest_bytes(contents["handoff-manifest.json"]),
        ),
        complete=complete,
        workload=HandoffFile(
            "sources/real-gpu/workload.jsonl",
            contents["sources/real-gpu/workload.jsonl"],
            _digest_bytes(contents["sources/real-gpu/workload.jsonl"]),
        ),
        cases=tuple(cases),
        files=all_files,
    )


def _write_snapshot_file(root: Path, file: HandoffFile) -> None:
    destination = _file_path(root, file.relative_path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(file.content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ProspectiveHandoffError(
            "prospective handoff snapshot staging failed"
        ) from None


def create_handoff_archive(
    snapshot: HandoffSnapshot, destination: Path
) -> HandoffSnapshot:
    """Create a bounded archive solely from retained snapshot bytes."""

    if destination.exists() or destination.is_symlink():
        raise ProspectiveHandoffError(
            "prospective handoff archive destination already exists"
        )
    descriptor: int | None = None
    with tempfile.TemporaryDirectory(prefix="inferdrome-p1-handoff-") as temporary:
        staged = Path(temporary) / "handoff"
        staged.mkdir(mode=0o700)
        for file in snapshot.files:
            _write_snapshot_file(staged, file)
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with (
                os.fdopen(descriptor, "wb") as output,
                gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
                tarfile.open(
                    fileobj=compressed, mode="w:", format=tarfile.USTAR_FORMAT
                ) as archive,
            ):
                descriptor = None
                for file in snapshot.files:
                    info = tarfile.TarInfo(file.relative_path)
                    info.size = len(file.content)
                    info.mode = 0o400
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, io.BytesIO(file.content))
                output.flush()
                os.fsync(output.fileno())
            metadata = os.lstat(destination)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not 1 <= metadata.st_size <= _MAX_HANDOFF_ARCHIVE_BYTES
            ):
                raise OSError
        except (OSError, tarfile.TarError):
            if descriptor is not None:
                os.close(descriptor)
            destination.unlink(missing_ok=True)
            raise ProspectiveHandoffError(
                "prospective handoff archive is unavailable"
            ) from None
    archived_bytes, _identity = _read_regular_once(
        destination,
        label="prospective handoff archive",
        maximum_bytes=_MAX_HANDOFF_ARCHIVE_BYTES,
    )
    digest = _digest_bytes(archived_bytes)
    return HandoffSnapshot(
        root=snapshot.root,
        manifest=snapshot.manifest,
        complete=snapshot.complete,
        workload=snapshot.workload,
        cases=snapshot.cases,
        files=snapshot.files,
        archive_path=destination,
        archive_sha256=digest,
        archive_size_bytes=destination.stat().st_size,
    )


def handoff_archive_limit() -> int:
    return _MAX_HANDOFF_ARCHIVE_BYTES
