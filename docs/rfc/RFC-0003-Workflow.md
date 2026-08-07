# RFC-0003: Workflow

- **Status:** Draft v0 (governance cadence: Draft → Independent Review → r-revision → Decision Package → Human Approval → Implementation)
- **Date:** 2026-08-07
- **Authors:** Anima engineering (drafted for governance review; per the M0.5 cadence, RFC-0003
  declares its own Infrastructure Workflow owner with semantic alignment to RFC-0106 — the same
  pattern as RFC-0002's A2 ruling, applied without a pre-Draft Decision Input)
- **Track:** Infrastructure (000x), consumes RFC-0001 (Capability Layer) and RFC-0002 (Planning),
  aligns to RFC-0106 (Ontology) + RFC-0107 (Execution, anchored)
- **Related:** RFC-0001 Capability Layer · RFC-0002 Planning (produces the `Plan` contract this RFC
  executes) · RFC-0004 Memory · RFC-0005 Multi-Agent · RFC-0106 Plan (Digital-Life Ontology, frozen —
  semantic alignment only) · RFC-0107 Execution (Digital-Life Ontology, anchored — semantic alignment only)
- **Canonical-owner note:** RFC-0003 **Infrastructure Workflow Authority** owns the canonical
  Infrastructure Workflow contract (self-declared in this Draft, per Study Review `3fbe0c2`:
  strictly-blocking GQs = 0, GQ1 resolved A2-style, no pre-Draft Decision Input issued). See §4.

---

## 0. Explicit non-redefinition statement

> **This RFC does not redefine the ontological meaning of Workflow / Execution established by
> RFC-0106 / RFC-0107.**
>
> RFC-0106 fixes Workflow's *meaning* in the digital-life ontology: Workflow is the **runtime
> executable shape** of a Plan — its `ordering` / `branching` / `rollback` (RFC-0106 §1.16, §4.1).
> It is **not** a separate state-class; it is the Plan's runtime form, owned by the same Planning
> Authority derivation that constructs the Plan. RFC-0107 fixes *Execution* — the act that runs a
> Plan's task graph — as an anchored Life-layer concern. RFC-0003 accepts both meanings in full and
> does **not** reopen them.
>
> What RFC-0003 adds is the **infrastructure-layer realization** of that meaning: the concrete
> **`WorkflowRun` contract** (the run that executes a Plan), the **Workflow Interface** (the stable
> API that starts / tracks / suspends / resumes / cancels a run), and the **run-state lifecycle**
> (recovery, crash-resume). This is the difference between *"what Workflow/Execution mean"*
> (RFC-0106/0107, ontology) and *"what the execution run + its producer/controller API look like"*
> (RFC-0003, infrastructure). The two levels are related by **semantic alignment**, not by override
> and not by replacement (§4).

---

## 1. Background / Problem

RFC-0002 gave Anima a canonical `Plan` contract and a Planning Interface. RFC-0001 gates the
capabilities a Plan's tasks call. But Anima still cannot answer:

> Given an *active* Plan, **how is it actually executed as a run** — step ordering honored, branches
> taken, failures retried within policy, progress checkpointed, and the whole thing resumed after a
> crash — without any consumer mutating the Plan or the run-state inconsistently?

Today the answer is implicit, scattered, and partly unwired:

1. `anima/constitution/state.py:188` defines a `Plan` (with `branching`/`rollback` descriptors) and
   `anima/constitution/execution.py:34` defines `execute_plan(Plan, ExecutionContext)` — but both are
   **unwired**; no production caller constructs or runs them (Recon / Study §1.6). RFC-0002 is now
   Anchored-equivalent (r1), so the `Plan` contract is consumable.
2. `anima/agent/state.py:30` defines `AgentTask` + `Step` (persisted to `agent_tasks.json`) — the
   *de-facto* production executor. It **conflates plan (step list) with run-state (status / result /
   current_step)** and is persisted, colliding with RFC-0106's "Plan never stored" rule. Its
   classification is **deferred from RFC-0002 §8** to this RFC (see §8 GQ8).
3. `anima/core/workflows.py:13` defines `WorkflowRun` — a **content-delivery ledger** (trigger →
   delivery intent → result), wired into `monitors/scheduler` and `core/autonomy`. It is a *different
   concern* from task-execution; its relationship to this RFC is **out of scope** (see §8 GQ2).
4. There is **no shared execution run contract** that RFC-0004 (Memory) or RFC-0005 (Multi-Agent) can
   rely on, no stable Workflow Interface other RFCs call, and no agreed run-state lifecycle.

**Core problem:** Anima lacks a *canonical, Plan-consuming Workflow run contract* and a *stable
Workflow Interface*. Without them, every downstream RFC (Memory outcome capture, Multi-Agent
delegation) implicitly assumes a different "execution" and silently disagrees — exactly the risk
RFC-0106/0107 warned about at the Life layer, now surfacing at the Infrastructure layer.

---

## 2. Goals

- **G1.** Define the **canonical `WorkflowRun` contract** — the concrete infrastructure
  representation of executing an RFC-0002 `Plan` (task graph consumption, per-task state, run-level
  status, checkpoint/suspend support), as a stable, versioned, Plan-referencing structure (§5).
- **G2.** Define the **Workflow Interface** — the stable API the Workflow Authority uses to
  *start / read / suspend / resume / cancel* a run, and the API Execution uses to *report* task
  outcomes (§6).
- **G3.** Define the **run-state lifecycle** — the runtime states a run moves through
  (`pending → running → completed|suspended|failed|cancelled`), explicitly *distinct* from the Plan
  lifecycle (RFC-0002 §7) and *not* a constitutional Owned lifecycle (§7, §7.1).
- **G4.** Keep the **Plan transient** at execution: RFC-0003 **consumes** an `active` Plan (reads
  `task_graph`/`ordering`/`branching`/`rollback`); it does **not** own, mutate, or persist the Plan.
  Run-state is persisted for recovery — but it is *run-state*, not Plan (carries RFC-0002 §8 forward).
- **G5.** Make the run **Plan-agnostic in structure**: any `active` Plan (planner / human / memory /
  external, per RFC-0002 G5) is executed identically — RFC-0003 consumes the Plan *structure*, not
  its producer.
- **G6.** Establish the interface boundary so later **advanced execution strategies** (parallelism /
  speculative execution / human-in-the-loop approval) can plug into the Workflow Interface *without*
  redefining the `WorkflowRun` contract (Roadmap §3 — advanced strategies deferred, no numbered RFC).

---

## 3. Non-goals

- **NG1.** Does **not** redefine the *ontological meaning* of Workflow / Execution (RFC-0106 / RFC-0107).
- **NG2.** Does **not** define *planning* (Plan construction) — that is RFC-0002 (Planning).
- **NG3.** Does **not** define *capability routing* — RFC-0001 already gates that; a run's tasks call
  capabilities via RFC-0001, but RFC-0003 owns only the run orchestration around those calls.
- **NG4.** Does **not** define *Memory* of run outcomes — RFC-0004 captures outcomes; RFC-0003 only
  reports them to the Planning Authority (which forwards to Reflection → Memory, per RFC-0106 §5.4).
- **NG5.** Does **not** define *Multi-Agent* orchestration — RFC-0005.
- **NG6.** Does **not** specify *advanced execution strategies* (parallelism / Tree-of-Thoughts
  execution / self-consistency). The Workflow Interface is in scope; the strategies that may drive it
  are deferred (Roadmap §7).
- **NG7.** Does **not** unify **Retry** or **Queue** as Workflow-owned abstractions — these are
  implementation concerns (Study GQ5 / GQ6). The contract carries *policy references* (e.g.
  `retry_policy`, `schedule_ref`) as metadata; the mechanisms belong to implementation (§8 GQ5/GQ6).
- **NG8.** Does **not** write code (Roadmap §8, Principle 5). Implementation follows only after
  Independent Review → Approval.
- **NG9.** Does **not** modify the Foundation (RFC-0100 / 0100A / 0100B / 0106 / 0107). If
  implementation surfaces a real contradiction, RFC-0003 will be annotated
  `Requires Foundation Evolution (F1/F2)` and wait for that decision — it does **not** modify
  Foundation inline (carries the reviewer's ruling *Frozen ≠ cannot draft* forward from RFC-0002).

---

## 4. Relationship to RFC-0106 / RFC-0107 (REQUIRED chapter)

Per the M0.5 cadence, RFC-0003 owns the canonical Workflow at the **Infrastructure** layer; RFC-0106
retains Workflow's *meaning* (Plan's runtime shape) and RFC-0107 retains *Execution* at the **Ontology**
layer. The relationship is **semantic alignment**, not replacement, not override.

```
RFC-0106  (Ontology — "Workflow = Plan's runtime shape: ordering/branching/rollback")
RFC-0107  (Ontology — "Execution = the act that runs a Plan's task graph")
        ▲
        │  semantic alignment (0003 realizes those meanings as a concrete run + execution API)
        │
RFC-0003  (Infrastructure — "what the WorkflowRun + execution/control API are")
```

### 4.1 What RFC-0003 **inherits** from RFC-0106 / RFC-0107 (no re-deciding)

| Ontology position | How RFC-0003 inherits it |
|-------------------|--------------------------|
| Workflow is the **runtime shape** of Plan — `ordering`/`branching`/`rollback` (RFC-0106 §1.16, §4.1) | The `WorkflowRun` contract **consumes** those descriptors from the `Plan` it executes; their *execution* is RFC-0003. |
| Workflow is **not a separate state-class**; it is Plan's runtime form, owned by the Planning Authority derivation (RFC-0106 §4) | RFC-0003 does **not** reify "Workflow" as a new Life state. It reifies a *new Infrastructure object* — the `WorkflowRun` (an execution run, with run-state) — distinct from the Plan. The Plan's shape is consumed; the run is owned by RFC-0003. |
| Execution **consumes** Plan; does **not** construct/revise it (RFC-0106 §8 E4) | The Workflow Interface consumes an `active` Plan read-only; it never calls `revise_plan` / `invalidate_plan` (RFC-0002 §6 boundary). |
| Execution reports outcome to the Planning Authority, which forwards to Reflection → Memory (RFC-0106 §5.4, §8) | RFC-0003's `report_task_outcome` / run-completion reports to the Planning Authority (RFC-0002); it does **not** write Memory (RFC-0004 territory). |
| Plan is **Transient-Derived**, never stored (RFC-0106 §4 P1 / §5.4) | RFC-0003 persists only **run-state** (the `WorkflowRun`), never the Plan. Re-plan is RFC-0002's job (§8 GQ8). |
| Execution Authority owns *execution* semantically (RFC-0107, anchored) | RFC-0003's **Infrastructure Workflow Authority** is the infra-layer counterpart that realizes that execution as a concrete run with a lifecycle; it is the **sole writer of run-state** (single-writer rule, O1). It does **not** redefine the ontology Execution Authority. |

### 4.2 What RFC-0003 does **NOT** inherit / does **NOT** decide (stays in ontology)

- RFC-0003 does **not** relitigate whether Workflow is a Life state-class — RFC-0106 answered **not a
  separate state-class** (it is Plan's runtime form), and RFC-0003 adopts that verbatim for the
  *shape*; it adds only the infra *run* object.
- RFC-0003 does **not** touch RFC-0100C (state permissions/transactions) — RFC-0106/0107 kept it
  deferred; a run *may* later surface a transaction need, but RFC-0003 introduces no transaction
  boundary of its own (parallel to RFC-0002 NG8 / RFC-0106 §9).
- RFC-0003 does **not** redefine the Execution Authority's *ontological* ownership (RFC-0107). It adds
  only the *infrastructure interface* that the Workflow Authority's implementation (the executor)
  exposes.

### 4.3 Why there is **no conflict**

The two RFCs operate at disjoint levels:

- RFC-0106 says *"Workflow is the runtime executable shape of a Plan (ordering/branching/rollback)."*
- RFC-0003 says *"Here is the concrete run object that executes that shape, and here is the API that
  starts / tracks / suspends / resumes / cancels it."*

The concrete run **realizes** the ontology's shape; it does not contradict it. No Foundation text is
edited, no new Life Authority is introduced, and the single-writer rule (O1) is preserved because only
the Workflow Interface may write run-state. Therefore RFC-0003 proceeds **without** a Foundation
Evolution gate.

### 4.4 End-to-end layered view (reference diagram)

This extends the RFC-0002 diagram; every later review should cite it:

```
RFC-0106  (Ontology — "what Plan / Workflow mean")
RFC-0107  (Ontology — "what Execution means")
      │
      │ semantic meaning
      ▼

RFC-0002  (Planning — owns Plan structure + Planning Interface)
      │
      │ produces
      ▼

Plan (active)
      │
      │ consumed by
      ▼

RFC-0003  (Workflow — owns WorkflowRun + Workflow Interface + run-state lifecycle)
      │
      │ executes
      ▼

ExecutionResult ──▶ reported to Planning Authority ──▶ Reflection ──▶ Memory (RFC-0004)
```

- RFC-0106/0107 own **what Plan / Workflow / Execution mean** (ontology).
- RFC-0002 owns **what the Plan structure + producer API are** — sole writer of a Plan.
- RFC-0003 owns **what the execution run + control API are** — sole writer of run-state; it only
  *consumes* the Plan structure, it does not own or redefine it.

---

## 5. Data Model — the canonical `WorkflowRun` contract

The `WorkflowRun` contract is the single, versioned, Plan-referencing structure that RFC-0004/0005
consume for outcome capture / delegation. It is the infrastructure realization of RFC-0106's "Workflow
executes a Plan" meaning.

```yaml
# workflowrun contract — RFC-0003 canonical structure (versioned, stable interface)
WorkflowRun:
  id:              uuid                      # unique per run (not a restore key)
  contract_version: 1.0.0                    # semver of the WORKFLOWRUN CONTRACT (interface stability)
  plan_ref:        string                    # ref to the ACTIVE Plan this run executes (read-only)
  goal_ref:        string                    # ref to the Goal (content source, read-only, for 0004/0005)
  status:          enum                      # pending | running | completed | suspended | failed | cancelled
                                             #   (RUN lifecycle, distinct from Plan lifecycle, §7)
  task_states:     list[TaskState]           # per-Task execution tracking, keyed by Plan.task_graph ids
  current_task_id: uuid | null               # which Task is in progress (resume anchor)
  checkpoint_ref:  string | null             # ref to a Checkpoint primitive snapshot, if present (§8 GQ4)
  retry_policy:    object | null             # metadata ref ONLY — mechanism is implementation (§8 GQ5)
  schedule_ref:    string | null             # metadata ref to a queue/scheduler, if used (§8 GQ6)
  budget_ref:      string | null             # ref to ExecutionContext.budget (ResourceBudget, if present)
  context_ref:     string                    # deterministic ref of the ExecutionContext used (audit)
  created_at:      timestamp
  completed_at:    timestamp | null

TaskState:
  task_id:         uuid                      # matches a Plan.task_graph Task id
  status:          enum                      # pending | running | done | failed | skipped
  attempts:        int                       # retry count for this Task (mechanism: impl, §8 GQ5)
  result:          object | null             # outcome payload (reported, not stored in Plan)
  error:           string | null
```

```yaml
# ExecutionResult — the run's terminal output, handed to the Planning Authority (RFC-0106 §5.4)
ExecutionResult:
  run_id:          uuid
  plan_ref:        string
  goal_ref:        string
  outcome:         enum                      # success | failure | cancelled
  task_results:    list[TaskState]           # full per-Task outcome
  summary:         string
```

> **Notes on `retry_policy` / `schedule_ref`.** These are **metadata references**, not mechanisms.
> RFC-0003 carries them so the contract is complete, but the *retry* and *queue* behaviors are
> **implementation concerns** (Study GQ5 / GQ6), explicitly out of this RFC's design scope (NG7). The
> contract does not prescribe how retries happen or which queue schedules a run.

**Stability.** `contract_version` is the interface-stability axis (semver, major = breaking contract
change). It is independent of the Plan's `contract_version` (RFC-0002 §5). RFC-0004/0005 pin to a
`contract_version` major; later RFC-0003 revisions bump the major only on a breaking change to this
structure.

---

## 6. Workflow Interface

The Workflow Interface is the stable API that the **Workflow Authority** *owns*; the **executor**
(e.g. `anima/agent/executor.py`, or a future constitution `execute_plan` realization) is its
*implementation*. Authority (ownership) and executor (implementation) are distinct. The interface is
the *sole* writer of run-state (RFC-0106 §8 E4 / O1).

```python
# Semantic interface (NOT implementation — no code in this Draft)
class WorkflowInterface:
    def start_run(self, plan_ref: str) -> WorkflowRun:
        """
        Begin executing an ACTIVE Plan. This is the run-activation operation (Study GQ9).
        The Plan must be in `active` state (RFC-0002 activate_plan); a `draft` Plan is rejected.
        Run starts in `pending`, transitions to `running` on first task dispatch.
        Only the Workflow Authority may start a run — it is the sole run-activator (§7.1).
        """

    def read_run(self, run_id: str) -> WorkflowRun:
        """Return the current run-state (for introspection / 0004-0005 consumption)."""

    def suspend_run(self, run_id: str, reason: str) -> WorkflowRun:
        """
        Pause a running run and capture a Checkpoint (if the Checkpoint primitive is present,
        §8 GQ4). Transitions running → suspended. Resume later via resume_run. Only the
        Workflow Authority may suspend.
        """

    def resume_run(self, run_id: str) -> WorkflowRun:
        """
        Continue a suspended run from its checkpoint / current_task_id. Transitions
        suspended → running. Only the Workflow Authority may resume.
        """

    def cancel_run(self, run_id: str, reason: str) -> WorkflowRun:
        """
        Terminate a run (user abort / Goal cancelled). Transitions running|suspended → cancelled.
        Only the Workflow Authority may cancel; Execution may REQUEST cancel via report_task_outcome
        (terminal failure), but the state transition is the Authority's (§7.1).
        """

    def report_task_outcome(self, run_id: str, task_id: str, outcome: TaskState) -> WorkflowRun:
        """
        Execution reports a single Task's result. Execution does NOT transition the run's
        lifecycle state itself — it reports; the Workflow Authority decides the run transition
        (running → completed when all tasks done; running → failed on terminal failure). This keeps
        the single-writer rule: Execution is a reporter, the Workflow Authority is the writer.
        """

    def list_runs(self, goal_ref: str) -> list[WorkflowRun]:
        """List runs (incl. terminal) for a Goal, for audit / 0004-0005 introspection."""
```

**Authority boundary (carries RFC-0106 §8 E4 / O1 forward).**
- The Workflow Interface is the **only** writer of run-state.
- `start_run` / `suspend_run` / `resume_run` / `cancel_run` are the only run-lifecycle-mutating
  operations; all four are **Workflow-Authority-only**. `read_run` / `list_runs` are read-only.
- Execution (the per-step runner) **reports** outcomes via `report_task_outcome` but does **not**
  transition run lifecycle; it never calls `start_run` / `cancel_run` as a state owner.
- The Planning Authority owns the Plan (RFC-0002); it does **not** write run-state. On run completion,
  the Workflow Authority reports `ExecutionResult` *to* the Planning Authority, which may `revise_plan`.
- Reflection / Memory (RFC-0004) write to **Memory**, never to run-state (RFC-0106 §8 E5).
- This preserves the single-writer rule (O1) at the infrastructure layer.

---

## 7. Run-state lifecycle (runtime states, NOT a constitutional lifecycle)

The `WorkflowRun` moves through **runtime run-states**. These are *operational* states of an execution
run, deliberately **distinct** from the Plan lifecycle (RFC-0002 §7) and **not** a constitutional Owned
lifecycle (RFC-0106 §4 P1 — Workflow is Plan's runtime form, not a new Life state).

```
            start_run (from an ACTIVE Plan)
                   │
                   ▼
              [ pending ] ──── first dispatch ────► [ running ]
                                                    │
                  ┌─────────────────────────────────┤
                  │                                 │
            suspend_run                       all tasks done
            (checkpoint)                     (report_task_outcome)
                  │                                 │
                  ▼                                 ▼
            [ suspended ] ──── resume_run ──► [ running ]    [ completed ]
                  │
            cancel_run / terminal failure
                  │
                  ▼
            [ cancelled ]  ──┐
            [ failed ]      ├── terminal, no auto-restart (re-plan is RFC-0002's job)
                             │
            (no `archived` / `adopted` state — run outcome sediments to Memory via Reflection)
```

| State | Meaning | Alignment |
|-------|---------|-----------|
| `pending` | Run created, not yet dispatching first task. | Infra run-state; distinct from Plan `draft`. |
| `running` | Actively executing tasks. | Consumes an `active` Plan; reads `task_graph`/`ordering`/`branching`/`rollback`. |
| `suspended` | Paused with a checkpoint; resumable. | Run-state only; Plan is untouched (transient, RFC-0106). |
| `completed` | All tasks done successfully. | Run ends; outcome → Planning Authority → Memory (RFC-0106 §5.4). |
| `failed` | Terminal failure (not auto-retried at run level — retries are per-Task impl, §8 GQ5). | Re-plan is RFC-0002's `revise_plan`, not a run restore. |
| `cancelled` | Aborted by Authority / Goal cancel. | Terminal; no restore (re-plan instead). |

### 7.1 Transition ownership (the lifecycle needs an owner)

Only the **Workflow Authority** — via the Workflow Interface (§6) — may *transition* a run between
run-states. The lifecycle has exactly one owner; no other Authority may move a run.

| Transition | Permitted actor | Notes |
|------------|-----------------|-------|
| `pending` → `running` (**start_run**) | **Workflow Authority only** | Run-activator (Study GQ9). Execution does not self-activate. |
| `running` → `suspended` (**suspend_run**) | **Workflow Authority only** | Captures checkpoint if present. |
| `suspended` → `running` (**resume_run**) | **Workflow Authority only** | Resumes from checkpoint / `current_task_id`. |
| `running` → `completed` | **Workflow Authority only** (driven by `report_task_outcome`) | Execution reports; Authority decides completion. |
| `running`/`suspended` → `failed`/`cancelled` | **Workflow Authority only** (`cancel_run` / terminal) | Execution may *request* via `report_task_outcome`, but the transition is the Authority's. |
| any run-state transition | Execution / Planning Authority / Memory | **Not permitted.** Execution reports; Planning owns Plan; Memory writes Memory (O1). |

Without a single owner, the run-state would have no Authority responsible for moving it to `running`,
and any consumer could silently transition it — re-introducing the multi-writer risk RFC-0106's
single-writer rule (O1) prohibits. This is the **same governance pattern** as RFC-0002 §7.1 (Plan
lifecycle owner), applied to the run.

**No `archived` / `adopted` state.** Run completion is captured as an `ExecutionResult` → reported to
the Planning Authority → forwarded to Reflection → **Memory** (RFC-0106 §5.4, §8). The run itself ends
by being *completed* / *failed* / *cancelled* — its outcome sediments to Memory, the run object is not
archived (mirrors Plan's "discarded, not archived").

**Re-plan vs run-restore.** A failed run does **not** restore; re-planning is RFC-0002's `revise_plan`
(emits a fresh Plan → a fresh `WorkflowRun`). Resumability after a crash is achieved by *recomputing*
the Plan (RFC-0002) and *reconciling* the new run against persisted `task_states` (where it left off) —
fully consistent with RFC-0106 V5 / RFC-0002 §8.

---

## 8. Deferred Study questions (defer only — no answer given)

The `WORKFLOW-UNIFICATION-STUDY.md` raised 10 governance questions (GQ1–GQ10). Per the Study Review
(`3fbe0c2`): strictly-blocking = 0, candidate = 1 (GQ1), implementation = 2 (GQ5/GQ6), draft-resolvable
= 7. This Draft resolves GQ1 the **A2 way** (self-declaration + semantic alignment, no pre-Draft
ruling) and declares scope for the draft-resolvable ones. The rest are **deferred** — RFC-0003 neither
answers nor reclassifies them; it only fixes the canonical `WorkflowRun` contract and the Workflow
Interface. **No secret answers.**

| Study Q | Treatment in this Draft | Status |
|---------|------------------------|--------|
| **GQ1** — canonical "Workflow" | RFC-0003 declares its own Infrastructure Workflow owner (§4, §6) with semantic alignment to RFC-0106/0107. Same A2 pattern as RFC-0002; no Foundation change, no pre-Draft ruling. | **Resolved A2-style** (not a secret answer — explicit, transparent declaration) |
| **GQ2** — content-delivery ledger in scope? | RFC-0003 scope = **task-execution of a Plan**. `anima/core/workflows.py:WorkflowRun` (content-delivery ledger) is a **distinct concern**, OUT of scope. Its reconciliation with this contract is **deferred to implementation** (naming collision acknowledged, §5 note). | Deferred |
| **GQ3** — `WorkflowRun` & `AgentTask` same layer? | Not answered. RFC-0003's `WorkflowRun` is the infra execution run; production `AgentTask` is deferred (see GQ8). Whether they are "peers" is left open. | Deferred |
| **GQ4** — `Checkpoint` (resume) owner? | `Checkpoint` is an interface-only Protocol (`contracts/lifecycle.py:36`); RFC-0107 anchored. Draft consumes it as an *optional* `checkpoint_ref` (§5) and `suspend_run`/`resume_run` reference it; the **primitive's ownership/impl is deferred to the Execution layer** (Foundation-aligned, not redefined here). | Deferred |
| **GQ5** — `Retry` belongs to Workflow? | **Implementation concern** (≥5 scattered sites). Draft carries `retry_policy` / `TaskState.attempts` as **metadata only** (NG7); the retry *mechanism* is out of scope. | Implementation (out of scope) |
| **GQ6** — `Queue` belongs to Workflow? | **Implementation concern** (≥3 scattered sites). Draft carries `schedule_ref` as **metadata only**; it does **not** define or unify a queue abstraction. Multiple queues may coexist; scheduling is implementation. | Implementation (out of scope) |
| **GQ7** — run-state lifecycle independent of Plan? | RFC-0003 **declares** the run-state lifecycle as **independent** of the Plan lifecycle (§7, distinct states/enums). This is a contract definition within Draft scope, not a governance ruling on the ontology. | Declared (within scope) |
| **GQ8** — `AgentTask`+`Step` classification (carried from RFC-0002 §8) | RFC-0002 deferred this to RFC-0003. This Draft does **not** reclassify them. It fixes only the canonical `WorkflowRun` contract; `AgentTask`/`Step` → run-state mapping is **deferred to implementation** (their persisted, plan-conflating nature is noted in §1.2, left for implementation). | Deferred |
| **GQ9** — run activation / hand-off authority | RFC-0003 declares `start_run` as the **sole run-activator** owned by the Workflow Authority (§6, §7.1), A2-style. Observed `OwnerAgent.submit` / `WorkflowStore.bind_intent` are de-facto (like RFC-0002's A3, not canonical). | Resolved A2-style |
| **GQ10** — missing lifecycle primitives (CancellationToken/Deadline/Checkpoint) owner? | These are Protocols anchored in RFC-0107 (`contracts/lifecycle.py`). Draft consumes them as optional refs (`budget_ref`/`checkpoint_ref`); their **impl ownership is deferred to the Execution layer**. No Foundation change. | Deferred |

**Net:** GQ1 and GQ9 are resolved the A2 way (transparent self-declaration); GQ7 is a within-scope
contract declaration; GQ2/GQ3/GQ4/GQ8/GQ10 are deferred without answer; GQ5/GQ6 are implementation
concerns explicitly out of scope. **No Study question is secretly answered.**

---

## 9. Examples

**E1 — Plan activated, run started (origin-agnostic).**
Planning Authority `activate_plan(P1)` → `active`. Workflow Authority `start_run(plan_ref=P1)` →
`WorkflowRun{ id=R1, status=pending }` → dispatches first Task → `running`. The Plan is consumed
read-only; never mutated.

**E2 — Task reports, Authority decides run transition.**
Execution runs T1; calls `report_task_outcome(R1, T1, done)`. Workflow Authority marks T1 `done`;
continues. After last Task, Authority transitions `running → completed`; emits `ExecutionResult`.

**E3 — Crash resume (Plan recomputed, run reconciled).**
Process crashes mid-run. On restart: RFC-0002 `request_plan(G1)` *recomputes* P1' (fresh id);
Workflow Authority `start_run(P1')` → reconciles `task_states` (T1 done, T2 pending) → resumes at T2.
Plan recomputed (RFC-0106 V5); run-state restored from persisted `task_states` (§7).

**E4 — Suspend / resume with checkpoint.**
Workflow Authority `suspend_run(R1)` → `suspended`, captures `checkpoint_ref` (if Checkpoint primitive
present, §8 GQ4). Later `resume_run(R1)` → `running` from checkpoint. Plan untouched (transient).

**E5 — Authority boundary preserved.**
`execution.cancel_run(R1)` or `reflection.save_run(...)` are **rejected** (O1): Execution may only
`report_task_outcome`; Memory writes Memory; only the Workflow Interface writes run-state.

**E6 — Content-delivery ledger stays separate (GQ2).**
`anima/core/workflows.py:WorkflowRun` (delivery) is **not** consumed by this contract; it remains a
distinct concern. RFC-0003 does not absorb or redefine it (deferred to implementation).

---

## 10. Limits & Open Questions (non-blocking)

- **Q1 — Task-level parallelism / ordering policy.** How Tasks run in parallel, how `branching` is
  chosen at runtime — executor implementation, not a contract question. *Resolved in implementation.*
- **Q2 — Retry policy shape.** The exact `retry_policy` schema + which layer enforces it (GQ5) is
  implementation. *Out of scope (NG7).*
- **Q3 — Scheduling / queue abstraction.** Whether one queue or many, and what `schedule_ref` points
  to (GQ6) — implementation. *Out of scope (NG7).*
- **Q4 — Checkpoint mechanism.** The reference implementation of `Checkpoint` (RFC-0107 anchored) is
  deferred to the Execution layer (GQ4/GQ10). *Deferred.*
- **Q5 — Advanced execution strategies.** Parallelism / Tree-of-Thoughts execution / human approval
  are deferred (Roadmap §7), no numbered RFC in M0.5. The Workflow Interface is strategy-agnostic.
  *Deferred, not blocked.*
- **Q6 — Existing production structures.** Classification of `AgentTask`/`Step` and of
  `core/workflows.py:WorkflowRun` vs this `WorkflowRun` contract is **deferred to implementation** (§8
  GQ2/GQ8). This RFC does not decide it. *Not a governance blocker.*

---

## 11. Evolution / next RFC

RFC-0003 is **consumable** by the next Infrastructure RFCs:

- **RFC-0004 Memory** captures `ExecutionResult` (outcome) — it reads run outcomes, does **not** write
  run-state (RFC-0106 §8 E5). RFC-0003 reports outcomes to the Planning Authority, which forwards to
  Reflection → Memory.
- **RFC-0005 Multi-Agent** may delegate `WorkflowRun`s (sub-runs) but does not redefine the contract;
  it consumes `WorkflowRun` / `ExecutionResult` and orchestrates at a higher layer.
- **Constitution `execute_plan`** (`constitution/execution.py:34`, currently unwired) is a candidate
  *implementation* of `report_task_outcome` + run orchestration; wiring it is implementation at
  Approval, not a Draft decision.
- **No Foundation Evolution required.** RFC-0003 introduces no new Life Authority, no new Ontology
  noun, no transaction boundary. If implementation surfaces a real contradiction no Life RFC foresaw,
  RFC-0003 will be annotated `Requires Foundation Evolution (F1/F2)` and wait for that decision — it
  does not modify Foundation inline.

### 11.1 Cadence (same as M0.4.x / RFC-0002)

This Draft proceeds: **Independent Review → r-revision → Decision Package → Human Approval →
Implementation (if approved)**. No code is written before Approval (Roadmap §8, Principle 5).

---

## 12. Appendix — governance trail

- Roadmap: `docs/governance/M0.5-ROADMAP.md` (APPROVED BASELINE, r1 frozen).
- Recon: `docs/governance/M0.5-RFC-0003-WORKFLOW-INVENTORY.md` · `…WORKFLOW-OWNERSHIP.md` ·
  `…WORKFLOW-DEPENDENCY-MAP.md` · `…WORKFLOW-OPEN-QUESTIONS.md`.
- Study: `docs/governance/M0.5-RFC-0003-WORKFLOW-UNIFICATION-STUDY.md` (consolidates Recon; 10 GQs).
- Study Review / Triage: `docs/governance/M0.5-RFC-0003-WORKFLOW-STUDY-REVIEW.md`
  (strictly-blocking = 0; candidate = 1 (GQ1); impl = 2 (GQ5/GQ6); draft-resolvable = 7).
- **No Workflow Decision Input was issued** — per the Study Review, strictly-blocking GQs = 0, and
  GQ1 is resolvable A2-style within this Draft (self-declaration + semantic alignment), mirroring
  RFC-0002. This is the validating case for the converged M0.5 cadence
  `Recon → Study → Study Review → Draft` (Decision Input as on-demand, not fixed step).
- Canonical-owner note: RFC-0003 **Infrastructure Workflow Authority** owns the canonical
  Infrastructure Workflow contract, in semantic alignment with RFC-0106/0107 — not a redefinition.
