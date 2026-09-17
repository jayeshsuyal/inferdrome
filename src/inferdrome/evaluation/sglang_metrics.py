"""Bounded SGLang 0.5.18 scheduler metrics; no routing/evidence registration.

Targets release commit 71de97b264b04dcd514cf904003028aefe9775c8. These
SGLang scheduler gauges are deliberately not vLLM load observations. Only the
single-rank, non-disaggregated, default-label serving profile is supported.
The profile and parser are source-reviewed and CPU-tested; runtime unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.evaluation.observations import (
    MAX_LABEL_VALUE_BYTES,
    MAX_METRIC_LINE_BYTES,
    MAX_METRIC_LINES,
    MAX_METRICS_BODY_BYTES,
)
from inferdrome.evaluation.policies import MAX_LOAD_COUNT, MAX_SAFE_INTEGER

SGLANG_RUNNING_GAUGE = "sglang:num_running_reqs"
SGLANG_QUEUE_GAUGE = "sglang:num_queue_reqs"
_GAUGES = (SGLANG_RUNNING_GAUGE, SGLANG_QUEUE_GAUGE)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_SAMPLE = re.compile(
    r"(sglang:num_(?:running|queue)_reqs)\{([^{}]*)\}[ \t]+([^ \t]+)[ \t]*"
)
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]{0,63})[ \t]*=[ \t]*"([^"\\]*)"')
_NUMBER = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class MissingSGLangMetrics(EvaluationError):
    """At least one required SGLang scheduler gauge is unavailable."""


class MalformedSGLangMetrics(EvaluationError):
    """The input violates the bounded SGLang scheduler metrics contract."""


def _bounded_integer(value: object, *, maximum: int = MAX_SAFE_INTEGER) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise MalformedSGLangMetrics("SGLang metric integer violates its bound")


def _validate_model(model: str) -> None:
    if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
        raise MalformedSGLangMetrics("SGLang metric model identity is unsupported")


@dataclass(frozen=True, slots=True)
class SGLangSchedulerCounts:
    """Engine-specific gauge values, without a portable routing load score.

    Running is a prefill running-request snapshot or decode batch request count
    at the last metrics report. Queued is the scheduler waiting queue at that
    report; the separate grammar queue is excluded. Reporting is phase-dependent,
    including decode-log intervals and an idle interval up to 30 seconds. Fields
    do not assert vLLM load equivalence or count every client-offered request.
    """

    reported_running_requests: int
    reported_queued_requests: int

    def __post_init__(self) -> None:
        _bounded_integer(self.reported_running_requests, maximum=MAX_LOAD_COUNT)
        _bounded_integer(self.reported_queued_requests, maximum=MAX_LOAD_COUNT)


@dataclass(frozen=True, slots=True)
class SGLangSchedulerSample:
    """One complete scrape with caller-owned monotonic acquisition timestamps.

    Acquisition freshness cannot prove scheduler update freshness: the pinned
    gauges carry no source measurement time. A future runtime must maintain
    that distinction and call ``available_at`` before using these values.
    """

    model_name: str
    started_ns: int
    completed_ns: int
    counts: SGLangSchedulerCounts

    def __post_init__(self) -> None:
        _validate_model(self.model_name)
        _bounded_integer(self.started_ns)
        _bounded_integer(self.completed_ns)
        if self.started_ns > self.completed_ns:
            raise MalformedSGLangMetrics("SGLang metric timestamps are unordered")
        if type(self.counts) is not SGLangSchedulerCounts:
            raise MalformedSGLangMetrics("SGLang metric counts have the wrong type")

    def freshness_at(
        self, *, now_ns: int, max_age_ns: int
    ) -> Literal["FRESH", "STALE"]:
        """Age from acquisition start; slow reads never refresh an old sample."""
        _bounded_integer(now_ns)
        _bounded_integer(max_age_ns)
        if now_ns < self.completed_ns or max_age_ns == 0:
            raise MalformedSGLangMetrics("SGLang metric freshness bounds are invalid")
        return "FRESH" if now_ns - self.started_ns <= max_age_ns else "STALE"

    def available_at(
        self, *, now_ns: int, max_age_ns: int
    ) -> SGLangSchedulerCounts | None:
        """Return no usable values when acquisition is stale."""
        if self.freshness_at(now_ns=now_ns, max_age_ns=max_age_ns) == "STALE":
            return None
        return self.counts


def _labels(text: str, *, model: str) -> None:
    expected = {
        "model_name": model,
        "engine_type": "unified",
        "tp_rank": "0",
        "pp_rank": "0",
        "moe_ep_rank": "0",
    }
    pairs = text.split(",")
    if len(pairs) != len(expected):
        raise MalformedSGLangMetrics("SGLang metric labels violate selected identity")
    labels: dict[str, str] = {}
    for pair in pairs:
        match = _LABEL.fullmatch(pair.strip(" \t"))
        if match is None:
            raise MalformedSGLangMetrics(
                "SGLang metric labels violate selected identity"
            )
        name, value = match.groups()
        if name in labels or len(value.encode("utf-8")) > MAX_LABEL_VALUE_BYTES:
            raise MalformedSGLangMetrics(
                "SGLang metric labels violate selected identity"
            )
        labels[name] = value
    if labels != expected:
        raise MalformedSGLangMetrics("SGLang metric labels violate selected identity")


def _count(text: str) -> int:
    if len(text) > 64 or _NUMBER.fullmatch(text) is None:
        raise MalformedSGLangMetrics("SGLang metric count violates numeric contract")
    try:
        value = Decimal(text)
        if (
            not value.is_finite()
            or not 0 <= value <= MAX_LOAD_COUNT
            or value != value.to_integral_value()
        ):
            raise MalformedSGLangMetrics(
                "SGLang metric count violates numeric contract"
            )
        return int(value)
    except InvalidOperation:
        raise MalformedSGLangMetrics(
            "SGLang metric count violates numeric contract"
        ) from None


def parse_sglang_metrics(
    content: bytes, *, model: str, started_ns: int, completed_ns: int
) -> SGLangSchedulerSample:
    """Require exactly one pinned running and queue gauge for the selected model.

    Base dimensions must match a single unified rank. Extra dimensions, ranks,
    duplicate target samples, escaped labels, noninteger/nonfinite counts and
    server sample timestamps are rejected. Unrelated metrics/comments are
    ignored within body, line and text bounds. Missing or malformed input does
    not create a sample, zero-fill counts or refresh an earlier observation.
    """
    _validate_model(model)
    if not isinstance(content, bytes) or len(content) > MAX_METRICS_BODY_BYTES:
        raise MalformedSGLangMetrics("SGLang metric input violates parsing bounds")
    lines = content.split(b"\n", MAX_METRIC_LINES)
    if len(lines) > MAX_METRIC_LINES:
        raise MalformedSGLangMetrics("SGLang metric input violates parsing bounds")
    found: dict[str, int] = {}
    for raw_line in lines:
        if len(raw_line) > MAX_METRIC_LINE_BYTES:
            raise MalformedSGLangMetrics("SGLang metric input violates parsing bounds")
        try:
            line = raw_line.removesuffix(b"\r").decode("utf-8")
        except UnicodeError:
            raise MalformedSGLangMetrics(
                "SGLang metric input is not valid text"
            ) from None
        if "\r" in line or "\x00" in line:
            raise MalformedSGLangMetrics("SGLang metric input is not valid text")
        line = line.strip(" \t")
        if not line or line.startswith("#"):
            continue
        name = _NAME.match(line)
        if name is None or name.group() not in _GAUGES:
            continue
        sample = _SAMPLE.fullmatch(line)
        if sample is None:
            raise MalformedSGLangMetrics("SGLang metric violates exposition contract")
        gauge, labels, number = sample.groups()
        if gauge in found:
            raise MalformedSGLangMetrics("SGLang metric sample is duplicated")
        _labels(labels, model=model)
        found[gauge] = _count(number)
    if len(found) != len(_GAUGES):
        raise MissingSGLangMetrics("required SGLang scheduler gauges are missing")
    return SGLangSchedulerSample(
        model_name=model,
        started_ns=started_ns,
        completed_ns=completed_ns,
        counts=SGLangSchedulerCounts(
            reported_running_requests=found[SGLANG_RUNNING_GAUGE],
            reported_queued_requests=found[SGLANG_QUEUE_GAUGE],
        ),
    )
