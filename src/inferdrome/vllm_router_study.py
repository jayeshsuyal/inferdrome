"""Scheduled-arrival client and frozen synthetic hotspot trace for PR2.

The trace and result artifacts contain metadata and digests, never prompts or
generated text. This is a measurement client, not a claim about GPU outcomes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import random
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp

from inferdrome.evaluation.stream import StreamError, StreamParser
from inferdrome.qwen3_campaign import qwen3_rendered_prompt
from inferdrome.qwen3_tokenizer import (
    QWEN3_TOKENIZER_CONFIG_SHA256,
    QWEN3_TOKENIZER_JSON_SHA256,
    QWEN3_TOKENIZERS_VERSION,
    load_verified_qwen3_tokenizer_files,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_affinity import ESCAPE_BUSY_DELTA, HISTORY_KEYS, HISTORY_TTL_NS
from inferdrome.vllm_request_identity import (
    REQUEST_ID_HEADER,
    _read_json,
    correlated_result,
    valid_request_id,
    validate_correlated_result,
)

POLICIES = ("round_robin", "least_busy", "cache_only", "cache_plus_load")
FOLLOWUP_POLICIES = ("round_robin", "cache_only", "cache_plus_load", "cache_saturation")
DOCUMENT_SENTENCE = (
    "This archived incident describes a request, its queue wait, a model "
    "response, and a measured completion time. "
)
SCHEMA = "inferdrome.vllm-router-study-plan.v1"
RESULT_SCHEMA = "inferdrome.vllm-router-study-result.v1"
CAPACITY_SCHEMA = "inferdrome.vllm-router-capacity-plan.v2"
CAPACITY_CERT_SCHEMA = "inferdrome.vllm-router-capacity-certificate.v2"
CAPACITY_DOCUMENT_SENTENCE = (
    "The archived event records a request identifier, intake time, queue state, "
    "operator note, intermediate observation, and final resolution. "
)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class Offer:
    index: int
    scheduled_ns: int
    epoch: int
    traffic_class: str
    tenant: str
    prompt: str
    document_id: int | None = None


def capacity_document(document_id: int, repeats: int) -> str:
    signature = hashlib.sha256(
        f"capacity-document-{document_id:04d}".encode()
    ).hexdigest()
    return (
        f"D{document_id:04d}-{signature[:16]} | archived event record\n"
        + CAPACITY_DOCUMENT_SENTENCE * repeats
    )


def capacity_trace(
    seed: int, count: int, duration_ns: int, workload: dict[str, Any]
) -> tuple[Offer, ...]:
    """Cycle all documents and shift a predeclared hotspot A→B→A."""
    document_count = workload["document_count"]
    repeats = workload["document_repeats"]
    prefix_lengths = workload["document_prefix_tokens"]
    target = workload["target_prefix_tokens"]
    hot_size = workload["hot_group_size"]
    pattern = workload.get("pattern_version")
    if pattern not in (None, "control.v1", "burst-hot-shift.v1"):
        raise ValueError("unknown capacity traffic pattern")
    burst = pattern == "burst-hot-shift.v1"
    cycle_percent = 20 if burst else 60
    if (
        not 0 <= seed < 2**32
        or not 1 <= count <= 20_000
        or duration_ns < 3
        or not 8 <= document_count <= 96
        or len(repeats) != document_count
        or len(prefix_lengths) != document_count
        or any(not isinstance(value, int) or not 1 <= value <= 512 for value in repeats)
        or not 256 <= target <= 6144
        or any(abs(value - target) > 32 for value in prefix_lengths)
        or not 1 <= hot_size <= document_count // 4
        or not 512 <= workload["context_length"] <= 16384
        or not 1 <= workload["output_tokens"] <= 256
        or max(prefix_lengths) + workload["output_tokens"] > workload["context_length"]
        or workload.get("cycle_percent") != cycle_percent
        or workload.get("hotspot_epochs") != ["A", "B", "A"]
        or (
            burst
            and (
                workload.get("burst_size") != 8
                or workload.get("burst_window_ns") != 200_000_000
                or hot_size != 2
            )
        )
        or (not burst and ("burst_size" in workload or "burst_window_ns" in workload))
    ):
        raise ValueError("capacity trace settings are outside bounds")
    rng = random.Random(seed)
    documents = tuple(
        capacity_document(document_id, repeats[document_id])
        for document_id in range(document_count)
    )
    offers: list[Offer] = []
    cycle = 0
    hot_cycle = 0
    for index in range(count):
        if burst:
            group = index // 8
            position = index % 8
            scheduled_ns = int(
                group * 8 * duration_ns / count
                + (position + rng.random()) * 200_000_000 / 8
            )
        else:
            scheduled_ns = int((index + rng.random()) * duration_ns / count)
        epoch = min(2, scheduled_ns * 3 // duration_ns)
        if rng.randrange(100) < cycle_percent:
            document_id = cycle % document_count
            cycle += 1
            traffic_class = "cycle"
        else:
            group = 0 if epoch != 1 else document_count // 2
            document_id = group + hot_cycle % hot_size
            hot_cycle += 1
            traffic_class = "hot"
        tenant = f"tenant-{document_id % 4}"
        document = documents[document_id]
        question = (
            f"\n\nQuestion:\nFor request {index:06d}, identify observation "
            f"{(index * 7 + seed) % 997:03d} and summarize the resolution."
        )
        offers.append(
            Offer(
                index,
                scheduled_ns,
                epoch,
                traffic_class,
                tenant,
                document + question,
                document_id,
            )
        )
    return tuple(offers)


def trace(seed: int, count: int, duration_ns: int) -> tuple[Offer, ...]:
    """70% current hotspot, 20% other shared document, 10% unique control."""
    if not 0 <= seed < 2**32 or not 1 <= count <= 10_000 or duration_ns < 3:
        raise ValueError("trace settings are outside bounds")
    rng = random.Random(seed)
    offers: list[Offer] = []
    for index in range(count):
        scheduled_ns = int((index + rng.random()) * duration_ns / count)
        epoch = min(2, scheduled_ns * 3 // duration_ns)
        hot = "a" if epoch != 1 else "b"
        other = "b" if hot == "a" else "a"
        roll = rng.random()
        if roll < 0.7:
            traffic_class, document_id, tenant = ("hot", f"{hot}00000", f"tenant-{hot}")
        elif roll < 0.9:
            traffic_class, document_id, tenant = (
                "warm",
                f"{other}00000",
                f"tenant-{other}",
            )
        else:
            traffic_class, document_id, tenant = (
                "unique",
                f"u{index:05d}",
                "tenant-control",
            )
        document = f"Record {document_id}: " + DOCUMENT_SENTENCE * 12
        prompt = document + f"\n\nQuestion:\nSummarize case {index:05d}."
        offers.append(Offer(index, scheduled_ns, epoch, traffic_class, tenant, prompt))
    return tuple(offers)


def trace_digest(offers: tuple[Offer, ...]) -> str:
    return _digest(
        [
            {
                "index": offer.index,
                "scheduled_ns": offer.scheduled_ns,
                "epoch": offer.epoch,
                "traffic_class": offer.traffic_class,
                "tenant": offer.tenant,
                "prompt_sha256": hashlib.sha256(offer.prompt.encode()).hexdigest(),
            }
            for offer in offers
        ]
    )


def _phase_trace(
    phase: str, seed: int, count: int, duration_ns: int
) -> tuple[Offer, ...]:
    # Identical user seeds cannot make calibration and evaluation traces equal.
    selected_seed = seed ^ 0xA5A5A5A5 if phase == "calibration" else seed
    return trace(selected_seed, count, duration_ns)


def make_plan(
    *,
    phase: Literal["calibration", "evaluation", "fixture"],
    seed: int,
    count: int,
    expected_prompt_tokens: int,
    duration_ns: int = 300_000_000_000,
    max_tokens: int = 128,
    first_content_slo_ns: int = 500_000_000,
    completion_slo_ns: int = 5_000_000_000,
) -> dict[str, Any]:
    if phase != "fixture" and duration_ns != 300_000_000_000:
        raise ValueError("measured windows must be five minutes")
    if (
        not 1 <= expected_prompt_tokens < 2048
        or not 1 <= max_tokens <= 256
        or expected_prompt_tokens + max_tokens > 2048
        or not 0 < first_content_slo_ns <= completion_slo_ns <= 60_000_000_000
    ):
        raise ValueError("study token or latency bounds are invalid")
    offers = _phase_trace(phase, seed, count, duration_ns)
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": phase,
        "seed": seed,
        "offered_count": count,
        "duration_ns": duration_ns,
        "trace_sha256": trace_digest(offers),
        "expected_prompt_tokens": expected_prompt_tokens,
        "max_tokens": max_tokens,
        "context_length": 2048,
        "ignore_eos": True,
        "stream_usage_required": True,
        "first_content_slo_ns": first_content_slo_ns,
        "completion_slo_ns": completion_slo_ns,
        "max_client_concurrency": 64,
        "request_deadline_ns": 60_000_000_000,
        "drain_ns": 60_000_000_000,
        "affinity": {
            "claim": "ESTIMATED_FROM_PRIOR_ROUTING_NOT_KV_RESIDENCY",
            "history_keys": HISTORY_KEYS,
            "ttl_ns": HISTORY_TTL_NS,
            "escape_busy_delta": ESCAPE_BUSY_DELTA,
        },
        "workload": "SYNTHETIC_CHANGING_HOTSPOT_A_B_A_70_20_10",
    }
    plan["plan_sha256"] = _digest(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> tuple[Offer, ...]:
    if plan.get("schema") == CAPACITY_SCHEMA:
        return validate_capacity_plan(plan)
    if set(plan) != set(
        make_plan(
            phase=plan["phase"],
            seed=plan["seed"],
            count=plan["offered_count"],
            expected_prompt_tokens=plan["expected_prompt_tokens"],
            duration_ns=plan["duration_ns"],
            max_tokens=plan["max_tokens"],
            first_content_slo_ns=plan["first_content_slo_ns"],
            completion_slo_ns=plan["completion_slo_ns"],
        )
    ):
        raise ValueError("study plan contains unsupported fields")
    expected = make_plan(
        phase=plan["phase"],
        seed=plan["seed"],
        count=plan["offered_count"],
        expected_prompt_tokens=plan["expected_prompt_tokens"],
        duration_ns=plan["duration_ns"],
        max_tokens=plan["max_tokens"],
        first_content_slo_ns=plan["first_content_slo_ns"],
        completion_slo_ns=plan["completion_slo_ns"],
    )
    if plan != expected:
        raise ValueError("study plan differs from its frozen deterministic recipe")
    return _phase_trace(
        plan["phase"], plan["seed"], plan["offered_count"], plan["duration_ns"]
    )


def verify_token_lengths(plan: dict[str, Any], tokenizer_root: Path) -> dict[str, Any]:
    """Require identical rendered Qwen3 input lengths before a measured run."""
    offers = validate_plan(plan)
    files = load_verified_qwen3_tokenizer_files(tokenizer_root)
    if importlib.metadata.version("tokenizers") != QWEN3_TOKENIZERS_VERSION:
        raise ValueError("pinned tokenizers version is unavailable")
    tokenizers = importlib.import_module("tokenizers")
    tokenizer = tokenizers.Tokenizer.from_str(files.tokenizer_json.decode("utf-8"))
    lengths = {
        len(
            tokenizer.encode(
                qwen3_rendered_prompt(offer.prompt), add_special_tokens=False
            ).ids
        )
        for offer in offers
    }
    if lengths != {plan["expected_prompt_tokens"]}:
        raise ValueError("trace prompt lengths differ from the frozen token count")
    certificate: dict[str, Any] = {
        "schema": "inferdrome.vllm-router-token-certificate.v1",
        "plan_sha256": plan["plan_sha256"],
        "trace_sha256": plan["trace_sha256"],
        "offered_count": len(offers),
        "prompt_tokens": plan["expected_prompt_tokens"],
        "max_tokens": plan["max_tokens"],
        "context_length": plan["context_length"],
        "tokenizers_version": QWEN3_TOKENIZERS_VERSION,
        "tokenizer_json_sha256": QWEN3_TOKENIZER_JSON_SHA256,
        "tokenizer_config_sha256": QWEN3_TOKENIZER_CONFIG_SHA256,
    }
    certificate["certificate_sha256"] = _digest(certificate)
    return certificate


def validate_token_certificate(
    plan: dict[str, Any], certificate: dict[str, Any]
) -> None:
    unsigned = {
        key: value for key, value in certificate.items() if key != "certificate_sha256"
    }
    if (
        set(unsigned)
        != {
            "schema",
            "plan_sha256",
            "trace_sha256",
            "offered_count",
            "prompt_tokens",
            "max_tokens",
            "context_length",
            "tokenizers_version",
            "tokenizer_json_sha256",
            "tokenizer_config_sha256",
        }
        or certificate.get("certificate_sha256") != _digest(unsigned)
        or unsigned["schema"] != "inferdrome.vllm-router-token-certificate.v1"
        or unsigned["plan_sha256"] != plan["plan_sha256"]
        or unsigned["trace_sha256"] != plan["trace_sha256"]
        or unsigned["offered_count"] != plan["offered_count"]
        or unsigned["prompt_tokens"] != plan["expected_prompt_tokens"]
        or unsigned["max_tokens"] != plan["max_tokens"]
        or unsigned["context_length"] != plan["context_length"]
        or unsigned["tokenizers_version"] != QWEN3_TOKENIZERS_VERSION
        or unsigned["tokenizer_json_sha256"] != QWEN3_TOKENIZER_JSON_SHA256
        or unsigned["tokenizer_config_sha256"] != QWEN3_TOKENIZER_CONFIG_SHA256
    ):
        raise ValueError("token certificate does not bind the study plan")


def _capacity_tokenizer(tokenizer_root: Path) -> Any:
    files = load_verified_qwen3_tokenizer_files(tokenizer_root)
    if importlib.metadata.version("tokenizers") != QWEN3_TOKENIZERS_VERSION:
        raise ValueError("pinned tokenizers version is unavailable")
    tokenizers = importlib.import_module("tokenizers")
    return tokenizers.Tokenizer.from_str(files.tokenizer_json.decode("utf-8"))


def capacity_workload(
    tokenizer_root: Path,
    *,
    document_count: int = 48,
    target_prefix_tokens: int = 4096,
    context_length: int = 8192,
    output_tokens: int = 128,
) -> dict[str, Any]:
    """Choose deterministic repeat counts nearest the requested prefix size."""
    if (
        not 8 <= document_count <= 96
        or not 256 <= target_prefix_tokens <= 6144
        or not 512 <= context_length <= 16384
        or not 1 <= output_tokens <= 256
        or target_prefix_tokens + output_tokens > context_length
    ):
        raise ValueError("capacity workload bounds are invalid")
    tokenizer = _capacity_tokenizer(tokenizer_root)
    repeats: list[int] = []
    prefix_lengths: list[int] = []
    first_tokens: set[tuple[int, ...]] = set()
    for document_id in range(document_count):

        def length(repeat: int, document_id: int = document_id) -> int:
            return len(
                tokenizer.encode(
                    capacity_document(document_id, repeat), add_special_tokens=False
                ).ids
            )

        low, high = 1, 512
        while low < high:
            middle = (low + high) // 2
            if length(middle) < target_prefix_tokens:
                low = middle + 1
            else:
                high = middle
        selected = min(
            (max(1, low - 1), low),
            key=lambda repeat: (abs(length(repeat) - target_prefix_tokens), repeat),
        )
        encoded = tokenizer.encode(
            capacity_document(document_id, selected), add_special_tokens=False
        ).ids
        repeats.append(selected)
        prefix_lengths.append(len(encoded))
        first_tokens.add(tuple(encoded[:16]))
    if (
        max(abs(value - target_prefix_tokens) for value in prefix_lengths) > 32
        or len(first_tokens) != document_count
    ):
        raise ValueError("document prefixes missed target or do not diverge early")
    return {
        "document_count": document_count,
        "target_prefix_tokens": target_prefix_tokens,
        "document_repeats": repeats,
        "document_prefix_tokens": prefix_lengths,
        "hot_group_size": max(1, document_count // 6),
        "cycle_percent": 60,
        "hotspot_epochs": ["A", "B", "A"],
        "context_length": context_length,
        "output_tokens": output_tokens,
    }


def make_capacity_plan(
    *,
    phase: str,
    seed: int,
    count: int,
    duration_ns: int,
    workload: dict[str, Any],
    prompt_tokens_by_index: list[int],
    max_client_concurrency: int = 256,
    first_content_slo_ns: int = 500_000_000,
    completion_slo_ns: int = 5_000_000_000,
) -> dict[str, Any]:
    if phase not in {"pilot", "evaluation", "fixture"}:
        raise ValueError("capacity phase is invalid")
    if (
        not 1 <= max_client_concurrency <= 1024
        or not 0 < first_content_slo_ns <= completion_slo_ns <= 60_000_000_000
        or len(prompt_tokens_by_index) != count
        or any(
            not isinstance(value, int)
            or value < 1
            or value + workload["output_tokens"] > workload["context_length"]
            for value in prompt_tokens_by_index
        )
    ):
        raise ValueError("capacity plan bounds are invalid")
    offers = capacity_trace(seed, count, duration_ns, workload)
    trace_rows = [
        {
            "index": offer.index,
            "scheduled_ns": offer.scheduled_ns,
            "epoch": offer.epoch,
            "traffic_class": offer.traffic_class,
            "document_id": offer.document_id,
            "prompt_sha256": hashlib.sha256(offer.prompt.encode()).hexdigest(),
        }
        for offer in offers
    ]
    plan: dict[str, Any] = {
        "schema": CAPACITY_SCHEMA,
        "phase": phase,
        "seed": seed,
        "offered_count": count,
        "duration_ns": duration_ns,
        "trace_sha256": _digest(trace_rows),
        "workload": workload,
        "prompt_tokens_by_index": prompt_tokens_by_index,
        "max_tokens": workload["output_tokens"],
        "context_length": workload["context_length"],
        "max_client_concurrency": max_client_concurrency,
        "request_deadline_ns": 60_000_000_000,
        "drain_ns": 60_000_000_000,
        "first_content_slo_ns": first_content_slo_ns,
        "completion_slo_ns": completion_slo_ns,
        "affinity": {
            "claim": "ESTIMATED_FROM_PRIOR_ROUTING_NOT_KV_RESIDENCY",
            "history_keys": HISTORY_KEYS,
            "ttl_ns": HISTORY_TTL_NS,
            "escape_busy_delta": ESCAPE_BUSY_DELTA,
        },
    }
    plan["plan_sha256"] = _digest(plan)
    return plan


def certify_capacity_plan(plan: dict[str, Any], tokenizer_root: Path) -> dict[str, Any]:
    offers = validate_capacity_plan(plan)
    tokenizer = _capacity_tokenizer(tokenizer_root)
    lengths = [
        len(
            tokenizer.encode(
                qwen3_rendered_prompt(offer.prompt), add_special_tokens=False
            ).ids
        )
        for offer in offers
    ]
    if lengths != plan["prompt_tokens_by_index"]:
        raise ValueError("capacity prompt lengths differ from plan")
    certificate: dict[str, Any] = {
        "schema": CAPACITY_CERT_SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "trace_sha256": plan["trace_sha256"],
        "offered_count": len(offers),
        "prompt_lengths_sha256": _digest(lengths),
        "prompt_tokens_min": min(lengths),
        "prompt_tokens_max": max(lengths),
        "max_tokens": plan["max_tokens"],
        "context_length": plan["context_length"],
        "tokenizers_version": QWEN3_TOKENIZERS_VERSION,
        "tokenizer_json_sha256": QWEN3_TOKENIZER_JSON_SHA256,
        "tokenizer_config_sha256": QWEN3_TOKENIZER_CONFIG_SHA256,
    }
    certificate["certificate_sha256"] = _digest(certificate)
    return certificate


def validate_capacity_plan(plan: dict[str, Any]) -> tuple[Offer, ...]:
    expected = make_capacity_plan(
        phase=plan["phase"],
        seed=plan["seed"],
        count=plan["offered_count"],
        duration_ns=plan["duration_ns"],
        workload=plan["workload"],
        prompt_tokens_by_index=plan["prompt_tokens_by_index"],
        max_client_concurrency=plan["max_client_concurrency"],
        first_content_slo_ns=plan["first_content_slo_ns"],
        completion_slo_ns=plan["completion_slo_ns"],
    )
    if plan != expected:
        raise ValueError("capacity plan differs from frozen deterministic recipe")
    return capacity_trace(
        plan["seed"], plan["offered_count"], plan["duration_ns"], plan["workload"]
    )


def validate_capacity_certificate(
    plan: dict[str, Any], certificate: dict[str, Any]
) -> None:
    unsigned = {
        key: value for key, value in certificate.items() if key != "certificate_sha256"
    }
    if certificate.get("certificate_sha256") != _digest(unsigned) or unsigned != {
        "schema": CAPACITY_CERT_SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "trace_sha256": plan["trace_sha256"],
        "offered_count": plan["offered_count"],
        "prompt_lengths_sha256": _digest(plan["prompt_tokens_by_index"]),
        "prompt_tokens_min": min(plan["prompt_tokens_by_index"]),
        "prompt_tokens_max": max(plan["prompt_tokens_by_index"]),
        "max_tokens": plan["max_tokens"],
        "context_length": plan["context_length"],
        "tokenizers_version": QWEN3_TOKENIZERS_VERSION,
        "tokenizer_json_sha256": QWEN3_TOKENIZER_JSON_SHA256,
        "tokenizer_config_sha256": QWEN3_TOKENIZER_CONFIG_SHA256,
    }:
        raise ValueError("capacity certificate does not bind the study plan")


@dataclass(frozen=True)
class RequestResult:
    index: int
    scheduled_ns: int
    epoch: int
    traffic_class: str
    tenant: str
    dispatch_ns: int | None
    response_headers_ns: int | None
    first_body_byte_ns: int | None
    first_content_ns: int | None
    terminal_ns: int
    max_content_gap_ns: int | None
    outcome: str
    http_status: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    ready_ns: int | None = None
    document_id: int | None = None


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("study router origin must be literal loopback HTTP")
    return value.rstrip("/")


async def run_trial(
    plan: dict[str, Any],
    *,
    router_origin: str,
    model: str,
    expected_policy: str,
    token_certificate: dict[str, Any] | None = None,
    correlate_requests: bool = False,
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one condition, optionally wrapping its legacy measurement with ID links."""
    if timing is not None:
        # Imported here because the timing recipe reuses the frozen plan reader.
        from inferdrome.vllm_arrival_timing import validate_timing

        if correlate_requests is not True:
            raise ValueError("timed studies require request correlation")
        offers = validate_timing(plan, timing)
    else:
        offers = validate_plan(plan)
    capacity = plan["schema"] == CAPACITY_SCHEMA
    if plan["phase"] != "fixture" or (
        timing is not None and token_certificate is not None
    ):
        if token_certificate is None:
            raise ValueError("measured runs require verified prompt lengths")
        if capacity:
            validate_capacity_certificate(plan, token_certificate)
        else:
            validate_token_certificate(plan, token_certificate)
    origin = _origin(router_origin)
    if expected_policy not in (*POLICIES, "cache_saturation"):
        raise ValueError("study policy is unsupported")
    if not model or len(model) > 200:
        raise ValueError("model identifier is invalid")
    semaphore = asyncio.Semaphore(plan["max_client_concurrency"])
    results: list[RequestResult | None] = [None] * len(offers)
    # Allocate before tasks start so even offers cancelled before dispatch keep
    # their own identity. This is a per-trial join key, not a routing input.
    request_links: list[dict[str, Any]] = (
        [
            {
                "index": offer.index,
                "request_id": uuid.uuid4().hex,
                "response_request_id": None,
            }
            for offer in offers
        ]
        if correlate_requests
        else []
    )
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=plan["max_client_concurrency"]),
        timeout=timeout,
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        headers={"Accept-Encoding": "identity"},
    ) as session:
        async with session.get(
            origin + "/router/stats", timeout=aiohttp.ClientTimeout(total=5)
        ) as response:
            before = await response.json()
        if (
            not isinstance(before, dict)
            or before.get("policy") != expected_policy
            or before.get("offered") != 0
            or before.get("terminal") != 0
            or before.get("ledger_rows") != 0
            or before.get("initial_ledger_bytes") != 0
            or before.get("accounting_failed") is not False
        ):
            raise ValueError("study requires a fresh router with the expected policy")
        start_ns = time.monotonic_ns()
        started_unix_ns = str(time.time_ns())

        async def one(offer: Offer) -> None:
            scheduled_at = start_ns + offer.scheduled_ns
            dispatch_ns: int | None = None
            response_headers_ns: int | None = None
            first_body_byte_ns: int | None = None
            first_content_ns: int | None = None
            gap_ns: int | None = None
            status: int | None = None
            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            ready_ns: int | None = None
            outcome = "error"
            parser = StreamParser(
                max_stream_bytes=1_048_576,
                max_event_bytes=65_536,
                max_content_events=4096,
            )
            try:
                delay = (scheduled_at - time.monotonic_ns()) / 1e9
                if delay > 0:
                    await asyncio.sleep(delay)
                ready_ns = time.monotonic_ns() - start_ns
                deadline = (scheduled_at + plan["request_deadline_ns"]) / 1e9
                async with asyncio.timeout_at(deadline):
                    async with semaphore:
                        dispatch_ns = time.monotonic_ns() - start_ns
                        body = {
                            "model": model,
                            "messages": [{"role": "user", "content": offer.prompt}],
                            "max_tokens": plan["max_tokens"],
                            "temperature": 0,
                            "n": 1,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                            "ignore_eos": True,
                            "chat_template_kwargs": {"enable_thinking": False},
                        }
                        async with session.post(
                            origin + "/v1/chat/completions",
                            json=body,
                            headers={
                                REQUEST_ID_HEADER: request_links[offer.index][
                                    "request_id"
                                ]
                            }
                            if correlate_requests
                            else None,
                            allow_redirects=False,
                        ) as response:
                            status = response.status
                            response_headers_ns = time.monotonic_ns() - start_ns
                            if correlate_requests:
                                echoes = response.headers.getall(REQUEST_ID_HEADER, [])
                                # Invalid or duplicated values are never copied into
                                # artifacts. Observed headers plus a null echo fail
                                # identity verification without storing raw input.
                                if len(echoes) == 1 and valid_request_id(echoes[0]):
                                    request_links[offer.index][
                                        "response_request_id"
                                    ] = echoes[0]
                            if status != 200:
                                outcome = "rejected" if status == 503 else "http_error"
                            elif response.content_type != "text/event-stream":
                                outcome = "protocol_error"
                            else:
                                async for chunk in response.content.iter_chunked(
                                    16_384
                                ):
                                    parser.feed(chunk, time.monotonic_ns() - start_ns)
                                parser.finish()
                                first_body_byte_ns = parser.first_body_byte_ns
                                first_content_ns = parser.first_content_ns
                                times = parser.content_event_times_ns
                                gap_ns = max(
                                    (b - a for a, b in pairwise(times)),
                                    default=0,
                                )
                                prompt_tokens = parser.prompt_tokens
                                completion_tokens = parser.completion_tokens
                                expected_prompt_tokens = (
                                    plan["prompt_tokens_by_index"][offer.index]
                                    if capacity
                                    else plan["expected_prompt_tokens"]
                                )
                                if prompt_tokens != expected_prompt_tokens:
                                    outcome = "prompt_length_mismatch"
                                elif completion_tokens != plan["max_tokens"]:
                                    outcome = "output_length_mismatch"
                                else:
                                    outcome = "completed"
            except TimeoutError:
                outcome = "timeout"
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except (aiohttp.ClientError, OSError):
                outcome = "transport_error"
            except StreamError:
                outcome = "protocol_error"
            finally:
                first_body_byte_ns = parser.first_body_byte_ns
                first_content_ns = parser.first_content_ns
                gap_ns = (
                    max(
                        (b - a for a, b in pairwise(parser.content_event_times_ns)),
                        default=0,
                    )
                    if parser.content_event_times_ns
                    else None
                )
                prompt_tokens = parser.prompt_tokens
                completion_tokens = parser.completion_tokens
                results[offer.index] = RequestResult(
                    offer.index,
                    offer.scheduled_ns,
                    offer.epoch,
                    offer.traffic_class,
                    offer.tenant,
                    dispatch_ns,
                    response_headers_ns,
                    first_body_byte_ns,
                    first_content_ns,
                    time.monotonic_ns() - start_ns,
                    gap_ns,
                    outcome,
                    status,
                    prompt_tokens,
                    completion_tokens,
                    ready_ns,
                    offer.document_id,
                )

        tasks = [
            asyncio.create_task(one(offer), name=f"inferdrome-offer-{offer.index}")
            for offer in offers
        ]
        interrupted = False
        drain_exceeded = False
        try:
            _, pending = await asyncio.wait(
                tasks, timeout=(plan["duration_ns"] + plan["drain_ns"]) / 1e9
            )
            drain_exceeded = bool(pending)
        except asyncio.CancelledError:
            interrupted = True
        finally:
            # Includes offers still sleeping until their scheduled arrival.
            # Await every child before the session can close or a result is saved.
            for task in tasks:
                if not task.done():
                    task.cancel()
            settlement = asyncio.gather(*tasks, return_exceptions=True)
            while not settlement.done():
                try:
                    await asyncio.shield(settlement)
                except asyncio.CancelledError:
                    interrupted = True
                    for task in tasks:
                        if not task.done():
                            task.cancel()
            settlement.result()
        for offer in offers:
            if results[offer.index] is None:
                results[offer.index] = RequestResult(
                    offer.index,
                    offer.scheduled_ns,
                    offer.epoch,
                    offer.traffic_class,
                    offer.tenant,
                    None,
                    None,
                    None,
                    None,
                    time.monotonic_ns() - start_ns,
                    None,
                    "cancelled",
                    None,
                    None,
                    None,
                    None,
                    offer.document_id,
                )
        rows = [row for row in results if row is not None]
        summary = summarize(plan, rows, include_client_timing=timing is not None)
        status = (
            "INTERRUPTED"
            if interrupted
            else "DRAIN_TIMEOUT"
            if drain_exceeded
            else "COMPLETED"
        )
        after: dict[str, Any] = {"error": "router stats unavailable after trial"}
        settlement_deadline = asyncio.get_running_loop().time() + 0.5
        while True:
            try:
                async with session.get(
                    origin + "/router/stats", timeout=aiohttp.ClientTimeout(total=0.25)
                ) as response:
                    candidate = await response.json()
                if isinstance(candidate, dict):
                    after = candidate
            except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
                break
            if (
                after.get("in_flight") == 0
                and after.get("terminal") == after.get("offered")
            ) or asyncio.get_running_loop().time() >= settlement_deadline:
                break
            await asyncio.sleep(0.05)
        accounting_valid = isinstance(after, dict) and (
            after.get("policy") == expected_policy
            and after.get("offered") == len(offers)
            and after.get("terminal") == len(offers)
            and after.get("ledger_rows") == len(offers)
            and after.get("in_flight") == 0
            and after.get("pending") == 0
            and after.get("active") == 0
            and after.get("busy") == [0, 0]
            and after.get("accounting_failed") is False
        )
        result = {
            "schema": (
                "inferdrome.vllm-router-capacity-result.v2"
                if capacity
                else RESULT_SCHEMA
            ),
            "plan_sha256": plan["plan_sha256"],
            "trace_sha256": plan["trace_sha256"],
            "token_certificate_sha256": (
                token_certificate["certificate_sha256"]
                if token_certificate is not None
                else None
            ),
            "policy": expected_policy,
            "model": model,
            "router_origin": origin,
            "started_unix_ns": started_unix_ns,
            "evidence_class": "SYNTHETIC_ONLY"
            if plan["phase"] == "fixture"
            else "LOCAL_MEASUREMENT_ONLY",
            "router_accounting_valid": accounting_valid,
            "status": status,
            "comparison_valid": status == "COMPLETED"
            and accounting_valid
            and not any(
                row.outcome in {"prompt_length_mismatch", "output_length_mismatch"}
                for row in rows
            ),
            "router_stats_after": after,
            "rows": [
                asdict(row)
                if capacity or timing is not None
                else {
                    key: value
                    for key, value in asdict(row).items()
                    if key not in {"ready_ns", "document_id"}
                }
                for row in rows
            ],
            "summary": summary,
        }
        if timing is not None:
            from inferdrome.vllm_arrival_timing import TIMED_RESULT_SCHEMA

            result.update(
                schema=TIMED_RESULT_SCHEMA,
                plan_sha256=timing["timing_sha256"],
                trace_sha256=timing["transformed_trace_sha256"],
                base_plan_schema=plan["schema"],
                base_plan_sha256=plan["plan_sha256"],
                base_trace_sha256=plan["trace_sha256"],
                token_certificate_scope="BASE_WORKLOAD_UNCHANGED_TIMING_ONLY",
                arrival_timing_scope="PLANNED_OFFERS_NOT_OBSERVED_ARRIVALS",
            )
        result["result_sha256"] = _digest(result)
        return (
            correlated_result(result, request_links) if correlate_requests else result
        )


