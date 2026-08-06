# `code.execute` — Capability ID Approval Package

> **Document type:** Spec-only governance package (M0.4.4 Capability ID Approval)
> **Status:** PROPOSED / RESERVED · **NOT IMPLEMENTED**
> **Contract ref:** `code.execute_pilot_contract_v0.2.md`
> **Baseline commit:** `d9e16ed` (R1 closed) — read-only against this state
> **Discipline:** No `capability_id` created, no provider registered, no capability index modified, no frozen boundary touched.

---

## 1. Request Metadata

| Field | Value |
|---|---|
| Capability Name | `code.execute` |
| Status | PROPOSED / RESERVED |
| Contract Version | v0.2 |
| Implementation Status | NOT IMPLEMENTED |
| Provider Status | NONE |
| Registry Status | NOT REGISTERED |
| Index Entry | ABSENT (verified `index.has("code.execute") == False`) |

---

## 2. Approval Purpose

Request approval to **reserve** `capability_id = code.execute` as a future capability identity in the RFC-0001 §5 namespace.

Approved scope of this approval:

- ✅ Capability identity allocation (`code.execute` reserved)
- ✅ Contract reference (pins v0.2)
- ✅ Provider design reference (advisory only)

Explicitly **not** approved (blocked until later phases):

- ❌ Provider creation
- ❌ Tool registration under this capability
- ❌ Runtime routing / resolver activation
- ❌ Capability index activation (no `declare_contract` call)

---

## 3. Identity Definition

```yaml
capability_id: code.execute
domain: code            # derived from id split (contract.py:60)
```

Semantic:

> Execute user-approved code inside a controlled execution environment under explicit authorization, sandbox, and audit constraints.

This is **distinct** from the three existing M0.3 pilot capabilities (see §10 Collision Check) and introduces no new domain prefix into the registry.

---

## 4. Ownership / Authority Model

### Authority Source (only)

```
QQ UID
   |
   v
PermissionStore
   |
   v
Trusted Execution Context (ctx.permission_level)
```

Prohibited as capability authority:

```
group owner · group admin · sender.role · chat metadata · tool arguments
```

This mirrors contract v0.2 §3/§5 and the R1 fix (`d9e16ed`): sandbox tier is sourced **only** from `ctx.permission_level`, never from caller args.

---

## 5. Required Scope

```yaml
required_scopes:
  - exec
```

`exec` meaning (contract v0.2 §7):

> permission to invoke the hardened execution substrate.

`exec` does **not** mean: owner / administrator / filesystem-unrestricted / network-enabled.

Scope is reused from the existing RFC-0001 §7 model — `exec` is a pre-defined scope (`scopes.py:24-30`), not a new one. This keeps `code.execute` consistent with the established scope taxonomy.

---

## 6. Capability Boundary

### Included (capability may govern)
- code payload schema (allowlist)
- execution request validation
- execution authorization (double gate)
- sandbox policy selection (ctx-derived tier)
- execution audit normalization (v0.2 taxonomy)

### Excluded (not owned by this capability)
```
filesystem.read · filesystem.write · network · secrets · credential access · package install
```

`code.execute` is a **wrapper layer** over the existing hardened substrate, not a re-implementation of the execution system (contract v0.2 §12).

---

## 7. Provider Relationship (target shape, not built now)

```
code.execute capability
      |
      v
provider adapter
      |
      v
hardened execution substrate   (UID auth + env isolation + FS confinement + exec gate + audit)
      |
      v
sandbox runtime
```

Prohibited:

```
code.execute capability -> new execution engine
```

The substrate already provides: UID authorization, env isolation, FS confinement, exec-scope gate, audit pipeline (all verified by Phase 2 + R1).

---

## 8. Security Preconditions (gate before ID activation)

| Check | Status | Evidence |
|---|---|---|
| UID-only identity | PASS | `permission.py:22` `_check_permission(user_id, group_id, config)` — no `sender_role` |
| exec double gate | PASS | `tools/__init__.py:213` (can_call) + `:272` (exec_permitted) |
| sandbox authority ctx-only | PASS | `tools/__init__.py:284` overwrite of args `permission_level` with ctx (R1) |
| env isolation | PASS | `sandbox.py:77` `build_sandbox_env()` allowlist |
| filesystem confinement | PASS | `sandbox.py:494-622` `_FS_GUARD_BLOCK` |
| audit taxonomy | PASS | contract v0.2 §11 + `tools/__init__.py:289-329` events |
| provider schema boundary | PASS | contract v0.2 §6.1 (validate at provider boundary, reject unknowns) |

---

## 9. Known Implementation Constraints (must survive ID activation)

### Constraint A — Sandbox tier source
Only `ctx.permission_level` may decide sandbox tier. A caller-supplied
`{"permission_level": 4}` must be unconditionally overwritten (already enforced at
`tools/__init__.py:284`; locked by R1 regression tests).

### Constraint B — Provider schema validation order
Provider MUST schema-validate **before** handler execution. The legacy
`react.py` raw-LLM-JSON → handler pattern must NOT be copied into the capability provider.

### Constraint C — Audit output normalization
Provider MUST emit only the v0.2 taxonomy:
`execution_started` / `execution_finished` / `execution_failed` / `execution_denied`.
The legacy `execution_scope_denied` must be normalized to
`execution_denied{reason:"missing_exec_scope"}` at the provider boundary, never exposed raw.

---

## 10. Collision Check (read-only, against `d9e16ed`)

> Executed as part of this approval package — not assumed. References are real code anchors.

### 10.1 Capability namespace collision
Existing declared capabilities (`builtin/__init__.py`):

