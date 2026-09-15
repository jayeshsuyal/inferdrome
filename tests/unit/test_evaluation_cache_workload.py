"""Synthetic tokenizers test logic only; no genuine Qwen tokenization is claimed."""

from __future__ import annotations

import importlib.metadata
import json
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import inferdrome.evaluation.cache_workload as workload
from inferdrome.evaluation.cache_workload import (
    CacheTokenizerUnavailable,
    CacheWorkloadError,
    WorkloadPair,
    _verify_cache_workloads,
    render_prompt,
    verify_cache_workloads,
)
from inferdrome.qwen3_campaign import QWEN3_RENDERING_PREFIX, qwen3_rendered_prompt
from inferdrome.qwen3_tokenizer import expected_qwen3_tokenizer_file_verification
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest


@dataclass
class Encoded:
    ids: list[int]


class CharacterTokenizer:
    """Deliberately not Qwen: one Unicode code point per synthetic token."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def encode(self, text: str, *, add_special_tokens: bool) -> Encoded:
        assert add_special_tokens is False
        self.calls.append(text)
        return Encoded([ord(character) for character in text])


def pair(length: int = 40, cases: int = 4) -> WorkloadPair:
    return WorkloadPair(
        "S" * length,
        tuple(chr(0x2000 + index) + "u" * (length - 1) for index in range(cases)),
        tuple(f"query-{index}" for index in range(cases)),
    )


def verify(value: WorkloadPair, *, block_size: int = 8, max_tokens: int = 128):
    return _verify_cache_workloads(
        (value,),
        tokenizer=CharacterTokenizer(),
        block_size=block_size,
        max_tokens=max_tokens,
    )


def test_fixed_rendering_is_inert_and_document_precedes_suffix() -> None:
    assert render_prompt("{{ arbitrary template }}", "different question") == (
        "{{ arbitrary template }}\n\nQuestion:\ndifferent question"
    )


def test_exact_ids_counts_and_hashes_produce_only_synthetic_potential() -> None:
    source = pair()
    tokenizer = CharacterTokenizer()
    result = _verify_cache_workloads(
        (source,), tokenizer=tokenizer, block_size=8, max_tokens=128
    )
    family = result.families[0]
    shared_prompt = render_prompt(source.shared_document, source.suffixes[0])
    rendered = qwen3_rendered_prompt(shared_prompt)
    ids = [ord(character) for character in rendered]
    assert tokenizer.calls[0] == QWEN3_RENDERING_PREFIX
    assert tokenizer.calls[1] == QWEN3_RENDERING_PREFIX + source.shared_document
    assert tokenizer.calls[2] == rendered
    assert result.offer_count == 4 and result.family_count == 1
    assert result.processed_tokens == sum(map(len, tokenizer.calls))
    assert result.expanded_prompt_bytes == sum(
        len(render_prompt(document, suffix).encode("utf-8"))
        for unique, suffix in zip(source.unique_documents, source.suffixes, strict=True)
        for document in (source.shared_document, unique)
    )
    assert family.input_sha256 == sha256_digest(
        canonical_json_bytes(
            {
                "shared_document": source.shared_document,
                "unique_documents": list(source.unique_documents),
                "suffixes": list(source.suffixes),
            }
        )
    )
    assert family.cases[0].shared_token_ids_sha256 == sha256_digest(
        canonical_json_bytes(ids)
    )
    assert family.cases[0].shared_rendered_sha256 == sha256_digest(rendered.encode())
    assert family.cases[0].shared_prompt_sha256 == sha256_digest(shared_prompt.encode())
    assert family.cases[0].shared_rendered_tokens == len(ids)
    assert all(
        case.shared_rendered_tokens == case.unique_rendered_tokens
        for case in family.cases
    )
    assert family.wrapper_overlap_tokens == len(QWEN3_RENDERING_PREFIX)
    assert family.unique_max_pairwise_lcp_tokens == family.wrapper_overlap_tokens
    assert family.shared_lcp_tokens == len(
        QWEN3_RENDERING_PREFIX + source.shared_document + "\n\nQuestion:\nquery-"
    )
    assert family.shared_document_lcp_tokens == len(
        QWEN3_RENDERING_PREFIX + source.shared_document
    )
    assert family.shared_lcp_token_ids_sha256 == sha256_digest(
        canonical_json_bytes(ids[: family.shared_lcp_tokens])
    )
    assert family.shared_document_lcp_token_ids_sha256 == sha256_digest(
        canonical_json_bytes(ids[: family.shared_document_lcp_tokens])
    )
    assert family.shared_document_complete_prefix_blocks == (
        family.shared_document_lcp_tokens // 8
    )
    assert family.extra_shared_prefix_blocks == (
        family.shared_document_complete_prefix_blocks
        - family.unique_max_complete_prefix_blocks
    )
    assert result.tokenization_status == "SYNTHETIC_TOKENIZER"
    assert result.evidence_class == "SYNTHETIC_ONLY"
    assert result.tokenizer_identity is None
    assert result.prefix_claim == "TOKEN_PREFIX_POTENTIAL_NOT_CACHE_HITS"
    exported = json.dumps(result.to_dict())
    assert source.shared_document not in exported and "query-" not in exported
    assert "Qwen" not in exported and "0.22.1" not in exported
    with pytest.raises(FrozenInstanceError):
        result.offer_count = 1


def test_all_families_are_checked_in_order() -> None:
    sources = tuple(pair(40 + index) for index in range(8))
    result = _verify_cache_workloads(
        sources, tokenizer=CharacterTokenizer(), block_size=8, max_tokens=128
    )
    assert result.family_count == 8 and result.offer_count == 32
    assert [family.family_index for family in result.families] == list(range(8))
    assert len({family.input_sha256 for family in result.families}) == 8


def test_equal_token_counts_do_not_hide_different_full_ids() -> None:
    first = pair()
    second = replace(first, shared_document="T" * len(first.shared_document))
    a, b = verify(first).families[0].cases[0], verify(second).families[0].cases[0]
    assert a.shared_rendered_tokens == b.shared_rendered_tokens
    assert a.shared_token_ids_sha256 != b.shared_token_ids_sha256


@pytest.mark.parametrize("marker", ["<|im_start|>", "<|im_end|>", "<|", "|>"])
@pytest.mark.parametrize("field", ["shared_document", "unique_documents", "suffixes"])
def test_reserved_chat_markers_fail_before_tokenization(
    marker: str, field: str
) -> None:
    source = pair()
    private = "private" + marker
    value = (
        private
        if field == "shared_document"
        else (private, *getattr(source, field)[1:])
    )
    source = replace(source, **{field: value})
    tokenizer = CharacterTokenizer()
    with pytest.raises(CacheWorkloadError, match="reserved chat marker") as raised:
        _verify_cache_workloads(
            (source,), tokenizer=tokenizer, block_size=8, max_tokens=128
        )
    assert not tokenizer.calls and "private" not in str(raised.value)


def test_wrapper_overlap_accounts_for_token_merge_at_document_boundary() -> None:
    class BoundaryTokenizer(CharacterTokenizer):
        def encode(self, text: str, *, add_special_tokens: bool) -> Encoded:
            encoded = super().encode(text, add_special_tokens=add_special_tokens)
            if text.startswith(QWEN3_RENDERING_PREFIX) and len(text) > len(
                QWEN3_RENDERING_PREFIX
            ):
                boundary = len(QWEN3_RENDERING_PREFIX) - 1
                encoded.ids[boundary : boundary + 2] = [
                    100_000 + encoded.ids[boundary + 1]
                ]
            return encoded

    result = _verify_cache_workloads(
        (pair(),), tokenizer=BoundaryTokenizer(), block_size=8, max_tokens=128
    )
    assert result.families[0].wrapper_overlap_tokens == len(QWEN3_RENDERING_PREFIX) - 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("block_size", True),
        ("block_size", 0),
        ("block_size", 257),
        ("block_size", 1.0),
        ("max_tokens", True),
        ("max_tokens", 0),
        ("max_tokens", 2048),
        ("max_tokens", 1.0),
    ],
)
def test_strict_numeric_bounds_before_tokenization(field: str, value: object) -> None:
    tokenizer = CharacterTokenizer()
    arguments = {"block_size": 8, "max_tokens": 128, field: value}
    with pytest.raises(CacheWorkloadError):
        _verify_cache_workloads((pair(),), tokenizer=tokenizer, **arguments)
    assert not tokenizer.calls


@pytest.mark.parametrize("sources", [(), (pair(),) * 9, [pair()]])
def test_family_container_and_count_bounds(sources: object) -> None:
    with pytest.raises(CacheWorkloadError):
        _verify_cache_workloads(
            sources, tokenizer=CharacterTokenizer(), block_size=8, max_tokens=128
        )


@pytest.mark.parametrize(
    "source",
    [
        pair(cases=3),
        pair(cases=4001),
        WorkloadPair("document", ("one",) * 4, ("a", "b", "c", "d")),
        WorkloadPair("document", ("a", "b", "c", "d"), ("one",) * 4),
        WorkloadPair("document", ("a", "b", "c"), ("a", "b", "c", "d")),
        WorkloadPair("", ("a", "b", "c", "d"), ("a", "b", "c", "d")),
        replace(pair(), shared_document="private\ud800"),
        replace(pair(), shared_document=17),
    ],
)
def test_invalid_explicit_cases_fail_before_tokenization(source: WorkloadPair) -> None:
    tokenizer = CharacterTokenizer()
    with pytest.raises(CacheWorkloadError) as raised:
        _verify_cache_workloads(
            (source,), tokenizer=tokenizer, block_size=8, max_tokens=128
        )
    assert not tokenizer.calls and "private" not in str(raised.value)


def test_per_prompt_and_aggregate_byte_bounds_precede_tokenization() -> None:
    for source in (pair(length=32_768), pair(length=20_000, cases=300)):
        tokenizer = CharacterTokenizer()
        with pytest.raises(CacheWorkloadError, match="byte bound"):
            _verify_cache_workloads(
                (source,), tokenizer=tokenizer, block_size=8, max_tokens=128
            )
        assert not tokenizer.calls


@pytest.mark.parametrize("bound", ["MAX_EXPANDED_PROMPT_BYTES", "MAX_PROCESSED_TOKENS"])
def test_resource_boundary_equality_is_accepted_then_one_less_rejects(
    bound: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pair()
    measured = verify(source)
    limit = (
        measured.expanded_prompt_bytes
        if bound == "MAX_EXPANDED_PROMPT_BYTES"
        else measured.processed_tokens
    )
    monkeypatch.setattr(workload, bound, limit)
    assert verify(source).to_dict() == measured.to_dict()
    monkeypatch.setattr(workload, bound, limit - 1)
    with pytest.raises(CacheWorkloadError):
        verify(source)


def test_context_bound_uses_rendered_input_and_reserved_output() -> None:
    suffixes = ("a", "b", "c", "d")
    overhead = len(qwen3_rendered_prompt("\n\nQuestion:\n" + suffixes[0]))
    source = replace(pair(length=2048 - 128 - overhead), suffixes=suffixes)
    assert verify(source).families[0].cases[0].shared_rendered_tokens + 128 == 2048
    with pytest.raises(CacheWorkloadError, match="context bound"):
        verify(source, max_tokens=129)


def test_matching_lengths_are_required_for_every_index() -> None:
    source = pair()
    source = replace(
        source,
        unique_documents=(
            *source.unique_documents[:-1],
            source.unique_documents[-1] + "x",
        ),
    )
    with pytest.raises(CacheWorkloadError, match="different token lengths"):
        verify(source)


def test_extra_complete_block_equality_boundary() -> None:
    suffixes = ("a", "b", "c", "d")
    length = 64 - len(QWEN3_RENDERING_PREFIX)
    valid = replace(pair(length=length), suffixes=suffixes)
    assert verify(valid, block_size=64).families[0].extra_shared_prefix_blocks == 1
    invalid = replace(pair(length=length - 1), suffixes=suffixes)
    with pytest.raises(CacheWorkloadError, match="no extra complete prefix block"):
        verify(invalid, block_size=64)


def test_short_document_cannot_borrow_blocks_from_common_question_suffix() -> None:
    source = WorkloadPair(
        "s",
        ("a", "b", "c", "d"),
        tuple("common question " * 20 + str(index) for index in range(4)),
    )
    assert len(QWEN3_RENDERING_PREFIX + source.shared_document) < 64
    with pytest.raises(CacheWorkloadError, match="no extra complete prefix block"):
        verify(source, block_size=64)


def test_document_boundary_probe_excludes_tokens_merged_with_question_separator() -> (
    None
):
    class DocumentBoundaryTokenizer(CharacterTokenizer):
        def encode(self, text: str, *, add_special_tokens: bool) -> Encoded:
            encoded = super().encode(text, add_special_tokens=add_special_tokens)
            if "\n\nQuestion:\n" in text:
                boundary = text.index("\n\nQuestion:\n") - 1
                encoded.ids[boundary : boundary + 2] = [100_000]
            return encoded

    source = replace(
        pair(length=64 - len(QWEN3_RENDERING_PREFIX)), suffixes=("a", "b", "c", "d")
    )
    with pytest.raises(CacheWorkloadError, match="no extra complete prefix block"):
        _verify_cache_workloads(
            (source,),
            tokenizer=DocumentBoundaryTokenizer(),
            block_size=64,
            max_tokens=128,
        )


def test_unique_divergence_compares_every_pair_including_nonfirst_neighbors() -> None:
    source = pair()
    source = replace(
        source,
        unique_documents=("a" * 40, "b" * 20 + "c" * 20, "b" * 20 + "d" * 20, "z" * 40),
    )
    with pytest.raises(CacheWorkloadError, match="do not diverge early"):
        verify(source)


@pytest.mark.parametrize("padding,valid", [(7, True), (8, False)])
def test_unique_divergence_boundary_is_relative_to_actual_wrapper(
    padding: int, valid: bool
) -> None:
    source = pair()
    source = replace(
        source,
        unique_documents=tuple(
            "p" * padding + chr(65 + i) + "x" * (39 - padding) for i in range(4)
        ),
    )
    if valid:
        assert verify(source).families[0].extra_shared_prefix_blocks > 0
    else:
        with pytest.raises(CacheWorkloadError, match="do not diverge early"):
            verify(source)


def test_tokenized_duplicate_is_rejected_even_when_suffix_bytes_differ() -> None:
    class NormalizingTokenizer(CharacterTokenizer):
        def encode(self, text: str, *, add_special_tokens: bool) -> Encoded:
            return super().encode(
                "".join(c for c in text if not c.isdigit()),
                add_special_tokens=add_special_tokens,
            )

    with pytest.raises(CacheWorkloadError, match="repeats a complete tokenized prompt"):
        _verify_cache_workloads(
            (pair(),), tokenizer=NormalizingTokenizer(), block_size=8, max_tokens=128
        )


@pytest.mark.parametrize("ids", [[True], [-1], [2**31], [1.0], []])
def test_invalid_tokenizer_output_is_sanitized(ids: list) -> None:
    class InvalidTokenizer:
        def encode(self, text: str, *, add_special_tokens: bool) -> Encoded:
            return Encoded(ids)

    with pytest.raises(CacheWorkloadError):
        _verify_cache_workloads(
            (pair(),), tokenizer=InvalidTokenizer(), block_size=8, max_tokens=128
        )


def test_tokenizer_exception_does_not_escape_raw_content() -> None:
    class BrokenTokenizer:
        def encode(self, text: str, *, add_special_tokens: bool) -> Encoded:
            raise RuntimeError("private document content")

    with pytest.raises(
        CacheWorkloadError, match=r"^cache workload tokenization failed$"
    ):
        _verify_cache_workloads(
            (pair(),), tokenizer=BrokenTokenizer(), block_size=8, max_tokens=128
        )


def test_missing_local_files_are_unavailable_without_download(tmp_path: Path) -> None:
    with pytest.raises(CacheTokenizerUnavailable):
        verify_cache_workloads(
            (pair(),), tokenizer_root=tmp_path, block_size=8, max_tokens=128
        )


@pytest.mark.parametrize("unsafe", ["wrong-digest", "symlink", "directory"])
def test_invalid_local_files_are_errors_not_unavailable(
    tmp_path: Path, unsafe: str
) -> None:
    (tmp_path / "tokenizer_config.json").write_text("private invalid config")
    tokenizer = tmp_path / "tokenizer.json"
    if unsafe == "symlink":
        tokenizer.symlink_to(tmp_path / "tokenizer_config.json")
    elif unsafe == "directory":
        tokenizer.mkdir()
    else:
        tokenizer.write_text("private invalid tokenizer")
    with pytest.raises(CacheWorkloadError) as raised:
        verify_cache_workloads(
            (pair(),), tokenizer_root=tmp_path, block_size=8, max_tokens=128
        )
    assert not isinstance(raised.value, CacheTokenizerUnavailable)
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("mode", ["missing", "wrong-version", "module-mismatch"])
def test_package_availability_and_pins_fail_without_emitting_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    for name in ("tokenizer.json", "tokenizer_config.json"):
        (tmp_path / name).write_bytes(b"{}")
    # Only reach the failing package branch; no real or fake certificate is emitted.
    monkeypatch.setattr(
        workload,
        "load_verified_qwen3_tokenizer_files",
        lambda _: SimpleNamespace(
            tokenizer_json=b"{}",
            verification=expected_qwen3_tokenizer_file_verification(),
        ),
    )

    def package(name: str):
        assert name == "tokenizers"
        if mode == "missing":
            raise ModuleNotFoundError("private package path")
        return SimpleNamespace(__version__="wrong-version")

    monkeypatch.setattr(workload.importlib, "import_module", package)
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda _: "0.22.1" if mode == "module-mismatch" else "wrong-version",
    )
    with pytest.raises(CacheWorkloadError) as raised:
        verify_cache_workloads(
            (pair(),), tokenizer_root=tmp_path, block_size=8, max_tokens=128
        )
    assert isinstance(raised.value, CacheTokenizerUnavailable) is (mode == "missing")
    assert "private" not in str(raised.value)


def test_invalid_workload_preflight_precedes_tokenizer_availability(
    tmp_path: Path,
) -> None:
    with pytest.raises(CacheWorkloadError) as raised:
        verify_cache_workloads(
            (pair(cases=3),),
            tokenizer_root=tmp_path / "missing",
            block_size=8,
            max_tokens=128,
        )
    assert not isinstance(raised.value, CacheTokenizerUnavailable)
