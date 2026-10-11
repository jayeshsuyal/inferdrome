"""One offline command surface for the verified Breakpoint artifact workflow."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferdrome breakpoint",
        description=(
            "Prepare, search, reduce, confirm and summarize timing evidence offline. "
            "Use COMMAND --help for its artifact inputs and actions."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("study", "plan and prepare a bounded study without executing trials"),
        ("search", "prepare and replay a bounded timing search"),
        ("reduce", "reduce a verified search candidate"),
        ("confirm", "freeze and evaluate one held-out confirmation batch"),
        ("summarize", "regenerate a saved report and write a concise Markdown view"),
        ("demo", "create a complete SYNTHETIC_ONLY walkthrough locally"),
    ):
        commands.add_parser(name, help=description)
    return parser


def _dispatch(command: str, arguments: Sequence[str]) -> int:
    if command == "study":
        from inferdrome.breakpoint_study import main as study_main

        return study_main(arguments)
    if command == "search":
        from inferdrome.vllm_bounded_search import main as search_main

        search_main(arguments)
    elif command == "reduce":
        from inferdrome.vllm_witness_reducer import main as reduce_main

        reduce_main(arguments)
    elif command == "confirm":
        from inferdrome.vllm_heldout_confirmation import main as confirm_main

        confirm_main(arguments)
    elif command == "summarize":
        from inferdrome.breakpoint_report import main as summarize_main

        return summarize_main(arguments)
    elif command == "demo":
        from inferdrome.breakpoint_demo import main as demo_main

        return demo_main(arguments)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Delegate without changing process arguments or weakening artifact checks."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] in {
            "study",
            "search",
            "reduce",
            "confirm",
            "summarize",
            "demo",
        }:
            return _dispatch(arguments[0], arguments[1:])
        build_parser().parse_args(arguments)
        return 0
    except SystemExit as error:
        # Existing artifact commands use exit 2 for awaiting/ineligible evidence.
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return error.code
        print(json.dumps(str(error.code), ensure_ascii=True), file=sys.stderr)
        return 1
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        # JSON quoting makes hostile control characters inert in terminal errors.
        message = json.dumps(str(error), ensure_ascii=True)
        print(f"inferdrome breakpoint: error: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("inferdrome breakpoint: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
