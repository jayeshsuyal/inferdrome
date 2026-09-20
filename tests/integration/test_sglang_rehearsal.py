"""SYNTHETIC_ONLY native calibration and confirmation on two CPU HTTP fakes.

No serving image, SGLang runtime, model artifacts, GPU or provider is exercised.
The lifecycle checks loopback readiness and cleanup only; reset declarations
do not establish observed cache state or GPU compatibility.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferdrome.dashboard.api import create_app
from inferdrome.evaluation import sglang_execution
from inferdrome.evaluation import sglang_rehearsal as sglang_rehearsal_module
from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    build_sglang_engine_binding,
    engine_binding_bytes,
    engine_binding_sha256,
    engine_choice_sha256,
)
from inferdrome.evaluation.faults import RoutingFaultResult
from inferdrome.evaluation.healthy import HealthyRoutingResult
from inferdrome.evaluation.load_calibration import load_calibration_protocol_bytes
from inferdrome.evaluation.load_calibration_rehearsal import (
    CandidateStudyRecipe,
    CompiledRehearsal,
    LoopbackTwoEndpointReadinessLifecycle,
    compile_rehearsal,
    recover_pinned_confirmation_catalog,
    run_rehearsal,
    write_pinned_confirmation_catalog,
)
from inferdrome.evaluation.sglang_profile import (
    SGLANG_IMAGE_REFERENCE,
    SglangServingConfig,
)
from inferdrome.evaluation.sglang_rehearsal import bind_sglang_rehearsal
from inferdrome.evaluation.sglang_report import load_sglang_report_bytes
from inferdrome.evaluation.sglang_results import load_sglang_trial_bytes
from inferdrome.evaluation.study import execute_trial
from inferdrome.evaluation.study_config import CompiledTrial
from inferdrome.evaluation.study_files import trial_filename
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest
from tests.integration import test_evaluation_routing_loopback as routing_loopback
from tests.integration import test_sglang_study_loopback as sglang_loopback
from tests.integration.test_load_calibration_rehearsal import _protocol, _recipe_configs
from tests.native_rehearsal_executor import NativeRehearsalExecutor
from tests.unit.test_evaluation_study_config import load
from tests.unit.test_sglang_serving_profile import config as serving_config

_MODEL = "Qwen/Qwen3-8B"


@pytest.fixture
def synthetic_sources(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Classify fake transport populations before production result validation."""
    monkeypatch.setattr(routing_loopback, "_MODEL", _MODEL)
    monkeypatch.setattr(sglang_loopback, "_MODEL", _MODEL)
    original = sglang_execution.wrap_sglang_result
    calls: list[str] = []

    def classified(
        native: HealthyRoutingResult | RoutingFaultResult,
        binding: Any,
        plan: Any,
        trial: CompiledTrial,
    ) -> Any:
        changes: dict[str, Any] = {
            "evidence_class": "SYNTHETIC_ONLY",
            "foreground": replace(native.foreground, evidence_class="SYNTHETIC_ONLY"),
        }
        if isinstance(native, RoutingFaultResult):
            changes["background"] = replace(
                native.background, evidence_class="SYNTHETIC_ONLY"
            )
        calls.append(trial.trial_id)
        return original(replace(native, **changes), binding, plan, trial)

    monkeypatch.setattr(sglang_execution, "wrap_sglang_result", classified)
    return calls


def _rehearsal(
    origins: tuple[str, str], *, mismatched_high_cache: bool = False
) -> CompiledRehearsal:
    configs = []
    for level_id, count, seed in (("load-low", 7, 11), ("load-high", 14, 29)):
        phases = []
        for config in _recipe_configs(
            origins, level_id=level_id, offered_count=count, seed=seed
        ):
            payload = config.model_dump(mode="json")
            payload["preparation"].update(
                cache_state="DECLARED_COLD",
                prefix_caching="DECLARED_DISABLED"
                if mismatched_high_cache and level_id == "load-high"
                else "DECLARED_ENABLED",
                serving_image_reference=SGLANG_IMAGE_REFERENCE.split("@", 1)[1],
            )
            phases.append(load(payload))
        configs.append((level_id, phases[0], phases[1]))
    protocol = load_calibration_protocol_bytes(_protocol(tuple(configs)))
    return compile_rehearsal(
        protocol,
        tuple(CandidateStudyRecipe(*config) for config in configs),
    )


