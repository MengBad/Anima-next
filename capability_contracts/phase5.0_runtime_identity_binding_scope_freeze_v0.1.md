# Phase 5.0 Runtime Identity Binding Design v0.1

## Status

Scope Freeze and contract design only. This artifact defines an offline
attestation boundary between an explicitly supplied runtime-instance snapshot
and a previously verified Continuity Manifest. It does not bind the live
runtime, alter startup, or perform handoff.

## Runtime Instance Attestation Contract

The attestation is a strict, serializable record containing exactly:

```text
schema_version
attestation_id
instance_id
identity_reference
continuity_manifest_reference
source_version
foundation_state
verification_timestamp
verification_result
```

`identity_reference` contains only the expected `identity_id`, `lineage_id`,
and identity-artifact digest. `continuity_manifest_reference` contains only the
expected `manifest_id` and manifest digest. Neither reference embeds Memory
content, permissions, credentials, or execution capability.

`verification_result` is an attestation outcome, not an authorization grant:

```text
identity_match
manifest_match
version_match
foundation_compatible
memory_continuity_verified
handoff_verified
errors
warnings
stop
```

The result is valid only when all six checks are true, `errors` is empty, and
`stop` is false. Any malformed or contradictory input produces `stop: true`.

## Verification rules

An offline verifier, to be defined in a later authorized implementation slice,
must check:

1. attestation schema and required fields;
2. instance identity reference against the supplied Identity artifact;
3. Continuity Manifest identity reference and digest;
4. source version against the manifest and read-only project snapshot;
5. `FROZEN` Foundation compatibility;
6. the explicit Memory continuity verification result;
7. the explicit Handoff verification result;
8. artifact digest and structural integrity.

The verifier must not infer a replacement identity or manifest. It must not
load implicit files, select a latest artifact, merge artifacts, or mutate any
input. Verification failure is a STOP result and requires explicit review.

## Boundary and non-goals

- no runtime lifecycle or startup integration;
- no automatic Memory loading or identity recovery;
- no migration, persistence, or artifact repair;
- no changes to Identity, Memory, Handoff, Foundation, Lifecycle, or
  CapabilityContract schemas;
- no permission transfer or execution authorization;
- no VPS access, deployment, daemon, timer, polling, or background service;
- no Agent runtime creation.

## Acceptance cases

The future offline verification slice must cover:

- a valid runtime attestation;
- identity mismatch;
- Continuity Manifest mismatch;
- source-version mismatch;
- corrupted or structurally invalid referenced artifact;
- failed Memory continuity result;
- failed Handoff verification result;
- Foundation incompatibility.

## Scope decision

This phase establishes a verification vocabulary only. It does not demonstrate
that the current Foundation requires a runtime binding layer, and it does not
authorize implementation or production use. A later implementation request
must separately prove that the offline contract can be implemented without
changing the frozen contracts above.
