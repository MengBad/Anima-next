# Phase 5.3 Runtime Instance Binding v0.1

## Status

Development-only implementation. A successful, explicit Runtime Identity
Attestation may create one immutable observation context and pass it into
`AnimaCore`. Without the opt-in attestation argument, Core construction is
unchanged and the context is `None`.

## Binding rule

The context is created only after the existing Identity, Handoff, Continuity
Manifest, Memory digest, version, and Foundation checks return a successful
attestation. It contains:

- `instance_id`
- `identity_id`
- `lineage_id`
- `source_commit`
- `source_version`
- `attestation_status = PASS`

The source values are copied from the verified Attestation and its verified
reference chain. No runtime inference or fallback identity is performed.

## Read-only boundary

`RuntimeIdentityContext` is a frozen value object. `AnimaCore` exposes it via a
read-only property for diagnostics and observation. It is not consulted by
capability authorization, permissions, lifecycle transitions, memory writes,
recovery, or migration.

If any verification fails, the binding result is `None` and the CLI stops before
configuration loading and Core construction.

## Verification

Synthetic tests cover valid Core binding, identity/manifest/version/handoff/
Memory failures, explicit instance-id mismatch, context immutability, and the
existing startup STOP gate.

## Non-goals

No production connection, runtime daemon, timer, polling, automatic recovery,
Memory writer, schema change, capability permission change, Foundation change,
Lifecycle change, or CapabilityContract change.