def _profiles(origins: tuple[str, str]) -> dict[EndpointId, SglangServingConfig]:
    return {
        "endpoint-a": serving_config(served_model_name=_MODEL, origin=origins[0]),
        "endpoint-b": serving_config(served_model_name=_MODEL, origin=origins[1]),
    }


class _Lifecycle(LoopbackTwoEndpointReadinessLifecycle):
    """CPU readiness/cleanup observer, without claiming an engine reset."""

    def __init__(
        self,
        origins: tuple[str, str],
        *,
        output: Path,
        replicas: tuple[Any, ...],
        failure: str | None = None,
    ) -> None:
        super().__init__(origins)
        self.output = output
        self.replicas = replicas
        self.failure = failure
        self.events: list[tuple[str, str]] = []
        self.choice_contents: list[bytes] = []

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        # Both recipe and engine choices must be durable before any lifecycle call.
        assert (self.output / "candidate-recipe-bindings.json").is_file()
        ledger = self.output / "engine-choice-bindings.json"
        content = ledger.read_bytes()
        assert canonical_json_bytes(json.loads(content)) + b"\n" == content
        assert b"sglang" in content and b"confirmation" in content.lower()
        assert b"127.0.0.1" not in content
        self.choice_contents.append(content)
        self.events.append(("prepare", trial.trial_id))
        assert all(replica.active == 0 for replica in self.replicas)
        if self.failure == "prepare_error":
            raise EvaluationError("synthetic partial prepare failure")
        if self.failure == "prepare_cancel":
            raise asyncio.CancelledError
        if self.failure == "prepare_timeout":
            await asyncio.sleep(1)
        await super().prepare(trial, stop=stop)

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        self.events.append(("cleanup", trial.trial_id))
        await super().cleanup(trial, stop=stop)
        await asyncio.gather(
            *(replica.assert_disconnected() for replica in self.replicas)
        )
        if self.failure == "cleanup_error":
            raise EvaluationError("synthetic cleanup uncertainty")


class _NativeSGLangExecutor:
    """Bind the deterministic native session to the unchanged SGLang wrapper.

    The calibration/confirmation test exercises the durable rehearsal and
    engine-binding chain, while the separate SGLang loopback study tests cover
    the socket-level SGLang client path.  Keeping this fixture on its supplied
    virtual clock prevents event-loop scheduling from turning the fixed 5 ms
    freshness threshold into an accidental host-speed requirement.
    """

    def __init__(
        self, native: NativeRehearsalExecutor, plan: Any, binding: Any
    ) -> None:
        self._native = native
        self._plan = plan
        self._binding = binding

    async def __call__(self, trial: CompiledTrial, *, stop: asyncio.Event) -> Any:
        return sglang_execution.wrap_sglang_result(
            await self._native(trial, stop=stop), self._binding, self._plan, trial
        )


