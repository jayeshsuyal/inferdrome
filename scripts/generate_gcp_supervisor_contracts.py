#!/usr/bin/env python3
"""Generate/check the additive v0.2 GCP safety-contract schemas."""

from __future__ import annotations

import argparse
import json
import os
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from inferdrome.deployment.gcp_cost_guard import gcp_cost_guard_contract_schemas
from inferdrome.deployment.gcp_lifecycle import (
    gcp_execution_v2_safety_contract_schemas,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    gcp_private_campaign_contract_schemas,
)
from inferdrome.deployment.gcp_securefs import SafeDirFD, SafeDirFSError
from inferdrome.deployment.gcp_supervisor import (
    gcp_execution_supervisor_contract_schemas,
)
from inferdrome.deployment.gcp_v2_contracts import gcp_v2_activation_contract_schemas
from inferdrome.deployment.gcp_v2_disk_cleanup import (
    gcp_v2_disk_cleanup_contract_schemas,
)
from inferdrome.deployment.gcp_watchdog_backend import (
    gcp_file_watchdog_backend_contract_schemas,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas/deployment/v2"
_MAX_SCHEMA_BYTES = 1_048_576
SchemaProducer = Callable[[], dict[str, dict[str, Any]]]
_SCHEMA_PRODUCERS: tuple[SchemaProducer, ...] = (
    gcp_execution_v2_safety_contract_schemas,
    gcp_execution_supervisor_contract_schemas,
    gcp_cost_guard_contract_schemas,
    gcp_v2_activation_contract_schemas,
    gcp_v2_disk_cleanup_contract_schemas,
    gcp_file_watchdog_backend_contract_schemas,
    gcp_private_campaign_contract_schemas,
)


def _pretty(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode()


def _collect_schemas() -> dict[str, dict[str, Any]]:
    """Collect producer outputs without allowing a later map to overwrite one."""

    schemas: dict[str, dict[str, Any]] = {}
    for producer in _SCHEMA_PRODUCERS:
        for filename, schema in producer().items():
            candidate = Path(filename)
            if (
                candidate.name != filename
                or candidate.suffixes != [".schema", ".json"]
                or not filename.startswith("gcp-")
            ):
                raise ValueError(f"invalid generated schema output name: {filename!r}")
            if filename in schemas:
                raise ValueError(f"duplicate generated schema output: {filename}")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError(
                    f"invalid Draft 2020-12 schema from {filename}: {error.message}"
                ) from error
            schemas[filename] = schema
    if not schemas:
        raise ValueError("no GCP v0.2 schemas were produced")
    return schemas


def _render() -> dict[Path, bytes]:
    schemas = _collect_schemas()
    return {
        SCHEMA_ROOT / filename: _pretty(schema) for filename, schema in schemas.items()
    }


def _open_schema_root() -> SafeDirFD:
    """Open the committed output directory without retaining a mutable path."""

    if not SCHEMA_ROOT.is_absolute():
        raise ValueError("generated schema root must be absolute")
    try:
        return SafeDirFD.open(SCHEMA_ROOT)
    except FileNotFoundError as error:
        raise ValueError("generated schema root is missing") from error
    except (SafeDirFSError, OSError) as error:
        raise ValueError("generated schema root is unsafe") from error


def _safe_schema_metadata(
    root: SafeDirFD,
    name: str,
) -> os.stat_result:
    """Require one direct private generated-schema file, never a link/tree."""

    try:
        return root.validated_regular_child(name)
    except FileNotFoundError:
        raise
    except SafeDirFSError as error:
        raise ValueError(
            f"unsafe generated schema path: {SCHEMA_ROOT / name}"
        ) from error


def _read_schema_bytes(root: SafeDirFD, name: str) -> bytes:
    """Read an exact regular child while detecting a name/inode substitution."""

    descriptor: int | None = None
    try:
        descriptor = root.open_child(name, os.O_RDONLY)
        metadata = root.validated_regular_child(name, descriptor=descriptor)
        if metadata.st_size > _MAX_SCHEMA_BYTES:
            raise ValueError(f"generated schema is too large: {SCHEMA_ROOT / name}")
        remaining = metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise ValueError(
                    f"generated schema changed while reading: {SCHEMA_ROOT / name}"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        root.validated_regular_child(name, descriptor=descriptor)
        return b"".join(chunks)
    except SafeDirFSError as error:
        raise ValueError(
            f"unsafe generated schema path: {SCHEMA_ROOT / name}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _existing_schema_paths(
    root: SafeDirFD,
    expected_paths: set[Path],
) -> tuple[set[Path], tuple[Path, ...], tuple[Path, ...]]:
    """Return expected regular files plus every unsafe/unmanaged direct entry.

    The generated v2 directory is intentionally flat.  Therefore an unexpected
    directory is itself a rejected nested-output tree; there is no safe reason
    to recurse through it before failing the check.
    """

    try:
        names = sorted(os.listdir(root.fd))
    except OSError as error:
        raise ValueError("could not list generated schema directory") from error
    expected_names = {path.name for path in expected_paths}
    regular: set[Path] = set()
    unsafe: list[Path] = []
    unmanaged: list[Path] = []
    for name in names:
        path = SCHEMA_ROOT / name
        try:
            metadata = root.stat_child(name)
        except (OSError, SafeDirFSError):
            unsafe.append(path)
            continue
        if name not in expected_names:
            # A direct directory necessarily contains only unexpected nested
            # generated outputs because the committed contract set is flat.
            if stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                unsafe.append(path)
            else:
                unmanaged.append(path)
            continue
        try:
            _safe_schema_metadata(root, name)
        except ValueError:
            unsafe.append(path)
        else:
            regular.add(path)
    return regular, tuple(sorted(unsafe)), tuple(sorted(unmanaged))


def _schema_directory_issues(
    rendered: dict[Path, bytes], *, require_current: bool
) -> tuple[str, ...]:
    """Check that the committed generated set is exact and valid."""

    expected_paths = set(rendered)
    try:
        root = _open_schema_root()
    except ValueError as error:
        return (str(error),)
    try:
        try:
            regular, unsafe, unmanaged = _existing_schema_paths(root, expected_paths)
        except ValueError as error:
            return (str(error),)
        issues = [f"unsafe generated schema path: {path}" for path in unsafe]
        issues.extend(f"unmanaged generated schema: {path}" for path in unmanaged)
        if not require_current:
            return tuple(issues)
        issues.extend(
            f"missing generated schema: {path}"
            for path in sorted(expected_paths - regular)
        )
        for path in sorted(regular & expected_paths):
            try:
                content = _read_schema_bytes(root, path.name)
            except (OSError, ValueError) as error:
                issues.append(f"could not read generated schema {path}: {error}")
                continue
            if content != rendered[path]:
                issues.append(f"stale generated schema: {path}")
            try:
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError("schema document must be an object")
                Draft202012Validator.check_schema(value)
            except (json.JSONDecodeError, SchemaError, ValueError) as error:
                issues.append(f"invalid generated schema {path}: {error}")
        return tuple(issues)
    finally:
        root.close()


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short generated-schema write")
        view = view[written:]


def _atomic_write_schema(root: SafeDirFD, path: Path, content: bytes) -> None:
    """Install one output through a private no-follow staging child."""

    name = path.name
    existing: os.stat_result | None
    try:
        existing = _safe_schema_metadata(root, name)
    except FileNotFoundError:
        existing = None
    stage_name: str | None = None
    descriptor: int | None = None
    stage_metadata: os.stat_result | None = None
    try:
        try:
            for _ in range(8):
                candidate = f".{name}.stage-{uuid.uuid4().hex}"
                try:
                    descriptor = root.open_child(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    continue
                stage_name = candidate
                break
            if descriptor is None or stage_name is None:
                raise ValueError(
                    f"could not reserve generated schema staging file: {path}"
                )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            stage_metadata = root.validated_regular_child(
                stage_name, descriptor=descriptor
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
        if stage_name is None:
            raise ValueError(f"could not reserve generated schema staging file: {path}")
        if stage_metadata is None:
            raise ValueError(f"generated schema staging file was not validated: {path}")
        root.replace_child(
            stage_name,
            name,
            expected_source=stage_metadata,
            expected_destination=existing,
        )
        if _read_schema_bytes(root, name) != content:
            raise ValueError(f"generated schema changed after atomic write: {path}")
    finally:
        # The source name no longer exists after a successful atomic replace.
        # If an error left it behind, remove only the verified private staging
        # child; a substituted or unsafe entry is deliberately left untouched.
        cleanup_metadata = stage_metadata
        if cleanup_metadata is None and stage_name is not None:
            with suppress(FileNotFoundError, SafeDirFSError):
                cleanup_metadata = root.validated_regular_child(stage_name)
        if cleanup_metadata is not None and stage_name is not None:
            with suppress(FileNotFoundError, SafeDirFSError, OSError):
                root.unlink_child(stage_name, expected=cleanup_metadata)


def _write_schemas(rendered: dict[Path, bytes]) -> None:
    root = _open_schema_root()
    try:
        expected_paths = set(rendered)
        _, unsafe, unmanaged = _existing_schema_paths(root, expected_paths)
        if unsafe or unmanaged:
            issues = [f"unsafe generated schema path: {path}" for path in unsafe]
            issues.extend(f"unmanaged generated schema: {path}" for path in unmanaged)
            raise ValueError("; ".join(issues))
        for path, content in sorted(rendered.items()):
            _atomic_write_schema(root, path, content)
    finally:
        root.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        rendered = _render()
        issues = _schema_directory_issues(rendered, require_current=args.check)
    except (OSError, ValueError) as error:
        print(f"GCP v0.2 safety schema generation failed: {error}")
        return 1
    if args.check:
        if issues:
            print("GCP v0.2 safety schemas are stale")
            for issue in issues:
                print(f"- {issue}")
            return 1
        print(f"GCP v0.2 safety schemas are current ({len(rendered)} files)")
        return 0
    if issues:
        print("GCP v0.2 safety schema generation refused unsafe directory state")
        for issue in issues:
            print(f"- {issue}")
        return 1
    try:
        _write_schemas(rendered)
    except (OSError, ValueError) as error:
        print(f"GCP v0.2 safety schema generation failed: {error}")
        return 1
    final_issues = _schema_directory_issues(rendered, require_current=True)
    if final_issues:
        print("GCP v0.2 safety schemas did not verify after atomic generation")
        for issue in final_issues:
            print(f"- {issue}")
        return 1
    print(f"generated GCP v0.2 safety schemas ({len(rendered)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
