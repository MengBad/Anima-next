# `code.execute` Pilot Capability Contract — Draft v0.2

> **STATUS: DRAFT — NOT IN REGISTRY**
> This document is a **specification-only** contract for the future `code.execute`
> capability. It creates **no `capability_id`**, registers **no provider**, modifies
> **no capability index**, and touches **no frozen boundary** (`ebab801`, `filesystem.read|write`,
> `RFC-0001`, `ARCHITECTURE`). No implementation code is written.
>
> - Draft version: `0.2`
> - Date: 2026-08-06
> - Author: Senior Developer (anima-next hardening track)
> - Supersedes: `v0.1` (initial draft)
> - Parent phase: `M0.4.3` Phase 1 Re-audit = **FINAL PASS** (post `d9e16ed`)
> - Review phase: `M0.4.4 code.execute Pilot Contract Review` = **CONDITIONAL APPROVAL**
>   → both findings closed in v0.2 (see §15)
> - Next phase: `Capability ID Approval` (pending)

### Revision History

| Ver | Date | Change |
|---|---|---|
| v0.1 | 2026-08-06 | Initial contract draft. Captured all 11 boundary clauses from `M0.4.3` re-audit (post `736b571` S1–S5 + `d9e16ed` R1). |
| v0.2 | 2026-08-06 | `M0.4.4` Review → CONDITIONAL APPROVAL. Two findings closed: **(F1)** Audit Event Taxonomy normalized (§11 normative; legacy `execution_scope_denied` must be normalized to `execution_denied{reason}` at provider boundary). **(F2)** Provider Schema Boundary mandated (§6.1): schema validation at provider boundary, raw LLM-args→handler pattern forbidden, unknown/forbidden fields rejected not ignored. §13/§14/§15/§17 updated to reflect alignment. |

---

## 1. Capability Identity

| Field | Value |
|---|---|
| `capability_id` | `code.execute` *(RESERVED — must NOT enter registry/index yet)* |
| `kind` | execution / runtime |
| `layer` | Capability Layer (`RFC-0001`) — a specific capability, not a new layer |
| `status` | DRAFT |
| `replaces_legacy` | wraps existing `run_python` / `run_shell` / `agent_exec` substrate; does **not** reimplement |

The reserved id `code.execute` is documented here only. It enters the capability
index **only after** `M0.4.4` Review closure and explicit Capability ID Approval.

---

## 2. Purpose

Encapsulate the already-hardened execution substrate as a first-class, contract-driven
capability so that future execution features are *declared*, *authorized*, and *audited*
by contract rather than by implicit legacy tool wiring.

```
User Intent
    │
    ▼
Capability Contract (this doc)
    │
    ▼
Execution Authorization (double gate)
    │
    ▼
Sandboxed Runtime (hardened substrate)
    │
    ▼
Audit Event
```

**NOT** (the forbidden mental model):

```
LLM  ──►  python subprocess
```

### Non-goals (explicit)
- Not a new execution engine. The substrate already exists and is verified.
- Not a replacement of the legacy execution path. Capability = wrapper adapter.
- Not a filesystem capability. Does **not** inherit `filesystem.read` / `filesystem.write`.

---

## 3. Trust Model

### 3.1 Identity source (authorization subject)
```
QQ UID
   │
   ▼
PermissionStore
   │
   ▼
trusted execution context (ctx.permission_level)
```

The **only** legitimate authorization subject is the resolved `QQ UID → PermissionStore → permission_level`.
Group membership and chat metadata are **context only**, never authorization.

### 3.2 Forbidden as execution-authorization inputs
- `QQ group owner`
- `QQ group admin`
- `sender.role` / any chat-role field
- any chat metadata beyond the resolved UID

**Verified substrate anchor:** `anima/pipeline/stages/permission.py::_check_permission(user_id, group_id, config)`
takes **no `sender_role`**; `permission_stage` writes group role only as
`ctx.meta["group_sender_role"]` (context key, never flows back into `permission_level`).

---

## 4. Authorization Contract (double gate)

Execution is permitted **iff both gates pass**. Failure of either → deny + audit.

