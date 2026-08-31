# Inferdrome v0.1 provider, GPU, SSH, and cost-cleanup evidence closure

## Disposition

**Provider-side v0.1 evidence disposition:**
`EXISTING_TRACKED_EVIDENCE_SUFFICIENT_NO_NEW_PAID_RUN_REQUIRED`

**Live-provider claim disposition:**
`UNSUPPORTED_NO_CURRENT_PROVIDER_STATE_CLAIM`

Assessment base: `47f2f9772c2db350e8cb5f9f39051686df8ef329` (`main`)

Assessment date: 2026-08-31 (America/Los_Angeles)

This is a repository-only closure assessment for Inferdrome's v0.1
provider/GPU/SSH/cost-cleanup evidence boundary. It requires no new paid
provider run: the frozen v0.1 definition of done requires an opt-in real-GPU
smoke result, and the release checklist already records a genuine,
customer-eligible Linux/NVIDIA-host bundle. Neither document requires a
current capacity observation, a new bill, or a present provider-cleanup
attestation.

The assessment does not authorize a GPU launch, provider contact, SSH
connection, credential use, release, tag, archive publication, or ExitSpec
decision. A new live run remains separately necessary if an owner needs to
make a new prospective provider, hardware, SSH, billing, cleanup, or
ExitSpec-linked claim.

## Verified tracked evidence

| Record | Verified repository fact | Boundary retained |
| --- | --- | --- |
| [`2026-08-20 A10 handoff`](../../evidence/gpu/2026-08-20-a10/handoff-manifest.json) | The v0.1 checklist's producer anchor pins a 689,272-byte genuine A10 archive, capture-manifest digest, customer-eligible bundle digest, and independent corruption/synthetic rejection facts. | `RETROSPECTIVE`, no producer ExitSpec contract or verdict; raw archive is `EXTERNAL_ONLY`. |
| [`2026-08-21 Qwen3-8B A10 handoff`](../../evidence/gpu/2026-08-21-qwen3-8b-a10/handoff-manifest.json) and [operational summary](../../evidence/gpu/2026-08-21-qwen3-8b-a10/operational-summary.json) | One NVIDIA A10 observation with 96/96 successful measured requests. The retained controller records bind post-termination semantic verification, termination status `absent`, a `$0.75` maximum budget, and a `$0.348463` controller estimate at `$1.29/hour`. | The operational summary is `OPERATIONAL_RECORD_NOT_PROVIDER_ATTESTATION`; provider billing and final invoice remain external. The archive is `EXTERNAL_ONLY`, the contract chronology is `RETROSPECTIVE`, and the acceptance verdict is null. |
| [`2026-08-23 Qwen3-8B A100 SXM4 handoff`](../../evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4/handoff-manifest.json) and [operational summary](../../evidence/gpu/2026-08-23-qwen3-8b-a100-sxm4/operational-summary.json) | One reported NVIDIA A100-SXM4-40GB observation with 96/96 successful measured requests. The retained controller records bind post-termination semantic verification, termination status `absent`, a `$1.25` maximum budget, and a `$0.432165` controller estimate at `$1.99/hour`. | The record expressly denies hardware attestation and provider-invoice proof. It remains `RETROSPECTIVE`, `EXTERNAL_ONLY`, and has no Inferdrome acceptance verdict. |

The record gates passed on this assessment base without raw-archive access:

```bash
PYTHONPATH=src <python-3.12> scripts/review_gpu_evidence_publication.py --check-records
PYTHONPATH=src <python-3.12> scripts/review_qwen3_gpu_evidence_publication.py --check-records
PYTHONPATH=src <python-3.12> scripts/review_qwen3_a100_sxm4_evidence.py --check-records
```

Those commands verify committed structure, canonical cross-digests, and the
claim boundaries. They do not reconstruct a missing raw archive, contact a
provider, or make any live-state assertion.

## Guard and transport controls

The current repository includes a static, GPU-free check for
[`capture_real_gpu_over_ssh.py`](../../scripts/capture_real_gpu_over_ssh.py):

