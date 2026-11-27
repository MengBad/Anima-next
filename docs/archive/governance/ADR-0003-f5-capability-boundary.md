# ADR-0003 — F5 Runtime Capability Boundary

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-09 |
| **Decided-by** | Engineering (F-series implementation, post-hoc freeze of F5.4a–F5.5) |
| **Supersedes** | none |
| **Amended-by** | none |
| **Relates-to** | RFC-0001 Capability Layer (`docs/rfc/RFC-0001-Capability.md`); F5.4a design freeze; F5.4b Resolver+Minimal Provider (`5f83192`); F5.5 Resume Coordinator (`858d2ee`); F6 Invariant Audit (`HEAD 858d2ee`) |
| **Frozen baseline** | `858d2ee` (m0.5-roadmap) |
| **Does NOT modify** | `docs/ARCHITECTURE.md` (`28f2582` 🔒 FROZEN) |

> **Nature of this ADR.** F5.4a–F5.5 were implemented and verified *before* this document was
> written. This ADR is the **governance freeze** of a decision that is already coded and tested
> (29 tests encode it). It exists so the boundary cannot be eroded by future convenience changes.
> It is documentation of a settled decision, not a proposal.

---

## 1. Context

F4/F5 established a crash-safe execution path:

```
Workflow lifecycle → Orchestrator (RUNNING durability)
                   → TaskExecutor (per-task Observation)
                   → (+ ExecutionLedger idempotency gate)
                   → Observation → report_task_outcome → Workflow persistence
```

F5.4b introduced a **capability boundary** so the executor can reach the external world through a
`CapabilityResolver → Provider` chain instead of a fake/recording executor. F5.5 bridged recovery
to resume. The code is correct and tested, but the *boundary rules* lived only in module docstrings
and tests — invisible to a future maintainer. Two concrete erosion risks were identified:

- **I1 erosion:** someone writes `executor.providers["x"].invoke()` "for convenience", collapsing
  orchestration responsibility into the executor.
- **Dual-track confusion:** `anima/tools/` already contains a V1 capability layer
  (`CapabilityManager.execute_capability`, `anima/tools/builtin/` Tool registry). A future
  maintainer "sees a resolver" and reuses it, breaking the F5 boundary.

This ADR freezes the boundary so those erosions are detectable by review and by the existing test
suite, not by post-incident debugging.

---

## 2. Decision — Three Invariants

### I1 — Executor ≠ Capability Selector

The executor **delegates selection** to the `CapabilityResolver`. It never holds a provider
registry and never picks a provider itself.

- **Authority location:** `CapabilityResolver` owns selection — candidate discovery, health/scope
  filtering, scoring, pick/degrade (`anima/runtime/capability_resolver.py:179-249`; registry held at
  `:186-191`; `resolve()` at `:194-249`).
- **Executor dependency:** `ResolvingTaskExecutor` is constructed with **only** the resolver —
  `self._resolver` and nothing else (`anima/runtime/resolving_executor.py:51-52`). It calls
  `self._resolver.resolve(need, execution_key)` at `:70`.

```
Executor                     Resolver                    Provider
   |                            |                            |
   |  need + execution_key ---> |                            |
   |                            |  candidates → score → pick |
   |                            | --- invoke ---------------> |
   | <--- ProviderResult -------|                            |
   |  project → Observation     |                            |
```

**FORBIDDEN (I1 violation):**
```python
executor.providers["xxx"].invoke(...)          # executor must not hold/select providers
executor._pick_provider(...)                   # selection authority belongs to Resolver
```

**ALLOWED:**
```python
self._resolver.resolve(need, execution_key)    # executor delegates selection
```

### I2 — Provider ≠ Workflow Authority

The provider produces **pure data only**. It never writes lifecycle or side-effect state; the
`ExecutionObservation` is the sole cross-layer carrier.

- **Structural guarantee:** `Provider` imports **no** constitution / workflow / ledger
  (`anima/runtime/providers/base.py:4`; imports at `:25-29` are only `abc`, `dataclasses`, `typing`).
- **Contract:** `invoke(capability, arguments, execution_key) -> ProviderResult`
  (`anima/runtime/providers/base.py:109-118`); all failures are expressed as
  `ProviderResult(success=False, error=...)`, not raised (`:18`, `:118`).