```
Gate A:  ctx.permission_level  >=  tool.required_level
                    AND
Gate B:  tool.granted_scopes["exec"]  ==  true
```

- **Gate A** — caller permission (chat/owner tier): enforced at
  `anima/tools/__init__.py::ToolRegistry.call` via `can_call(min_level)`.
- **Gate B** — execution scope: enforced at `ToolRegistry._invoke` for the explicit
  `EXECUTION_TOOLS` set (`{"run_python","run_shell","agent_exec"}`) via `exec_permitted()`.

| Failure | Canonical result (v0.2 normalized) |
|---|---|
| Gate A fails (level too low) | deny, audit `execution_denied {reason: insufficient_permission}` |
| Gate B fails (no exec scope) | deny, audit `execution_denied {reason: missing_exec_scope}` |
| Both pass | execute, audit `execution_started` → `execution_finished\|failed` |

Neither gate can substitute for the other:
- Missing exec scope → denied even for an owner.
- Exec scope alone → does **not** elevate a low-permission caller (Gate A still applies).

> **v0.2 clarification (M0.4.4 Gate 2 finding):** `exec` scope is an *execution authorization*,
> **not** a caller-identity elevation. `granted_scopes["exec"] == true` MUST NOT be interpreted
> as, or mapped to, owner tier. Identity (and thus sandbox tier) is resolved solely from `ctx`
> per §3/§5. A future provider MUST NOT derive `sandbox_tier` from scope presence.

**Verified substrate anchor:** `anima/tools/__init__.py::_invoke` computes
`exec_meta` and short-circuits when `tool.name in EXECUTION_TOOLS and not exec_permitted(tool.granted_scopes)`.
Legacy exec tools carry `granted_scopes={"exec": True}` (`builtin/__init__.py`, `agent_tools.py`).

---

## 5. Sandbox Authority Contract (R1 hard constraint)

> **Sandbox tier is ALWAYS derived from the trusted execution context. Never from tool arguments or LLM output.**

```
ctx.permission_level
   │
   ▼
sandbox_tier_for(level)            # strict min(level, 4)
   │
   ▼
sandbox policy (network / fs_confined / isolation)
```

The following MUST be rejected/ignored as inputs to sandbox tier selection:

```json
{ "permission_level": 4 }          // privilege-parameter injection — FORBIDDEN
{ "network": true }
{ "filesystem": "unrestricted" }
```

**Verified substrate anchor (R1 fix, commit `d9e16ed`):**
`ToolRegistry._invoke` now does `args = {**args, "permission_level": ctx.get("permission_level", LEVEL_USER)}`
— the trusted `ctx` value **unconditionally overwrites** any caller-supplied
`permission_level`. `sandbox_tier_for` maps `min(level,4)` so a non-owner can never
legitimately reach `SandboxTier.OWNER`. `run_python` tool schema is `{"code": {...}}` only
(no `permission_level` param exposed).

**This is the single most important contract clause carried from the re-audit.** R1 was
the last open Sandbox-Boundary defect; its fix is the precondition for this contract's validity.

> **v0.2 clarification (M0.4.4 Gate 6 finding):** `owner` tier is an *identity property*
> (resolved from a trusted owner `UID`), **not** a capability parameter. It cannot be requested,
> elevated-to, or conveyed via any capability input. Any provider that accepts an owner-tier
> request as a payload field re-opens the R1 class of vulnerability and is non-compliant.

---

## 6. Input Schema Contract (allowlist)

Tool input is **allowlist-only**. No implicit/undefined parameters are honored.

Allowed:
```json
{ "code": "print(1)" }
```

Forbidden (caller-supplied authorization/escalation fields):
```json
{ "code": "...", "permission_level": 4, "network": true, "filesystem": "unrestricted" }
```

Rationale: **caller arguments are not an authorization载体.** Authorization is resolved
solely from `ctx`, never from the payload.

**Verified substrate anchor:** `run_python` registration schema = `{"code": {type:string}}`.
The R1 regression tests assert the schema excludes `permission_level` and that an injected
unknown field does not alter the resolved tier.

### 6.1 Provider Schema Boundary (M0.4.4 finding #2 — mandated in v0.2)

