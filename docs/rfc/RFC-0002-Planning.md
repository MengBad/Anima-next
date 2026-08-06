# RFC-0002: Planning

- **Status:** Draft v0.1 (governance cadence: Draft → Independent Review → r-revision → Decision Package → Human Approval → Implementation)
- **Date:** 2026-08-07
- **Authors:** Anima engineering (drafted for governance review; per reviewer ruling A2 — RFC-0002 Infrastructure Planning Authority owns the canonical Plan)
- **Track:** Infrastructure (000x), consumes RFC-0001 (Capability Layer)
- **Related:** RFC-0001 Capability Layer · RFC-0003 Workflow (consumes the `Plan` contract) · RFC-0004 Memory · RFC-0005 Multi-Agent · RFC-0106 Plan (Digital-Life Ontology, frozen — semantic alignment only)
- **Canonical-owner ruling:** A2 — *RFC-0002 Infrastructure Planning Authority* (governance decision 2026-08-07; see §4)

---

## 0. Explicit non-redefinition statement

> **This RFC does not redefine the ontological meaning of Plan established by RFC-0106.**
>
> RFC-0106 fixes Plan's *meaning* in the digital-life ontology: Plan is the life's
> **Transient-Derived** operational structure for reaching a committed Goal —
> `Plan = p(Goal, Drive, Context)`, constructed by the Planning Authority, consumed by
> Execution, **never stored** (RFC-0106 §4 P1 / §5.4 P3). RFC-0002 accepts that meaning in
> full and does **not** reopen it.
>
> What RFC-0002 adds is the **infrastructure-layer realization** of that meaning: the concrete
> `Plan` *contract* (the structure other RFCs read/write), the **Planning Interface** (the
> stable API that produces and revisions a Plan), and the Plan's **runtime lifecycle states**.
> This is the difference between *"what Plan means"* (RFC-0106, ontology) and *"what the Plan
> data structure and its producer API look like"* (RFC-0002, infrastructure). The two levels
> are related by **semantic alignment**, not by override and not by replacement (§4).

---

## 1. Background / Problem

M0.4.x proved Anima can *safely gate and route* a capability (RFC-0001): given a known need,
the agent can reach a gated, observable provider. But it cannot yet answer:

> Given a messy, multi-step, long-running **Goal**, what should the agent actually *do*, in
> what order, and how does it recover when interrupted?

Today the answer is implicit and scattered:

1. `anima/constitution/state.py:188` defines a `Plan` dataclass (task_graph / ordering /
   branching / rollback) — but it is **unwired**; nothing constructs it in production (M1.6
   Planner Interface is *deferred until RFC-0106 anchors*; RFC-0106 is now **Anchored**, so
   M1.6 is unlocked).
2. `anima/agent/state.py:20` defines `AgentTask` + `Step` — the *de-facto* production
   structure — persisted to `agent_tasks.json`. It conflates **planning** (the step list) with
   **execution progress** (status / result / current_step) and is **persisted**. Its
   relationship to RFC-0106's "Plan never stored" rule and to this RFC's `Plan` contract is
   **out of scope** here and **deferred to RFC-0003** (see §8).
3. There is **no shared `Plan` contract** that RFC-0003 (Workflow) can consume, no stable
   Planning Interface other RFCs can call, and no agreed Plan lifecycle.

**Core problem:** Anima lacks a *canonical, origin-agnostic Plan contract* and a *stable
Planning Interface*. Without them, every downstream RFC (Workflow, Memory, Multi-Agent)
implicitly assumes a different "plan" and silently disagrees — exactly the risk RFC-0106
warned about at the Life layer, now surfacing at the Infrastructure layer.

---

## 2. Goals

- **G1.** Define the **canonical `Plan` contract** — the concrete infrastructure representation
  of RFC-0106's Plan concept (task graph / ordering / branching / rollback), as a stable,
  versioned, origin-agnostic structure (§5).
- **G2.** Define the **Planning Interface** — the stable API the Planner (and, by extension,
  other producers) uses to *request / read / revise* a Plan (§6).
- **G3.** Define the **Plan lifecycle** — the runtime states a Plan moves through
  (draft → active → superseded, plus `deprecated` for invalidation), explicitly *not* a
  constitutional Owned lifecycle (§7, §7.1).
- **G4.** Keep Plan **Transient-Derived** at the infrastructure layer, in semantic alignment
  with RFC-0106: the Plan *structure* is recomputed, never restored; any durability for
  crash-recovery belongs to RFC-0003's **run-state**, not to the Plan (§7, §8).