class _DeterministicLifecycle:
    """Record the same durable rehearsal boundary without socket scheduling.

    Socket-level SGLang request, metrics, and readiness coverage remains in
    ``test_sglang_study_loopback``.  This fixture owns only the rehearsal's
    pre-dispatch ledger assertions, so its exact freshness protocol is driven
    by the supplied native virtual clock rather than host event-loop timing.
    """

    def __init__(self, origins: tuple[str, str], *, output: Path) -> None:
        self._origins = origins
        self.output = output
        self.events: list[tuple[str, str]] = []
        self.choice_contents: list[bytes] = []

    async def prepare(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        assert not stop.is_set()
        assert tuple(
            endpoint.origin for endpoint in trial.config.foreground.endpoints
        ) == (self._origins)
        # Both recipe and engine choices must be durable before any dispatch.
        assert (self.output / "candidate-recipe-bindings.json").is_file()
        ledger = self.output / "engine-choice-bindings.json"
        content = ledger.read_bytes()
        assert canonical_json_bytes(json.loads(content)) + b"\n" == content
        assert b"sglang" in content and b"confirmation" in content.lower()
        assert b"127.0.0.1" not in content
        self.choice_contents.append(content)
        self.events.append(("prepare", trial.trial_id))

    async def cleanup(self, trial: CompiledTrial, *, stop: asyncio.Event) -> None:
        assert tuple(
            endpoint.origin for endpoint in trial.config.foreground.endpoints
        ) == (self._origins)
        self.events.append(("cleanup", trial.trial_id))


def test_sglang_calibration_and_confirmation_keep_one_prebound_engine_choice(
    tmp_path: Path, synthetic_sources: list[str]
) -> None:
    async def exercise() -> None:
        origins = ("http://127.0.0.1:18101", "http://127.0.0.1:18102")
        rehearsal = _rehearsal(origins)
        profiles = _profiles(origins)
        output = tmp_path / "rehearsal"
        output.mkdir(mode=0o700)
        lifecycle = _DeterministicLifecycle(origins, output=output)
        native = NativeRehearsalExecutor()

        def deterministic_executor_for(
            bindings: Any, level_id: str, phase: Any
        ) -> _NativeSGLangExecutor:
            item = bindings._phase(level_id, phase)
            return _NativeSGLangExecutor(native, item.plan, item.binding)

        with pytest.MonkeyPatch.context() as patch, native.study_clock_context():
            patch.setattr(
                sglang_rehearsal_module.SGLangRehearsalBindings,
                "executor_for",
                deterministic_executor_for,
            )
            result = await asyncio.wait_for(
                run_rehearsal(
                    rehearsal,
                    output,
                    lifecycle=lifecycle,
                    sglang_profiles=profiles,
                ),
                45,
            )
            assert result.calibration_selection.selected_level_id == "load-high"
            assert result.confirmation.status == "READY"
            assert result.confirmation_manifest is not None
            assert result.confirmation_manifest.status == "COMPLETED"
            assert len(synthetic_sources) == 24
            assert len(lifecycle.events) == 48
            assert len(set(lifecycle.choice_contents)) == 1
            assert len(native.calls) == 24
            native.assert_quiescent()
            binding_digests = set()
            choices = set()
            for candidate in rehearsal.candidates:
                for phase, plan in (
                    ("calibration", candidate.calibration_plan),
                    ("confirmation", candidate.confirmation_plan),
                ):
                    binding = build_sglang_engine_binding(plan, profiles)
                    choices.add(engine_choice_sha256(binding))
                    binding_digests.add(engine_binding_sha256(binding))
                    stem = f"{phase}-{candidate.level.level_id}"
                    if (
                        phase == "confirmation"
                        and candidate.level.level_id == "load-low"
                    ):
                        assert not (output / stem).exists()
                        continue
                    assert (output / stem / "engine-binding.json").read_bytes() == (
                        engine_binding_bytes(binding)
                    )
                    manifest = json.loads(
                        (output / stem / "manifest.json").read_bytes()
                    )
                    for index, trial in enumerate(plan.trials):
                        content = (output / stem / trial_filename(index)).read_bytes()
                        checked = load_sglang_trial_bytes(content, plan, trial, binding)
                        raw = checked.to_dict()
                        assert (
                            checked.result_sha256
                            == manifest["trials"][index]["result_sha256"]
                        )
                        assert checked.evidence_class == "SYNTHETIC_ONLY"
                        assert raw["engine"] == "sglang"
                        assert raw["telemetry_source_age"] == "UNAVAILABLE"
                        assert len(raw["foreground"]["records"]) == len(
                            trial.config.foreground.offers
                        )
                        assert (
                            b'"running":' not in content
                            and b'"waiting":' not in content
                        )
                    report_content = (
                        output / f"{stem}-report" / "report.json"
                    ).read_bytes()
                    report = load_sglang_report_bytes(report_content, plan, binding)
                    assert report["evidence_class"] == "SYNTHETIC_ONLY"
                    assert report["runtime_verification"] == "UNVERIFIED"
                    assert report["evidence_eligible"] is False
                    assert report["dashboard_projection"] == "ENGINE_BOUND_V2"
            assert len(choices) == 1
            assert len(binding_digests) == 4
            ledger = lifecycle.choice_contents[0]
            assert all(
                digest.encode() in ledger for digest in binding_digests | choices
            )
            assert result.engine_choice_bindings_sha256 == sha256_digest(ledger)
            assert json.loads(ledger)["candidate_recipe_bindings_sha256"] == (
                result.candidate_recipe_bindings_sha256
            )
            for path in output.iterdir():
                assert path.stat().st_mode & 0o077 == 0
            linkage = (output / "calibration-linkage.json").read_bytes()
            assert result.confirmation_report_path is not None
            assert (
                sha256_digest(result.confirmation_report_path.read_bytes()).encode()
                in linkage
            )
            catalog = output / "dashboard-catalog.json"
            digest = write_pinned_confirmation_catalog(result, catalog_path=catalog)
            recovered = output / "dashboard-catalog-recovered.json"
            assert (
                recover_pinned_confirmation_catalog(output, catalog_path=recovered)
                == digest
            )
            assert recovered.read_bytes() == catalog.read_bytes()
            assert (
                json.loads(catalog.read_bytes())["entries"][0]["kind"] == "SGLANG_STUDY"
            )
            assert digest == sha256_digest(result.confirmation_report_path.read_bytes())
            with TestClient(
                create_app(
                    runs_root=tmp_path / "runs", evaluation_reports_catalog=recovered
                )
            ) as client:
                index = client.get("/api/v1/evaluation-reports").json()
                assert (
                    index["projection_version"] == "inferdrome.evaluation-dashboard.v2"
                )
                assert index["rejected"] == []
                summary = index["reports"][0]
                assert summary["report_sha256"] == digest
                assert summary["engine_identity"]["engine"] == "sglang"
                assert summary["engine_identity"]["engine_choice_sha256"] in choices
                assert (
                    summary["source_schema"] == "inferdrome.evaluation-study-report.v2"
                )
                assert summary["evidence_class"] == "SYNTHETIC_ONLY"
                assert summary["runtime_verification"] == "UNVERIFIED"
                assert summary["evidence_eligible"] is False
                detail = client.get(
                    f"/api/v1/evaluation-reports/{summary['report_id']}"
                ).json()
                assert detail["summary"] == summary
                assert len(detail["trials"]) == 8
            _assert_catalog_tampering_rejected(output, result)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())


