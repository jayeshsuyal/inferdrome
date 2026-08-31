# v0.1 raw A10 archive privacy and licensing decision packet

**Review date:** 2026-08-31

**Repository baseline:** `47f2f9772c2db350e8cb5f9f39051686df8ef329` (`main`)

**Exact archive:** 689,272 bytes, `sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424`

**Owner-decision context:** 2026-08-31 v0.1 raw A10 archive privacy and
licensing closure

## Decision snapshot

| Decision | Status | Basis |
| --- | --- | --- |
| Privacy clearance for public delivery | **NOT GRANTED** | Native prompts, responses, diagnostics, host paths, a private-network address, GPU UUIDs, and process identifiers remain in the exact sealed bytes. |
| Licensing clearance for public delivery | **NOT GRANTED** | No archive-bound decision resolves the model, workload, vLLM/runtime, or generated-output rights. |
| Publication authorization | **EXPLICITLY REFUSED** | Jayesh directed that this exact archive remain unpublished. |
| Archive status | **KEEP `EXTERNAL_ONLY`** | The owner has made a refusal decision, not an approval decision. |

The owner decision is a refusal to authorize public distribution. It is not a
privacy clearance, licensing clearance, redistribution grant, release-asset
approval, ExitSpec outcome, or authorization to change the raw archive. It is
not legal advice.

## Owner decision record

**Decision owner:** Jayesh

**Decision date:** 2026-08-31

**Decision context:** v0.1 raw A10 archive privacy and licensing closure for
the exact digest named above.

**Exact archive binding:** 689,272 bytes,
`sha256:f2408fd0649a7c79f5962872003781ebb9c878b802db27d633cf246f13b6f424`

**Apache-2.0 scope:** Inferdrome-authored repository material only; it grants
no rights in this archive, its model/tokenizer, workload, vLLM/runtime, or
generated output.

**Decision:** the exact raw A10 archive must remain `KEEP_EXTERNAL_ONLY` and
unpublished.

**Binding effect:** no public copy, release asset, repository vendoring,
upload, or other redistribution of these exact bytes is authorized. The
archive's existing `EXTERNAL_ONLY` classification remains unchanged. This
decision is deliberately narrower than a privacy, licensing, or provenance
clearance: it grants none of them.

## Scope, custody, and provenance

The reviewed object is the exact 2026-08-20 A10 archive above, bound to capture
manifest `sha256:1d4ea1e251c5a84a104333ab8579d580838701a70cc38b64b68c88f66266e0cb`,
capture-producer commit `c08b46d9fbd87477f45d130aa3c63615937c4dc3`, and the
sealed publication-review and handoff records in
[`evidence/gpu/2026-08-20-a10`](../../evidence/gpu/2026-08-20-a10). Both
historical commits are ancestors of the reviewed `main` baseline.

The review tool's documented ignored location is
`gpu-proof-retrieved/20260820T003703Z-c08b46d9fbd8-4fbd20e6-UNVERIFIED/capture.tar.gz`.
It is absent from this clean checkout. No alternate location was inferred or
searched, no raw byte was copied or changed, and no fresh byte-level scan was
claimed. This packet therefore revalidates the sealed record and reports its
limits; it does not replace a fresh exact-byte review if the owner later makes
the archive available through approved custody.

The sealed review had already performed path-safe isolated extraction and a
bounded scan of every regular member. Its aggregate inventory is 92 directories,
310 files, and 3,137,959 expanded bytes. It recorded no links, devices, special
files, duplicate members, traversal paths, or unreviewed oversized members.

## Privacy and disclosure assessment

The sealed scan found no secret-shaped value, email-address, or public-network
address detector match, and no binary/non-UTF-8 member. Those are detector
results, not a general clearance for personal, customer, or confidential data.
The retained native material includes:

- 6 prompt artifacts (502 retained records across separate runs);
- 12 generated-response artifacts (1,002 retained records, including one
  intentionally corrupted artifact);
- 88 stdout/stderr artifacts and 22 environment/package-inventory artifacts;
- absolute paths in 61 members, a private-network address in 5 members, GPU
  UUIDs in 12 members, and process identifiers in 11 members.

The model/tokenizer is pinned as `Qwen/Qwen2.5-0.5B-Instruct`; the handoff also
binds the managed vLLM 0.26 evidence profile. These are third-party model and
runtime identifiers, not publication permission. The sealed record provides no
separate affirmative clearance for provider, account, host, or customer
identifiers beyond the bounded detector results above. Do not infer their
absence from the lack of a dedicated detector result.

**Privacy conclusion:** public delivery is not cleared. The owner decision
requires that the archive remain unpublished regardless of this assessment. A
future attempt to change that decision would still require a named data/privacy
owner to verify prompt and response origin, permitted audience, any personal or
customer-data obligations, and whether retained diagnostics and identifiers may
be disclosed. The archive must stay byte-for-byte unchanged; redaction would
create a different object requiring a separate evidence and publication
decision.

## Licensing assessment

The exact archive contains no retained license file. At the historical review,
the repository license was not yet selected; the current repository's
Apache-2.0 license covers Inferdrome-authored repository material only. It does
not clear the archive-bound model/tokenizer, workload, vLLM/runtime artifacts,
or generated responses.

All of these archive-bound decisions remain unresolved: model license,
workload-publication license, vLLM/runtime license, generated-output rights,
and owner approval for delivery. The frozen record's former
`REPOSITORY_LICENSE_UNSELECTED` reason is historical, but current Apache-2.0
selection does not resolve any of those external scopes.

**Licensing conclusion:** public delivery is not cleared. The owner decision
does not settle any licensing question. A future attempt to change that decision
would require a licensing owner or counsel to record the applicable terms,
required notices/attributions, redistribution permissions, and any
generated-output or workload restrictions against this exact digest.

## Validation and residual uncertainty

On the baseline above, the archive-independent record gate passed:

```text
PYTHONPATH=src <bundled-python-3.12> scripts/review_gpu_evidence_publication.py --check-records
```

It pins both canonical record hashes and cross-identities, requires
`EXTERNAL_ONLY`, null producer/Inferdrome acceptance authority, retrospective
chronology, and the historical record's outstanding owner-approval field. That
field is consistent with the current refusal: the sealed record is not changed
to invent an approval. The focused publication-record test suite also passed
(32 tests). The repository-only candidate preflight intentionally continues to
report its archive-publication item as `MANUAL`: the checked checklist entry
records the decision, while automation does not independently parse or certify
human decisions.

The gate validates committed metadata even when the archive is absent. It does
not establish current raw-byte custody, rescan the archive, grant privacy or
licensing rights, authorize publication, or establish ExitSpec acceptance.

## Future-change limitations

This refusal applies to the exact digest above. It does not classify a changed,
redacted, repacked, or copied archive as safe or eligible. Any future request to
change the decision must begin with approved custody of the exact archive and
then separately establish, without relying on this packet:

- exact-byte identity and a fresh path-safe, bounded full-member review;
- provenance and prompt/response data-rights review;
- archive-bound model, workload, vLLM/runtime, and generated-output licensing
  decisions, including required notices; and
- a new explicit owner decision that expressly authorizes the proposed delivery
  of the exact digest.

Until then, the archive remains `EXTERNAL_ONLY` and unpublished. No public copy,
release asset, altered archive, or derivative delivery may be created from these
records.