- **G5.** Make the `Plan` contract **origin-agnostic**: it may be produced by the Planner
  (0002), a Human Plan, Memory-derived planning (0004), or an external API. Workflow (0003)
  consumes the *structure*, not the producer (Roadmap §4 note).
- **G6.** Establish the interface boundary so later **advanced reasoning strategies** (CoT /
  Tree Search / Reflection) can plug into the Planning Interface *without* redefining the
  `Plan` contract (Roadmap §3 — reasoning strategies are deferred, no numbered RFC in M0.5).

---

## 3. Non-goals

- **NG1.** Does **not** redefine the *ontological meaning* of Plan (RFC-0106 owns that).
- **NG2.** Does **not** define *runtime execution* of steps — that is RFC-0003 (Workflow).
- **NG3.** Does **not** define *Memory* the plan reads/writes — that is RFC-0004.
- **NG4.** Does **not** define *actual provider calls* — RFC-0001 already gates those.
- **NG5.** Does **not** define *Multi-Agent* orchestration — RFC-0005.
- **NG6.** Does **not** specify *advanced reasoning strategies* (CoT / Tree Search /
  Reflection / Self-critique). The Planning Interface is in scope; the strategies that may
  later drive it are deferred (Roadmap §7).
- **NG7.** Does **not** write code. This is a Draft; implementation follows only after
  Independent Review → Approval (Roadmap §8, Principle 5).
- **NG8.** Does **not** modify the Foundation (RFC-0100 / 0100A / 0100B / 0106). If
  implementation later surfaces a real contradiction, RFC-0002 will be annotated
  `Requires Foundation Evolution (F1/F2)` and wait for that decision — it does **not** modify
  Foundation inline (carries forward the reviewer's ruling that *Frozen ≠ cannot draft*).

---

## 4. Relationship to RFC-0106 (REQUIRED chapter)

Per the governance decision (2026-08-07), RFC-0002 owns the canonical Plan at the
**Infrastructure** layer; RFC-0106 retains Plan's meaning at the **Ontology** layer. The
relationship is **semantic alignment**, not replacement, not override.

```
RFC-0106  (Ontology — "what Plan means")
        ▲
        │  semantic alignment (0002 realizes 0106's meaning as a concrete contract)
        │
RFC-0002  (Infrastructure — "what the Plan structure + producer API are")
```

### 4.1 What RFC-0002 **inherits** from RFC-0106 (no re-deciding)

| RFC-0106 position | How RFC-0002 inherits it |
|-------------------|--------------------------|
| Plan is **Transient-Derived**, `p(Goal, Drive, Context)` (§4 P1) | The `Plan` contract is a *derivation output*; RFC-0002 treats it as recomputed, never restored. |
| Plan has **no storage authority** (§5.4 P3 / V4) | RFC-0002 does **not** provision a Plan store. Persistence for recovery is delegated to RFC-0003 run-state (§8). |
| Plan is **constructed by the Planning Authority only** (§6.2, V3) | The Planning Interface (§6) is the *sole* constructor/revise API. Execution/Reflection do not write Plan (carries E4 forward). |
| Plan is the **graph of Tasks**, not a single Task (§1.15/§4) | The `Plan` contract's `task_graph` is a list of `Task` nodes (§5); a Task is the executable unit, not the Plan. |
| Plan **reads** Goal/Drive/Context as signals, mutates none (§5.1, V3 C2) | The Planning Interface inputs are read-only Goal/Drive/Context refs; no upstream mutation. |
| Branching / Rollback are **Workflow shape**, not a Plan lifecycle (§4.1) | The `Plan` contract carries `branching`/`rollback` *descriptors*; their *execution* is RFC-0003. |

### 4.2 What RFC-0002 does **NOT** inherit / does **NOT** decide (stays in 0106)

- RFC-0002 does **not** relitigate whether Plan is Owned or Derived — RFC-0106 answered
  **Transient-Derived**, and RFC-0002 adopts that verbatim.
- RFC-0002 does **not** touch RFC-0100C (state permissions/transactions) — RFC-0106 confirmed
  the 6th consecutive "no"; RFC-0002 introduces no transaction boundary either.
- RFC-0002 does **not** redefine the Planning Authority's *ontological* ownership (RFC-0105 §6
  / RFC-0106 §6). It adds only the *infrastructure interface* that the Planning Authority's
  implementation (the Planner / M1.6) exposes.

