# Inferdrome v0.2 release checklist

Status: **No v0.2 release is currently authorized.**

This checklist applies only to a future v0.2 release. It preserves the frozen
v0.1 release history and cannot be completed by importing, copying, or relying
on a v0.1 approval, review, evidence record, or exception. See the separate
[v0.2 release inputs](V0_2_RELEASE_INPUTS.md) for the exact record inputs.

All v0.2 release checkboxes below are deliberately unchecked until their named
owners record the current-series facts for the exact candidate commit.

## Manual v0.2 release inputs

- [ ] Review the exact v0.2 release scope and deferred boundaries.
- [ ] Complete an independent v0.2 GCP safety and cleanup review.
- [ ] Record explicit v0.2 release authorization for the exact release commit and annotated tag.

The offline preflight reports these entries but cannot verify them. A checked
box is a pointer to an external human record, not a signature, provider
approval, or proof that a local content digest was authorized.

## Machine checks

The active v0.2 preflight requires the exact `0.2.0.dev0` development version
for candidate work and the exact `0.2.0` version plus `v0.2.0` annotated-tag
state for final phases. It also checks the repository license, locked Python
dependencies, active-series release input/checklist structure, claim
boundaries, CI inventory, and clean-worktree policy. It retains a bounded
historical v0.1 capture-ancestry check as lineage integrity only; that check
does not make v0.1 evidence or approvals into v0.2 release inputs.

The preflight is local and fail-closed. It never creates a tag, contacts a
provider, accesses credentials, launches a GPU, or publishes an artifact.