- **Four-bucket model (F5's core architectural result):**

```
WorkflowRunStore       → lifecycle facts (run/task status)
ExecutionLedger        → side-effect facts (idempotency record)
Provider               → external call results (pure data in, pure data out)
Observation            → the ONLY object crossing layers (executor → workflow)
```

**FORBIDDEN (I2 violation):**
```python
provider.workflow.transition(...)     # never
provider.store.save_run(...)          # never
provider.report_task_outcome(...)     # never
```

**ALLOWED:**
```python
result = provider.invoke(capability, arguments, execution_key)
return ProviderResult(data=..., success=True)   # pure data only
```

### I3 — Execution Identity Is Immutable

The idempotency key `(run_id, task_id, attempt)` is the **sole** runtime execution identity. It is
computed once and propagated **verbatim** through every layer.

- **Definition:** `ExecutionLedger` key = `(run_id, task_id, attempt)`
  (`anima/runtime/execution_ledger.py:15`; `_key_dict` at `:61-63`; storage subdir `_SUBDIR =
  "execution_ledger"` at `:51`, isolated from `WorkflowRunStore`'s `workflow_runs/`).
- **Verbatim propagation:**
  - Orchestrator computes the id: `execution_run_id = source_run_id or run.id`
    (`anima/runtime/orchestrator.py:201`).
  - Executor gates + forwards: `ledger.get(execution_key)` → `self._resolver.resolve(need,
    execution_key)` (`anima/runtime/resolving_executor.py:57-70`).
  - Resolver passes verbatim: `need.capability_id, need.args, execution_key`
    (`anima/runtime/capability_resolver.py:199, :235`).
  - Provider consumes verbatim: "consumed verbatim (I3)" (`anima/runtime/providers/base.py:114`).
- **Recovery preserves I3:** `ResumeCoordinator` seeds the ledger with the **original** crashed
  run's id so ledger facts survive `recover_run`'s NEW run_id: `source_run_id=original_run_id`
  (`anima/runtime/resume_coordinator.py:96`; orchestrator `:191, :201`).

**FORBIDDEN (I3 violation):** adding any dimension to the key —
```python
(run_id, task_id, attempt, provider_id)   # breaks recovery replay
(run_id, task_id, attempt, timestamp)     # breaks idempotency
(run_id, task_id, attempt, session_id)    # breaks F5.3/F5.5 replay
```

**ALLOWED:** new dimensions go into `metadata`, never into identity:
```python
ProviderResult(data=..., success=True, metadata={"provider_id": ..., "attempt_ts": ...})
```

---

## 3. Ownership Map

| Component | Owns | Forbidden to own |
|---|---|---|
| `WorkflowRunStore` | lifecycle facts (run/task status) | external calls, provider selection |
| `ExecutionLedger` | side-effect facts (idempotency record) | workflow state, provider selection |
| `Provider` | external call result (pure data) | workflow state, ledger writes, selection |
| `Observation` | cross-layer carrier (executor→workflow) | authority over any store |
| `CapabilityRegistry` | routing table (capability→providers) | execution act, workflow state |
| `CapabilityResolver` | selection logic (candidate→score→pick) | execution act, workflow state |
| `ResolvingTaskExecutor` | per-task Observation production | provider selection, workflow state |
| `ResumeCoordinator` | bridge recover_run → _resume_run (one run) | redefining recovery, provider selection |

**F4 recovery purity (invariant):** `ResumeCoordinator` only *reads* `recover_run`
(`anima/runtime/resume_coordinator.py:44`); `recovery.py` is never modified by F5. A scheduler /
startup enumeration loop is intentionally **out of scope** of recovery (see §6 Deferred).

---

## 4. Dual-Track Boundary — V1 vs F5 Capability Layers

There are **two** capability surfaces in the codebase. They are deliberately separate and must stay
that way.

| | V1 Tool/Capability Layer | F5 Runtime Capability Layer |
|---|---|---|
| **Path** | `anima/tools/` (`capability.py` `CapabilityManager.execute_capability`; `anima/tools/builtin/` Tool registry) | `anima/runtime/capability_resolver.py` + `anima/runtime/providers/` |
| **Role** | legacy / tool execution surface (LLM-prompt-driven self-growing capability wrapper) | workflow runtime execution surface (F5 execution correctness + recovery) |
| **Selection** | `CapabilityManager` (V1, separate) | `CapabilityResolver` (F5, this ADR) |
| **State authority** | none here; consumed by V1 runtime | none here; carrier = `Observation` |
| **Frozen by** | V1 governance | **this ADR (I1/I2/I3)** |

**Rule:** Do **not** reuse V1 `anima/tools/` resolver/manager as the F5 `CapabilityResolver`, and do
not route F5 workflow tasks through the V1 Tool registry. They are distinct execution surfaces with
distinct invariants. Migration from V1→F5 is **out of scope** of this ADR — this ADR only fixes the
boundary so future work does not silently conflate them.

---

## 5. Extension Rules (how to add safely)

Adding a new capability or provider MUST preserve I1/I2/I3:

1. **New provider** → subclass `Provider`, implement `invoke(capability, arguments, execution_key) ->
   ProviderResult`. Never import constitution/workflow/ledger. Return data, never write state.
2. **New capability** → register in `CapabilityRegistry`; add a `CapabilityContract` (timeout,
   retry, permission). Selection stays in the resolver.
3. **New side-effecting provider (future)** → MUST consult the ledger *before* the external call
   and carry an external idempotency token; if it cannot guarantee idempotency, mark
   `idempotent=False` and refuse automatic retry. (Protocol to be proven in F5.7, see §6.)
4. **New execution dimension** → goes into `Observation.metadata` / `ProviderResult.metadata`,
   never into the `(run_id, task_id, attempt)` key.

A new provider that compiles but violates I2 structurally (imports workflow/ledger) is a **failing**
change regardless of tests passing.

---

## 6. Deferred (explicitly out of scope of this ADR)

- **F5.6 — Startup Recovery Coordinator.** Enumerate interrupted runs at startup and call
  `recover_and_resume` per run. This is a *startup orchestration* layer (allowed to be called from
  `bootstrap`), **not** an executor capability and **not** a scheduler inside `recovery.py`.
- **F5.7 — External Side Effect Boundary.** Prove `external side effect → execution_key →
  idempotency guarantee` with a Mock HTTP mutation provider (rollback-able, testable, sandboxed).
  Forbidden first choices: message send / payment / delete. Retry ownership, provider idempotency
  contract, and external token semantics must be settled here before ledger responsibility can be
  extended to providers.
- **Governance gap E2 (separate doc):** a dedicated note on V1/F5 dual-track *migration* strategy.
  This ADR fixes the boundary; it does not plan migration.

---

## 7. Verification

The boundary is encoded in the test suite (the strongest freeze mechanism). Current count: **29
tests**.

| Suite | Tests | Encodes |
|---|---|---|
| `tests/test_runtime_capability_resolver.py` | 15 | I1 (selection in resolver), I3 (verbatim key), scoring/degrade/retry |
| `tests/test_runtime_resume_coordinator.py` | 7 | I3 (source_run_id=original), F4 recovery purity, single-run bridge |
| `tests/test_runtime_executor_integration.py` | 7 | I2 (Observation carrier), RUNNING durability, ledger gate |

Pre-F5 baseline at `HEAD 858d2ee`: full suite green with 0 regression introduced by F5
(19 pre-existing environment failures in `test_sandbox_*` / `test_exec_security_m043` are unrelated
to F5 — same root cause reproduced on a clean tree with F5 files removed).

---

## 8. Consequences

**Positive**
- The F5 capability boundary is now review-detectable, not incident-detectable.
- I1/I2/I3 are explicit, with forbidden-dependency rules a reviewer can check mechanically.
- V1/F5 dual-track confusion is named and walled off.

**Negative / cost**
- New providers must follow the structural constraints (no workflow/ledger imports) — a minor
  friction cost that buys boundary integrity.
- F5.6/F5.7 remain open; the runtime cannot yet (a) auto-resume on startup or (b) safely perform
  real external side effects. These are tracked as deferred, not forgotten.

**Stability**
- This ADR is **Accepted** and frozen at `858d2ee`. Amending it requires the same governance
  cadence as any architectural decision; silent edits to the boundary are prohibited.
