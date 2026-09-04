#!/usr/bin/env python3
"""Check committed isolated external-router-v1 contract snapshots."""

from __future__ import annotations

import argparse

from inferdrome.external_router.schema import check_schemas, write_schemas


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.check:
        check_schemas()
    else:
        write_schemas()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