def _assert_catalog_tampering_rejected(output: Path, result: Any) -> None:
    """Mutate durable antecedents after successful CPU execution, never rerun it."""
    report_path = result.confirmation_report_path
    assert report_path is not None
    ledger_path = output / "engine-choice-bindings.json"
    binding_path = output / "confirmation-load-high" / "engine-binding.json"
    selection_path = output / "calibration-selection-binding.json"
    linkage_path = output / "calibration-linkage.json"
    paths = (report_path, ledger_path, binding_path, selection_path, linkage_path)
    originals = {path: path.read_bytes() for path in paths}

    def write(path: Path, content: bytes) -> None:
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(content)
        path.chmod(0o400)

    def write_json(path: Path, value: Any) -> None:
        write(path, canonical_json_bytes(value) + b"\n")

    def repin_ledger() -> None:
        for path in (selection_path, linkage_path):
            value = json.loads(path.read_bytes())
            value["engine_choice_bindings_sha256"] = sha256_digest(
                ledger_path.read_bytes()
            )
            write_json(path, value)

    mutations = (
        "missing-ledger",
        "missing-study-binding",
        "unanchored-ledger",
        "missing-phase",
        "duplicate-phase",
        "phase-order",
        "phase-config",
        "phase-digest",
        "engine-choice",
        "candidate-digest",
        "extra-ledger-field",
        "binding-mismatch",
        "missing-engine-anchors",
        "report-component-digest",
        "report-statistical-digest",
        "report-unsupported",
        "report-unwrapped",
        "report-unsupported-version",
    )
    for case in mutations:
        try:
            if case == "missing-ledger":
                ledger_path.unlink()
            elif case == "missing-study-binding":
                binding_path.unlink()
            elif case == "binding-mismatch":
                value = json.loads(binding_path.read_bytes())
                value["settings"]["random_seed"] += 1
                write_json(binding_path, value)
            elif case == "missing-engine-anchors":
                for path in (selection_path, linkage_path):
                    value = json.loads(path.read_bytes())
                    del value["engine_choice_bindings_sha256"]
                    write_json(path, value)
            elif case.startswith("report-"):
                value = json.loads(report_path.read_bytes())
                if case == "report-component-digest":
                    value["engine_binding_sha256"] = "sha256:" + "0" * 64
                elif case == "report-statistical-digest":
                    value["statistical_report_sha256"] = "sha256:" + "0" * 64
                elif case == "report-unsupported":
                    value["dashboard_projection"] = "UNSUPPORTED_ENGINE_BINDING"
                elif case == "report-unwrapped":
                    value = value["statistical_report"]
                else:
                    value["schema_version"] = "inferdrome.evaluation-study-report.v99"
                write_json(report_path, value)
                linkage = json.loads(linkage_path.read_bytes())
                linkage["confirmation_report_sha256"] = sha256_digest(
                    report_path.read_bytes()
                )
                write_json(linkage_path, linkage)
            else:
                value = json.loads(ledger_path.read_bytes())
                if case == "missing-phase":
                    value["phases"].pop(0)
                elif case == "duplicate-phase":
                    value["phases"][0] = value["phases"][1]
                elif case == "phase-order":
                    value["phases"].reverse()
                elif case == "phase-config":
                    value["phases"][0]["engine_binding"]["config_sha256"] = (
                        "sha256:" + "0" * 64
                    )
                    binding = value["phases"][0]["engine_binding"]
                    value["phases"][0]["engine_binding_sha256"] = sha256_digest(
                        canonical_json_bytes(binding) + b"\n"
                    )
                elif case == "phase-digest":
                    value["phases"][0]["engine_binding_sha256"] = "sha256:" + "0" * 64
                elif case in ("engine-choice", "unanchored-ledger"):
                    value["engine_choice_sha256"] = "sha256:" + "0" * 64
                elif case == "candidate-digest":
                    value["candidate_recipe_bindings_sha256"] = "sha256:" + "0" * 64
                else:
                    value["unknown"] = True
                write_json(ledger_path, value)
                if case != "unanchored-ledger":
                    repin_ledger()
            for direct in (False, True):
                path = output / f"rejected-{case}-{direct}.json"
                with pytest.raises(EvaluationError):
                    if direct:
                        write_pinned_confirmation_catalog(result, catalog_path=path)
                    else:
                        recover_pinned_confirmation_catalog(output, catalog_path=path)
                assert not path.exists()
        finally:
            for path, content in originals.items():
                write(path, content)
    with pytest.raises(EvaluationError):
        write_pinned_confirmation_catalog(
            replace(result, engine_choice_bindings_sha256=None),
            catalog_path=output / "rejected-unbound-result.json",
        )