### 4.3 Why there is **no conflict**

The two RFCs operate at disjoint levels:

- RFC-0106 says *"Plan is a transient-derived operational structure for reaching a Goal."*
- RFC-0002 says *"Here is the concrete data structure for that structure, and here is the API
  that produces it."*

The concrete structure **is** an instance of the ontology's meaning; it does not contradict it.
No Foundation text is edited, no new Authority is introduced, and the single-writer rule (O1)
is preserved because only the Planning Interface may construct/revise a Plan. Therefore RFC-0002
proceeds **without** a Foundation Evolution gate.

### 4.4 End-to-end layered view (reference diagram)

This is the diagram every later review should cite — it shows the boundary each RFC owns:

```
RFC-0106
Ontology
      │
      │ semantic meaning
      ▼

RFC-0002
Planning
      │
      │ produces
      ▼

Plan

      │
      │ consumed by
      ▼

RFC-0003
Workflow
```

- RFC-0106 owns **what Plan means** (ontology).
- RFC-0002 owns **what the Plan structure + producer API are** (infrastructure) — and is the
  sole writer of a Plan.
- RFC-0003 owns **how a Plan is executed** (run-state, crash recovery) — it only *consumes* the
  Plan structure, it does not own or redefine it.

---

## 5. Data Model — the canonical `Plan` contract

The `Plan` contract is the single, versioned, origin-agnostic structure that RFC-0003 consumes.
It is the infrastructure realization of RFC-0106's Plan concept.

```yaml
# plan contract — RFC-0002 canonical structure (versioned, stable interface)
Plan:
  id:            uuid                      # unique per construction (not a restore key)
  contract_version: 1.0.0                  # semver of the PLAN CONTRACT (interface stability)
  goal_ref:      string                    # ref to the Goal this Plan serves (content source, read-only)
  source:        enum                      # planner | human | memory | external_api
                                           #   origin-agnostic: 0003 consumes any of these (Roadmap §4)
  task_graph:    list[Task]                # decomposition: the graph of work (RFC-0106 §4.1)
  ordering:      object                    # sequence / dependency spec among Tasks
  branching:     object                    # conditional / fallback paths (Workflow shape, RFC-0106 §4.1)
  rollback:      object                    # unwind-on-failure spec (Workflow shape, NOT a Plan lifecycle)
  context_ref:   string                    # deterministic ref of the Context read (audit only)
  drive_emphasis: string                   # "approach" | "avoid" — Drive orientation, read-only influence
  focus:         float                     # Drive focus, read-only influence
  status:        enum                      # draft | active | superseded | deprecated  (RUNTIME routing state, §7)
  created_at:    timestamp
```

```yaml
# Task — a single executable unit inside a Plan (RFC-0100 §1.9 / RFC-0106 §4)
Task:
  id:            uuid
  title:         string                    # human-readable
  intent:        string                    # what this task contributes toward the Goal
  capability_need: object                   # OPTIONAL: a declared need ({ type, args }) routed via RFC-0001
                                           #   (present only when the task implies a capability; may be empty)
  depends_on:    list[uuid]                 # task ids this Task waits on
  estimates:     object                    # optional planner hints (cost/latency/risk) — not commitments
```

> **Note on `capability_need`.** A Task *may* carry a declared capability need so RFC-0001 can
> route it at execution time, but the `Plan` contract itself does **not** execute capabilities
> (RFC-0001 / RFC-0003 own that). `capability_need` is metadata, not an action.

**Stability.** `contract_version` is the interface-stability axis (semver, major = breaking
contract change). It is independent of any content/versioning of the Goal. RFC-0003 pins to a
`contract_version` major; later RFC-0002 revisions bump the major only on a breaking change to
this structure.

---

## 6. Planning Interface

The Planning Interface is the stable API the **Planning Authority's implementation** (the
Planner; M1.6 `construct_plan`) exposes. It is the *sole* constructor/revise surface for a Plan
(RFC-0106 §6.2 V3 — only the Planning Authority may construct/revise Plan).

