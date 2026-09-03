"""Approval-bound IAP tunnel supervision tests; no gcloud or socket bind."""

from __future__ import annotations

from pathlib import Path

import pytest

import inferdrome.deployment.gcp_private_campaign_google as campaign_google
from inferdrome.deployment.gcp_private_campaign_google import (
    GcpPrivateCampaignIapTunnelSupervisor,
    _SubprocessIapInvocation,
    _terminate_iap_process_group,
)
from inferdrome.deployment.gcp_private_campaign_v2 import (
    GcpPrivateCampaignTransportError,
    build_gcp_private_campaign_create_request,
)
from inferdrome.errors import AdapterError
from inferdrome.execution.subprocess_runner import ExecutableIdentity
from tests.unit.test_gcp_private_campaign_v2 import _proposal


class _Socket:
    def close(self) -> None:
        return None


class _Process:
    def __init__(self, *, exited: bool = False, pid: int = 42_001) -> None:
        self.exited = exited
        self.pid = pid
        self.wait_timeouts: list[int] = []

    def poll(self) -> int | None:
        return 0 if self.exited else None

    def terminate(self) -> None:
        raise AssertionError("IAP cleanup must signal the process group")

    def wait(self, *, timeout: int) -> int:
        self.wait_timeouts.append(timeout)
        return 0

    def kill(self) -> None:
        raise AssertionError("IAP cleanup must signal the process group")


class _Invocation:
    def __init__(self, *, parents_exited: bool = False) -> None:
        self.argv: list[tuple[str, ...]] = []
        self.identities: list[ExecutableIdentity] = []
        self.processes: list[_Process] = []
        self._parents_exited = parents_exited

    def start(
        self,
        *,
        identity: ExecutableIdentity,
        argv: tuple[str, ...],
    ) -> _Process:
        self.argv.append(argv)
        self.identities.append(identity)
        process = _Process(
            exited=self._parents_exited,
            pid=42_001 + len(self.processes),
        )
        self.processes.append(process)
        return process


def _identity() -> ExecutableIdentity:
    return ExecutableIdentity(
        path=Path("/opt/inferdrome/bin/gcloud"),
        device=1,
        inode=2,
        mode=0o100755,
        links=1,
        size=3,
        modified_ns=4,
        changed_ns=5,
    )


def _request():
    proposal, startup = _proposal()
    return proposal, build_gcp_private_campaign_create_request(
        proposal=proposal, startup_payload=startup
    )


def test_iap_supervisor_starts_exact_engine_and_runner_tunnels_and_closes_them() -> (
    None
):
    proposal, request = _request()
    invocation = _Invocation()
    resolver_calls: list[str] = []
    validator_calls: list[ExecutableIdentity] = []
    terminated: list[_Process] = []
    identity = _identity()
    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
        executable_resolver=lambda binary: resolver_calls.append(binary) or identity,
        executable_validator=lambda selected: validator_calls.append(selected),
        process_group_terminator=lambda process: terminated.append(process),
    )

    endpoints = supervisor.open(request, timeout_seconds=10)

    assert endpoints.readiness_origins == (
        "http://127.0.0.1:18000",
        "http://127.0.0.1:18001",
    )
    assert endpoints.runner_control_origin == "http://127.0.0.1:18002"
    assert resolver_calls == ["gcloud"]
    assert len(validator_calls) == 4
    assert invocation.identities == [identity, identity, identity]
    assert len(invocation.argv) == 3
    assert all("ssh" not in argument for argv in invocation.argv for argument in argv)
    assert invocation.argv[0][:5] == (
        str(identity.path),
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
    assert terminated == list(reversed(invocation.processes))


def test_iap_preflight_resolves_only_the_explicit_binary_not_an_ambient_account() -> (
    None
):
    _, request = _request()
    invocation = _Invocation()
    resolver_calls: list[str] = []
    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
        executable_resolver=lambda binary: resolver_calls.append(binary) or _identity(),
        executable_validator=lambda _identity: None,
        process_group_terminator=lambda _process: None,
    )

    supervisor.preflight_principal(request)

    assert resolver_calls == ["gcloud"]
    assert not invocation.argv