@pytest.mark.parametrize(
    "mismatch",
    ["later-candidate-cache", "profile-model", "explicit-executor", "lifecycle-choice"],
)
def test_mismatch_is_rejected_before_lifecycle_or_native_dispatch(
    tmp_path: Path, synthetic_sources: list[str], mismatch: str
) -> None:
    async def exercise() -> None:
        async with sglang_loopback._replicas() as (origins, replicas):
            rehearsal = _rehearsal(
                origins, mismatched_high_cache=mismatch == "later-candidate-cache"
            )
            profiles = _profiles(origins)
            if mismatch == "profile-model":
                profiles["endpoint-b"] = profiles["endpoint-b"].model_copy(
                    update={"served_model_name": "other-model"}
                )
            output = tmp_path / "rejected"
            output.mkdir(mode=0o700)
            lifecycle = _Lifecycle(origins, output=output, replicas=replicas)
            if mismatch == "lifecycle-choice":
                lifecycle.engine_choice_sha256 = "sha256:" + "0" * 64
            with pytest.raises(EvaluationError):
                await run_rehearsal(
                    rehearsal,
                    output,
                    lifecycle=lifecycle,
                    sglang_profiles=profiles,
                    executor=execute_trial if mismatch == "explicit-executor" else None,
                )
            assert not lifecycle.events
            assert not synthetic_sources
            assert all(not replica.requests for replica in replicas)
            assert list(output.iterdir()) == []
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())


