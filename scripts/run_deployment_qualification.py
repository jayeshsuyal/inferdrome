#!/usr/bin/env python3
"""Guarded command entrypoint for the local Docker Compose qualification."""

from __future__ import annotations

from inferdrome.deployment.qualification import qualification_main

if __name__ == "__main__":
    raise SystemExit(qualification_main())
