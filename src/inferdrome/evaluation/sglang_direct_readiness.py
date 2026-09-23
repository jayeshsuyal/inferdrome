"""Version-specific 0.5.15 preparation on the shared bounded HTTP transport."""

from __future__ import annotations

from collections.abc import Callable

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.sglang_direct_profile import (
    build_sglang_direct_profile,
    validate_sglang_direct_readiness,
)
from inferdrome.evaluation.sglang_profile import SglangReadiness, SglangServingConfig
from inferdrome.evaluation.sglang_readiness import (
    _FLUSH_SUCCESS,
    SGLangCacheReset,
    SGLangProbeTransport,
    SGLangReadinessAcquisition,
    _acquire,
    _completed,
    _local_config,
    _reported_idle,
    _timestamp,
    acquire_sglang_metrics,
)


async def acquire_sglang_direct_readiness(
    config: SglangServingConfig,
    transport: SGLangProbeTransport,
    *,
    deadline_ns: int,
    now_ns: Callable[[], int],
) -> SGLangReadinessAcquisition:
    """One health/generation/model attempt, before warmup/drain/cache reset."""
    config = _local_config(config)
    started = _timestamp(now_ns())
    responses = []
    for path in ("/health", "/health_generate", "/model_info"):
        responses.append(
            await _acquire(
                transport, config, path, deadline_ns=deadline_ns, now_ns=now_ns
            )
        )
    health, generation, info = responses
    readiness = validate_sglang_direct_readiness(
        config,
        health_status=health.status,
        generation_status=generation.status,
        model_info_status=info.status,
        model_info=info.body,
    )
    completed = _completed(now_ns, started=started, deadline_ns=deadline_ns)
    return SGLangReadinessAcquisition(readiness, generation.status, started, completed)


async def reset_sglang_direct_cache(
    config: SglangServingConfig,
    transport: SGLangProbeTransport,
    *,
    readiness: SGLangReadinessAcquisition,
    locally_owned_requests_drained: bool,
    max_age_ns: int,
    deadline_ns: int,
    now_ns: Callable[[], int],
) -> SGLangCacheReset:
    """Flush once after owner warmup/drain, then non-generating readbacks.

    The drain flag is an owner assertion about its own request tasks. Freshly
    acquired zero gauges do not prove server idle: their source age is unknown.
    The pinned server's successful flush response is the authoritative server
    acceptance. An error/cancellation returns no reset receipt and never retries
    an uncertain mutation. The owner must prevent new offers for this sequence.
    """
    config = _local_config(config)
    profile = build_sglang_direct_profile(config)
    if locally_owned_requests_drained is not True:
        raise EvaluationError("SGLang reset requires locally owned requests drained")
    _timestamp(max_age_ns)
    if max_age_ns == 0:
        raise EvaluationError("SGLang reset metric age bound must be positive")
    if (
        type(readiness) is not SGLangReadinessAcquisition
        or type(readiness.readiness) is not SglangReadiness
        or readiness.readiness != SglangReadiness(profile.config_sha256)
        or type(readiness.generation_status) is not int
        or readiness.generation_status != 200
        or not (
            _timestamp(readiness.started_ns)
            <= _timestamp(readiness.completed_ns)
            <= _timestamp(now_ns())
        )
    ):
        raise EvaluationError("SGLang reset readiness binding is invalid")
    before = await acquire_sglang_metrics(
        config, transport, deadline_ns=deadline_ns, now_ns=now_ns
    )
    _reported_idle(before, now_ns=now_ns(), max_age_ns=max_age_ns)
    response = await _acquire(
        transport,
        config,
        "/flush_cache",
        method="POST",
        deadline_ns=deadline_ns,
        now_ns=now_ns,
    )
    if response.body != _FLUSH_SUCCESS:
        raise EvaluationError("SGLang flush response violates the pinned contract")
    flush_completed = _timestamp(now_ns())
    health = await _acquire(
        transport, config, "/health", deadline_ns=deadline_ns, now_ns=now_ns
    )
    info = await _acquire(
        transport, config, "/model_info", deadline_ns=deadline_ns, now_ns=now_ns
    )
    # Generation status is the actual earlier probe result, never a new probe
    # after reset or a fabricated replacement for a missing readiness response.
    checked = validate_sglang_direct_readiness(
        config,
        health_status=health.status,
        generation_status=readiness.generation_status,
        model_info_status=info.status,
        model_info=info.body,
    )
    after = await acquire_sglang_metrics(
        config, transport, deadline_ns=deadline_ns, now_ns=now_ns
    )
    _reported_idle(after, now_ns=now_ns(), max_age_ns=max_age_ns)
    _completed(now_ns, started=after.completed_ns, deadline_ns=deadline_ns)
    return SGLangCacheReset(checked, before, after, flush_completed)
