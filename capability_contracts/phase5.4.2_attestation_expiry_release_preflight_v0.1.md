# Phase 5.4.2 Attestation Expiry & Release Preflight v0.1

## Status

Offline-only validation. No VPS, systemd, production configuration, or
credential access is involved.

## Expiry contract

Runtime Attestation now requires `expires_at`, an ISO-8601 timestamp with an
explicit timezone. The verifier accepts an injectable clock for deterministic
tests and passes only when `now < expires_at`. Exact expiry, expired, missing,
malformed, or timezone-naive values STOP. No network time, renewal, or bundle
generation is used.

## Release preflight contract

`verify_release_preflight()` validates an explicit proof containing:

- Release Candidate commit, archive SHA256, and bundle SHA256;
- complete Runtime Attestation bundle;
- previous release commit/archive and rollback procedure reference.

It recomputes the archive and bundle digests, composes the Identity/Handoff/
Continuity/Attestation validators, proves:

```text
Release commit = Attestation source_commit
              = Continuity Manifest source_commit
              = project state source_commit
```

It also requires `FROZEN` Foundation compatibility, synthetic-only Memory
provenance, bundle security cleanliness, and complete rollback metadata.

Any mismatch returns `stop: true`; no input is changed and no replacement or
repair is attempted.

## Security boundary

The preflight rejects credential material, credential-like fields, executable
payload fields, capability-permission fields, and non-synthetic Memory
provenance. The bundle is not a Git artifact and contains no deployment secret.

## Non-goals

No production deployment, SSH, systemd change, config mutation, Memory schema
change, Foundation/Lifecycle/CapabilityContract change, daemon, timer, polling,
automatic recovery, renewal, or migration.
