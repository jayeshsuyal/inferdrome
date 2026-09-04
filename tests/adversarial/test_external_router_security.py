"""Adversarial fail-closed coverage for attached external-router evidence."""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from inferdrome.errors import VerificationError
from inferdrome.external_router.contracts import OpaqueId
from inferdrome.external_router.llmd import (
    MAX_ATTACHED_RECORD_BYTES,
    ExternalRouterAdapterError,
    adapt_llmd_attached_record,
)
from inferdrome.external_router.schema import schema_bytes
from inferdrome.external_router.verifier import verify_external_router_evidence
from tests.external_router_support import (
    FIXTURE_ROOT,
    REPOSITORY_ROOT,
    fixture_record,
    fixture_record_bytes,
    fixture_router_config,
)


def _adapt(record: dict[str, object]) -> None:
    adapt_llmd_attached_record(
        fixture_record_bytes(record),
        router_config_bytes=fixture_router_config(),
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda record: record["candidates"].__setitem__(
            1,
            {
                "endpoint_id": "endpoint-a",
                "endpoint_identity_sha256": "sha256:" + "a" * 64,
            },
        ),
        lambda record: record["decision"]["selection"].update(
            {"endpoint_id": "endpoint-not-declared"}
        ),
        lambda record: record["outcome"].update({"endpoint_id": "endpoint-b"}),
        lambda record: record["observations"][0].update({"epoch": 5}),
        lambda record: record["observations"][0].update({"age_ns": 1_001}),
    ),
)
def test_unbound_or_contradictory_topology_and_telemetry_fails_closed(mutate) -> None:
    record = fixture_record()
    mutate(record)
    with pytest.raises(ExternalRouterAdapterError):
        _adapt(record)


def test_router_decision_must_bind_the_request_correlation() -> None:
    record = fixture_record()
    decision = record["decision"]
    assert isinstance(decision, dict)
    decision["correlation_id"] = "corr-other"
    with pytest.raises(ExternalRouterAdapterError):
        _adapt(record)


def test_malformed_duplicate_and_nonfinite_input_fails_closed() -> None:
    duplicate = fixture_record_bytes().replace(
        b'"schema_version":',
        b'"schema_version":"wrong","schema_version":',
        1,
    )
    with pytest.raises(ExternalRouterAdapterError, match="invalid JSON"):
        adapt_llmd_attached_record(
            duplicate,
            router_config_bytes=fixture_router_config(),
        )

    nonfinite = fixture_record_bytes().replace(b"1000", b"NaN", 1)
    with pytest.raises(ExternalRouterAdapterError, match="invalid JSON"):
        adapt_llmd_attached_record(
            nonfinite,
            router_config_bytes=fixture_router_config(),
        )

    oversized = b"{" + b" " * MAX_ATTACHED_RECORD_BYTES
    with pytest.raises(ExternalRouterAdapterError, match="invalid size"):
        adapt_llmd_attached_record(
            oversized,
            router_config_bytes=fixture_router_config(),
        )


def test_wrong_config_profile_or_unavailable_fact_shape_fails_closed() -> None:
    with pytest.raises(ExternalRouterAdapterError, match="configuration identity"):
        adapt_llmd_attached_record(
            fixture_record_bytes(),
            router_config_bytes=b'{"different":"local-only"}',
        )

    profile = fixture_record()
    profile["router"]["profile_id"] = "unknown-profile-v1"
    with pytest.raises(ExternalRouterAdapterError):
        _adapt(profile)

    fact = fixture_record()
    fact["decision"]["reason"] = {"state": "UNAVAILABLE", "value": "invented"}
    with pytest.raises(ExternalRouterAdapterError):
        _adapt(fact)

    partial_timing = fixture_record()
    observation = partial_timing["observations"][0]
    assert isinstance(observation, dict)
    observation.update(
        {
            "state": "UNAVAILABLE",
            "unavailable_reason": "MISSING",
            "age_ns": None,
            "freshness_bound_ns": None,
        }
    )
    with pytest.raises(ExternalRouterAdapterError):
        _adapt(partial_timing)


