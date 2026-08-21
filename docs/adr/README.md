# Architecture decision records

Architecture decision records capture decisions that shape Inferdrome's public
claims, trust boundaries, or long-lived interfaces.

## Status values

- `Proposed`: under active review;
- `Accepted`: normative for the stated release;
- `Superseded`: replaced by a newer ADR; or
- `Rejected`: considered and intentionally not adopted.

Accepted ADRs are not edited to reverse their decision. Material changes require
a new ADR that supersedes the old one.

## v0.1 decisions

- [ADR 0001: Separate measurement from customer acceptance](0001-separate-measurement-from-acceptance.md)
- [ADR 0002: Use a pinned external benchmark producer first](0002-use-pinned-external-benchmark-producer.md)
- [ADR 0003: Preserve native output and publish canonical records](0003-native-output-and-canonical-records.md)
- [ADR 0004: Claim integrity, not execution truth or authorship](0004-integrity-not-execution-attestation.md)
- [ADR 0005: Freeze public schemas after the capability spike](0005-schema-freeze-after-capability-spike.md)

## Post-v0.1 decisions

- [ADR 0006: Add a local read-only evidence dashboard](0006-local-read-only-evidence-dashboard.md)
- [ADR 0007: Add immutable descriptive Trial Sets](0007-add-immutable-descriptive-trial-sets.md)
- [ADR 0008: Add operator-attested controlled comparisons](0008-add-operator-attested-controlled-comparisons.md)
- [ADR 0009: Add fail-closed controlled-comparison execution](0009-add-fail-closed-comparison-execution.md)
- [ADR 0010: Freeze a two-track cross-GPU capability campaign](0010-freeze-cross-gpu-capability-campaign.md)
