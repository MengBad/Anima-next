# Phase 4.3 Instance Handoff Verification Contract v0.1

> Offline, read-only verification only. This contract does not migrate,
> copy, merge, overwrite, authorize, start, or deploy an Anima instance.

## Source Instance

```yaml
source_identity_id: string
source_lineage_id: string
source_version: string
source_manifest_digest: sha256
```

## Target Instance

```yaml
target_identity_id: string
target_lineage_id: string
target_environment: local | development | staging
```

Production is intentionally outside this offline prototype's eligible target
environments. No production connection is attempted.

## Handoff Artifact

```yaml
identity_manifest: object
memory_artifact_manifest: object
memory_artifact: object
project_state_reference: object
authorization_record: object
verification_result: object
```

The raw `memory_artifact` is supplied only as read-only verification input; it
is never written or migrated.

### Memory artifact manifest

```yaml
artifact_id: string
schema_version: integer
memory_manifest_digest: sha256
source_instance: string
source_lineage_id: string
provenance_refs: [string]
created_at: timestamp
```

### Authorization record

The prototype requires an explicit, approved, lineage-bound record:

```yaml
authorization_id: string
status: approved
source_lineage_id: string
target_environment: local | development | staging
```

This verifies presence and scope evidence only. It does not change the existing
Operator permission model.

## Verification output

`verify_instance_handoff()` returns:

```yaml
source_identity_valid: boolean
target_eligible: boolean
identity_manifest_consistent: boolean
memory_digest_consistent: boolean
artifact_provenance_complete: boolean
authorization_present: boolean
foundation_compatible: boolean
errors: [string]
warnings: [string]
stop: boolean
```

Any failed rule sets `stop: true`. No automatic fallback or repair is allowed.

## Verification rules

- source manifest identity, lineage, version, and digest must match;
- target identity and lineage must match the source lineage;
- target environment must be offline-eligible;
- identity manifest and memory manifest must bind the same source;
- actual Memory artifact digest must match both manifests;
- provenance references must be non-empty;
- source project state must pass Phase 4.2 Identity Verification;
- authorization must exist, be approved, and bind lineage/environment;
- Foundation state must remain `FROZEN` compatible;
- supplied prior verification result is treated as untrusted input and
  recomputed.

## STOP conditions

STOP on any mismatch, missing artifact, invalid provenance, missing
authorization, version mismatch, foundation mismatch, or invalid Memory
artifact. The verifier must not choose another source, merge artifacts,
transfer permissions, modify data, or start an instance.
