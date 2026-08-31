# v0.1 human security-review approval record

**Record date:** 2026-08-31

**Reviewer:** Jayesh Suyal

**Reviewed repository commit:**
`2d0f857b3bddea1870671d01bb04e9fe1bb1282e` (`main` at review)

## Exact approval

> “I, Jayesh Suyal, reviewed Inferdrome’s threat model and final security audit at commit `2d0f857b3bddea1870671d01bb04e9fe1bb1282e` on 2026-08-31. I approve this security boundary for the v0.1 release, subject to the limitations recorded in those documents.”

## Reviewed-document identity binding

This approval applies only to the following exact repository objects as they
were reachable from the reviewed commit above. Git object IDs identify the
tracked blobs; the byte count and SHA-256 identify the file contents
independently of the Git object format.

| Reviewed document | Git mode and blob at `2d0f857b3bddea1870671d01bb04e9fe1bb1282e` | Bytes | SHA-256 of exact bytes |
| --- | --- | ---: | --- |
| [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md) | `100644` / `8911858b21184ee33691eb2920c51d2550e5f7f0` | 19,490 | `51670262dbea2d8cf271eaac998e7b7db70d8d9a36d84c85b02ada695d07d523` |
| [`docs/reviews/V0_1_MODEL_ASSISTED_SECURITY_AUDIT.md`](V0_1_MODEL_ASSISTED_SECURITY_AUDIT.md) | `100644` / `2d9b3ada7d29c9389ca6f0623244a3c98556b1ad` | 26,963 | `8d9cd6615516b99b3d806e2e89c39b77ab4bdbbaeb6d44b3adb3414a6009dd82` |

The cited commit is the current `origin/main` baseline when this record was
created and contains both objects above. This record itself was not part of the
reviewed content. Any later change to either reviewed document is outside this
approval unless the reviewer records a new approval binding the changed bytes.

## Approval scope and continuing limitations

This is the named human security-review approval required by the v0.1 release
checklist. It accepts the documented security boundary, including the final
audit's repository-finding disposition, only subject to the limitations in the
two identified documents. It is not a blanket release authorization, legal
opinion, privacy or licensing clearance, provider attestation, penetration
test, or ExitSpec decision.

In particular, this approval does not alter any of the following:

- ExitSpec remains independently owned: prospective `PASS`, `FAIL`, and
  `NOT_PROVEN` demonstrations and the ingestion receipt digest remain required.
- The raw A10 archive remains `KEEP_EXTERNAL_ONLY`, `EXTERNAL_ONLY`, ignored,
  and unpublished. This approval grants no privacy, licensing, redistribution,
  publication, or external-material right for that archive or any derivative.
- Provider, SSH, GPU, Docker, Kubernetes, cloud, billing, termination, and
  cost-cleanup evidence remain operational matters requiring actual evidence;
  no live operation or current external state is established by this record.
- The version must still transition from `0.1.0.dev0` to `0.1.0` on a separate
  release commit. Final-pre-tag PR and `main` CI, an authorized annotated
  `v0.1.0` tag, post-tag verification, and a GitHub Release or other external
  immutable release record remain undone.

The model-assisted audit remains model-assisted evidence; this record is the
human approval of its documented boundary and limitations, not a claim that
automation or the model performed human sign-off.