def test_reserved_engine_choice_ledger_prevents_mutation_and_dispatch(
    tmp_path: Path, synthetic_sources: list[str]
) -> None:
    async def exercise() -> None:
        async with sglang_loopback._replicas() as (origins, replicas):
            rehearsal = _rehearsal(origins)
            output = tmp_path / "reserved"
            output.mkdir(mode=0o700)
            reserved = output / "engine-choice-bindings.json"
            reserved.write_bytes(b"existing-private-owner-reservation\n")
            reserved.chmod(0o600)
            lifecycle = _Lifecycle(origins, output=output, replicas=replicas)
            with pytest.raises(FileExistsError):
                await run_rehearsal(
                    rehearsal,
                    output,
                    lifecycle=lifecycle,
                    sglang_profiles=_profiles(origins),
                )
            assert reserved.read_bytes() == b"existing-private-owner-reservation\n"
            assert not lifecycle.events
            assert not synthetic_sources
            assert all(not replica.requests for replica in replicas)
            assert not (output / "calibration-load-low").exists()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "forgery",
    [
        "phase-config",
        "phase-plan",
        "duration-reserve",
        "output-reserve",
        "trial-mapping",
        "recipe-digest",
        "numeric-alias",
        "config-numeric-alias",
    ],
)
def test_forged_compiled_rehearsal_is_rejected_before_any_phase(
    tmp_path: Path, synthetic_sources: list[str], forgery: str
) -> None:
    async def exercise() -> None:
        # No listener is needed: the malformed declaration must never reach I/O.
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        original = _rehearsal(origins)
        later = original.candidates[1]
        if forgery == "phase-config":
            raw = later.confirmation_config.model_dump(mode="json")
            raw["preparation"]["prefix_caching"] = "DECLARED_DISABLED"
            later = replace(later, confirmation_config=load(raw))
        elif forgery == "phase-plan":
            later = replace(
                later, confirmation_plan=original.candidates[0].confirmation_plan
            )
        elif forgery == "trial-mapping":
            rows = list(later.calibration)
            rows[1] = replace(rows[1], source_index=0)
            later = replace(later, calibration=tuple(rows))
        elif forgery == "recipe-digest":
            later = replace(later, confirmation_recipe_sha256="sha256:" + "0" * 64)
        elif forgery == "config-numeric-alias":
            config = later.confirmation_config
            blocks = list(config.blocks)
            blocks[0] = blocks[0].model_copy(
                update={
                    "foreground": blocks[0].foreground.model_copy(
                        update={"max_tokens": float(blocks[0].foreground.max_tokens)}
                    )
                }
            )
            later = replace(
                later,
                confirmation_config=config.model_copy(update={"blocks": tuple(blocks)}),
            )
        forged = replace(original, candidates=(original.candidates[0], later))
        if forgery == "duration-reserve":
            forged = replace(forged, final_cleanup_reserve_ns=0)
        elif forgery == "output-reserve":
            forged = replace(forged, reserved_output_bytes=0)
        elif forgery == "numeric-alias":
            forged = replace(
                forged, worst_case_duration_ns=float(original.worst_case_duration_ns)
            )
        output = tmp_path / "forged"
        output.mkdir(mode=0o700)
        lifecycle = _Lifecycle(origins, output=output, replicas=())
        with pytest.raises(EvaluationError):
            await run_rehearsal(
                forged,
                output,
                lifecycle=lifecycle,
                sglang_profiles=_profiles(origins),
            )
        assert not lifecycle.events
        assert not lifecycle.choice_contents
        assert not synthetic_sources
        assert list(output.iterdir()) == []
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())


