"""Namespaced run/verify/inspect CLI behavior for sealed local evidence."""

from __future__ import annotations

import json
from io import BytesIO, TextIOWrapper
from pathlib import Path
from types import SimpleNamespace

import pytest

import inferdrome.routing_execution.cli as routing_cli
from inferdrome.deployment.gcp_private_campaign_v2 import (
    FakeGcpPrivateCampaignTransport,
    build_gcp_private_campaign_create_request,
)
from inferdrome.deployment.gcp_private_routing_handoff import (
    build_gcp_private_routing_execution_inputs,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.routing_execution.executor import ManualMonotonicClock, run_execution
from inferdrome.routing_execution.stdin_bundle import RuntimeInputBundle
from tests.routing_execution_support import StaticEndpointTransport, write_inputs
from tests.unit.test_gcp_private_campaign_v2 import _proposal

main = routing_cli.main


def test_cli_verify_and_inspect_only_render_verified_package(
    tmp_path: Path, capsys: object
) -> None:
    config_path, workload_path, _, _ = write_inputs(tmp_path)
    sealed = run_execution(
        config_path,
        workload_path,
        tmp_path / "package",
        transport_factory=StaticEndpointTransport,
        clock=ManualMonotonicClock(),
    )
    assert (
        main(["verify", str(sealed.path), "--expected-digest", sealed.retained_digest])
        == 0
    )
    verified = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert verified == {"retained_digest": sealed.retained_digest, "valid": True}
    assert main(["inspect", str(sealed.path)]) == 0
    inspected = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert inspected["verified"] is True
    assert inspected["execution_id"] == "routing-execution-v1"


def _gcp_private_bundle() -> tuple[bytes, bytes, bytes]:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    readiness = FakeGcpPrivateCampaignTransport().observe_readiness(
        request, timeout_seconds=1
    )
    inputs = build_gcp_private_routing_execution_inputs(proposal, readiness)
    transport_map = canonical_json_bytes(
        {
            "endpoint_a_origin": "http://host.docker.internal:18000",
            "endpoint_b_origin": "http://host.docker.internal:18001",
            "schema_version": "inferdrome.routing-execution-iap-transport-map.v1",
        }
    )
    bundle = RuntimeInputBundle(
        config_bytes=inputs.routing_config_bytes,
        workload_bytes=inputs.workload_bytes,
        iap_transport_map_bytes=transport_map,
    ).encode()
    return bundle, inputs.routing_config_bytes, inputs.workload_bytes


def test_cli_stdin_bundle_never_materializes_a_host_input_path(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, expected_config, expected_workload = _gcp_private_bundle()
    captured: dict[str, object] = {}

    def fake_run(
        config_bytes: bytes,
        workload_bytes: bytes,
        output: Path,
        *,
        transport_factory: object,
    ) -> SimpleNamespace:
        captured.update(
            {
                "config": config_bytes,
                "workload": workload_bytes,
                "output": output,
                "transport_factory": transport_factory,
            }
        )
        return SimpleNamespace(path=output, retained_digest="sha256:" + "a" * 64)

    stream = TextIOWrapper(BytesIO(bundle), encoding="utf-8")
    monkeypatch.setattr(routing_cli.sys, "stdin", stream)
    monkeypatch.setattr(routing_cli, "run_execution_from_bytes", fake_run)

    output = tmp_path / "package"
    assert main(["run", "--input-bundle-stdin", "--output", str(output)]) == 0
    emitted = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]

    assert emitted == {
        "package_path": str(output),
        "retained_digest": "sha256:" + "a" * 64,
    }
    assert captured["config"] == expected_config
    assert captured["workload"] == expected_workload
    assert captured["output"] == output
    assert callable(captured["transport_factory"])


def test_cli_stdin_bundle_rejects_before_execution_or_transport(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def unexpected(*args: object, **kwargs: object) -> object:
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("execution must not start")

    stream = TextIOWrapper(BytesIO(b"not-json"), encoding="utf-8")
    monkeypatch.setattr(routing_cli.sys, "stdin", stream)
    monkeypatch.setattr(routing_cli, "run_execution_from_bytes", unexpected)

    with pytest.raises(SystemExit) as error:
        main(["run", "--input-bundle-stdin", "--output", str(tmp_path / "package")])

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert error.value.code == 2
    assert called is False
    assert "not-json" not in captured.err


def test_cli_stdin_bundle_rejects_file_argument_mixing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _, _ = _gcp_private_bundle()
    stream = TextIOWrapper(BytesIO(bundle), encoding="utf-8")
    monkeypatch.setattr(routing_cli.sys, "stdin", stream)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "run",
                "--input-bundle-stdin",
                "--deployment-config",
                str(tmp_path / "config.json"),
                "--output",
                str(tmp_path / "package"),
            ]
        )

    assert error.value.code == 2


def test_cli_rejects_gcp_private_file_mode_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config_bytes, workload_bytes = _gcp_private_bundle()
    config_path = tmp_path / "config.json"
    workload_path = tmp_path / "workload.jsonl"
    config_path.write_bytes(config_bytes)
    workload_path.write_bytes(workload_bytes)
    called = False

    def unexpected(*args: object, **kwargs: object) -> object:
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("GCP_PRIVATE file mode must not execute on the host")

    monkeypatch.setattr(routing_cli, "run_execution", unexpected)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "run",
                "--deployment-config",
                str(config_path),
                "--workload",
                str(workload_path),
                "--output",
                str(tmp_path / "package"),
            ]
        )

    assert error.value.code == 2
    assert called is False
