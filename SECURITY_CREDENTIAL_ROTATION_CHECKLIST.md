# Anima-next Credential Rotation Checklist

> Prepared during Release Hardening. This checklist contains no credential
> value and has not executed any production change.

## Scope

- [x] Identify the exposed credential class: VPS deployment/diagnostic SSH credential.
- [x] Remove credential-bearing diagnostic files from the release source and
      local reachable Git history.
- [x] Confirm the current release plan does not ship diagnostic SSH probes.
- [ ] Generate a new credential outside Git, chat, and shell history.
- [ ] Apply the new credential to the VPS access control plane.
- [ ] Disable or revoke the old credential.
- [ ] Update the controlled deployment secret source.
- [ ] Verify deployment access with the new credential only.
- [ ] Verify the old credential is rejected.
- [ ] Review SSH sessions, authorized keys, and recent authentication logs.
- [ ] Record rotation operator, timestamp, target, and verification evidence.

## Release gate

Production release remains blocked until every unchecked production action above
is completed and independently evidenced. Rotation must be performed as a
separate security operation; it is not part of a source deployment commit.

## Current status

```text
Source remediation: COMPLETE
History remediation: COMPLETE locally
Production rotation: NOT EXECUTED
Production release authorization: NOT READY
```
