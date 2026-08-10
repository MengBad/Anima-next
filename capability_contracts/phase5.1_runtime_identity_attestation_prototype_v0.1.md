# Phase 5.1 Runtime Identity Attestation Prototype v0.1

## Status

Development-only prototype. The check is opt-in and runs before Anima runtime
construction when `anima start --identity-attestation <explicit-json-bundle>`
is supplied. With no option, startup behavior is unchanged.

## Composition

The prototype composes the existing read-only verifiers:

1. Identity Continuity Verification;
2. Instance Handoff Verification;
3. Continuity Manifest Verification;
4. Runtime Attestation reference and result consistency checks.

The explicit bundle supplies all artifacts. No implicit Memory file is loaded.
The verifier recomputes the results and compares them with the supplied
attestation result; it does not trust a claimed successful result.

The attestation records both `source_version` and the verified `source_commit`
so a later observation context can report the exact validated project source.

## STOP behavior

The prototype emits a structured result and exits before configuration recovery,
core construction, channel binding, or runtime start when any check fails.
It never repairs, migrates, merges, overwrites, restores, or authorizes an
instance.

## Boundary audit

- no production/VPS connection or deployment;
- no default startup behavior change;
- no Foundation, Lifecycle, CapabilityContract, Identity, Memory, or Handoff
  contract modification;
- no automatic Memory loading, identity recovery, migration, daemon, timer, or
  polling;
- no Agent runtime creation.

## Verification

Offline tests cover valid attestation, identity mismatch, manifest mismatch,
version mismatch, corrupted Memory artifact, input immutability, and the
startup STOP gate. Existing Phase 4.2–4.4 verification tests remain green.
