"""Paired comparisons over masks of original compression groups.

Only the planned timing intervention is reduced. Request populations and the
four-cell comparison design stay fixed; runtime cache and queue states are not
claimed to be equivalent.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from inferdrome.routing_execution.canonical import canonical_json_bytes
from inferdrome.vllm_arrival_timing import _artifact_json_types, make_timing
from inferdrome.vllm_paired_protocol import _PARAMETER_FIELDS, make_protocol
from inferdrome.vllm_reduced_timing import make_reduced_timing, original_groups
from inferdrome.vllm_request_identity import _read_json

SCHEMA = "inferdrome.vllm-router-reduced-protocol.v1"
REDUCTION_SCOPE = "WHOLE_ORIGINAL_EPOCH_GROUPS_TIMING_ONLY"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _active_groups(
    plan: dict[str, Any], source_parameters: dict[str, Any]
) -> list[str]:
    return sorted(
        group["group_id"]
        for group in original_groups(plan, source_parameters)
        if group["moved_count"] > 0
    )


def group_universe(
    plans: list[dict[str, Any]], source_parameters: dict[str, Any]
) -> list[str]:
    """Return the shared mask domain, derived from unchanged original groups."""
    if not isinstance(plans, list) or not 4 <= len(plans) <= 32 or len(plans) % 4:
        raise ValueError("reduced comparison requires four to thirty-two blocks")
    return sorted(
        {
            group_id
            for plan in plans
            for group_id in _active_groups(plan, source_parameters)
        }
    )


def _check_mask(retained_groups: object, universe: list[str]) -> list[str]:
    if (
        not isinstance(retained_groups, list)
        or not retained_groups
        or any(not isinstance(value, str) for value in retained_groups)
        or retained_groups != sorted(set(retained_groups))
        or not set(retained_groups).issubset(universe)
    ):
        raise ValueError(
            "retained groups must be a nonempty sorted unique active subset"
        )
    return list(retained_groups)


def _timings(
    plans: list[dict[str, Any]],
    source_parameters: dict[str, Any],
    retained_groups: list[str],
) -> list[dict[str, dict[str, Any]]]:
    timings = []
    retained = set(retained_groups)
    for plan in plans:
        local = sorted(retained.intersection(_active_groups(plan, source_parameters)))
        candidate = make_reduced_timing(
            plan, source_parameters=source_parameters, retained_groups=local
        )
        if candidate["moved_count"] == 0:
            raise ValueError("reduced candidate must move offers in every block")
        timings.append(
            {
                "baseline": make_timing(
                    plan,
                    group_size=1,
                    retained_spacing_bps=10_000,
                    max_advance_ns=0,
                ),
                "candidate": candidate,
            }
        )
    return timings


def make_reduced_protocol(
    plans: list[dict[str, Any]],
    *,
    source_parameters: dict[str, Any],
    retained_groups: list[str],
    **comparison_options: Any,
) -> dict[str, Any]:
    """Freeze a shared original-group mask and regenerate all paired trial pins."""
    try:
        source = make_protocol(
            plans, candidate_parameters=source_parameters, **comparison_options
        )
        retained = _check_mask(
            retained_groups, group_universe(plans, source_parameters)
        )
        timings = _timings(plans, source_parameters, retained)
        protocol = {
            **source,
            "schema": SCHEMA,
            "retained_groups": retained,
            "reduction_scope": REDUCTION_SCOPE,
            "trials": [
                {
                    **trial,
                    "timing_sha256": timings[trial["block"] - 1][trial["condition"]][
                        "timing_sha256"
                    ],
                }
                for trial in source["trials"]
            ],
        }
        protocol["protocol_sha256"] = _digest(
            {key: value for key, value in protocol.items() if key != "protocol_sha256"}
        )
        return protocol
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed reduced protocol input") from error


def validate_reduced_protocol(
    protocol: dict[str, Any], plans: list[dict[str, Any]]
) -> None:
    """Regenerate the entire source design, shared mask and resulting trial pins."""
    try:
        if not isinstance(protocol, dict):
            raise ValueError("reduced protocol must be an object")
        _artifact_json_types(protocol)
        expected = make_reduced_protocol(
            plans,
            source_parameters=protocol["candidate_parameters"],
            retained_groups=protocol["retained_groups"],
            **{
                key: protocol[key]
                for key in _PARAMETER_FIELDS - {"candidate_parameters"}
            },
        )
        if canonical_json_bytes(protocol) != canonical_json_bytes(expected):
            raise ValueError("reduced protocol differs from its deterministic recipe")
    except (
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("malformed reduced protocol") from error


def reduced_timings(
    plans: list[dict[str, Any]], protocol: dict[str, Any]
) -> list[dict[str, dict[str, Any]]]:
    """Return validated baseline/candidate timing artifacts in block order."""
    validate_reduced_protocol(protocol, plans)
    return _timings(
        plans, protocol["candidate_parameters"], protocol["retained_groups"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize reduced paired protocols and timing artifacts offline"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--protocol", type=Path, required=True)
    materialize.add_argument("--plans", type=Path, nargs="+", required=True)
    materialize.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    protocol = _read_json(args.protocol.read_bytes())
    plans = [_read_json(path.read_bytes()) for path in args.plans]
    timings = reduced_timings(plans, protocol)
    artifacts = {"protocol.json": protocol}
    for block, pair in enumerate(timings, start=1):
        for condition, timing in pair.items():
            artifacts[f"b{block:02d}-{condition}-timing.json"] = timing
    # Check and serialize every artifact before reserving any destination.
    encoded = {
        name: canonical_json_bytes(value) + b"\n" for name, value in artifacts.items()
    }
    args.output_dir.mkdir(mode=0o700)
    for name, content in encoded.items():
        with (args.output_dir / name).open("xb") as stream:
            stream.write(content)


if __name__ == "__main__":
    main()
