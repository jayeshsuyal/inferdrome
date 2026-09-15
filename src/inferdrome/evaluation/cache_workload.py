"""Bounded local token-prefix checks for one explicit matched cache workload.

Rendering is fixed string concatenation, never caller-provided template code.
Token identity establishes potential reuse only; no cache hit, cache state, live
runtime identity, or performance claim follows from this offline certificate.
"""

from __future__ import annotations

import importlib.metadata
import stat
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Literal, Protocol

from inferdrome.errors import AdapterError
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.qwen3_campaign import (
    QWEN3_8B_REVISION,
    QWEN3_RENDERING_PREFIX,
    qwen3_rendered_prompt,
)
from inferdrome.qwen3_tokenizer import (
    QWEN3_TOKENIZERS_VERSION,
    load_verified_qwen3_tokenizer_files,
)
from inferdrome.routing_execution.canonical import canonical_json_bytes, sha256_digest

MAX_FAMILIES = 8
MAX_CASES_PER_FAMILY = 4000
MAX_OFFERS = 50_000
MAX_EXPANDED_PROMPT_BYTES = 8 * 1024 * 1024
MAX_PROCESSED_TOKENS = 8_000_000
MAX_PROMPT_BYTES = 32_768
MAX_MODEL_LEN = 2048
MAX_BLOCK_SIZE = 256
_SEPARATOR = "\n\nQuestion:\n"


class CacheWorkloadError(EvaluationError):
    """Sanitized workload or pinned-tokenizer contract violation."""


class CacheTokenizerUnavailable(CacheWorkloadError):
    """Required local tokenizer files or package are unavailable."""


@dataclass(frozen=True)
class WorkloadPair:
    shared_document: str = field(repr=False)
    unique_documents: tuple[str, ...] = field(repr=False)
    suffixes: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class CacheTokenizerIdentity:
    library: str
    library_version: str
    model_revision: str
    tokenizer_revision: str
    tokenizer_json_sha256: str
    tokenizer_config_sha256: str


@dataclass(frozen=True)
class CacheCaseVerification:
    case_index: int
    shared_prompt_sha256: str
    unique_prompt_sha256: str
    shared_rendered_sha256: str
    unique_rendered_sha256: str
    shared_token_ids_sha256: str
    unique_token_ids_sha256: str
    shared_rendered_tokens: int
    unique_rendered_tokens: int


@dataclass(frozen=True)
class CacheFamilyVerification:
    family_index: int
    input_sha256: str
    shared_lcp_tokens: int
    shared_lcp_token_ids_sha256: str
    shared_document_lcp_tokens: int
    shared_document_lcp_token_ids_sha256: str
    unique_max_pairwise_lcp_tokens: int
    wrapper_overlap_tokens: int
    shared_complete_prefix_blocks: int
    shared_document_complete_prefix_blocks: int
    unique_max_complete_prefix_blocks: int
    extra_shared_prefix_blocks: int
    cases: tuple[CacheCaseVerification, ...]


@dataclass(frozen=True)
class CacheWorkloadVerification:
    tokenization_status: Literal["VERIFIED_PINNED_QWEN3", "SYNTHETIC_TOKENIZER"]
    evidence_class: Literal["LOCAL_MEASUREMENT_ONLY", "SYNTHETIC_ONLY"]
    tokenizer_identity: CacheTokenizerIdentity | None
    block_size: int
    max_tokens: int
    family_count: int
    offer_count: int
    expanded_prompt_bytes: int
    processed_tokens: int
    families: tuple[CacheFamilyVerification, ...]
    max_model_len: int = MAX_MODEL_LEN
    schema_version: str = "inferdrome.evaluation-cache-workload-verification.v1"
    prefix_claim: str = "TOKEN_PREFIX_POTENTIAL_NOT_CACHE_HITS"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _Encoding(Protocol):
    @property
    def ids(self) -> list[int]: ...


class _Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> _Encoding: ...


def _text_bytes(value: str) -> bytes:
    if type(value) is not str:
        raise CacheWorkloadError("cache workload text has an invalid type")
    if "<|" in value or "|>" in value:
        raise CacheWorkloadError("cache workload text contains a reserved chat marker")
    try:
        content = value.encode("utf-8")
    except UnicodeError:
        raise CacheWorkloadError("cache workload text is not valid UTF-8") from None
    if not 1 <= len(content) <= MAX_PROMPT_BYTES:
        raise CacheWorkloadError("cache workload text exceeds its byte bound")
    return content


