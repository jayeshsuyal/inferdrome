"""Adversarial fail-closed coverage for attached external-router evidence."""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import pytest

from inferdrome.errors import VerificationError
from inferdrome.external_router.llmd import (
    MAX_ATTACHED_RECORD_BYTES,
    ExternalRouterAdapterError,
    adapt_llmd_attached_record,
)
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