```bash
PYTHONPATH=src <python-3.12> scripts/capture_real_gpu_over_ssh.py --check
```

It validates local assets only. Live/dry-run capture modes separately require
an explicit regular, non-symlink identity file and a raw-byte `known_hosts`
digest pin before any SSH or SCP action. The source denies ambient SSH config
and agent use, and the associated regression tests cover missing identity,
host-pin mismatch, bounded optional key discovery, and signal cleanup.

`src/inferdrome/lambda_gpu_guard.py` is verified by its unit suite to use a
buffered Decimal cost window, require a provider rate match, retain an explicit
unresolved termination state, and keep the watchdog armed when confirmation is
absent. The local capacity-check modes verify only frozen tier/rate policy; they
do not call the provider API:

```bash
PYTHONPATH=src <python-3.12> scripts/watch_lambda_a100_capacity.py --check
PYTHONPATH=src <python-3.12> scripts/watch_lambda_gpu_capacity.py \
  --gpu-tier a100-40gb-sxm4 --check
```

These controls establish safe repository behavior and the historical receipts
show that the guarded controller recorded an `absent` termination result in the
two retained Qwen3 observations. They do not prove that any account currently
has no instance, bill, credential exposure, or network failure.

## Why another paid run is not a v0.1 prerequisite

The relevant v0.1 release requirement is producer-side: a real compatible-host
bundle and its integrity/rejection evidence. It is already marked complete in
the [release checklist](../V0_1_RELEASE_CHECKLIST.md#release-blocking-evidence).
The separately owned release blockers are ExitSpec's prospective `PASS`,
`FAIL`, and `NOT_PROVEN` demonstrations and receipt; archive publication and
owner licensing/privacy approval; human security sign-off; and the final
release commit/tag/CI process. None can be closed by rerunning the retrospective
provider observation.

The latest tracked security audit explicitly limits its repository conclusion:
provider lifecycle, billing, and cleanup controls are not a current provider,
capacity, bill, termination, or cleanup attestation. Its cited green candidate
CI run covers the provider-control code at `c1668e5`; the diff from that commit
to this assessment base adds only the audit report, not provider/GPU/SSH/cost
control code or evidence records.

## Explicit unsupported claims

This closure record must not be used to claim any of the following:

- `NO_CURRENT_PROVIDER_CAPACITY_OR_ACTIVE_INSTANCE_CLAIM`
- `NO_PROVIDER_HARDWARE_ATTESTATION`
- `NO_LIVE_SSH_OR_HOST_LEGITIMACY_VALIDATION`
- `NO_CURRENT_BILLING_OR_FINAL_INVOICE_CLAIM`
- `NO_CURRENT_TERMINATION_OR_ACCOUNT_CLEANUP_CLAIM`
- `NO_RAW_ARCHIVE_PUBLICATION_OR_OWNER_PRIVACY_LICENSE_APPROVAL`
- `NO_EXITSPEC_PASS_FAIL_NOT_PROVEN_OR_RECEIPT_CLAIM`
- `NO_RELEASE_AUTHORIZATION`

If a future change requires one of those claims, it needs a separate bounded
spend authorization and a new immutable, independently reviewed record. That
authorization must specify the provider/GPU/image/workload, runtime and spend
ceiling, preflight and host-pin/identity checks, watchdog and termination
sequence, post-termination billing verification, artifact set, abort
conditions, and the precise claim to close.

## Validation on the assessment base

- All three committed evidence-record gates passed, as shown above.
- `capture_real_gpu_over_ssh.py --check` passed without an SSH, SCP,
  `ssh-keyscan`, provider, GPU, Docker, Kubernetes, or cloud operation.
- The A100, A100 SXM4, and H100 local Lambda capacity-policy checks passed
  without API access.
- `scripts/release_preflight.py --phase candidate --repository-only
  --require-clean` reported `REPOSITORY_READY`; it left the documented external
  owner/manual items pending rather than treating them as passes.