```python
# Semantic interface (NOT implementation — no code in this Draft)
class PlanningInterface:
    def request_plan(self, goal_ref: str, context_ref: str) -> Plan:
        """
        Produce a fresh Plan for an active Goal.
        Maps to M1.6 construct_plan(goal, drive, context) once implemented.
        Reads Goal/Drive/Context as signals (read-only); emits a new derived Plan.
        Never restores a prior Plan (RFC-0106 §5.4 V5).
        """

    def read_plan(self, plan_id: str) -> Plan:
        """Return the current Plan structure (for introspection / 0003 consumption)."""

    def revise_plan(self, goal_ref: str, reason: str) -> Plan:
        """
        Produce a NEW Plan for the same Goal (re-plan trigger: execution failure,
        Goal shift, Context change). Supersedes the prior Plan (§7); never mutates it.
        Only this interface may revise — Execution/Reflection call read_plan/consume, not revise.
        """

    def list_plans(self, goal_ref: str) -> list[Plan]:
        """List Plans (incl. superseded) for a Goal, for audit/introspection."""

    def invalidate_plan(self, plan_id: str, reason: str) -> Plan:
        """
        Declare a Plan INVALID without issuing a replacement (distinct from revise_plan).
        Use when the Plan must not be consumed (e.g. underlying Goal cancelled, or the
        context it was derived from is found stale/contradicted) but no fresh Plan is being
        produced yet. Workflow (0003) MUST NOT consume a Plan in `deprecated` state.
        Only the Planning Authority may invalidate (see §7.1).
        """
```

**Authority boundary (carries RFC-0106 §6.2 / E4 forward).**
- The Planning Interface is the **only** writer of Plan state.
- `request_plan` / `revise_plan` / `invalidate_plan` are the only state-mutating operations;
  all three are **Planning-Authority-only**. `read_plan` / `list_plans` are read-only (any
  authorized consumer).
- Workflow (RFC-0003) **consumes** a Plan (reads `task_graph`/`ordering`/…) — it does **not**
  call `revise_plan` or `invalidate_plan`. On execution outcome it reports to the Planning
  Authority, which may `revise_plan` (RFC-0106 E4).
- Reflection / Memory (RFC-0004) write to **Memory**, never to Plan (RFC-0106 §8 E5).
- This preserves the single-writer rule (O1) at the infrastructure layer.