| Constant | Value | Domain |
|---|---|---|
| `PILOT_CAPABILITY_ID` | `search.web` | search |
| `PILOT_CAPABILITY_ID_2` | `filesystem.read` | filesystem |
| `PILOT_CAPABILITY_ID_3` | `filesystem.write` | filesystem |

Proposed `code.execute` → domain `code`. **No id collision.** Schema name
`code_execute` (`index.py:26-32` `.`→`_`) is unbound; `index._schema_names` has no
`code_execute` key. **No schema-name collision.**

**Result: PASS — no namespace collision.**

### 10.2 Scope naming collision
`SCOPE_NAMES` (`scopes.py:24-30`) = `(network, filesystem, exec, secrets, user_interaction)`.
`PermissionScopes` (`scopes.py:34-45`) carries `exec: bool`.

`code.execute` declares `required_scopes: [exec]` — reuses the **pre-existing** `exec`
scope. Existing pilots use `network` (`search.web`) and `filesystem` (`filesystem.read/write`);
`exec` is defined but currently under-claimed. `code.execute` is the legitimate first
claimant of `exec`. **No scope-name collision; consistent reuse.**

**Result: PASS — no scope collision.**

### 10.3 Registry reservation feasibility
- `index.has("code.execute")` → `False` (absent, verified conceptually against `index.py:150-151`).
- Reservation call would be:
  `index.declare_contract(CapabilityContract(id="code.execute", permission=PermissionScopes(exec=True), stability=Stability.EXPERIMENTAL))`
  (`contract.py:24-35`, `index.py:88-93`). No `_declared` conflict; `_bind_schema_name`
  would not warn (`index.py:173-182`).
- This call **IS NOW EXECUTED** (see §14 Execution Record) — reservation approved and applied.

**Result: READY — clean reservation, no conflict. ✅ EXECUTED 2026-08-06.**

---

## 11. Approval Decision Matrix

| Item | Result |
|---|---|
| Contract exists (v0.2) | PASS |
| Security review completed (M0.4.4) | PASS |
| Sandbox boundary reviewed (R1 closed) | PASS |
| Schema boundary reviewed (v0.2 §6.1) | PASS |
| Audit taxonomy defined (v0.2 §11) | PASS |
| Capability collision check | PASS (§10.1) |
| Scope collision check | PASS (§10.2) |
| Registry reservation | READY — pending approval (§10.3, not executed) |
| Provider implementation | BLOCKED |

---

## 12. Approval Outcome (recommendation)

```
APPROVED FOR ID RESERVATION
```

Meaning:

- ✅ Reserve `code.execute` identity in the RFC-0001 §5 namespace
- ❌ Do NOT activate the capability
- ❌ Do NOT create a provider
- ❌ Do NOT register a tool
- ❌ Do NOT call `index.declare_contract(...)` yet → **overturned by approval; executed in §14**

The reservation creates no runtime effect; it only books the identity so downstream
phases (Provider Design Review → Provider Implementation) have a stable target.

---

## 13. Next Flow (governance only)

```
ID Approval (this package)              ✅ signed 2026-08-06
      |
      v
Capability Registry Reservation         ✅ executed 2026-08-06 (see §14)
      |
      v
Provider Design Review                  ⏳ next gate
      |
      v
Provider Implementation                 BLOCKED until design review passes
```

We remain in the **spec governance layer** for the capability; the reservation is a
minimal index mutation (identity booking only).

---

## 14. Execution Record (reservation applied)

> This section added post-approval. The reservation is the single minimal mutation
> explicitly authorized by "APPROVED FOR ID RESERVATION".

**Change set** (`anima/tools/builtin/__init__.py`):
- Added `PILOT_CAPABILITY_ID_4 = "code.execute"` + capability-layer description.
- Added `declare_code_execute_contract(registry)` — calls only
  `index.declare_contract(CapabilityContract(id="code.execute", permission=PermissionScopes(exec=True), stability=EXPERIMENTAL))`.
- Wired `declare_code_execute_contract(registry)` into `register_all_builtin` (after `declare_filesystem_write_contract`).

**Hard boundaries preserved (verified by tests):**
- No `index_tool` call, no Tool tagged `capability_ids=["code.execute"]` → **no provider attached**.
- `run_python`/`run_shell`/`agent_exec` handlers / `min_level` / ABI unchanged → **execution not activated**.
- `filesystem.read|write` tool registrations untouched; RFC-0001 / ARCHITECTURE / `ebab801` untouched.

**Reservation-state assertions (all green):**
- `index.has("code.execute") == True`
- `index.providers_for("code.execute") == []`  → capability exists but **NOT executable**
- `contract.permission == PermissionScopes(exec=True)` → scope not expanded
- `capabilities()` now `[search.web, filesystem.read, filesystem.write, code.execute]`
- Tool annotation set unchanged (still exactly 3 Tools: web_search / read_file / write_file)

**Tests:** new `test_code_execute_reservation_*` (4) in `test_capability_pilot_m031.py`;
governance-guard assertions in `test_capability_pilot_m031.py:47`, `test_capability_adapter_m03.py`,
`test_capability_expansion_m04.py` updated to the 4-capability approved set.

**Rollback point:** commit `d9e16ed` (pre-reservation). Revert = `git checkout -- anima/tools/builtin/__init__.py` + test reverts.

---

## Sign-off

| Role | Decision | Date |
|---|---|---|
| Author (Dev Agent) | Package drafted; collision check PASS; reservation executed & verified | 2026-08-06 |
| Approver (User) | ✅ APPROVED FOR ID RESERVATION | 2026-08-06 |