@pytest.mark.parametrize(
    "unsafe_alias",
    (
        "10.23.45.67:8443",
        "2001:db8::1",
        "router-a:8443",
        "router.internal.example",
        "https://router.example/v1",
        "operator@example.test",
        "/var/run/router.sock",
        "sk-proj-not-a-retained-alias",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "hf_non_sensitive_but_token_shaped",
        "api_key_not_an_alias",
    ),
)
def test_retained_ids_reject_addresses_paths_and_token_shaped_values(
    unsafe_alias: str,
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(OpaqueId).validate_python(unsafe_alias)


@pytest.mark.parametrize(
    "alias",
    ("endpoint-a", "observer_02", "policy-route-b", "request42"),
)
def test_retained_ids_accept_simple_pseudonymous_aliases(alias: str) -> None:
    assert TypeAdapter(OpaqueId).validate_python(alias) == alias


def test_adapter_rejects_sensitive_identifier_before_retaining_a_record() -> None:
    record = fixture_record()
    request = record["request"]
    assert isinstance(request, dict)
    request["request_id"] = "sk-proj-not-a-retained-alias"
    with pytest.raises(ExternalRouterAdapterError):
        _adapt(record)


@pytest.mark.parametrize(
    "unsafe_alias",
    (
        "sk-proj-not-a-retained-alias",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "hf_token_shaped_value",
        "api_key_not_an_alias",
    ),
)
def test_published_profile_schema_rejects_token_shaped_retained_ids(
    unsafe_alias: str,
) -> None:
    record = fixture_record()
    request = record["request"]
    assert isinstance(request, dict)
    request["request_id"] = unsafe_alias
    schema = json.loads(schema_bytes()["llm-d-attached-record.schema.json"])
    validator = Draft202012Validator(schema)
    assert validator.is_valid(fixture_record())
    assert not validator.is_valid(record)


def test_cli_inputs_reject_symlink_and_hardlink(
    tmp_path: Path,
) -> None:
    from inferdrome.external_router.cli import _InputError, _read_regular_file

    record = tmp_path / "record.json"
    record.write_bytes((FIXTURE_ROOT / "attached-record.json").read_bytes())
    link = tmp_path / "record-link.json"
    link.symlink_to(record.name)
    with pytest.raises(_InputError):
        _read_regular_file(link, maximum_bytes=MAX_ATTACHED_RECORD_BYTES)

    hard = tmp_path / "record-hard.json"
    os.link(record, hard)
    with pytest.raises(_InputError):
        _read_regular_file(record, maximum_bytes=MAX_ATTACHED_RECORD_BYTES)


def test_cli_input_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    from inferdrome.external_router.cli import _InputError, _read_regular_file

    fifo = tmp_path / "record.fifo"
    os.mkfifo(fifo)
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    with pytest.raises(_InputError):
        _read_regular_file(fifo, maximum_bytes=MAX_ATTACHED_RECORD_BYTES)


@pytest.mark.parametrize("flag", ("O_NOFOLLOW", "O_NONBLOCK"))
@pytest.mark.parametrize("missing", (True, False))
def test_cli_input_requires_safe_open_flags_before_os_open(
    flag: str, missing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inferdrome.external_router.cli import _InputError, _read_regular_file

    open_calls = 0

    def forbidden_open(*args: object, **kwargs: object) -> int:
        nonlocal open_calls
        del args, kwargs
        open_calls += 1
        raise AssertionError("unsafe platform must fail before os.open")

    monkeypatch.setattr(os, "open", forbidden_open)
    if missing:
        monkeypatch.delattr(os, flag, raising=False)
    else:
        monkeypatch.setattr(os, flag, 0, raising=False)

    with pytest.raises(_InputError, match="required safe file-open flags"):
        _read_regular_file(Path("unsafe-input"), maximum_bytes=1)
    assert open_calls == 0


def test_cli_input_rejects_same_size_in_place_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inferdrome.external_router.cli import _InputError, _read_regular_file

    record = tmp_path / "record.json"
    original = b"a" * 128
    record.write_bytes(original)
    original_read = os.read
    rewrote = False

    def read_then_rewrite(descriptor: int, count: int) -> bytes:
        nonlocal rewrote
        content = original_read(descriptor, count)
        if not rewrote and content:
            rewrote = True
            record.write_bytes(b"b" * len(original))
        return content

    monkeypatch.setattr(os, "read", read_then_rewrite)
    with pytest.raises(_InputError):
        _read_regular_file(record, maximum_bytes=MAX_ATTACHED_RECORD_BYTES)


def test_adapter_namespace_has_no_network_cluster_cloud_or_subprocess_imports() -> None:
    root = REPOSITORY_ROOT / "src" / "inferdrome" / "external_router"
    forbidden_roots = {
        "socket",
        "urllib",
        "http",
        "requests",
        "subprocess",
        "kubernetes",
        "google",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        assert not imports & forbidden_roots, path


def test_verifier_cannot_accept_a_rehashed_noncanonical_receipt() -> None:
    captured = adapt_llmd_attached_record(
        fixture_record_bytes(),
        router_config_bytes=fixture_router_config(),
    )
    noncanonical = b"{\n" + captured.canonical_bytes[1:-1] + b"\n}"
    with pytest.raises(VerificationError):
        verify_external_router_evidence(noncanonical)