def render_prompt(document: str, suffix: str) -> str:
    """Render one user message with the document preceding the varying suffix."""
    _text_bytes(document)
    _text_bytes(suffix)
    prompt = document + _SEPARATOR + suffix
    _text_bytes(prompt)
    return prompt


def _preflight(
    pairs: tuple[WorkloadPair, ...], block_size: int, max_tokens: int
) -> tuple[int, int]:
    if type(pairs) is not tuple or not 1 <= len(pairs) <= MAX_FAMILIES:
        raise CacheWorkloadError("cache workload family count exceeds its bound")
    if type(block_size) is not int or not 1 <= block_size <= MAX_BLOCK_SIZE:
        raise CacheWorkloadError("cache block size exceeds its bound")
    if type(max_tokens) is not int or not 1 <= max_tokens < MAX_MODEL_LEN:
        raise CacheWorkloadError("cache output token limit exceeds its bound")
    offers = expanded_bytes = 0
    for pair in pairs:
        if type(pair) is not WorkloadPair:
            raise CacheWorkloadError("cache workload family has an invalid type")
        if (
            type(pair.unique_documents) is not tuple
            or type(pair.suffixes) is not tuple
            or not 4 <= len(pair.suffixes) <= MAX_CASES_PER_FAMILY
            or len(pair.unique_documents) != len(pair.suffixes)
        ):
            raise CacheWorkloadError("cache workload cases violate their bounds")
        _text_bytes(pair.shared_document)
        for document, suffix in zip(pair.unique_documents, pair.suffixes, strict=True):
            shared = render_prompt(pair.shared_document, suffix)
            unique = render_prompt(document, suffix)
            expanded_bytes += len(shared.encode("utf-8")) + len(unique.encode("utf-8"))
            if expanded_bytes > MAX_EXPANDED_PROMPT_BYTES:
                raise CacheWorkloadError(
                    "expanded cache workload exceeds its byte bound"
                )
        if len(set(pair.unique_documents)) != len(pair.unique_documents) or len(
            set(pair.suffixes)
        ) != len(pair.suffixes):
            raise CacheWorkloadError("cache documents and suffixes must be distinct")
        offers += len(pair.suffixes)
        if offers > MAX_OFFERS:
            raise CacheWorkloadError("cache workload offer count exceeds its bound")
    return offers, expanded_bytes


