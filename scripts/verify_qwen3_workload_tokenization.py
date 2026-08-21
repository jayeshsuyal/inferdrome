#!/usr/bin/env python3
"""Reverify Qwen3 campaign input lengths with the exact tokenizer snapshot."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
from typing import Any

from inferdrome.errors import AdapterError
from inferdrome.qwen3_campaign import (
    qwen3_rendered_prompt,
    qwen3_workload_manifest,
    qwen3_workload_prompts,
)
from inferdrome.qwen3_tokenizer import (
    QWEN3_TOKENIZERS_VERSION,
    load_verified_qwen3_tokenizer_files,
)


def _require_tokenizers_version(observed: str) -> None:
    if observed != QWEN3_TOKENIZERS_VERSION:
        raise SystemExit(
            "tokenizers package version differs from the frozen Qwen3 verifier"
        )


def _tokenizer(content: bytes) -> Any:
    try:
        import tokenizers
        from tokenizers import Tokenizer

        observed_version = importlib.metadata.version("tokenizers")
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise SystemExit(
            "the pinned tokenizers package is required for verification"
        ) from None
    _require_tokenizers_version(observed_version)
    if getattr(tokenizers, "__version__", None) != observed_version:
        raise SystemExit("tokenizers module and package versions disagree")
    try:
        return Tokenizer.from_str(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SystemExit("pinned tokenizer.json could not be loaded") from None


def verify(tokenizer_root: Path) -> None:
    try:
        files = load_verified_qwen3_tokenizer_files(tokenizer_root)
    except AdapterError as error:
        raise SystemExit(str(error)) from None
    tokenizer = _tokenizer(files.tokenizer_json)
    manifest = qwen3_workload_manifest()
    prompts = qwen3_workload_prompts()
    records = manifest["records"]
    if len(prompts) != len(records):
        raise AssertionError
    for prompt, record in zip(prompts, records, strict=True):
        raw_count = len(tokenizer.encode(prompt, add_special_tokens=False).ids)
        rendered_count = len(
            tokenizer.encode(
                qwen3_rendered_prompt(prompt),
                add_special_tokens=False,
            ).ids
        )
        if raw_count != record["raw_prompt_tokens"]:
            raise SystemExit(
                f"raw token count drifted at sequence {record['sequence_index']}"
            )
        if rendered_count != record["rendered_input_tokens"]:
            raise SystemExit(
                f"rendered token count drifted at sequence {record['sequence_index']}"
            )
    print(f"Qwen3 workload tokenization verified ({len(prompts)} prompts)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tokenizer_root", type=Path)
    args = parser.parse_args()
    verify(args.tokenizer_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