This clause closes the residual risk that a future provider re-introduces the legacy
`raw LLM args → handler` pattern and silently re-opens R1-style injection.

1. **Validation location.** Schema validation occurs at the **provider boundary**, *before*
   the handler executes. It is **not** the handler's responsibility to ignore unknown fields.
2. **Forbidden legacy pattern.** The legacy flow
   ```
   react.py: LLM JSON  ──►  args  ──►  handler
   ```
   MUST NOT be copied into the `code.execute` provider. The provider MUST NOT pass
   unvalidated caller payload through to execution.
3. **Reject, don't ignore.** Any incoming payload containing an unknown key, or any key on the
   forbidden list (`permission_level`, `network`, `filesystem`, or any non-allowlisted field),
   MUST be **rejected** with `execution_denied {reason: schema_violation}` + audit. Silently
   dropping unknown fields is **forbidden** — rejection is mandatory so injection attempts are
   visible and denied, not silently neutralized-and-forgotten.
4. **Effect.** After this boundary, the handler receives only validated `code` (plus any
   explicitly contract-approved optional fields introduced by future amendments). Sandbox tier
   and authorization are never read from the payload (see §3/§4/§5).

---

## 7. Scope Contract

```yaml
required_scopes:
  - exec
optional_scopes:        # pilot = none active
  - filesystem.workspace.write
  - network.outbound
  - package.install
```

- Pilot scope = **`exec` only**.
- `code.execute` does **NOT** inherit `filesystem.read` or `filesystem.write`.
  Those remain separate capabilities with their own grants.
- Future scope expansion requires a new contract amendment, not a silent addition.

**Verified substrate anchor:** `anima/capability/scopes.py::exec_permitted(scopes)`
returns `bool(scopes.exec)`; `PermissionScopes.exec` defaults `False`. Now a live gate
(post `736b571` S5), not a no-op.

---

## 8. Filesystem Policy

Default: **workspace-confined**.

| Allowed | Denied (deny-markers take precedence) |
|---|---|
| `sandbox_workspace/` | `~/.ssh`, `.env`, `id_rsa`/`id_ed25519`, `.pem`/`.key`/`.p12`/`.pfx` |
| runtime temp files | `secrets`, `credential`, `config.yaml`/`yml`/`json` |
| stdlib / site-packages (read) | `.git`, `.netrc`, `shadow`, `authorized_keys`, `known_hosts` |
| | `.aws`, `.kube`, `.docker`, `token`, `.npmrc`, `.pypirc` |

- **Owner-tier privilege** (`fs_confined=False`, network allowed) is reachable **only** via a
  trusted owner `UID` resolved through `PermissionStore` — never via tool args, LLM output,
  or group role (see §5 clarification).
- User(2)/Admin(3) tiers are confined to workspace + Python runtime roots.

**Verified substrate anchor:** `anima/tools/sandbox.py::_FS_GUARD_BLOCK` wraps
`open`/`io.open`/`os.open`/`listdir`/`scandir`/`remove`/`unlink`/`rename`/`replace`/`mkdir`/
`makedirs`/`rmdir`; `FS_DENY_MARKERS` precedence; `SandboxTier.OWNER.fs_confined=False` by design.

---

## 9. Network Policy

Default: **`network = false`**.

- Current pilot: no network egress from the sandbox.
- Network egress becomes available **only** if a future contract amendment declares
  `required_scope: network.outbound` AND the provider enforces it.
- Owner-tier `agent_exec` (separate tool, separate contract surface) is the only
  network-capable path and remains owner-only via Gate A.
- **Pilot v1 forbids dynamic network elevation.** `{"allow_network": true}` as a runtime input
  is prohibited (covered by §6.1 schema rejection).

---

## 10. Secret Policy

- Execution environment is built from an **explicit allowlist** — **no `os.environ` passthrough**.
- Sensitive-named keys are stripped even if explicitly passed.
- Audit MUST NOT record: env contents, secret values, tokens, credentials.

**Verified substrate anchor:** `anima/tools/sandbox.py::build_sandbox_env()` +
`SAFE_ENV_ALLOWLIST` + `_SENSITIVE_ENV_MARKERS`; used by `run_python` and `run_shell`.
Hardening test asserts no injected API key reaches `os.environ` inside the sandbox.

