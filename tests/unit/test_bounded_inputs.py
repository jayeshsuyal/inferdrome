"""Focused boundary coverage for bounded untrusted-input helpers."""

from collections.abc import Callable, Iterator
from typing import Any, Self

import pytest

import inferdrome.adapters.vllm_bench as vllm_bench
import inferdrome.bundle.reader as bundle_reader
import inferdrome.resolution.workload as workload_parser
from inferdrome.errors import (
    AdapterError,
    SourceInputError,
    VerificationError,
    WorkLimitError,
)
from inferdrome.limits import WorkBudget, WorkLimits, collect_bounded
from inferdrome.parsing import (
    BoundedParseError,
    StructuredDataLimits,
    bounded_json_int,
    validate_json_structure,
    validate_top_level_json_array_limit,
    validate_yaml_structure,
)
from inferdrome.resolution.workload import WorkloadLimits, parse_custom_workload


class _FindSentinelBytes(bytes):
    """Fail if a bounded workload scan asks for more than its N+1 record."""

    find_calls: int
    max_find_calls: int

    def __new__(cls, value: bytes, *, max_find_calls: int) -> Self:
        instance = super().__new__(cls, value)
        instance.find_calls = 0
        instance.max_find_calls = max_find_calls
        return instance

    def find(self, sub: bytes, start: int = 0) -> int:
        self.find_calls += 1
        if self.find_calls > self.max_find_calls:
            raise AssertionError("workload scan advanced beyond the N+1 sentinel")
        return super().find(sub, start)


def test_collect_bounded_consumes_only_limit_plus_one_sentinel() -> None:
    consumed: list[int] = []

    def values() -> Iterator[int]:
        for value in range(10):
            consumed.append(value)
            if value > 3:
                raise AssertionError("iterator advanced beyond the N+1 sentinel")
            yield value

    with pytest.raises(RuntimeError, match="population limit"):
        collect_bounded(
            values(),
            limit=3,
            error=lambda: RuntimeError("population limit exceeded"),
        )

    assert consumed == [0, 1, 2, 3]


def test_work_budget_accepts_exact_unit_and_byte_limits_then_fails_atomically() -> None:
    budget = WorkBudget(
        WorkLimits(max_units=2, max_bytes=3, max_seconds=1.0),
        clock=lambda: 10.0,
    )

    budget.reserve(units=2, bytes_=3)
    assert (budget.units, budget.bytes) == (2, 3)

    with pytest.raises(WorkLimitError, match="work limit"):
        budget.reserve(units=1)
    assert (budget.units, budget.bytes) == (2, 3)

    with pytest.raises(WorkLimitError, match="byte limit"):
        budget.reserve(bytes_=1)
    assert (budget.units, budget.bytes) == (2, 3)


def test_work_budget_accepts_exact_deadline_and_rejects_time_overage() -> None:
    now = [100.0]
    budget = WorkBudget(
        WorkLimits(max_units=1, max_bytes=1, max_seconds=1.0),
        clock=lambda: now[0],
    )

    now[0] = 101.0
    budget.checkpoint()

    now[0] = 101.000_001
    with pytest.raises(WorkLimitError, match="time limit"):
        budget.checkpoint()


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), float("-inf")])
def test_work_limits_reject_non_finite_deadlines(seconds: float) -> None:
    with pytest.raises(ValueError, match="positive numbers"):
        WorkLimits(max_units=1, max_bytes=1, max_seconds=seconds)


def test_json_depth_limit_accepts_64_and_rejects_65() -> None:
    validate_json_structure("[" * 64 + "0" + "]" * 64)

    with pytest.raises(BoundedParseError, match="structural depth"):
        validate_json_structure("[" * 65 + "0" + "]" * 65)


def test_json_token_limit_accepts_exact_count_and_rejects_next_token() -> None:
    limits = StructuredDataLimits(
        max_depth=64,
        max_tokens=3,
        max_integer_digits=256,
    )

    validate_json_structure("[0]", limits=limits)

    with pytest.raises(BoundedParseError, match="token count"):
        validate_json_structure("[0,0]", limits=limits)


def test_json_integer_limit_accepts_256_digits_and_rejects_257() -> None:
    exact = "9" * 256
    over = "9" * 257

    validate_json_structure(exact)
    assert str(bounded_json_int(exact)) == exact

    with pytest.raises(BoundedParseError, match="digit limit"):
        validate_json_structure(over)
    with pytest.raises(BoundedParseError, match="digit limit"):
        bounded_json_int(over)


def test_json_numeric_overflow_is_a_bounded_verification_error() -> None:
    with pytest.raises(VerificationError, match="bounded JSON"):
        bundle_reader.strict_json_value(b'{"value":1e10000}', label="probe")


