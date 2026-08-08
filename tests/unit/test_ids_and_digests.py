"""Identifier and digest-domain invariants."""

import re

import pytest

from inferdrome.domain.digests import (
    DigestDomain,
    canonical_json_bytes,
    digest_bytes,
    digest_canonical_json,
)
from inferdrome.domain.ids import (
    new_run_id,
    new_trial_set_id,
    request_id_from_index,
    sha256_digest,
)


def test_generated_run_id_matches_public_domain() -> None:
    assert re.fullmatch(r"run-[0-9a-f]{32}", new_run_id())


def test_generated_trial_set_id_matches_public_domain() -> None:
    assert re.fullmatch(r"trial-set-[0-9a-f]{32}", new_trial_set_id())


@pytest.mark.parametrize(
    ("index", "expected"),
    [(0, "req-00000000"), (17, "req-00000017"), (99_999_999, "req-99999999")],
)
def test_request_id_derivation(index: int, expected: str) -> None:
    assert request_id_from_index(index) == expected


@pytest.mark.parametrize("index", [-1, 100_000_000])
def test_request_id_rejects_out_of_domain_index(index: int) -> None:
    with pytest.raises(ValueError):
        request_id_from_index(index)


def test_tagged_sha256_uses_exact_bytes() -> None:
    assert sha256_digest(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_digest(b"record") != sha256_digest(b"record\n")


def test_canonical_json_is_order_independent() -> None:
    left = {"z": 1, "a": 2}
    right = {"a": 2, "z": 1}
    assert canonical_json_bytes(left) == b'{"a":2,"z":1}'
    assert digest_canonical_json(DigestDomain.REQUEST_PLAN, left) == (
        digest_canonical_json(DigestDomain.REQUEST_PLAN, right)
    )


def test_digest_domains_are_disjoint() -> None:
    payload = b"same bytes"
    digests = {digest_bytes(domain, payload) for domain in DigestDomain}
    assert len(digests) == len(DigestDomain)
