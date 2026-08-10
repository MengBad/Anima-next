# Phase 4.4 Continuity Manifest Design v0.1

## Status

Offline design and verification only. This document defines a reference envelope
for continuity checks; it does not introduce a runtime handoff or migration
mechanism.

## Contract

`ContinuityManifest` contains exactly:

- `schema_version`, `manifest_id`, `created_at`
- `identity_reference`: `identity_id`, `lineage_id`, `artifact_version`, `digest`
- `memory_artifact_reference`: `artifact_id`, `digest`, `schema_version`, `source_lineage_id`
- `project_state_reference`: `source_commit`, `source_version`
- `foundation_state`
- `verification_metadata`: `verifier_version`, `verified_at`, `policy`
- `handoff_reference`: `handoff_id`, `verification_id`
- `authorization_reference`

The manifest carries references and digests only. It contains no Memory record
content, permissions, execution capability, or replacement for the Identity,
Memory, or Handoff contracts.

## Verification contract

`verify_continuity_manifest()` accepts an explicit manifest, Identity artifact,
Memory artifact, prior Handoff verification result, project state snapshot, and
an optional expected authorization reference. It is pure/read-only and returns
structured booleans, errors, warnings, and `stop`.

The verifier checks schema and required fields, Identity artifact validity and
digest/reference consistency, Memory digest consistency, successful Handoff
verification, project version consistency, `FROZEN` Foundation compatibility,
and presence/match of the authorization reference.

Any failed check returns `stop: true`. The verifier never repairs, fills,
selects, merges, migrates, overwrites, or writes an artifact.

## Explicit non-goals and boundary

- no startup/runtime integration;
- no VPS access or deployment;
- no changes to Memory schema, Foundation, Lifecycle, or CapabilityContract;
- no automatic identity generation, Memory modification, migration, daemon,
  timer, polling, or permission transfer.

## Acceptance cases

The offline tests cover a valid manifest and STOP results for missing/invalid
Identity, Memory mismatch, invalid Handoff result, project version mismatch,
Foundation mismatch, and authorization-reference mismatch.
