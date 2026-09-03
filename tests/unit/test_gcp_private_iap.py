"""Approval-bound IAP tunnel supervision tests; no gcloud or socket bind."""

from __future__ import annotations

from inferdrome.deployment.gcp_private_campaign_google import (
    GcpPrivateCampaignIapTunnelSupervisor,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    build_gcp_private_campaign_create_request,
)
from tests.unit.test_gcp_private_campaign_v2 import _proposal


class _Socket:
    def close(self) -> None:
        return None


class _Process:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: int) -> None:
        del timeout
        return None

    def kill(self) -> None:
        self.terminated = True


class _Invocation:
    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []
        self.processes: list[_Process] = []

    def start(self, argv: tuple[str, ...]) -> _Process:
        self.argv.append(argv)
        process = _Process()
        self.processes.append(process)
        return process


def test_iap_supervisor_starts_exact_engine_and_runner_tunnels_and_closes_them() -> (
    None
):
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    invocation = _Invocation()
    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )

    endpoints = supervisor.open(request, timeout_seconds=10)

    assert endpoints.readiness_origins == (
        "http://127.0.0.1:18000",
        "http://127.0.0.1:18001",
    )
    assert endpoints.runner_control_origin == "http://127.0.0.1:18002"
    assert len(invocation.argv) == 3
    assert all("ssh" not in argument for argv in invocation.argv for argument in argv)
    assert invocation.argv[0][:5] == (
        "gcloud",
        "compute",
        "start-iap-tunnel",
        proposal.instance_name,
        "8000",
    )
    assert invocation.argv[1][4] == "8001"
    assert invocation.argv[2][4] == "8002"
    assert all(
        argv[argv.index("--impersonate-service-account") + 1]
        == proposal.iap_connectivity.controller_principal
        for argv in invocation.argv
    )
    supervisor.close()
    assert all(process.terminated for process in invocation.processes)


def test_iap_supervisor_never_uses_an_ambient_active_account_probe() -> None:
    proposal, startup = _proposal()
    request = build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )
    invocation = _Invocation()
    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )

    supervisor.preflight_principal(request)
    assert not invocation.argv
