"""Tiny synthetic matched studies over the existing two-replica loopback."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import call, patch

import pytest

from inferdrome.evaluation import cli, study
from inferdrome.evaluation.faults import RoutingFaultResult
from inferdrome.evaluation.observations import AiohttpProbeTransport
from inferdrome.evaluation.policies import POLICY_IDS
from inferdrome.evaluation.study_config import CompiledTrial, compile_study
from inferdrome.evaluation.study_files import trial_filename
from tests.integration.test_evaluation_routing_loopback import (
    _BACKGROUND,
    _FOREGROUND,
    _GUARD_SECONDS,
    _MODEL,
    _OUTPUT,
    _config,
    _replicas,
)
from tests.unit.test_evaluation_study_config import load, study_payload


def _payload(origins: tuple[str, str]) -> dict:
    value = study_payload("STALE_LOAD")
    recipe = _config(origins, POLICY_IDS[0]).model_dump(mode="json")
    stale = value["blocks"][0]
    stale.update(
        foreground=recipe["foreground"],
        background=recipe["background"],
        telemetry=recipe["telemetry"],
        fault=recipe["fault"],
        block_id="stale",
    )
    healthy = {
        key: item for key, item in stale.items() if key not in {"background", "fault"}
    }
    healthy.update(scenario="HEALTHY", block_id="healthy")
    value["blocks"] = [healthy, stale]
    value["profiles"][0].update(
        window_start_ns=100_000_000,
        window_end_ns=800_000_000,
        first_content_slo_ns=100_000_000,
        completion_slo_ns=150_000_000,
    )
    value["limits"]["cooldown_ns"] = 1_000_000
    return value


@asynccontextmanager
async def _primed_study_probes(
    owner: ModuleType, trial: CompiledTrial
) -> AsyncIterator[None]:
    """Ready real fixture connections before the native trial starts its clock.

    Discard these responses. The production session must still acquire fresh
    health and both load channels within its unchanged 100 ms warmup gate.
    """
    probes: list[AiohttpProbeTransport] = []
    try:
        for _ in range(2):
            probes.append(
                AiohttpProbeTransport(
                    trial.config.foreground,
                    max_response_bytes=trial.config.telemetry.max_response_bytes,
                )
            )
        router, observer = probes

        async def check(probe: AiohttpProbeTransport, origin: str, path: str) -> None:
            response = await probe.get(origin, path)
            assert response.status == 200
            if path == "/metrics":
                assert response.body

        async with asyncio.timeout(_GUARD_SECONDS):
            async with asyncio.TaskGroup() as tasks:
                for endpoint in trial.config.foreground.endpoints:
                    for probe, path in (
                        (router, "/health"),
                        (router, "/metrics"),
                        (observer, "/metrics"),
                    ):
                        tasks.create_task(check(probe, endpoint.origin, path))
        with patch.object(
            owner, "AiohttpProbeTransport", side_effect=probes
        ) as factory:
            yield
        expected = call(
            trial.config.foreground,
            max_response_bytes=trial.config.telemetry.max_response_bytes,
        )
        assert factory.call_args_list == [expected, expected]
        # Check production ownership before any defensive fixture cleanup. A
        # native entrypoint that leaks these clients must fail this assertion.
        assert all(probe.closed for probe in probes)
    finally:
        pending = [probe for probe in probes if not probe.closed]
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*(probe.close() for probe in pending)), _GUARD_SECONDS
            )


@pytest.mark.parametrize("cold_probe_delay_s", [0.0, 0.12])
def test_matched_healthy_and_fault_study_closes_clients_and_reports_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cold_probe_delay_s: float,
) -> None:
    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            for replica in replicas:
                replica.initial_probe_delay = cold_probe_delay_s
            config = load(_payload(origins))
            calls: list[str] = []

            async def execute(trial, *, stop):
                assert not any(replica.active for replica in replicas)
                calls.append(trial.trial_id)
                async with _primed_study_probes(study, trial):
                    result = await study.execute_trial(trial, stop=stop)
                await asyncio.gather(
                    *(replica.assert_disconnected() for replica in replicas)
                )
                changes = {
                    "evidence_class": "SYNTHETIC_ONLY",
                    "foreground": replace(
                        result.foreground, evidence_class="SYNTHETIC_ONLY"
                    ),
                }
                if isinstance(result, RoutingFaultResult):
                    changes["background"] = replace(
                        result.background, evidence_class="SYNTHETIC_ONLY"
                    )
                return replace(result, **changes)

            output = tmp_path / "study"
            manifest = await asyncio.wait_for(
                study.run_study(config, output, executor=execute), 15
            )
            assert manifest.status == "COMPLETED", {
                "reason": manifest.reason,
                "started_trials": [
                    (row.trial_id, row.state, row.result_status, row.failure_reason)
                    for row in manifest.trials
                    if row.state != "NOT_RUN"
                ],
            }
            assert calls == [f"trial-{index:04d}" for index in range(8)]
            assert all(row.state == "RETURNED" for row in manifest.trials)

            def forbid_clients(*args, **kwargs):
                raise AssertionError("offline report constructed a client")

            monkeypatch.setattr(study, "AiohttpTransport", forbid_clients)
            monkeypatch.setattr(study, "AiohttpProbeTransport", forbid_clients)
            report = study.report_study(config, output, tmp_path / "report")
            assert report["evidence_class"] == "SYNTHETIC_ONLY"
            assert report["coverage"]["returned_trials"] == 8
            assert (
                report["comparative_headline"] == "DESCRIPTIVE_UNCALIBRATED_REHEARSAL"
            )
            assert len(report["strata"]) == 2
            assert (
                json.loads((tmp_path / "report" / "report.json").read_bytes()) == report
            )
            markdown = (tmp_path / "report" / "report.md").read_text()
            assert "SYNTHETIC_ONLY" in markdown
            for directory in (output, tmp_path / "report"):
                assert directory.stat().st_mode & 0o777 == 0o700
                for path in directory.iterdir():
                    assert path.stat().st_mode & 0o777 == 0o400
                    text = path.read_text()
                    assert all(
                        secret not in text
                        for secret in (
                            _BACKGROUND,
                            _FOREGROUND,
                            _MODEL,
                            _OUTPUT,
                            *origins,
                        )
                    )
            plan = compile_study(config)
            first = (output / trial_filename(0)).read_bytes()
            with pytest.raises(ValueError):
                study.load_study_trial_bytes(first, plan, plan.trials[1])
            assert not [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ]

    asyncio.run(exercise())


def test_study_plan_cli_is_offline_and_no_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "config.json"
    source.write_text(json.dumps(study_payload()))
    source.chmod(0o600)
    output = tmp_path / "plan.json"

    def forbid_clients(*args, **kwargs):
        raise AssertionError("offline compilation constructed a client")

    monkeypatch.setattr(study, "AiohttpTransport", forbid_clients)
    monkeypatch.setattr(study, "AiohttpProbeTransport", forbid_clients)
    args = ["study-plan", "--config", os.fspath(source), "--output", os.fspath(output)]
    assert cli.main(args) == 0
    original = output.read_bytes()
    assert cli.main(args) == 2
    assert output.read_bytes() == original
    assert "private-model" not in original.decode()
    assert "private foreground" not in original.decode()
    assert "private-model" not in capsys.readouterr().err


def test_study_run_and_report_cli_use_one_private_local_bundle(tmp_path: Path) -> None:
    async def command(*arguments: str) -> tuple[int, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "inferdrome.evaluation",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode is not None
        return process.returncode, stdout, stderr

    async def exercise() -> None:
        async with _replicas() as (origins, replicas):
            value = _payload(origins)
            value["blocks"] = value["blocks"][:1]
            source = tmp_path / "config.json"
            source.write_text(json.dumps(value))
            source.chmod(0o600)
            run_args = (
                "study-run",
                "--config",
                os.fspath(source),
                "--output-dir",
                os.fspath(tmp_path / "run"),
            )
            code, stdout, stderr = await command(*run_args)
            assert code == 0, (stdout, stderr)
            assert json.loads(stdout)["returned_trials"] == 4
            await asyncio.gather(
                *(replica.assert_disconnected() for replica in replicas)
            )
            before = sum(len(replica.requests) for replica in replicas)
            code, stdout, stderr = await command(
                "study-report",
                "--config",
                os.fspath(source),
                "--study-dir",
                os.fspath(tmp_path / "run"),
                "--output-dir",
                os.fspath(tmp_path / "report"),
            )
            assert code == 0, (stdout, stderr)
            assert sum(len(replica.requests) for replica in replicas) == before
            report = json.loads((tmp_path / "report" / "report.json").read_bytes())
            assert report["status"] == "COMPLETED"
            assert report["evidence_eligible"] is False
            assert all(
                secret not in (stdout + stderr).decode()
                for secret in (
                    _MODEL,
                    _FOREGROUND,
                    *origins,
                )
            )
            code, _, _ = await command(*run_args)
            assert code == 2
            assert sum(len(replica.requests) for replica in replicas) == before

    asyncio.run(exercise())