def test_iap_supervisor_rejects_a_changed_binary_before_any_tunnel_starts() -> None:
    _, request = _request()
    invocation = _Invocation()

    def reject_changed(_identity: ExecutableIdentity) -> None:
        raise AdapterError("subprocess executable identity changed")

    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
        executable_resolver=lambda _binary: _identity(),
        executable_validator=reject_changed,
        process_group_terminator=lambda _process: None,
    )

    with pytest.raises(
        GcpPrivateCampaignTransportError,
        match="IAP_EXECUTABLE_CHANGED",
    ):
        supervisor.open(request, timeout_seconds=10)

    assert not invocation.argv


def test_iap_supervisor_revalidates_before_each_tunnel_start() -> None:
    _, request = _request()
    invocation = _Invocation()
    validation_calls = 0
    terminated: list[_Process] = []

    def reject_after_first_tunnel(_identity: ExecutableIdentity) -> None:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 3:
            raise AdapterError("subprocess executable identity changed")

    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
        executable_resolver=lambda _binary: _identity(),
        executable_validator=reject_after_first_tunnel,
        process_group_terminator=lambda process: terminated.append(process),
    )

    with pytest.raises(
        GcpPrivateCampaignTransportError,
        match="IAP_EXECUTABLE_CHANGED",
    ):
        supervisor.open(request, timeout_seconds=10)

    assert validation_calls == 3
    assert len(invocation.argv) == 1
    assert terminated == invocation.processes


def test_subprocess_iap_invocation_uses_the_verified_absolute_binary_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    captured: dict[str, object] = {}

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> _Process:
        captured["argv"] = argv
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(campaign_google, "validate_executable_identity", lambda _: None)
    monkeypatch.setattr(campaign_google.subprocess, "Popen", fake_popen)

    _SubprocessIapInvocation().start(
        identity=identity,
        argv=(str(identity.path), "compute", "start-iap-tunnel"),
    )

    assert captured["argv"] == (
        str(identity.path),
        "compute",
        "start-iap-tunnel",
    )
    assert captured["executable"] == str(identity.path)
    assert captured["shell"] is False
    assert captured["close_fds"] is True
    assert captured["start_new_session"] is True


def test_subprocess_iap_invocation_rejects_a_substituted_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(
        campaign_google,
        "validate_executable_identity",
        lambda _identity: (_ for _ in ()).throw(
            AssertionError("substituted argv must fail before validation")
        ),
    )

    with pytest.raises(
        GcpPrivateCampaignTransportError,
        match="IAP_EXECUTABLE_IDENTITY_MISMATCH",
    ):
        _SubprocessIapInvocation().start(
            identity=identity,
            argv=("gcloud", "compute", "start-iap-tunnel"),
        )


def test_iap_group_teardown_signals_descendants_after_parent_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(exited=True, pid=42_123)
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        campaign_google.os,
        "killpg",
        lambda process_group_id, signal_value: signals.append(
            (process_group_id, signal_value)
        ),
    )

    _terminate_iap_process_group(process)  # type: ignore[arg-type]

    assert signals == [
        (process.pid, campaign_google.signal.SIGTERM),
        (process.pid, campaign_google.signal.SIGKILL),
    ]
    assert process.wait_timeouts == [5, 5]


def test_iap_supervisor_never_skips_a_group_when_its_parent_has_exited() -> None:
    _, request = _request()
    invocation = _Invocation()
    terminated: list[_Process] = []
    supervisor = GcpPrivateCampaignIapTunnelSupervisor(
        invocation=invocation,
        connector=lambda *args, **kwargs: _Socket(),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
        executable_resolver=lambda _binary: _identity(),
        executable_validator=lambda _identity: None,
        process_group_terminator=lambda process: terminated.append(process),
    )

    supervisor.open(request, timeout_seconds=10)
    for process in invocation.processes:
        process.exited = True
    supervisor.close()

    assert terminated == list(reversed(invocation.processes))
