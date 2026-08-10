# Phase 4.2 Identity Verification Prototype Design v0.1

> Offline and read-only prototype. It validates an Identity Manifest against an
> existing Phase 1 Memory artifact and an explicit project-state snapshot. It
> does not implement Identity Runtime, startup integration, permissions,
> handoff execution, memory migration, or personality behavior.

## Contract boundary

The prototype consumes the existing `MemoryArtifact` contract unchanged. It
adds no Memory fields and does not import chat memory, vector memory, runtime,
Lifecycle, or CapabilityContract paths.

## Identity Manifest

Required fields:

```yaml
schema_version: integer
identity_id: string
lineage_id: string
source_instance: string
source_commit: string
source_version: string
foundation_state: string
identity_artifact_version: string
persona_config_digest: sha256
memory_manifest_digest: sha256
created_at: timestamp
verification_policy:
  required_foundation_state: FROZEN
  require_identity_category: boolean
```

The manifest is strict, serializable, digest-referencing, and contains no
permission or execution fields.

## Verification output

`verify_identity_continuity()` returns:

```yaml
identity_match: boolean
lineage_match: boolean
artifact_integrity: boolean
foundation_compatibility: boolean
errors: [string]
warnings: [string]
stop: boolean
```

Any failed verification rule sets `stop: true`. The verifier does not select a
replacement identity, repair input, merge artifacts, overwrite identity, or
migrate memory.

## Verification rules

- strict manifest fields and schema version;
- required project-state fields;
- ISO-8601 timestamp and SHA-256 digest format;
- identity and source-instance match;
- lineage match;
- source commit and version match;
- persona digest match;
- canonical Memory artifact digest match;
- Phase 1 Memory artifact validation;
- Foundation state agreement and `FROZEN` compatibility;
- optional requirement for at least one `identity` Memory record.

## Implementation boundary

Implementation is isolated in:

```text
anima/memory/identity_verification.py
```

It is not imported by Anima startup or runtime. Tests are isolated in:

```text
tests/test_identity_verification.py
```

No production connection, deployment, daemon, timer, polling, automatic
write, or permission integration is included.