@pytest.mark.parametrize(
    ("parser", "message"),
    [
        (vllm_bench._strict_json_object, "endpoint preflight response"),
        (vllm_bench._strict_invocation_object, "vLLM invocation evidence"),
    ],
)
def test_vllm_json_helpers_bound_depth_and_integer_size(
    parser: Callable[[bytes], dict[str, Any]],
    message: str,
) -> None:
    exact = b'{"value":' + b"9" * 256 + b"}"
    assert parser(exact)["value"] == int("9" * 256)

    with pytest.raises(AdapterError, match=message):
        parser(b'{"value":' + b"9" * 257 + b"}")
    with pytest.raises(AdapterError, match=message):
        parser(b'{"value":' + b"[" * 64 + b"0" + b"]" * 64 + b"}")


@pytest.mark.parametrize(
    "prefix,digit",
    [
        ("", "9"),
        ("0b", "1"),
        ("0x", "f"),
    ],
)
def test_yaml_integer_limit_covers_supported_radices(
    prefix: str,
    digit: str,
) -> None:
    validate_yaml_structure(f"value: {prefix}{digit * 256}\n")

    with pytest.raises(BoundedParseError, match="digit limit"):
        validate_yaml_structure(f"value: {prefix}{digit * 257}\n")


def test_yaml_integer_limit_covers_sexagesimal_values() -> None:
    with pytest.raises(BoundedParseError, match="digit limit"):
        validate_yaml_structure("value: 1" + ":59" * 128 + "\n")


def test_yaml_depth_limit_accepts_64_and_rejects_65() -> None:
    validate_yaml_structure("[" * 64 + "0" + "]" * 64)

    with pytest.raises(BoundedParseError, match="structural depth"):
        validate_yaml_structure("[" * 65 + "0" + "]" * 65)


def test_top_level_json_array_item_limit_accepts_exact_and_rejects_over() -> None:
    validate_top_level_json_array_limit(
        '{"items":[0,{"nested":[1,2]},"three"],'
        '"other":{"items":[0,1,2,3]}}',
        key="items",
        max_items=3,
    )

    with pytest.raises(BoundedParseError, match="items array"):
        validate_top_level_json_array_limit(
            '{"items":[0,{"nested":[1,2]},"three",4]}',
            key="items",
            max_items=3,
        )


def test_jsonl_over_cap_is_rejected_before_any_record_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[bytes] = []

    def record_parse(content: bytes, *, label: str) -> None:
        parsed.append(content)

    monkeypatch.setattr(bundle_reader, "strict_json_value", record_parse)
    exact = b"{}\n{}\n"
    assert bundle_reader.strict_jsonl_lines(
        exact,
        label="records",
        max_line_bytes=64,
        max_records=2,
    ) == (b"{}", b"{}")
    assert parsed == [b"{}", b"{}"]

    parsed.clear()
    with pytest.raises(VerificationError, match="record count exceeds limit"):
        bundle_reader.strict_jsonl_lines(
            exact + b"not-json\n",
            label="records",
            max_line_bytes=64,
            max_records=2,
        )
    assert parsed == []


def test_jsonl_line_bounds_and_lf_records_are_checked_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[bytes] = []

    def record_parse(content: bytes, *, label: str) -> None:
        parsed.append(content)

    monkeypatch.setattr(bundle_reader, "strict_json_value", record_parse)
    with pytest.raises(VerificationError, match="invalid line size"):
        bundle_reader.strict_jsonl_lines(
            b"12345\n",
            label="records",
            max_line_bytes=4,
            max_records=1,
        )
    assert parsed == []

    monkeypatch.undo()
    with pytest.raises(VerificationError, match="bounded JSON"):
        bundle_reader.strict_jsonl_lines(
            b"{}\r{}\n",
            label="records",
            max_line_bytes=64,
            max_records=1,
        )


def test_workload_does_not_parse_unused_malformed_suffix() -> None:
    limits = WorkloadLimits(
        max_records=2,
        max_line_bytes=128,
        max_prompt_characters=32,
    )

    assert parse_custom_workload(
        b'{"prompt":"used"}\nnot-json\n',
        required_prompt_count=1,
        limits=limits,
    ) == ("used",)


def test_workload_over_cap_stops_at_sentinel_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[str] = []

    def record_parse(text: str) -> None:
        parsed.append(text)

    monkeypatch.setattr(workload_parser, "validate_json_structure", record_parse)
    content = _FindSentinelBytes(
        b'{"prompt":"one"}\n'
        b'{"prompt":"two"}\n'
        b"not-json\n"
        b'{"prompt":"never-scanned"}\n',
        max_find_calls=3,
    )
    limits = WorkloadLimits(
        max_records=2,
        max_line_bytes=128,
        max_prompt_characters=32,
    )

    with pytest.raises(SourceInputError, match="record count exceeds its limit"):
        parse_custom_workload(
            content,
            required_prompt_count=1,
            limits=limits,
        )

    assert content.find_calls == 3
    assert parsed == []
