"""Namespaced run/verify/inspect CLI behavior for sealed local evidence."""

from __future__ import annotations

import json
from pathlib import Path

from inferdrome.routing_execution.cli import main
from inferdrome.routing_execution.executor import ManualMonotonicClock, run_execution
from tests.routing_execution_support import StaticEndpointTransport, write_inputs


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