def test_sglang_marked_lifecycle_cannot_enter_native_vllm_path(
    tmp_path: Path, synthetic_sources: list[str]
) -> None:
    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        rehearsal = _rehearsal(origins)
        output = tmp_path / "wrong-engine"
        output.mkdir(mode=0o700)
        lifecycle = _Lifecycle(origins, output=output, replicas=())
        lifecycle.engine_choice_sha256 = bind_sglang_rehearsal(
            rehearsal, _profiles(origins)
        ).engine_choice_sha256
        with pytest.raises(EvaluationError):
            await run_rehearsal(
                rehearsal, output, lifecycle=lifecycle, executor=execute_trial
            )
        assert not lifecycle.events
        assert not lifecycle.choice_contents
        assert not synthetic_sources
        assert list(output.iterdir()) == []
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())


def test_sglang_report_envelopes_are_reserved_before_any_lifecycle_work(
    tmp_path: Path, synthetic_sources: list[str]
) -> None:
    async def exercise() -> None:
        origins = ("http://127.0.0.1:18001", "http://127.0.0.1:18002")
        original = _rehearsal(origins)
        protocol = original.calibration_plan.protocol.model_dump(mode="json")
        # The native reserve fits, but the additional v2 JSON/Markdown envelopes
        # and durable engine ledger must fit before creating any output.
        protocol["max_session_output_bytes"] = original.reserved_output_bytes
        rehearsal = compile_rehearsal(
            load_calibration_protocol_bytes(canonical_json_bytes(protocol)),
            tuple(
                CandidateStudyRecipe(
                    item.level.level_id,
                    item.calibration_config,
                    item.confirmation_config,
                )
                for item in original.candidates
            ),
        )
        output = tmp_path / "bounded-output"
        output.mkdir(mode=0o700)
        lifecycle = _Lifecycle(origins, output=output, replicas=())
        with pytest.raises(EvaluationError, match="output reserve"):
            await run_rehearsal(
                rehearsal,
                output,
                lifecycle=lifecycle,
                sglang_profiles=_profiles(origins),
            )
        assert not lifecycle.events
        assert not synthetic_sources
        assert list(output.iterdir()) == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure,executed,cleanup",
    [
        ("prepare_error", 0, "CONFIRMED"),
        ("prepare_cancel", 0, "CONFIRMED"),
        ("prepare_timeout", 0, "CONFIRMED"),
        ("cleanup_error", 1, "FAILED"),
    ],
)
def test_failure_retains_cleanup_and_never_starts_the_next_trial(
    tmp_path: Path,
    synthetic_sources: list[str],
    failure: str,
    executed: int,
    cleanup: str,
) -> None:
    async def exercise() -> None:
        async with sglang_loopback._replicas() as (origins, replicas):
            rehearsal = _rehearsal(origins)
            output = tmp_path / "failed"
            output.mkdir(mode=0o700)
            lifecycle = _Lifecycle(
                origins, output=output, replicas=replicas, failure=failure
            )
            with pytest.raises(EvaluationError, match="incomplete calibration study"):
                await asyncio.wait_for(
                    run_rehearsal(
                        rehearsal,
                        output,
                        lifecycle=lifecycle,
                        sglang_profiles=_profiles(origins),
                    ),
                    10,
                )
            assert len(synthetic_sources) == executed
            assert lifecycle.events == [
                ("prepare", "trial-0000"),
                ("cleanup", "trial-0000"),
            ]
            manifest = json.loads(
                (output / "calibration-load-low" / "manifest.json").read_bytes()
            )
            assert manifest["status"] == (
                "CANCELLED" if failure == "prepare_cancel" else "ABORTED"
            )
            assert manifest["trials"][0]["state"] == "ABORTED"
            assert all(row["state"] == "NOT_RUN" for row in manifest["trials"][1:])
            receipts = json.loads(
                (output / "calibration-load-low-lifecycle.json").read_bytes()
            )
            assert len(receipts["receipts"]) == 1
            assert receipts["receipts"][0]["cleanup"] == cleanup
            assert not (output / "calibration-load-high").exists()
            assert not (output / "confirmation-load-high").exists()
            assert all(replica.active == 0 for replica in replicas)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())
