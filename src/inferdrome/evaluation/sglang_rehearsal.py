"""One declared SGLang engine choice across every native rehearsal phase.

This is a pure pre-dispatch binding helper. The existing rehearsal remains the
only session/lifecycle owner, and existing reducers own calibration selection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from inferdrome.evaluation.contracts import EndpointId, EvaluationError
from inferdrome.evaluation.engine_binding import (
    EvaluationEngineBinding,
    build_sglang_engine_binding,
    engine_binding_sha256,
    engine_choice_sha256,
)
from inferdrome.evaluation.sglang_execution import SGLangStudyExecutor
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.study_config import CompiledStudy, StudyConfig
from inferdrome.routing_execution.canonical import canonical_json_bytes

if TYPE_CHECKING:
    from inferdrome.evaluation.load_calibration_rehearsal import CompiledRehearsal

Phase = Literal["CALIBRATION", "CONFIRMATION"]


@dataclass(frozen=True)
class _PhaseBinding:
    level_id: str
    phase: Phase
    plan: CompiledStudy = field(repr=False)
    binding: EvaluationEngineBinding


@dataclass(frozen=True)
class SGLangRehearsalBindings:
    protocol_sha256: str
    engine_choice_sha256: str
    phases: tuple[_PhaseBinding, ...]
    _profiles: dict[EndpointId, SglangServingConfig] = field(repr=False)

    @property
    def contexts(self) -> tuple[tuple[CompiledStudy, EvaluationEngineBinding], ...]:
        return tuple((item.plan, item.binding) for item in self.phases)

    def binding_for(self, level_id: str, phase: Phase) -> EvaluationEngineBinding:
        return self._phase(level_id, phase).binding

    def executor_for(self, level_id: str, phase: Phase) -> SGLangStudyExecutor:
        item = self._phase(level_id, phase)
        return SGLangStudyExecutor(item.plan, item.binding, self._profiles)

    def _phase(self, level_id: str, phase: Phase) -> _PhaseBinding:
        selected = [
            item
            for item in self.phases
            if item.level_id == level_id and item.phase == phase
        ]
        if len(selected) != 1:
            raise EvaluationError("SGLang rehearsal phase is not uniquely bound")
        return selected[0]

    def ledger_bytes(self, candidate_recipe_bindings_sha256: str) -> bytes:
        return (
            canonical_json_bytes(
                {
                    "schema_version": (
                        "inferdrome.evaluation-load-rehearsal-engine-bindings.v1"
                    ),
                    "protocol_sha256": self.protocol_sha256,
                    "candidate_recipe_bindings_sha256": (
                        candidate_recipe_bindings_sha256
                    ),
                    "engine_choice_sha256": self.engine_choice_sha256,
                    "phases": [
                        {
                            "level_id": item.level_id,
                            "phase": item.phase,
                            "engine_binding": item.binding.model_dump(mode="json"),
                            "engine_binding_sha256": engine_binding_sha256(
                                item.binding
                            ),
                        }
                        for item in self.phases
                    ],
                    "runtime_identity": "UNVERIFIED",
                    "evidence_eligible": False,
                }
            )
            + b"\n"
        )


def bind_sglang_rehearsal(
    rehearsal: CompiledRehearsal,
    profiles: Mapping[EndpointId, SglangServingConfig],
    *,
    containerized: bool = False,
) -> SGLangRehearsalBindings:
    """Check every candidate and both phases before any lifecycle mutation.

    Even an ultimately unselected confirmation recipe must retain the same
    endpoint, artifact, cache, telemetry and launch-mapping choice.
    """
    if type(containerized) is not bool:
        raise EvaluationError("SGLang rehearsal launch choice is invalid")
    rehearsal = _checked_rehearsal(rehearsal)
    selected_profiles = dict(profiles)
    phases: list[_PhaseBinding] = []
    for candidate in rehearsal.candidates:
        phase_plans: tuple[tuple[Phase, CompiledStudy], ...] = (
            ("CALIBRATION", candidate.calibration_plan),
            ("CONFIRMATION", candidate.confirmation_plan),
        )
        for phase, plan in phase_plans:
            binding = build_sglang_engine_binding(
                plan, selected_profiles, containerized=containerized
            )
            phases.append(_PhaseBinding(candidate.level.level_id, phase, plan, binding))
    choices = {engine_choice_sha256(item.binding) for item in phases}
    if len(choices) != 1:
        raise EvaluationError("rehearsal phases do not retain one SGLang engine choice")
    return SGLangRehearsalBindings(
        rehearsal.calibration_plan.protocol_sha256,
        choices.pop(),
        tuple(phases),
        selected_profiles,
    )


def _checked_rehearsal(rehearsal: CompiledRehearsal) -> CompiledRehearsal:
    """Recompile both configs, trial mappings and reserves before any phase runs."""
    from inferdrome.evaluation.load_calibration import LoadCalibrationProtocol
    from inferdrome.evaluation.load_calibration_rehearsal import (
        CandidateStudyRecipe,
        CompiledRehearsal,
        compile_rehearsal,
    )

    def encode(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json", warnings=False)
        if is_dataclass(value) and not isinstance(value, type):
            return {item.name: getattr(value, item.name) for item in fields(value)}
        raise ValueError

    def exact_shape(value: object) -> str:
        # Unlike canonical JSON, standard JSON retains 1 versus 1.0. Pair this
        # with dataclass equality so copied numeric or tuple/list aliases fail.
        return json.dumps(value, default=encode, sort_keys=True, allow_nan=False)

    try:
        if type(rehearsal) is not CompiledRehearsal:
            raise ValueError
        protocol = LoadCalibrationProtocol.model_validate(
            rehearsal.calibration_plan.protocol.model_dump(
                mode="python", warnings=False
            )
        )
        if type(rehearsal.candidates) is not tuple or len(rehearsal.candidates) != len(
            protocol.levels
        ):
            raise ValueError
        candidates = tuple(
            CandidateStudyRecipe(
                level_id=candidate.level.level_id,
                calibration_config=StudyConfig.model_validate(
                    candidate.calibration_config.model_dump(
                        mode="python", warnings=False
                    )
                ),
                confirmation_config=StudyConfig.model_validate(
                    candidate.confirmation_config.model_dump(
                        mode="python", warnings=False
                    )
                ),
            )
            for candidate in rehearsal.candidates
        )
        rebuilt = compile_rehearsal(protocol, candidates)
        if rehearsal != rebuilt or exact_shape(rehearsal) != exact_shape(rebuilt):
            raise ValueError
        return rebuilt
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise EvaluationError(
            "SGLang rehearsal differs from its compiled recipes"
        ) from None