def _quantile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))]


def summarize(
    plan: dict[str, Any],
    rows: list[RequestResult],
    *,
    include_client_timing: bool = False,
) -> dict[str, Any]:
    if len(rows) != plan["offered_count"] or {r.index for r in rows} != set(
        range(len(rows))
    ):
        raise ValueError("incomplete offered population")
    window_ns = int(plan["duration_ns"])
    capacity = plan["schema"] == CAPACITY_SCHEMA

    def metric(values: list[int]) -> dict[str, int | None]:
        return {"count": len(values), "p95_ns": _quantile(values, 0.95)}

    def group(
        part: list[RequestResult], denominator_ns: int, scope: str
    ) -> dict[str, Any]:
        completed = [r for r in part if r.outcome == "completed"]
        good = [
            r
            for r in completed
            if r.first_content_ns is not None
            and r.first_content_ns - r.scheduled_ns <= plan["first_content_slo_ns"]
            and r.terminal_ns - r.scheduled_ns <= plan["completion_slo_ns"]
        ]
        return {
            "offered": len(part),
            "outcomes": dict(Counter(r.outcome for r in part)),
            "slo_good": len(good),
            "slo_goodput_rps": len(good) * 1e9 / denominator_ns,
            "goodput_denominator_ns": denominator_ns,
            "goodput_scope": scope,
            "successful_only": {
                "population": "COMPLETED_REQUESTS_ONLY",
                "completed_count": len(completed),
                "scheduled_to_first_content": metric(
                    [
                        r.first_content_ns - r.scheduled_ns
                        for r in completed
                        if r.first_content_ns is not None
                    ]
                ),
                "scheduled_to_terminal": metric(
                    [r.terminal_ns - r.scheduled_ns for r in completed]
                ),
                "max_content_gap": metric(
                    [
                        r.max_content_gap_ns
                        for r in completed
                        if r.max_content_gap_ns is not None
                    ]
                ),
            },
            "completion_tokens_total": sum(r.completion_tokens or 0 for r in completed),
        }

    def epoch_window_ns(epoch: int) -> int:
        # trace() assigns epoch=floor(3*scheduled_ns/duration_ns).
        start = (epoch * window_ns + 2) // 3
        end = ((epoch + 1) * window_ns + 2) // 3
        return end - start

    result = {
        "all_offered": group(rows, window_ns, "FULL_OFFERED_WINDOW"),
        "by_epoch": {
            str(e): group(
                [r for r in rows if r.epoch == e],
                epoch_window_ns(e),
                "EPOCH_OFFERED_WINDOW",
            )
            for e in range(3)
        },
        "by_class": {
            c: group(
                [r for r in rows if r.traffic_class == c],
                window_ns,
                "FULL_OFFERED_WINDOW_CONTRIBUTION",
            )
            for c in (("hot", "cycle") if capacity else ("hot", "warm", "unique"))
        },
        "by_tenant": {
            t: group(
                [r for r in rows if r.tenant == t],
                window_ns,
                "FULL_OFFERED_WINDOW_CONTRIBUTION",
            )
            for t in (
                tuple(f"tenant-{i}" for i in range(4))
                if capacity
                else ("tenant-a", "tenant-b", "tenant-control")
            )
        },
        "latency_origin": "SCHEDULED_ARRIVAL_INCLUDES_CLIENT_AND_ROUTER_WAIT",
        "content_timing": "COMPLETE_SSE_CONTENT_FRAMES_NOT_WIRE_OR_TOKEN_TIMING",
    }
    if capacity or include_client_timing:
        lag = [
            max(0, row.ready_ns - row.scheduled_ns)
            for row in rows
            if row.ready_ns is not None
        ]
        client_queue = [
            max(0, row.dispatch_ns - row.ready_ns)
            for row in rows
            if row.dispatch_ns is not None and row.ready_ns is not None
        ]
        result["client_timing"] = {
            "scheduling_lag": metric(lag),
            "client_queue_delay": metric(client_queue),
            "never_dispatched": sum(row.dispatch_ns is None for row in rows),
            "population": "ALL_OFFERS_WITH_OBSERVED_STAGE_TIMES",
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic vLLM router study")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument(
        "--phase", choices=("calibration", "evaluation"), required=True
    )
    prepare.add_argument("--seed", type=int, required=True)
    prepare.add_argument("--offers", type=int, required=True)
    prepare.add_argument("--expected-prompt-tokens", type=int, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-tokens")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--tokenizer-root", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--router", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--policy", choices=POLICIES, required=True)
    run.add_argument("--token-certificate", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--timing",
        type=Path,
        help="verified arrival-timing artifact; requires correlation",
    )
    run.add_argument(
        "--correlate-requests",
        action="store_true",
        help="write a versioned wrapper with request IDs for offline ledger joins",
    )
    args = parser.parse_args()
    if (
        args.command == "run"
        and args.timing is not None
        and not args.correlate_requests
    ):
        parser.error("--timing requires --correlate-requests")
    # Reserve the no-replace destination before any request can be dispatched.
    with args.output.open("x", encoding="utf-8") as stream:
        if args.command == "prepare":
            content = make_plan(
                phase=args.phase,
                seed=args.seed,
                count=args.offers,
                expected_prompt_tokens=args.expected_prompt_tokens,
            )
        elif args.command == "verify-tokens":
            plan = json.loads(args.plan.read_text())
            content = verify_token_lengths(plan, args.tokenizer_root)
        else:
            plan = (
                json.loads(args.plan.read_text())
                if args.timing is None
                else _read_json(args.plan.read_bytes())
            )
            certificate = (
                json.loads(args.token_certificate.read_text())
                if args.timing is None
                else _read_json(args.token_certificate.read_bytes())
            )
            arrival_timing = (
                _read_json(args.timing.read_bytes())
                if args.timing is not None
                else None
            )
            if args.timing is not None and not isinstance(arrival_timing, dict):
                raise ValueError("timing descriptor must be an object")
            content = asyncio.run(
                run_trial(
                    plan,
                    router_origin=args.router,
                    model=args.model,
                    expected_policy=args.policy,
                    token_certificate=certificate,
                    correlate_requests=args.correlate_requests,
                    timing=arrival_timing,
                )
            )
        json.dump(content, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    if args.command == "run":
        measurement = content
        if args.correlate_requests:
            measurement = content["measurement"]
            try:
                validate_correlated_result(content)
            except ValueError as error:
                # Retain the failed identity observation for inspection.
                parser.exit(2, f"request identity incomplete: {error}\n")
        if args.timing is not None:
            from inferdrome.vllm_timed_result import verify_timed_result

            assert isinstance(arrival_timing, dict)
            try:
                verify_timed_result(
                    plan, arrival_timing, content, token_certificate=certificate
                )
            except ValueError as error:
                parser.exit(2, f"timed result invalid: {error}\n")
        if not measurement["comparison_valid"]:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
