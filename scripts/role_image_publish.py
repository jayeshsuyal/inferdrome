#!/usr/bin/env python3
"""Plan and validate a separately approved manual role-image publication.

This module is deliberately local-only: it neither imports Docker bindings nor
opens a network connection.  A future manually dispatched GitHub workflow may
use its bounded plan and local-inspection check after adding its own externally
observed registry-receipt boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

_SOURCE_REPOSITORY = "https://github.com/jayeshsuyal/inferdrome"
_PLATFORM = "linux/amd64"
_VLLM_VERSION = "0.26.0"
_ROLES = ("private-engine", "cpu-runner-observer")
_REPOSITORIES = {
    "private-engine": "ghcr.io/jayeshsuyal/inferdrome-private-engine",
    "cpu-runner-observer": "ghcr.io/jayeshsuyal/inferdrome-cpu-runner-observer",
}
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:[.-][0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_RUN_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_INPUT_BYTES = 65_536


class RoleImagePublicationError(ValueError):
    """A local publication-plan or receipt boundary was not satisfied."""


def _fail(message: str) -> NoReturn:
    raise RoleImagePublicationError(message)


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        _fail("publication record cannot be canonicalized")


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"publication {field} is invalid")
    return value


def _validate_role(role: object) -> str:
    if not isinstance(role, str) or role not in _ROLES:
        _fail("publication runtime role is invalid")
    return role


def _validate_commit(value: object) -> str:
    commit = _required_string(value, field="source commit")
    if _COMMIT.fullmatch(commit) is None:
        _fail("publication source commit is invalid")
    return commit


def _validate_version(value: object) -> str:
    version = _required_string(value, field="version")
    if len(version) > 64 or _VERSION.fullmatch(version) is None:
        _fail("publication version is invalid")
    return version


def _validate_run_id(value: object) -> str:
    run_id = _required_string(value, field="workflow run identity")
    if _RUN_ID.fullmatch(run_id) is None:
        _fail("publication workflow run identity is invalid")
    return run_id


def _expected_labels(*, role: str, source_commit: str, version: str) -> dict[str, str]:
    return {
        "org.opencontainers.image.source": _SOURCE_REPOSITORY,
        "org.opencontainers.image.revision": source_commit,
        "org.opencontainers.image.version": version,
        "com.inferdrome.image-purpose": role,
        "com.inferdrome.runtime-role": role,
        "com.inferdrome.engine-service-boundary": "separate-vllm-engine-service",
        "com.inferdrome.vllm-version": _VLLM_VERSION,
        "com.inferdrome.engine-entrypoint": (
            "inferdrome.deployment.gcp_private_engine_adapter"
        ),
    }


def plan_role_image(
    *, role: object, source_commit: object, version: object, workflow_run_id: object
) -> dict[str, object]:
    """Return the exact local plan for one fixed runtime role.

    The transient tag is only a transport alias.  A usable downstream runtime
    identity is emitted later as the returned ``repository@sha256`` digest.
    """

    validated_role = _validate_role(role)
    validated_commit = _validate_commit(source_commit)
    validated_version = _validate_version(version)
    validated_run_id = _validate_run_id(workflow_run_id)
    repository = _REPOSITORIES[validated_role]
    return {
        "schema_version": "inferdrome.role-image-publication-plan.v1",
        "role": validated_role,
        "repository": repository,
        "source_repository": _SOURCE_REPOSITORY,
        "source_commit": validated_commit,
        "inferdrome_version": validated_version,
        "platform": _PLATFORM,
        "workflow_run_id": validated_run_id,
        "transient_tag": (
            f"{repository}:publication-{validated_commit[:12]}-{validated_run_id}"
        ),
        "build": {
            "script": "scripts/build_runner_image.py",
            "flavor": "release",
            "image_kind": "vllm-benchmark-runner",
            "runtime_role": validated_role,
            "platform": _PLATFORM,
            "pull": False,
        },
        "required_labels": _expected_labels(
            role=validated_role,
            source_commit=validated_commit,
            version=validated_version,
        ),
    }


def _require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"publication {field} is invalid")
    return value


def validate_plan(value: object) -> dict[str, object]:
    """Return an exact fixed-repository plan or reject an altered record."""

    plan = _require_mapping(value, field="plan")
    allowed = {
        "schema_version",
        "role",
        "repository",
        "source_repository",
        "source_commit",
        "inferdrome_version",
        "platform",
        "workflow_run_id",
        "transient_tag",
        "build",
        "required_labels",
    }
    if set(plan) != allowed:
        _fail("publication plan fields are invalid")
    if plan.get("schema_version") != "inferdrome.role-image-publication-plan.v1":
        _fail("publication plan schema is invalid")
    expected = plan_role_image(
        role=plan.get("role"),
        source_commit=plan.get("source_commit"),
        version=plan.get("inferdrome_version"),
        workflow_run_id=plan.get("workflow_run_id"),
    )
    if plan != expected:
        _fail("publication plan does not match the fixed role contract")
    return expected


def validate_local_inspection(
    plan_value: object,
    *,
    labels: object,
    platform: object,
    user: object,
) -> dict[str, object]:
    """Validate the local post-build image facts before a future push.

    Docker inspection is performed by a manually dispatched workflow, not this
    pure script.  The accepted fields intentionally exclude image IDs and any
    mutable tag as a future receipt still needs the registry content digest.
    """

    plan = validate_plan(plan_value)
    inspected = _require_mapping(labels, field="image labels")
    required_labels = _require_mapping(plan["required_labels"], field="plan labels")
    if inspected != required_labels:
        _fail("local image labels do not match the role plan")
    if platform != _PLATFORM or user != "2000:0":
        _fail("local image platform or user is invalid")
    return {
        "schema_version": "inferdrome.role-image-local-inspection.v1",
        "role": plan["role"],
        "source_commit": plan["source_commit"],
        "platform": _PLATFORM,
        "user": "2000:0",
        "verified": True,
    }


def record_registry_digest(
    plan_value: object, *, immutable_image: object
) -> dict[str, object]:
    """Validate a supplied digest binding without claiming a registry event."""

    plan = validate_plan(plan_value)
    image = _required_string(immutable_image, field="immutable image")
    repository = _required_string(plan["repository"], field="plan repository")
    prefix = f"{repository}@"
    digest = image.removeprefix(prefix)
    if image == prefix or _DIGEST.fullmatch(digest) is None:
        _fail("registry digest does not match the fixed role repository")
    return {
        "schema_version": "inferdrome.role-image-digest-binding.v1",
        "validation_scope": "LOCAL_FORMAT_AND_ROLE_BINDING_ONLY",
        "role": plan["role"],
        "repository": repository,
        "immutable_image": image,
        "source_repository": plan["source_repository"],
        "source_commit": plan["source_commit"],
        "inferdrome_version": plan["inferdrome_version"],
        "platform": plan["platform"],
        "workflow_run_id": plan["workflow_run_id"],
    }


def validate_publication_pair(
    engine_receipt_value: object, runner_receipt_value: object
) -> dict[str, object]:
    """Validate two supplied role bindings without allowing digest collapse."""

    required_fields = {
        "schema_version",
        "validation_scope",
        "role",
        "repository",
        "immutable_image",
        "source_repository",
        "source_commit",
        "inferdrome_version",
        "platform",
        "workflow_run_id",
    }
    engine = _require_mapping(engine_receipt_value, field="engine receipt")
    runner = _require_mapping(runner_receipt_value, field="runner receipt")
    if set(engine) != required_fields or set(runner) != required_fields:
        _fail("publication receipt fields are invalid")
    if (
        engine.get("role") != "private-engine"
        or runner.get("role") != "cpu-runner-observer"
    ):
        _fail("publication receipt roles are invalid")
    engine_plan = plan_role_image(
        role=engine.get("role"),
        source_commit=engine.get("source_commit"),
        version=engine.get("inferdrome_version"),
        workflow_run_id=engine.get("workflow_run_id"),
    )
    runner_plan = plan_role_image(
        role=runner.get("role"),
        source_commit=runner.get("source_commit"),
        version=runner.get("inferdrome_version"),
        workflow_run_id=runner.get("workflow_run_id"),
    )
    if engine != record_registry_digest(
        engine_plan, immutable_image=engine.get("immutable_image")
    ) or runner != record_registry_digest(
        runner_plan, immutable_image=runner.get("immutable_image")
    ):
        _fail("publication receipts do not match their fixed role plans")
    shared = (
        "source_repository",
        "source_commit",
        "inferdrome_version",
        "platform",
        "workflow_run_id",
    )
    if any(engine[field] != runner[field] for field in shared):
        _fail("publication receipts do not share one source identity")
    engine_image = _required_string(
        engine["immutable_image"], field="engine digest"
    )
    runner_image = _required_string(
        runner["immutable_image"], field="runner digest"
    )
    engine_digest = engine_image.rsplit("@", maxsplit=1)[1]
    runner_digest = runner_image.rsplit("@", maxsplit=1)[1]
    if engine_digest == runner_digest:
        _fail("publication role content digests must differ")
    return {
        "schema_version": "inferdrome.role-image-digest-pair.v1",
        "validation_scope": "LOCAL_FORMAT_AND_ROLE_BINDING_ONLY",
        "source_repository": engine["source_repository"],
        "source_commit": engine["source_commit"],
        "inferdrome_version": engine["inferdrome_version"],
        "platform": engine["platform"],
        "workflow_run_id": engine["workflow_run_id"],
        "private_engine_image": engine["immutable_image"],
        "cpu_runner_observer_image": runner["immutable_image"],
    }


def _read_json(path: Path) -> object:
    try:
        content = path.read_bytes()
    except OSError:
        _fail("publication input cannot be read")
    if len(content) > _MAX_INPUT_BYTES:
        _fail("publication input is too large")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("publication input is not valid JSON")


def _write_create_no_replace(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        _fail("publication output cannot be created")


def _emit(value: object, *, output: Path | None = None) -> None:
    content = _canonical_bytes(value)
    if output is None:
        sys.stdout.buffer.write(content)
        return
    _write_create_no_replace(output, content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="role_image_publish",
        description=(
            "Plan and validate the fixed manual Inferdrome role-image publication."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--role", required=True, choices=_ROLES)
    plan.add_argument("--source-commit", required=True)
    plan.add_argument("--version", required=True)
    plan.add_argument("--workflow-run-id", required=True)
    plan.add_argument("--output", type=Path)
    inspect = commands.add_parser("verify-local-inspection")
    inspect.add_argument("--plan", required=True, type=Path)
    inspect.add_argument("--labels-json", required=True, type=Path)
    inspect.add_argument("--platform", required=True)
    inspect.add_argument("--user", required=True)
    inspect.add_argument("--output", type=Path)
    receipt = commands.add_parser("validate-registry-digest")
    receipt.add_argument("--plan", required=True, type=Path)
    receipt.add_argument("--immutable-image", required=True)
    receipt.add_argument("--output", type=Path)
    pair = commands.add_parser("validate-digest-pair")
    pair.add_argument("--private-engine-receipt", required=True, type=Path)
    pair.add_argument("--cpu-runner-observer-receipt", required=True, type=Path)
    pair.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "plan":
            _emit(
                plan_role_image(
                    role=arguments.role,
                    source_commit=arguments.source_commit,
                    version=arguments.version,
                    workflow_run_id=arguments.workflow_run_id,
                ),
                output=arguments.output,
            )
        elif arguments.command == "verify-local-inspection":
            _emit(
                validate_local_inspection(
                    _read_json(arguments.plan),
                    labels=_read_json(arguments.labels_json),
                    platform=arguments.platform,
                    user=arguments.user,
                ),
                output=arguments.output,
            )
        elif arguments.command == "validate-registry-digest":
            _emit(
                record_registry_digest(
                    _read_json(arguments.plan),
                    immutable_image=arguments.immutable_image,
                ),
                output=arguments.output,
            )
        elif arguments.command == "validate-digest-pair":
            _emit(
                validate_publication_pair(
                    _read_json(arguments.private_engine_receipt),
                    _read_json(arguments.cpu_runner_observer_receipt),
                ),
                output=arguments.output,
            )
        else:  # pragma: no cover - argparse retains the closed command set.
            _fail("publication command is invalid")
    except RoleImagePublicationError as error:
        print(f"role image publication rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