---

## 11. Audit Contract (normative event taxonomy)

Every execution lifecycle emits structured events. **The event taxonomy below is normative:**
the provider MUST emit exactly these four canonical event names with the required fields, and
MUST NOT surface legacy/raw event names to the audit stream.

### Canonical event table

| Event | Required fields | Emitted when |
|---|---|---|
| `execution_started` | `actor`, `tool`, `permission_level`, `sandbox_tier`, `payload_hash` | gates pass, execution begins |
| `execution_finished` | `duration_ms`, `success: true` | execution completes successfully |
| `execution_failed` | `error_class`, `duration_ms` | execution raises |
| `execution_denied` | `reason ∈ {insufficient_permission, missing_exec_scope, schema_violation}` | either gate fails, or schema rejected |

### Field rules
- `actor` = resolved QQ UID (never a role/group).
- `permission_level` / `sandbox_tier` = **trusted `ctx` values only** (post-R1), never caller args.
- `payload_hash` = `sha256(payload)[:16]`.
- Audit MUST NOT record env contents, secret values, tokens, or credentials (see §10).

### M0.4.4 finding #1 — event normalization (mandated in v0.2)

> The legacy substrate currently emits `execution_scope_denied` for Gate B failure, and Gate A
> failure returns via `can_call` without a dedicated `execution_*` event. **This is a delta, not
> a compliant state.** The `code.execute` provider MUST normalize before emission:
> - Legacy `execution_scope_denied` → `execution_denied {reason: "missing_exec_scope"}`.
> - Gate A denial → `execution_denied {reason: "insufficient_permission"}` (must now be emitted).
> - Provider MUST NOT write `execution_scope_denied` (or any legacy name) to the audit stream.

**Verified substrate anchor:** `anima/tools/__init__.py::_execution_audit_meta` +
emits `execution_started`/`finished`/`failed`; deny path currently emits `execution_scope_denied`.
Audit `permission_level`/`sandbox_tier` read **only** from trusted `ctx` (post-R1), so audit
cannot be forged. The normalization above is the single remaining contract/substrate alignment
item and is listed as Delta 1 (RESOLVED-at-contract-level in v0.2; implementation action tracked).

---

## 12. Migration Boundary

```
legacy tools (run_python / run_shell / agent_exec)
        │   (already on hardened substrate)
        ▼
hardened execution substrate
        │
        ▼  (future)
code.execute capability  ──►  provider adapter  ──►  same hardened substrate
```

Principle: **a Capability is a wrapper/adapter layer, not a re-implementation of the
execution system.** The substrate built across `736b571` (S1–S5) + `d9e16ed` (R1) is the
single runtime; `code.execute` declares intent+authorization on top of it.

---

## 13. Pilot Entry Criteria (re-confirm before implementation)

| Item | Requirement | Status (post `d9e16ed` + v0.2) |
|---|---|---|
| Identity | UID-only | ✅ PASS |
| Permission | double gate (A+B) | ✅ PASS |
| Sandbox | ctx-only authority (R1) | ✅ PASS |
| Secret | env isolated | ✅ PASS |
| Filesystem | confined | ✅ PASS |
| Network | `false` by default, no dynamic elevation | ✅ PASS |
| Audit | normalized event taxonomy (v0.2 §11) | ✅ PASS (was CONDITIONAL) |
| Schema Boundary | provider-side validation, reject unknown (v0.2 §6.1) | ✅ PASS (was CONDITIONAL) |
| Scope | `exec` required | ✅ PASS |
| Capability | contract exists **before** provider | ✅ DRAFT v0.2 (this doc) |

All gates PASS → implementation may be **proposed** (not auto-approved) at Capability ID Approval.

---

## 14. Contract–Substrate Delta (honest gaps)

These are the only divergences between this contract and the currently-verified substrate.
They are **documented deltas**, not blocking defects — all closable without touching frozen
boundaries, and implementation is currently BLOCKED anyway.

