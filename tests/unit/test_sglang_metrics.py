"""SYNTHETIC_ONLY SGLang metrics; runtime unverified, no GPU/performance evidence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from inferdrome.evaluation.observations import (
    MAX_LABEL_VALUE_BYTES,
    MAX_METRIC_LINE_BYTES,
    MAX_METRIC_LINES,
    MAX_METRICS_BODY_BYTES,
)
from inferdrome.evaluation.policies import MAX_LOAD_COUNT, MAX_SAFE_INTEGER
from inferdrome.evaluation.sglang_metrics import (
    SGLANG_QUEUE_GAUGE,
    SGLANG_RUNNING_GAUGE,
    MalformedSGLangMetrics,
    MissingSGLangMetrics,
    SGLangSchedulerCounts,
    SGLangSchedulerSample,
    parse_sglang_metrics,
)

_MODEL = "Synthetic/Model-sentinel_47"
_LABELS = (
    f'model_name="{_MODEL}",engine_type="unified",'
    'tp_rank="0",pp_rank="0",moe_ep_rank="0"'
)


def _gauge(name: str, value: str = "1", labels: str = _LABELS) -> bytes:
    return f"{name}{{{labels}}} {value}\n".encode()


def _body(running: str = "1", queued: str = "2") -> bytes:
    return (
        b"# SYNTHETIC_ONLY runtime unverified; no GPU/performance evidence\n"
        + _gauge(SGLANG_RUNNING_GAUGE, running)
        + _gauge(SGLANG_QUEUE_GAUGE, queued)
    )


def _parse(body: bytes) -> SGLangSchedulerSample:
    return parse_sglang_metrics(body, model=_MODEL, started_ns=100, completed_ns=105)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("0", 0),
        ("1.0", 1),
        ("+2", 2),
        (".0", 0),
        ("1.e2", 100),
        ("1.0e+02", 100),
        (str(MAX_LOAD_COUNT), MAX_LOAD_COUNT),
    ],
)
def test_exact_bounded_scheduler_counts(number: str, expected: int) -> None:
    sample = _parse(_body(number, number))
    assert sample.model_name == _MODEL
    assert sample.counts == SGLangSchedulerCounts(expected, expected)
    assert (sample.started_ns, sample.completed_ns) == (100, 105)


def test_base_labels_allow_order_and_whitespace_but_do_not_alias_vllm() -> None:
    labels = f' pp_rank="0",model_name="{_MODEL}",tp_rank="0",'
    labels += ' moe_ep_rank="0", engine_type = "unified" '
    body = (
        b"# SYNTHETIC_ONLY runtime unverified\r\n"
        b"# TYPE sglang:num_running_reqs gauge\r\n"
        + _gauge(SGLANG_RUNNING_GAUGE, "3", labels)
        + b'vllm:num_requests_running{model_name="another",engine="0"} 999\n'
        + b'sglang:num_queue_reqs_by_reason{reason="grammar"} 999\n'
        + b'sglang:num_grammar_queue_reqs{ignored="true"} 999\n'
        + _gauge(SGLANG_QUEUE_GAUGE, "4", labels)
    )
    counts = _parse(body).counts
    assert counts.reported_running_requests == 3
    assert counts.reported_queued_requests == 4
    assert not hasattr(counts, "score")


@pytest.mark.parametrize(
    "number",
    [
        "-1",
        "-0",
        "NaN",
        "+Inf",
        "Inf",
        "-Inf",
        "1.5",
        "true",
        "1_000",
        "0x1",
        "1e-99999",
        "1e99999",
        str(MAX_LOAD_COUNT + 1),
        "9" * 65,
        "1e9999999999999999999999999999999999",
        "1 1234567890",
        "1 # exemplar",
    ],
)
def test_noninteger_nonfinite_unbounded_or_timestamped_samples_fail_closed(
    number: str,
) -> None:
    with pytest.raises(MalformedSGLangMetrics):
        _parse(_body(number))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"# SYNTHETIC_ONLY comments only\n",
        b"unrelated_metric 42\n",
        _gauge(SGLANG_RUNNING_GAUGE),
        _gauge(SGLANG_QUEUE_GAUGE),
        b'vllm:num_requests_running{model_name="another",engine="0"} 1\n'
        b'vllm:num_requests_waiting{model_name="another",engine="0"} 2\n',
    ],
)
def test_missing_or_vllm_only_body_produces_no_sglang_sample(body: bytes) -> None:
    with pytest.raises(MissingSGLangMetrics):
        _parse(body)


@pytest.mark.parametrize(
    "labels",
    [
        _LABELS.replace(_MODEL, "Other/Model"),
        _LABELS.replace('engine_type="unified"', 'engine_type="prefill"'),
        _LABELS.replace('engine_type="unified"', 'engine_type="decode"'),
        _LABELS.replace('tp_rank="0"', 'tp_rank="1"'),
        _LABELS.replace('pp_rank="0"', 'pp_rank="1"'),
        _LABELS.replace('moe_ep_rank="0"', 'moe_ep_rank="1"'),
        _LABELS.replace('tp_rank="0"', 'tp_rank="00"'),
        _LABELS.replace('tp_rank="0"', "tp_rank=0"),
        _LABELS.replace('tp_rank="0"', 'tp_rank="\\n"'),
        _LABELS.replace('tp_rank="0",', ""),
        _LABELS.replace('tp_rank="0"', 'pp_rank="0"'),
        _LABELS + ',dp_rank="0"',
        _LABELS + ',priority="0"',
        _LABELS + ',extra="custom-label"',
        _LABELS + ",",
        _LABELS.replace(_MODEL, "x" * (MAX_LABEL_VALUE_BYTES + 1)),
    ],
)
def test_other_models_ranks_and_unsupported_dimensions_are_malformed(
    labels: str,
) -> None:
    body = _gauge(SGLANG_RUNNING_GAUGE, labels=labels) + _gauge(SGLANG_QUEUE_GAUGE)
    with pytest.raises(MalformedSGLangMetrics):
        _parse(body)


@pytest.mark.parametrize(
    "extra",
    [
        _gauge(SGLANG_RUNNING_GAUGE),
        _gauge(SGLANG_QUEUE_GAUGE),
        _gauge(SGLANG_RUNNING_GAUGE, labels=_LABELS.replace(_MODEL, "Other/Model")),
    ],
)
def test_duplicate_and_multiple_model_series_are_not_selected_or_summed(
    extra: bytes,
) -> None:
    with pytest.raises(MalformedSGLangMetrics):
        _parse(_body() + extra)


@pytest.mark.parametrize(
    "body",
    [
        f"{SGLANG_RUNNING_GAUGE} 1\n".encode() + _gauge(SGLANG_QUEUE_GAUGE),
        f"{SGLANG_RUNNING_GAUGE}{{{_LABELS}}}\n".encode() + _gauge(SGLANG_QUEUE_GAUGE),
        _body() + b"# invalid utf8 \xff\n",
        _body() + b"# invalid\x00text\n",
        _body() + b"# internal\rcarriage\n",
        b"#" * (MAX_METRIC_LINE_BYTES + 1),
        b"\n" * MAX_METRIC_LINES,
        b"\n" * (MAX_METRICS_BODY_BYTES + 1),
    ],
)
def test_body_line_text_and_target_exposition_limits(body: bytes) -> None:
    with pytest.raises(MalformedSGLangMetrics):
        _parse(body)


@pytest.mark.parametrize("model", ["", "private-secret\n", "x" * 201, "/Model"])
def test_invalid_model_error_does_not_echo_input(model: str) -> None:
    with pytest.raises(MalformedSGLangMetrics) as error:
        parse_sglang_metrics(_body(), model=model, started_ns=0, completed_ns=1)
    assert "private" not in str(error.value).lower()
    assert _MODEL not in repr(error.value)


def test_non_bytes_body_is_rejected() -> None:
    with pytest.raises(MalformedSGLangMetrics):
        _parse(_body().decode())  # type: ignore[arg-type]


def test_samples_are_immutable_and_acquisition_start_ages_counts_out() -> None:
    sample = _parse(_body())
    assert sample.freshness_at(now_ns=110, max_age_ns=10) == "FRESH"
    assert sample.available_at(now_ns=110, max_age_ns=10) == sample.counts
    assert sample.freshness_at(now_ns=111, max_age_ns=10) == "STALE"
    assert sample.available_at(now_ns=111, max_age_ns=10) is None
    with pytest.raises(FrozenInstanceError):
        sample.started_ns = 111  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        sample.counts.reported_running_requests = 0  # type: ignore[misc]


def test_slow_scrape_is_already_unavailable_at_receipt() -> None:
    sample = parse_sglang_metrics(_body(), model=_MODEL, started_ns=0, completed_ns=50)
    assert sample.available_at(now_ns=50, max_age_ns=10) is None


@pytest.mark.parametrize(
    "body", [b"", _body("NaN"), _body() + _gauge(SGLANG_QUEUE_GAUGE)]
)
def test_failed_acquisition_does_not_refresh_retained_sample(body: bytes) -> None:
    sample = _parse(_body())
    with pytest.raises((MissingSGLangMetrics, MalformedSGLangMetrics)):
        parse_sglang_metrics(body, model=_MODEL, started_ns=111, completed_ns=112)
    assert sample.available_at(now_ns=112, max_age_ns=10) is None
    assert (sample.started_ns, sample.completed_ns) == (100, 105)


@pytest.mark.parametrize(
    ("started", "completed"),
    [(-1, 0), (2, 1), (0, MAX_SAFE_INTEGER + 1), (True, 2), (0, 1.0)],
)
def test_invalid_acquisition_timestamps_are_rejected(
    started: int, completed: int
) -> None:
    with pytest.raises(MalformedSGLangMetrics):
        parse_sglang_metrics(
            _body(), model=_MODEL, started_ns=started, completed_ns=completed
        )


@pytest.mark.parametrize(
    ("now", "age"),
    [
        (104, 10),
        (110, 0),
        (110, -1),
        (True, 10),
        (110, False),
        (110.0, 10),
        (110, MAX_SAFE_INTEGER + 1),
        (MAX_SAFE_INTEGER + 1, 10),
    ],
)
def test_invalid_freshness_queries_do_not_return_usable_counts(
    now: int, age: int
) -> None:
    with pytest.raises(MalformedSGLangMetrics):
        _parse(_body()).available_at(now_ns=now, max_age_ns=age)


@pytest.mark.parametrize("count", [-1, MAX_LOAD_COUNT + 1, True, 1.0, None])
def test_direct_count_construction_cannot_bypass_bounds(count: int) -> None:
    with pytest.raises(MalformedSGLangMetrics):
        SGLangSchedulerCounts(count, 0)


def test_direct_sample_construction_requires_the_specific_count_type() -> None:
    with pytest.raises(MalformedSGLangMetrics):
        SGLangSchedulerSample(_MODEL, 0, 1, (1, 2))  # type: ignore[arg-type]
