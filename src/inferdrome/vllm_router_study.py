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

POLICIES = ("round_robin", "least_busy", "cache_only", "cache_plus_load")
DOCUMENT_SENTENCE = (
    "This archived incident describes a request, its queue wait, a model "
    "response, and a measured completion time. "
)
SCHEMA = "inferdrome.vllm-router-study-plan.v1"
RESULT_SCHEMA = "inferdrome.vllm-router-study-result.v1"


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
) -> dict[str, Any]:
    """Run one policy condition; caller resets and warms replicas separately."""
    offers = validate_plan(plan)
    if plan["phase"] != "fixture":
        if token_certificate is None:
            raise ValueError("measured runs require verified prompt lengths")
        validate_token_certificate(plan, token_certificate)
    origin = _origin(router_origin)
    if expected_policy not in POLICIES:
        raise ValueError("study policy is unsupported")
    if not model or len(model) > 200:
        raise ValueError("model identifier is invalid")
    semaphore = asyncio.Semaphore(plan["max_client_concurrency"])
    results: list[RequestResult | None] = [None] * len(offers)
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
                            allow_redirects=False,
                        ) as response:
                            status = response.status
                            response_headers_ns = time.monotonic_ns() - start_ns
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
                                if prompt_tokens != plan["expected_prompt_tokens"]:
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
                )
        rows = [row for row in results if row is not None]
        summary = summarize(plan, rows)
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
            "schema": RESULT_SCHEMA,
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
            "rows": [asdict(row) for row in rows],
            "summary": summary,
        }
        result["result_sha256"] = _digest(result)
        return result


def _quantile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))]


def summarize(plan: dict[str, Any], rows: list[RequestResult]) -> dict[str, Any]:
    if len(rows) != plan["offered_count"] or {r.index for r in rows} != set(
        range(len(rows))
    ):
        raise ValueError("incomplete offered population")
    window_ns = int(plan["duration_ns"])

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

    return {
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
            for c in ("hot", "warm", "unique")
        },
        "by_tenant": {
            t: group(
                [r for r in rows if r.tenant == t],
                window_ns,
                "FULL_OFFERED_WINDOW_CONTRIBUTION",
            )
            for t in ("tenant-a", "tenant-b", "tenant-control")
        },
        "latency_origin": "SCHEDULED_ARRIVAL_INCLUDES_CLIENT_AND_ROUTER_WAIT",
        "content_timing": "COMPLETE_SSE_CONTENT_FRAMES_NOT_WIRE_OR_TOKEN_TIMING",
    }


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
    args = parser.parse_args()
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
            plan = json.loads(args.plan.read_text())
            certificate = json.loads(args.token_certificate.read_text())
            content = asyncio.run(
                run_trial(
                    plan,
                    router_origin=args.router,
                    model=args.model,
                    expected_policy=args.policy,
                    token_certificate=certificate,
                )
            )
        json.dump(content, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    if args.command == "run" and not content["comparison_valid"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
