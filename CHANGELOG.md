# Inferdrome changelog

## Unreleased — 0.3.0.dev0

This development entry starts the next additive evidence cycle. It is not a
tag, a publication record, an execution authorization, or a claim that a live
router or campaign has run.

### Attached external-router evidence

- Adds the provider-neutral `external_router` namespace and its first concrete
  `llm-d-attached-v1` local-fixture profile.
- Captures only digested router config and logical endpoint identity alongside
  request/correlation, candidate, observation, decision, and terminal-outcome
  facts. It never retains origins, prompts, model outputs, headers, or raw
  router payloads.
- Requires bound topology and monotonic observation age/epoch facts, makes
  stale or unavailable signals explicit, and never invents a policy or reason.
- Emits canonical digest-bound receipts that can be independently verified
  offline. It does not embed, replace, or claim a native llm-d API.

## 0.2.0

This release records the repository and package identity for Inferdrome v0.2.0.
Repository source never self-authorizes an annotated `v0.2.0` tag, a GitHub
Release, a provider operation, or evidence publication. If an annotated tag or
GitHub Release is published, its external identity is established by separately
authorized, exact-commit-bound human release actions.

### Capability table

The canonical capability/limitations contract is
[the v0.2 capability and limitations contract](docs/V0_2_CAPABILITIES.md).
Inspect its exact machine-readable form with `python -m inferdrome capabilities`.
This changelog deliberately does not widen that contract.

| Capability | v0.2.0 status | Honest boundary |
| --- | --- | --- |
| Local routing-evidence prototype | `PROVEN_LOCAL_SOCKET_LEVEL` | Two loopback endpoints, sealed package, and offline replay are proven locally. |
| Historical Qwen3-8B A10 records | `PRESERVED_EXTERNAL_ONLY` | Privacy-safe metadata is retained; raw archives remain `EXTERNAL_ONLY`. |
| Two-A100 multi-endpoint campaign | `UNEXECUTED` | No same-host or distributed real campaign evidence exists. |
| GCP operation | `LOCAL_FAKE_VALIDATED` | Guarded lifecycle behavior is locally fake-validated; no production operation is claimed. |
| Kubernetes operation | `NOT_CLAIMED` | No cluster, GPU workload, or production operation is claimed. |

Inferdrome remains a measurement/evidence plane. It does not provide a
production router, policy verdict, promotion control, or acceptance authority.

### Included in v0.2.0

- The one-command, deterministic local routing-evidence walkthrough and its
  verified dashboard explanation.
- A closed capability/limitations contract exposed through
  `python -m inferdrome capabilities`.
- Final `0.2.0` Python, lockfile, and runner-image default identities, plus
  isolated wheel install/import/CLI validation.

### Verification and release boundary

For repository-owned pre-tag verification, run:

```bash
python scripts/release_preflight.py \
  --phase final-pre-tag --repository-only --require-clean
```

This checks repository-owned final-pre-tag conditions and reports, rather than
satisfies, required human review/authorization inputs. It never creates a tag
or release. Tag and GitHub Release identities, if published, remain external
exact-commit-bound human actions; this source does not self-authorize them.
