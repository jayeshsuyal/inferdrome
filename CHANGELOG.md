# Inferdrome changelog

## 0.2.0 — release candidate (unpublished)

Status: **Prepared for review; not tagged or published**

This release candidate is a package/repository identity only. It does not
authorize an annotated `v0.2.0` tag, a GitHub Release, a provider operation, or
evidence publication. Those remain separate exact-commit-bound human actions.

### Capability table

The canonical capability/limitations contract is
[the v0.2 capability and limitations contract](docs/V0_2_CAPABILITIES.md).
Inspect its exact machine-readable form with `python -m inferdrome capabilities`.
This changelog deliberately does not widen that contract.

| Capability | Release-candidate status | Honest boundary |
| --- | --- | --- |
| Local routing-evidence prototype | `PROVEN_LOCAL_SOCKET_LEVEL` | Two loopback endpoints, sealed package, and offline replay are proven locally. |
| Historical Qwen3-8B A10 records | `PRESERVED_EXTERNAL_ONLY` | Privacy-safe metadata is retained; raw archives remain `EXTERNAL_ONLY`. |
| Two-A100 multi-endpoint campaign | `UNEXECUTED` | No same-host or distributed real campaign evidence exists. |
| GCP operation | `LOCAL_FAKE_VALIDATED` | Guarded lifecycle behavior is locally fake-validated; no production operation is claimed. |
| Kubernetes operation | `NOT_CLAIMED` | No cluster, GPU workload, or production operation is claimed. |

Inferdrome remains a measurement/evidence plane. It does not provide a
production router, policy verdict, promotion control, or acceptance authority.

### Included in the candidate

- The one-command, deterministic local routing-evidence walkthrough and its
  verified dashboard explanation.
- A closed capability/limitations contract exposed through
  `python -m inferdrome capabilities`.
- Final `0.2.0` Python, lockfile, and runner-image default identities, plus
  isolated wheel install/import/CLI validation.

### Verification and release boundary

From a clean candidate checkout, run:

```bash
python scripts/release_preflight.py \
  --phase final-pre-tag --repository-only --require-clean
```

This confirms repository-owned final-pre-tag conditions and reports, rather
than satisfies, required human review/authorization inputs. It never creates a
tag or release. The current candidate must still receive parent review, merge,
post-merge verification, and a separate exact-commit-bound authorization before
any annotated tag or GitHub Release is created.