1. **[RESOLVED at contract level in v0.2 — F1]** Event naming. Contract §11 is now normative:
   canonical `execution_denied {reason}`. Legacy `execution_scope_denied` MUST be normalized at
   the provider boundary before emission; Gate A denial MUST emit `execution_denied
   {reason: insufficient_permission}`. *Implementation action: add normalization + Gate A audit
   in the provider; no frozen boundary touched.*
2. **[RESOLVED at contract level in v0.2 — F2]** Provider Schema Boundary. §6.1 mandates
   validation at the provider boundary and rejection (not silent ignore) of unknown/forbidden
   fields. The legacy `react.py raw args → handler` pattern is explicitly forbidden for the
   provider. *Implementation action: build the schema boundary in the provider; R1 override
   already makes the substrate safe, this hardens the entry point.*
3. **[INFO]** `code.execute` id itself. Does not exist yet — by design (DRAFT). Created only at
   Capability ID Approval.

---

## 15. M0.4.4 Review Outcome

`M0.4.4 code.execute Pilot Contract Review` result: **CONDITIONAL APPROVAL**, with both
findings closed in v0.2 (so the capability is review-ready for ID Approval).

| Review Gate | Verdict | Note |
|---|---|---|
| Gate 0 Scope Discipline | ✅ PASS | spec-only, no id/provider/index/substrate rewrite |
| Gate 1 Identity Model | ✅ PASS | UID-only, §3 |
| Gate 2 Authorization Model | ✅ PASS | double gate, §4 (+ v0.2 scope≠identity clarification) |
| Gate 3 Sandbox Authority | ✅ PASS | R1, §5 |
| Gate 4 Input Schema | ✅ PASS | §6 + §6.1 provider boundary (F2) |
| Gate 5 Secret Boundary | ✅ PASS | §10 |
| Gate 6 Filesystem Boundary | ✅ PASS | §8 (+ owner=identity not param clarification) |
| Gate 7 Network Boundary | ✅ PASS | §9 |
| Gate 8 Audit | ✅ PASS (was CONDITIONAL) | §11 normalized taxonomy (F1) |
| Gate 9 Schema Boundary | ✅ PASS (was CONDITIONAL) | §6.1 (F2) |

**Two findings → both closed in v0.2:** F1 (Audit Event Taxonomy) and F2 (Provider Schema
Boundary). No security-base deficiency; the prior CONDITIONAL was purely contract/substrate
semantic alignment, now resolved.

---

## 16. Review & Approval Workflow

```
Contract Draft v0.1  ──►  M0.4.4 Review (CONDITIONAL)
                              │
                              ▼
Contract Revision v0.2  ──►  findings F1+F2 CLOSED
                              │
                              ▼
Capability ID Approval  ──►  permission to register `code.execute`
                              │
                              ▼
Provider Implementation  ──►  BLOCKED until ID Approval
```

Checklist for Capability ID Approval:
- [x] §14 delta F1 resolved at contract level (normative taxonomy)
- [x] §14 delta F2 resolved at contract level (provider schema boundary)
- [x] No frozen boundary touched (`ebab801` / `filesystem.*` / `RFC-0001` / `ARCHITECTURE`)
- [ ] Capability Approver sign-off (§17)
- [ ] (Separate gate) Provider Implementation proposed only after ID Approval

---

## 17. Sign-off

| Role | Name | Decision | Date |
|---|---|---|---|
| Author | Senior Developer | Draft v0.2 submitted (findings closed) | 2026-08-06 |
| Security Reviewer | _pending_ | _pending_ | _ |
| Capability Approver | _pending_ | _pending_ | _ |

**Current binding status:**
```
M0.4.3
  Phase 1:   FAIL → CLOSED
  Phase 2:   S1–S6 COMPLETE        (736b571)
  R1:        COMPLETE              (d9e16ed)
  Re-audit:  FINAL PASS
code.execute:
  CONTRACT:  v0.1 DRAFT → v0.2 (M0.4.4 findings F1+F2 CLOSED)
  REVIEW:    M0.4.4 CONDITIONAL APPROVAL → review-ready for ID Approval
  IMPLEMENTATION:  BLOCKED (provider only after Capability ID Approval)
```