def _lcp(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    for index, (first, second) in enumerate(zip(left, right, strict=False)):
        if first != second:
            return index
    return min(len(left), len(right))


def _max_pairwise_lcp(sequences: list[tuple[int, ...]]) -> int:
    # A maximum pairwise prefix is attained by adjacent lexicographic entries;
    # this avoids an O(number_of_cases**2) pair enumeration.
    ordered = sorted(sequences)
    return max(_lcp(first, second) for first, second in pairwise(ordered))


def _verify(
    pairs: tuple[WorkloadPair, ...],
    *,
    tokenizer: _Tokenizer,
    block_size: int,
    max_tokens: int,
    preflight: tuple[int, int],
    identity: CacheTokenizerIdentity | None,
) -> CacheWorkloadVerification:
    processed = 0

    def encode(text: str) -> tuple[int, ...]:
        nonlocal processed
        try:
            ids = tokenizer.encode(text, add_special_tokens=False).ids
        except Exception:
            raise CacheWorkloadError("cache workload tokenization failed") from None
        if type(ids) is not list:
            raise CacheWorkloadError(
                "cache tokenizer returned invalid token identifiers"
            )
        processed += len(ids)
        if processed > MAX_PROCESSED_TOKENS:
            raise CacheWorkloadError(
                "cache workload exceeds its token processing bound"
            )
        if any(type(token) is not int or not 0 <= token <= 2**31 - 1 for token in ids):
            raise CacheWorkloadError(
                "cache tokenizer returned invalid token identifiers"
            )
        return tuple(ids)

    wrapper = encode(QWEN3_RENDERING_PREFIX)
    families: list[CacheFamilyVerification] = []
    for family_index, pair in enumerate(pairs):
        shared_prefix: tuple[int, ...] | None = None
        # Probe the actual document boundary without the separator or question.
        # Comparing with full renders conservatively excludes tokens that merge
        # across that boundary; a common question cannot supply the required block.
        document_prefix = encode(QWEN3_RENDERING_PREFIX + pair.shared_document)
        unique_sequences: list[tuple[int, ...]] = []
        shared_seen: set[str] = set()
        unique_seen: set[str] = set()
        wrapper_overlap = len(wrapper)
        cases: list[CacheCaseVerification] = []
        for case_index, (document, suffix) in enumerate(
            zip(pair.unique_documents, pair.suffixes, strict=True)
        ):
            shared_prompt = render_prompt(pair.shared_document, suffix)
            unique_prompt = render_prompt(document, suffix)
            shared_rendered = qwen3_rendered_prompt(shared_prompt)
            unique_rendered = qwen3_rendered_prompt(unique_prompt)
            shared_ids, unique_ids = encode(shared_rendered), encode(unique_rendered)
            if len(shared_ids) != len(unique_ids):
                raise CacheWorkloadError(
                    "matched cache prompts have different token lengths"
                )
            if not shared_ids or len(shared_ids) + max_tokens > MAX_MODEL_LEN:
                raise CacheWorkloadError(
                    "cache prompt and output exceed the model context bound"
                )
            shared_digest = sha256_digest(canonical_json_bytes(shared_ids))
            unique_digest = sha256_digest(canonical_json_bytes(unique_ids))
            if shared_digest in shared_seen or unique_digest in unique_seen:
                raise CacheWorkloadError(
                    "cache workload repeats a complete tokenized prompt"
                )
            shared_seen.add(shared_digest)
            unique_seen.add(unique_digest)
            wrapper_overlap = min(
                wrapper_overlap, _lcp(wrapper, shared_ids), _lcp(wrapper, unique_ids)
            )
            shared_prefix = (
                shared_ids
                if shared_prefix is None
                else shared_prefix[: _lcp(shared_prefix, shared_ids)]
            )
            document_prefix = document_prefix[: _lcp(document_prefix, shared_ids)]
            unique_sequences.append(unique_ids)
            cases.append(
                CacheCaseVerification(
                    case_index=case_index,
                    shared_prompt_sha256=sha256_digest(shared_prompt.encode("utf-8")),
                    unique_prompt_sha256=sha256_digest(unique_prompt.encode("utf-8")),
                    shared_rendered_sha256=sha256_digest(
                        shared_rendered.encode("utf-8")
                    ),
                    unique_rendered_sha256=sha256_digest(
                        unique_rendered.encode("utf-8")
                    ),
                    shared_token_ids_sha256=shared_digest,
                    unique_token_ids_sha256=unique_digest,
                    shared_rendered_tokens=len(shared_ids),
                    unique_rendered_tokens=len(unique_ids),
                )
            )
        assert shared_prefix is not None
        shared_lcp = len(shared_prefix)
        document_lcp = len(document_prefix)
        unique_lcp = _max_pairwise_lcp(unique_sequences)
        if unique_lcp >= wrapper_overlap + block_size:
            raise CacheWorkloadError(
                "unique cache controls do not diverge early enough"
            )
        shared_blocks = shared_lcp // block_size
        document_blocks = document_lcp // block_size
        unique_blocks = unique_lcp // block_size
        extra_blocks = document_blocks - max(
            unique_blocks, wrapper_overlap // block_size
        )
        if extra_blocks < 1:
            raise CacheWorkloadError(
                "shared cache workload has no extra complete prefix block"
            )
        families.append(
            CacheFamilyVerification(
                family_index=family_index,
                input_sha256=sha256_digest(
                    canonical_json_bytes(
                        {
                            "shared_document": pair.shared_document,
                            "unique_documents": list(pair.unique_documents),
                            "suffixes": list(pair.suffixes),
                        }
                    )
                ),
                shared_lcp_tokens=shared_lcp,
                shared_lcp_token_ids_sha256=sha256_digest(
                    canonical_json_bytes(shared_prefix)
                ),
                shared_document_lcp_tokens=document_lcp,
                shared_document_lcp_token_ids_sha256=sha256_digest(
                    canonical_json_bytes(document_prefix)
                ),
                unique_max_pairwise_lcp_tokens=unique_lcp,
                wrapper_overlap_tokens=wrapper_overlap,
                shared_complete_prefix_blocks=shared_blocks,
                shared_document_complete_prefix_blocks=document_blocks,
                unique_max_complete_prefix_blocks=unique_blocks,
                extra_shared_prefix_blocks=extra_blocks,
                cases=tuple(cases),
            )
        )
    offers, expanded_bytes = preflight
    return CacheWorkloadVerification(
        tokenization_status="VERIFIED_PINNED_QWEN3"
        if identity
        else "SYNTHETIC_TOKENIZER",
        evidence_class="LOCAL_MEASUREMENT_ONLY" if identity else "SYNTHETIC_ONLY",
        tokenizer_identity=identity,
        block_size=block_size,
        max_tokens=max_tokens,
        family_count=len(pairs),
        offer_count=offers,
        expanded_prompt_bytes=expanded_bytes,
        processed_tokens=processed,
        families=tuple(families),
    )


def _verify_cache_workloads(
    pairs: tuple[WorkloadPair, ...],
    *,
    tokenizer: _Tokenizer,
    block_size: int,
    max_tokens: int,
) -> CacheWorkloadVerification:
    """Pure test seam; an injected tokenizer can only produce synthetic evidence."""
    return _verify(
        pairs,
        tokenizer=tokenizer,
        block_size=block_size,
        max_tokens=max_tokens,
        preflight=_preflight(pairs, block_size, max_tokens),
        identity=None,
    )


def verify_cache_workloads(
    pairs: tuple[WorkloadPair, ...],
    *,
    tokenizer_root: Path,
    block_size: int,
    max_tokens: int,
) -> CacheWorkloadVerification:
    """Verify with existing local pinned files/package; never download or install."""
    preflight = _preflight(pairs, block_size, max_tokens)
    if not isinstance(tokenizer_root, Path) or not tokenizer_root.is_absolute():
        raise CacheWorkloadError("cache tokenizer root must be an absolute local path")
    try:
        for index, path in enumerate(
            (
                tokenizer_root,
                tokenizer_root / "tokenizer.json",
                tokenizer_root / "tokenizer_config.json",
            )
        ):
            metadata = path.lstat()
            if (index == 0 and not stat.S_ISDIR(metadata.st_mode)) or (
                index != 0
                and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1)
            ):
                raise CacheWorkloadError(
                    "cache tokenizer path has an invalid file type"
                )
    except OSError:
        raise CacheTokenizerUnavailable(
            "pinned cache tokenizer files are unavailable"
        ) from None
    try:
        files = load_verified_qwen3_tokenizer_files(tokenizer_root)
    except AdapterError:
        raise CacheWorkloadError(
            "cache tokenizer files violate their pinned identity"
        ) from None
    try:
        tokenizers = importlib.import_module("tokenizers")
        version = importlib.metadata.version("tokenizers")
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise CacheTokenizerUnavailable(
            "pinned cache tokenizer package is unavailable"
        ) from None
    if (
        version != QWEN3_TOKENIZERS_VERSION
        or getattr(tokenizers, "__version__", None) != version
    ):
        raise CacheWorkloadError("cache tokenizer package violates its pinned identity")
    try:
        tokenizer = tokenizers.Tokenizer.from_str(files.tokenizer_json.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise CacheWorkloadError(
            "pinned cache tokenizer could not be constructed"
        ) from None
    identity = CacheTokenizerIdentity(
        library="tokenizers",
        library_version=version,
        model_revision=QWEN3_8B_REVISION,
        tokenizer_revision=QWEN3_8B_REVISION,
        tokenizer_json_sha256=files.verification.tokenizer_json_sha256,
        tokenizer_config_sha256=files.verification.tokenizer_config_sha256,
    )
    return _verify(
        pairs,
        tokenizer=tokenizer,
        block_size=block_size,
        max_tokens=max_tokens,
        preflight=preflight,
        identity=identity,
    )