**Origin-agnostic producers.** `source` in the contract lets non-Planner producers (human,
memory-derived, external API) emit the *same* `Plan` structure via their own interfaces. They
MUST emit a contract-valid `Plan`; they do **not** use `revise_plan` (that is the Planning
Authority's own re-plan path). RFC-0003 consumes the structure regardless of `source`.

---

## 7. Plan lifecycle (runtime states, NOT a constitutional lifecycle)

The Plan moves through **runtime routing states**. These are *operational* states of a
transient structure, deliberately **not** a constitutional Owned lifecycle (RFC-0106 §4 P1 —
Plan has no independent lifecycle, no storage authority).

```
            request_plan / revise_plan
                   │
                   ▼
              [ draft ] ───────────────► handed to Workflow (0003) ──► [ active ]
                   │                                                        │
                   │  revise_plan (new Goal signal / failure)              │ execution consumed
                   │  produces a NEW Plan, supersedes this one             │
                   ▼                                                        ▼
            [ superseded ]  (discarded, never restored)        (Plan discarded after consumption)

            invalidate_plan (no replacement: Goal cancelled / context stale)
                   │  (from draft or active)
                   ▼
              [ deprecated ]  (MUST NOT be consumed by Workflow; discarded, never restored)
```

| State | Meaning | RFC-0106 alignment |
|-------|---------|--------------------|
| `draft` | Produced, not yet executing. | Transient; recomputed, not restored. |
| `active` | Handed to Workflow (0003) for execution. | Consumed by Execution (RFC-0106 §5.2 V2). |
| `superseded` | A newer Plan for the same Goal exists (re-plan). | Old Plan **discarded**, never archived/restored (§5.4 V5). |
| `deprecated` | Declared invalid by the Planning Authority; MUST NOT be consumed. No replacement implied (distinct from `superseded`). | Transient; **discarded**, never archived/restored (§5.4 V5). |

### 7.1 Transition ownership (the lifecycle needs an owner)

Only the **Planning Authority** — via the Planning Interface (§6) — may *transition* a Plan
between runtime states. The lifecycle has exactly one owner; no other Authority may move a Plan.

| Transition | Permitted actor | Notes |
|------------|-----------------|-------|
| `draft` → `active` (**activate**) | **Planning Authority only** | Workflow does **not** self-activate a Plan; it consumes only an `active` Plan. |
| `active` → `superseded` (**supersede**, on re-plan) | **Planning Authority only** (`revise_plan`) | Emits a fresh Plan; never mutates the old one. |
| `draft`/`active` → `deprecated` (**invalidate**) | **Planning Authority only** (`invalidate_plan`) | No replacement implied; Workflow must not consume. |
| consume (`active` → discarded) | **Workflow (RFC-0003)** | Read-only consumption; Workflow does **not** change the Plan's state. |
| any state transition | Execution / Memory | **Not permitted.** Neither may transition a Plan (RFC-0106 §8 E4/E5; single-writer rule O1). |

Without a single owner, the lifecycle would have no Authority responsible for moving a Plan to
`active`, and any consumer could silently transition it — re-introducing the multi-writer risk
that RFC-0106's single-writer rule (O1) prohibits. `superseded` implies a replacement exists;
`deprecated` implies **no** replacement — the two are distinct lifecycle events, which is why
`invalidate` is a separate operation from `revise`.

**No `archived` / `done` / `adopted` state.** Plan completion is tracked by the **Goal's**
lifecycle (RFC-0105) and by RFC-0003's **run-state**, not by the Plan. The Plan itself ends by
being *consumed* (active → discarded), *superseded* (re-plan → discarded), or *deprecated*
(invalidated → discarded). This is exactly RFC-0106's "Plan is discarded, not archived."

**Re-plan vs restore.** `revise_plan` always emits a **fresh** `Plan` (new `id`); the prior
Plan becomes `superseded` and is dropped. There is no `Plan.restore()`. Resumability after a
crash is achieved by *recomputing* the Plan and *reconciling* it against persisted run-state
(see §8) — fully consistent with RFC-0106 V5.

---

## 8. Resumability & the persistence boundary (resolves Decision B)

**Decision B** (persistence boundary) was ruled *non-blocking* by the reviewer (2026-08-07) and
is resolved here within RFC-0002, without touching the Foundation.

- **The `Plan` itself is never persisted** (RFC-0106 §5.4 P3 / V4). It is transient; on crash,
  the Planner *recomputes* it via `request_plan`/`revise_plan`.
- **Execution progress is persisted — but as RFC-0003 run-state, not as a Plan.** The persisted
  record (which Steps ran, their results, `current_step`) is RFC-0003's artifact. RFC-0002 owns
  only the *interface boundary* for resumability: after a crash, (a) RFC-0002 recomputes the
  Plan, (b) RFC-0003 reconciles the recomputed Plan against its persisted run-state to decide
  where to resume.
- **Existing production structures are out of scope.** `anima/agent/state.py:20` defines
  `AgentTask` + `Step` (persisted to `agent_tasks.json`); `anima/constitution/state.py:188`
  defines a `Plan` dataclass. Their eventual classification — whether `AgentTask`/`Step` map to
  RFC-0003 run-state, and how the constitution `Plan` dataclass relates to the §5 contract — is
  **deferred to RFC-0003** (and to implementation at Approval). This RFC neither reclassifies
  them nor decides their fate; it only fixes the canonical `Plan` contract (§5) and the Planning
  Interface (§6). Their migration is **not** a decision of this Draft.

This keeps Plan transient (honoring RFC-0106) while still delivering the Roadmap's
"recoverable / resumable" requirement via RFC-0003's run-state — no contradiction.

---

## 9. Examples

**E1 — Planner produces a Plan (origin: planner).**
`request_plan(goal_ref=G1, context_ref=C1)` → Planner reads Goal G1 + Drive (orientation) +
Context C1, emits `Plan{ id=P1, source=planner, task_graph=[T1,T2,T3], ordering=sequential,
branching=…, status=draft }`. No caller assigned the Plan; it was derived internally (RFC-0106 §7.1).

**E2 — Hand-off to Workflow (no Plan mutation).**
Plan P1 → `active`; RFC-0003 consumes `task_graph`/`ordering`. Execution does **not** call
`revise_plan` (RFC-0106 E4). On finishing, it reports outcome to the Planning Authority.

**E3 — Re-plan on failure (fresh, not restore).**
Execution fails at T2. Planning Authority calls `revise_plan(goal_ref=G1, reason="T2 failed")`
→ emits `Plan{ id=P2, status=draft }`; P1 becomes `superseded` and is discarded. P2 is a *fresh
derivation*, never a restore of P1 (RFC-0106 §5.4 V5 / E2).

**E4 — Human / Memory / external Plan (origin-agnostic).**
A human-authored plan arrives as `Plan{ id=PH, source=human, … }`; RFC-0003 consumes it
identically to a planner Plan. The `source` field records origin; the structure is unchanged.

**E5 — Resumability after crash (Plan recomputed, progress restored).**
Process crashes mid-execution. On restart: RFC-0002 `request_plan(G1)` *recomputes* P1';
RFC-0003 reconciles P1' against its persisted run-state (T1 done, T2 pending) and resumes at T2.
The Plan is recomputed (never restored); only run-state is restored (§8).

**E6 — Authority boundary preserved.**
`execution.revise_plan(...)` or `reflection.save_plan(...)` are **rejected** (RFC-0106 §8 E4/E5,
O1). Only the Planning Interface writes Plan.

---

## 10. Limits & Open Questions (non-blocking)

- **Q1 — Task granularity / branching policy.** How fine Tasks are, how branching is chosen —
  Planner implementation (M1.6), not a contract question. *Resolved in implementation.*
- **Q2 — Re-plan trigger heuristics.** *When* to re-plan (failure / Goal shift / Context change)
  is the Planner's deliberation. RFC-0002 fixes only: Planning Interface is the sole re-plan
  path; re-plan emits a fresh Plan. *Resolved in implementation.*
- **Q3 — Multi-Plan / sub-Plan nesting.** How the Planner prioritizes or nests Plans is its own
  deliberation; RFC-0002 fixes only that all Plans are produced via the Planning Interface.
  *Resolved in implementation.*
- **Q4 — `capability_need` routing detail.** The exact RFC-0001 resolution of a Task's
  `capability_need` at execution is RFC-0003 + RFC-0001 territory. *Resolved in 0003.*
- **Q5 — Advanced reasoning strategies.** CoT / Tree Search / Reflection that may *drive*
  `request_plan` are deferred (Roadmap §7), no numbered RFC in M0.5. The Planning Interface is
  strategy-agnostic; strategies plug in later without contract change. *Deferred, not blocked.*
- **Q6 — Existing production structures.** The classification of `AgentTask`/`Step` and of the
  constitution `Plan` dataclass is **deferred to RFC-0003** (§8). This RFC does not decide it.
  *Not a governance blocker.*

---

## 11. Evolution / next RFC

RFC-0002 is **consumable** by the next Infrastructure RFCs:

- **RFC-0003 Workflow** consumes the **`Plan` contract** (§5) as the structure it executes.
  0003 pins to `contract_version` major; it must **not** redefine the Plan structure — it reads
  `task_graph`/`ordering`/`branching`/`rollback` and owns their *execution* (step runtime,
  retries, **run-state persistence / crash recovery** — §8). 0003 consumes the *structure*, not
  the Planner (Roadmap §4 note).
- **RFC-0004 Memory** may act as a `source=memory` Plan producer (memory-derived planning) and
  is a reader of plan outcomes — it does **not** write Plan (RFC-0106 §8 E5).
- **RFC-0005 Multi-Agent** orchestrates Plans (delegates them) but does not redefine the
  contract.
- **M1.6 Planner Interface** (unlocked now that RFC-0106 is Anchored) implements
  `request_plan`/`revise_plan` as the `p(Goal, Drive, Context)` derivation — single-writer,
  transient, with a smoke test that *is* the validation (per M1 discipline).
- **No Foundation Evolution required.** RFC-0002 introduces no new Authority, no new Ontology
  noun, no transaction boundary. If implementation surfaces a real contradiction no Life RFC
  foresaw, RFC-0002 will be annotated `Requires Foundation Evolution (F1/F2)` and wait for that
  decision — it does not modify Foundation inline.

### 11.1 Cadence (same as M0.4.x)

This Draft proceeds: **Independent Review → r-revision → Decision Package → Human Approval →
Implementation (if approved)**. No code is written before Approval (Roadmap §8, Principle 5).

---

## 12. Appendix — governance trail

- Roadmap: `docs/governance/M0.5-ROADMAP.md` (APPROVED BASELINE, r1 frozen).
- Recon: `docs/governance/M0.5-PLANNING-RECON-REPORT.md`.
- Study: `docs/governance/M0.5-PLAN-UNIFICATION-STUDY.md`.
- Decision Input: `docs/governance/M0.5-PLAN-UNIFICATION-DECISION-INPUT.md` (reviewer ruling:
  A=blocking, B/C=non-blocking, RFC-0002 unblocked).
- Authority Study: `docs/governance/M0.5-PLAN-AUTHORITY-STUDY.md` (Decision A only).
- Canonical-owner ruling (2026-08-07): **A2 — RFC-0002 Infrastructure Planning Authority** owns
  the canonical Plan; semantic alignment with RFC-0106, not override.
