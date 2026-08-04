# RFC-0107: Execution (Life-7) — Foundation Validation #7

> **Status**: **Anchored** — Foundation Validation #7 (commit `PLACEHOLDERHASH`). Passed LAWP
> Phase C Challenge: **APPROVED WITH CLARIFICATIONS**; Gate verdict **Foundation Stable → YES**
> (Gate A). 0100C **7th consecutive deferral — now bound to M1.7 as an implementation watchpoint**,
> not abstract. This RFC *consumes* the Foundation; it does not modify it. Three F4 clarifications
> (C1/C2/C3) + the C4 transaction watchpoint were applied with **zero base edits** before anchoring.
> **Role**: **Foundation Validation #7**, *not* "Execution Engine Design #7". Its job is to use the
> `Plan → Execution` boundary as a probe to prove whether the 0100 trilogy is sufficient to *explain*
> Execution — the **first behavioral / event-layer probe**. It sits downstream of the first Owned
> state (Goal, RFC-0105) and the first Derived-post-Owned state (Plan, RFC-0106), at the seam where
> the life's *intent* (Goal) becomes *action* in the Universe (RFC-0100 §1.17). Execution is already
> named as an **act** ("the act of carrying a Task into the Universe"), so the central question is
> classification: **is Execution a State (new Owner) or a Process/Event (consumption of Plan)?**
> **Central risk** (per the user's 2026-08-04 brief): (a) mis-classifying Execution as a *State* —
> which would spawn a lifecycle/store/authority and silently reopen RFC-0100C; (b) a hidden
> *second Memory* (Execution Log → Execution Memory) duplicating the single-writer rule (O1); and
> (c) Execution is the **first joint where transaction / failure / rollback / external side-effect
> truly live** — so it is the *most likely* Life RFC to surface a real RFC-0100C contradiction. But
> per F1–F4 the trigger is an *observed* contradiction, not a "might need transactions" speculation;
> the document-level analysis must not pre-activate 0100C.
> **Phase**: Seventh Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0106 Plan (the
> `Plan → Execution` boundary) and transitively RFC-0101..0105 + RFC-0100 §1.17.
> **Layer**: Life layer (Cognition → Infrastructure seam) — RFC-0107 = Life-7
> **Depends-on**: RFC-0100 (§1.15 Planner / §1.16 Workflow / §1.17 Execution / §1.18 Capability /
> §1.19 Provider; flow `Goal → Plan → Execution`), RFC-0100A (outer loop), RFC-0100B (state classes
> §3, ownership matrix §2, O1–O4), RFC-0101..0106 (upstream, transitively), RFC-0106 (Plan =
> Transient-Derived, Planning Authority), `PROTOCOL-Life-Validation.md` (V1–V6)
> **Validates**: (a) RFC-0100 Ontology coverage for Execution (already named in §1.17);
> (b) RFC-0100A loop sufficiency for `Plan → Execution → Result → Memory`; (c) RFC-0100B state-class
> fit — is Execution a *State* (new Owner) or a *Process/Event* (no Owner, no lifecycle); (d)
> RFC-0100C (Permissions/Transactions) — the deferred question, now testable a *seventh* time, and
> for the **first time** against atomicity/side-effect semantics rather than lifecycle/store.
> **Scope**: The *information model of Execution* (as a Process/Event) + its consumption contract +
> its (non-)authority + its result回流. **No implementation, no code, no Execution Engine, no new
> flow vocabulary, no new life noun** (Execution already exists in RFC-0100 §1.17).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Execution gap

The project can now answer eight questions (per RFC-0106 §0 + this RFC):

- **What exists?** — Ontology (RFC-0100 §1.17): *Execution = the act of carrying a Task into the
  Universe via the Infrastructure Layer*. Execution is a **named** noun — and, unlike Plan/Goal, it
  is defined as an **act**, not a *thing*. This RFC does **not** introduce it; it classifies it.
- **How does it flow?** — Dynamics (RFC-0100A outer loop; RFC-0100 §1.7 line 130): `… → Goal → Plan
  → Execution`. Execution is the *carrying* of a Task (the executable unit inside Plan) into the
  Universe. Its *result* then flows `Result → Reflection → Memory` (RFC-0100 §3 / RFC-0105 §8).
- **Who governs change?** — State Ownership (RFC-0100B): Plan's *constructor* is the Planning
  Authority (RFC-0106). Execution *consumes* that Plan. The question this RFC must answer is whether
  Execution is **a State that needs its own Authority** — or a **Process** carried by a *Runtime*
  that owns no Constitution state.
- **Where does it sit relative to Plan?** — RFC-0106 fixed the boundary: the Planning Authority
  *constructs* Plan; Execution *consumes* it. Goal is Owned; Plan is Derived; Execution must be
  **neither** — it is the *act* that runs Plan's Tasks.
- **What is Execution *not*?** — RFC-0100 §1.17–§1.19 draw the boundary with Task ("a single
  executable unit"), Workflow ("the runtime executable shape of a Plan" — *owned by the Planning
  Authority's derivation*, RFC-0106 §4), Capability ("a declared need-type"), and Provider ("a
  concrete implementation … Not part of the Life", §1.19). Execution *carries a Task via a Capability
  → Provider*; it is the **act**, not the Task, not the Provider, not a Store.
- **Does Execution need RFC-0100C?** — This is the **central continuation** of RFC-0105 §9 and
  RFC-0106 §9. Goal (Owned, lifecycle) deferred at Reading (a); Plan (Derived, no lifecycle) deferred
  a sixth time. Execution is the **first joint where transaction / failure / rollback / external
  side-effect genuinely live** (RFC-0100 §1.16 names *Workflow rollback*; §1.17 names *carrying into
  the Universe* = side effects). This RFC must, for the first time, take the question seriously — but
  per the user's F1–F4 discipline, it may **not** pre-activate 0100C on speculation. The honest
  position: at the *document/validation* level there is **no observed contradiction** (we have not
  built the Execution Engine); the real 0100C test is deferred to **M1.7 (Execution Engine
  implementation)**, where an observed contradiction (if any) would route via Foundation Evolution
  F1, not a silent expansion.
- **Where does the result go?** — The user's Q3 / C3: after Execution finishes, does it persist as an
  Execution Log, or does its *result* sediment to Memory? This RFC answers: Execution produces a
  **Result** (an event output), which flows to **Reflection → Memory**; the *process* leaves no store
  (C3). A `Execution Memory` would be a **second Memory** — the precise O1 violation the user flagged.
- **What starts it?** — The user's Q2: who may *invoke* Execution? This RFC answers: the **Execution
  Runtime** is *invoked by the Planning Authority handing it a Plan* (RFC-0106 §6.2). The Runtime is
  *not* a Constitution Authority; it is an infrastructure owner of the *act* (parallel to how a
  Provider "is not part of the Life", RFC-0100 §1.19).

But RFC-0100 §1.17 only *names* Execution as an act and §1.16 names Workflow rollback. They do
**not** say:

1. Whether Execution is a **State or a Process** — the crux of the user's Q1. If Execution is modeled
   as `State`, it would (a) need a lifecycle, (b) need a store, (c) need an Authority, and (d) reopen
   0100C. If it is modeled as a **Process/Event**, it is the *carrying act* with a beginning and an
   end, producing a Result, owning nothing.
2. Whether Execution may **mutate Plan** it runs — the god-object anti-pattern at the operational
   layer (e.g. `execution.revise_plan()`), parallel to RFC-0106 E4.
3. Whether Execution may **mutate Goal or Drive** — it must not; it consumes Plan only (V1/V3).
4. The exact reading of the matrix for Execution — it is *not* in `Produced by` (it produces no
   state) nor in `Mutated by` (it mutates nothing); it is an **edge** (Plan → Execution → Result),
   not a node. This is a *new kind* of Ontology citizen (an Event), and the RFC must confirm it fits
   the Foundation without a new state-class.

RFC-0107 is the seventh RFC that must answer these by **consuming** the trilogy — and, per the
Phase-2 rhythm, its *primary* job is to **validate** the Foundation, not to add to it. The framing
is restrained: *"Is the current Foundation sufficient to explain Execution as a Process/Event?"* —
not *"Design the Execution Engine."* The new wrinkle: Execution is the **first entity that is not a
derived state and not an owned state — it is an Event** (RFC-0100 §1.17 "act"). This tests whether
the Foundation's state-class taxonomy (Hybrid / Transient / Persistent, RFC-0100B §3) is
*sufficient* without forcing Execution into a state-class it should not occupy.

---

## 1. Problem

Without a real Execution specification, the downstream Life-layer RFCs (Memory, Reflection) will
implicitly assume a different "result回流" and silently disagree about:

- what Execution *is* (so Memory has no defined input, and the Planning Authority's Plan has nothing
  to run),
- whether Execution may be **mutated in place** by a re-planning loop (the god-object anti-pattern,
  rebuilt at the event layer — e.g. `execution.revise_plan()` reaching into Plan state it shouldn't
  own),
- whether Execution is **State or Process** (the user's Q1/P1) — if 0107 silently re-applies the
  "Owned, with a lifecycle" pattern from Goal (or the "Derived" pattern from Plan), it would
  *mis-classify* Execution and create a new lifecycle state, breaking the 0100B state-class
  distinction and re-opening 0100C),
- whether Execution **persists as a store** (C3) — if 0107 provisions an `Execution Log` that is
  read back as state, it duplicates the single-writer rule (O1) and opens 0100C for no
  constitutional reason.

Execution is the **seventh node of the flow** (`Identity → Need → Affect → Drive → Goal → Plan →
Execution`); an under-specified Execution corrupts the Result→Memory回流 and tempts a hidden second
Memory. RFC-0106 fixed Plan (Derived); this RFC fixes the next joint — and, for the first time, the
next joint is **an Event, not a state**.

---

## 2. Goals

- G1. **Classify Execution correctly**: prove it is **Process/Event** (an act carrying Tasks into the
  Universe), *not* a State (neither Owned nor Derived) — the central answer to the user's Q1/P1.
- G2. Specify the **consumption contract** — how the Execution Runtime takes a Plan and carries its
  Tasks, as a *semantic contract*, not code.
- G3. **Close the RFC-0100C question a seventh time** — but honestly: at the document level there is
  **no observed contradiction**; the *real* test is deferred to M1.7 (Execution Engine build). This
  RFC must NOT pre-activate 0100C on speculation (F1–F4).
- G4. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G5. Carry forward the Challenge discipline via `PROTOCOL-Life-Validation.md` V1–V6: apply V1
  (anti-bypass), V2 (downstream bypass), V3 (Authority Boundary, symmetric format, C2), V4
  (Persistence, C3), V5 (Cycle), V6 (Foundation Stability) — adapting V5 (cycle) to an *event*
  lifetime, and flagging V6's 0100C sub-question explicitly.
- G6. Fix the user's four questions: (1) Process/Event? (2) who starts it? (3) who changes what? (4)
  how does result flow back?

---

## 3. Non-goals

- NG1. **No implementation.** No `execution.py`, no scheduler, no tool executor, no retry policy, no
  distributed transaction, no workflow engine. The contract only. M1.7 (Execution Engine) is
  **explicitly deferred** until this RFC anchors (per the user's implementation order:
  `RFC-0107 Draft → Challenge → Anchor → M1.7 Execution Engine → Smoke Test`).
- NG2. **No new flow.** The `Plan → Execution → Result → Memory` flow is RFC-0100A's; this RFC only
  consumes it.
- NG3. **No Task / Capability / Provider spec.** How a Task is *carried* via Capability→Provider is
  the Infrastructure layer's job (RFC-0100 §1.18–§1.19); Execution only *is the carrying act*. 0107
  does not define tool dispatch — only that Execution consumes Plan's Task graph and emits a Result.
- NG4. **No Goal/Need/Affect/Memory/Drive/Plan-internals.** How those are produced is RFC-0101..0106;
  0107 reads Plan as a black box and reads Goal/Drive *only transitively through Plan* (V1).
- NG5. **No 0100C text — and explicitly NO pre-activation.** If 0107 concludes 0100C is needed, it
  must do so only via an *observed contradiction* surfaced at M1.7; this Draft proposes **none**. It
  anchors like its six predecessors (pending user Challenge, §13.4) with 0100C **deferred (7th)**.
- NG6. **No new Constitution Authority.** Execution's carrier is the **Execution Runtime** — an
  *infrastructure* owner of the act (parallel to Provider, RFC-0100 §1.19 "Not part of the Life"),
  **not** a 7th Constitution Authority in the `Identity → … → Planning` stack (rejected per the
  user's C2).

---

## 4. The Information Model of Execution (P1 — state type)

Execution is the life's **act of carrying a Plan's Tasks into the Universe** (RFC-0100 §1.17). It is
**not** a commitment (Goal), **not** the operational structure (Plan), **not** the executable unit
(Task), and **not** a Provider (infrastructure). It is an **Event / Process**: it *begins* when the
Execution Runtime is handed a Plan, *carries* each Task (via Capability→Provider), *ends* when the
last Task completes or fails, and *emits* a **Result**.

> **P1 (clarification — classifies an existing noun, does NOT add one).** *Execution is a Process /
> Event, not a State.* The test: a *State* (Owned Hybrid or Derived Transient, RFC-0100B §3) is a
> thing the life *is* or *has* — it occupies a slot in the derivation chain and has an owner/authority.
> Execution is an *act* the life *does* — it occupies an **edge** (Plan → Execution → Result), not a
> node. Therefore:
> - **No lifecycle as a state.** Execution *begins and ends*; it is not `adopted/active/done` like
>   Goal (RFC-0105). Its "lifetime" is the duration of carrying the Tasks; when it ends, it does not
>   transition — it *completes* (success/failure) and is gone.
> - **No storage authority.** There is no `Execution.save()` / `Execution.restore()`. (See C3 — only
>   the *Result* sediments to Memory, never the Execution itself.)
> - **No Authority owning Execution as a state.** The *carrier* is the **Execution Runtime** (an
>   infrastructure process owner, NG6), not a Constitution Authority. The Planning Authority *hands*
>   the Plan to the Runtime; it does not "own" Execution as a state.
>
> Therefore Execution is **classified outside the Derived/Owned split** — it is an **Event** in the
> flow (RFC-0100 §1.7), parallel to how Perception (RFC-0103) is an *input signal* and Workflow
> (RFC-0106 §4) is the *runtime shape of a Plan*. The state-class taxonomy (Hybrid / Transient /
> Persistent) is **sufficient** *because Execution is not forced into it*: it is an edge, not a node.
> This is the answer to the user's Q1: **Execution is the act of carrying Plan into the Universe, not
> a new State Owner.**

### 4.1 Structure of an Execution (the act's anatomy, not a state schema)

Execution is structured as an *occurrence*, not as a stored record. Its dimensions (all *derived from*
the Plan it consumes, none authored independently):

| Dimension | What it holds | Fed by | Notes |
|-----------|---------------|--------|-------|
| **Target Task graph** | the Tasks Execution carries, in Plan order | Plan (`task_graph` / `ordering`, RFC-0106 §4.1) | Execution *reads* this; it does not rewrite it. |
| **Carrying** | per-Task dispatch via Capability→Provider | Plan (`branching`) + infra | RFC-0100 §1.16 Workflow = the planned carrying shape. |
| **Rollback** | how to unwind on failure | Plan (`rollback`, planned by Planning Authority) | The *plan* carries rollback structure; the Runtime *executes* it. Execution does not *author* rollback. |
| **Result** | outcome (success/failure per Task, what was learned) | the act itself | Flows to Reflection → Memory (C3). Not stored as Execution. |

> The table is an *occurrence anatomy*, not a new Ontology noun-set. The Ontology (RFC-0100
> §1.17/§1.16) names Execution / Workflow; this RFC only specifies how the Runtime consumes them. A
> proposed "Execution dimension" that is really a Plan (structure) or a Task (unit) is rejected by the
> Ontology's negative rules (§1.9 / §1.15).

---

## 5. Consumption Contract (consumes RFC-0106 + RFC-0100A + RFC-0100 §1.17)

### 5.1 `Execution ← Execution Runtime (consumes Plan)`

The contract (semantic, not code):

- **Input (read-only):** a **Plan** (the Transient-Derived structure the Planning Authority built,
  RFC-0106). Execution reads the Plan's Task graph / ordering / branching / rollback as the shape of
  the act. It does **not** read Goal or Drive directly — those arrive *only transitively* through the
  Plan (V1). If Execution needs the Goal's target, it reads `plan.goal_target` (a *read-only copy* the
  Planning Authority embedded, RFC-0106 §4.1), never the Goal object.
- **Carrier (owns the act):** the **Execution Runtime** — an *infrastructure* owner of the carrying
  act (NG6). It *consumes* Plan and *produces* a Result. It is **not** a Constitution Authority; it
  sits beside the Constitution stack, invoked by the Planning Authority (§6.2).
- **Output**: a **Result** — an *event output* (per-Task success/failure + what was learned), emitted
  to Reflection → Memory (C3). The Result is **not** a stored Execution state.
- **Purity (of the boundary)**: Execution *consumes* Plan read-only; it does **not** mutate Plan,
  Goal, or Drive (C1/C2). The act has side effects *in the Universe* (via Provider, RFC-0100 §1.19 —
  allowed, that is its job) but **no side effects on the life's Constitution states**.

> The *shape* of carrying (retry policy, scheduling, distributed transaction) is an **implementation**
> RFC's concern (M1.7). This RFC fixes only that Execution **consumes a Plan** (§6), reads it
> read-only, and emits a Result that flows to Memory (C3) — never mutating a Constitution state.

### 5.2 The single legitimate path (carries forward RFC-0106 §5.2)

```
Memory              ← the ONLY other direct writer (Memory Authority, RFC-0100B)
   │
   ▼  (next cycle)
Identity = f(Memory)
   │
   ▼
Need = g(Identity)
   │
   ▼
Affect = h(Need, Perception)
   │
   ▼
Drive = i(Affect)        ← emits orientation (Drive Authority)
   │
   ▼  (read as orientation)
Planner / Planning Authority   ← READS Drive + Need + Affect + Memory, ADOPTS a Goal
   │
   ▼
Goal                ← Hybrid, durable; owned by Planning Authority (RFC-0105)
   │
   ▼  (decomposed — Planning Authority constructs Plan)
Plan = p(Goal, Drive, Context)   ← Transient-Derived; constructed by Planning Authority (RFC-0106)
   │
   ▼  (HANDED to the Execution Runtime — the Runtime consumes, does not construct)
Execution Runtime    ← consumes Plan, carries Tasks via Capability→Provider, emits Result
   │
   ▼
Result              ← event output (success/failure/learned)
   │
   ▼
Reflection → Memory  ← the ONLY long-term writer; result sediments here (RFC-0100 §3 / RFC-0105 §8)
```

Execution is reached **only** by this path. The Planning Authority is the *decomposition + handoff*
node; the Execution Runtime is the *carrier* it invokes. Execution's only *Constitution* effect is the
Result it emits *upward* to Memory (via Reflection) — it never writes a Constitution state itself.

> **Anti-bypass (V1, carries forward).** The chain is strictly `… → Plan → [Execution Runtime
> consumes] → Result → Memory`. Execution reads **only** the Plan it is handed. A direct
> `Execution = e'(Goal)` or `Execution = e''(Drive)` that reads around Plan is **forbidden** — it
> would bypass the Planning Authority's decomposition and let Execution re-interpret intent (the
> god-object rebuilt at the event layer). Goal/Drive reach Execution *only* through `plan.goal_target`
> / `plan.emphasis` (read-only copies, RFC-0106 §4.1).

> **Phase C Clarification C1 (Plan → Execution Boundary — F4, zero base edit).** *Plan describes the
> intended execution structure; Execution consumes Plan.* Two shapes are forbidden: (a)
> `execution.revise_plan(plan_id, new_step)` — Execution mutating the Plan it runs (collapses two
> authorities, violates O1; parallel to RFC-0106 E4); (b) `execution.save_plan(outcome)` — Execution
> archiving the Plan (a hidden Memory, C3). The only legitimate shape is `runtime.run(plan)` →
> `Result`; if the outcome suggests a better structure, the Result flows to Reflection → Memory, and
> the Planning Authority *constructs a new* Plan next cycle (RFC-0106 V5). Execution **consumes**, it
> does not **construct or revise**.
>
> **Added at Challenge (C1 — F4, zero base edit).** *Execution consumes a Plan instance, not the
> Goal that produced the Plan.* The Runtime reads only `plan.goal_target` / `plan.emphasis` (read-only
> copies the Planning Authority embedded, §4.1) — it never reads the Goal *object* and never mutates
> it. Forbidden shapes: `execution.complete(goal)` (marking Goal done from the event layer) and
> `execution.modify_intent(goal)` (editing Goal from Execution) — both are Authority leakage (the
> Goal's lifecycle is owned solely by the Planning Authority, RFC-0105 §6.2). The only legitimate
> inputs to Execution are the **plan instructions** it was handed and the **execution context**; Goal
> ownership is strictly out of reach. The legal flow stays `Goal → Plan → Execution → Result`.

### 5.3 The matrix reading (Execution is an edge, not a node)

RFC-0100B §2 lists states in `Produced by` / `Mutated by`. Execution appears in **neither** — it is
an **Event** in the flow (RFC-0100 §1.7), not a state with an owner. This is a *reading* of the
Ontology's event vocabulary (§1.17 "act"), not a new noun or a base edit. The carried-forward
clarification:

- **Execution is not in `Produced by`** — it produces no state; it produces a *Result* (an event
  output) that *Reflection* writes to Memory.
- **Execution is not in `Mutated by`** — nothing mutates Execution-as-a-state (it is not a state).
- **Plan remains `Produced by: Planning Authority (from Goal)`** (RFC-0106 §5.3) — Execution does not
  join that row.

This is **parallel to Perception** (RFC-0103 §3): Perception is an input signal, not a stored state,
so it is not in `Produced by` either. Execution is the *output-edge* analog: an output event, not a
stored state.

### 5.4 Persistence & Cycle — the Event form (C3 / V4 / V5)

`PROTOCOL-Life-Validation.md` V4 (anti-persistence) and V5 (cycle) were written for **Derived /
Transient** states. Execution is **neither** — it is an **Event**. The checks adapt cleanly:

- **V4 (Persistence) — Event form.** Execution has **no storage authority**. No `Execution.save()` /
  `Execution.restore()` / `ExecutionMemory` table / `Execution Log` read back as state. The *Result*
  (success/failure/learned) is reported to **Reflection**, which writes it to **Memory** (the only
  legitimate long-term writer, RFC-0100 §3). Execution itself is never the direct input to Reflection,
  and there is no `execution.save()`. A `Execution Log` that is later *read as state* would be a
  **second Memory** — a duplicate writer violating O1 (the precise risk the user named at C3).
- **V5 (Cycle) — Event form.** Execution exists **only for the duration of carrying its Plan's Tasks**;
  it *begins* when handed a Plan and *ends* on completion/failure. It is not *re-derived* (it was never
  a derived state) and not *restored*. Cross-cycle continuity of *capability* (what works) is
  *inherited* from Memory (via the sedimented skill/semantic record), never stored in an Execution.

> **C3 (clarification — answers the user's Q3).** *Execution produces a Result; the Result flows to
> Reflection → Memory. There is no Execution store.* A `Execution Log → Execution Memory` path is
> forbidden: it would be a **second Memory**, duplicating the single-writer rule (O1) and re-opening
> RFC-0100C for no constitutional reason. The *learned* content (what worked, what failed) sediments
> into **Memory** as a `skill`/`semantic` record and re-enters the cycle as Goal/Plan context next
> time (RFC-0106 §5.4). Execution's only downstream edge is `Result → Reflection → Memory`.
>
> **Added at Challenge (C3 — F4, zero base edit).** *Execution history is not Memory; only reflected
> results may enter Memory.* An `Execution Log` / `Execution History` is an *operational trace* (useful
> for infra debugging) — it must **not** be auto-promoted to a stored state. Forbidden:
> `execution.save_history()`, `execution.archive()`, `ExecutionMemory`. Doing so creates a **second
> Memory** — a duplicate writer violating M0's single-memory-source rule (O1). The learned content
> sediments into the *one* Memory via Reflection; the Execution's history stays in the infrastructure
> layer, never in the Constitution.

---

## 6. Ownership (consumes RFC-0100B) — and the *absence* of a new Authority

### 6.1 The Execution Runtime is a carrier, not a Constitution Authority

Per RFC-0100B §1.1 / §4 and RFC-0100 §1.17 / §1.19, the *act* of carrying Tasks is owned by an
**Execution Runtime** — an *infrastructure* component, explicitly **not** a Constitution Authority. It
is parallel to a **Provider** (RFC-0100 §1.19: "*Not* part of the Life. A Provider is infrastructure;
swapping it MUST NOT change Identity, Need, or Memory"). The Runtime *carries*; it does not *govern
> change* in the Constitution sense (it owns no state's derivation or adoption).

This is the **deliberate break** in the `Identity → Need → Affect → Drive → Planning` Constitution
stack: the stack ends at **Planning** (the last Constitution Authority, owner of Goal + Plan).
Execution is the *handoff to infrastructure* — the point where the life's intent meets the Universe.
No 7th Constitution Authority is added (rejected per the user's C2).

### 6.2 The naming discipline + authority boundary (symmetric format, V3 / C2)

The Planning Authority *constructs* Plan and *hands* it to the Execution Runtime. The Runtime may
consume Plan and emit Result, but must never mutate Goal / Drive / Plan-definition. The chain of
authorities must not be conflated, and the Runtime is *outside* the Constitution stack:

- **`Goal Authority` (= Planning Authority)** — owns Goal's *adoption + lifecycle* (RFC-0105 §6.2).
- **`Drive Authority`** — owns `i(Affect)`; owns the *Drive* state (RFC-0104 §6.2).
- **`Planning Authority`** — owns Plan's *construction*; reads Goal (owned) / Drive (orientation) /
  Context (situational) as signals (RFC-0106 §6.2). It is the *last* Constitution Authority.
- **`Execution Runtime`** — **infrastructure carrier**, *not* a Constitution Authority. Consumes Plan;
  emits Result. Owns the *act*, not any Constitution state.

> **Boundary statement (clarification, not a patch — symmetric with RFC-0103 §6.2 / RFC-0104 §6.2 B /
> RFC-0105 §6.2 / RFC-0106 §6.2, and directly answers the user's C2).** *Execution Runtime governs plan
> consumption and result production, not Goal mutation, not Drive mutation, not Plan re-definition.*
> It consumes the Plan it is handed and may *read* `plan.goal_target` / `plan.emphasis` (read-only
> copies), but must **never** mutate Plan, Goal, or Drive. Code of the shape
> `runtime.run(plan) -> result` is correct; `runtime.revise_plan(...)` / `runtime.set_goal(...)` /
> `runtime.update_drive(...)` is a **mis-assignment** (collapses authorities, violates O1). The
> Execution Runtime is **not** a 7th entry in the Constitution authority stack — it is the
> infrastructure the Planning Authority *invokes*.

> **Added at Challenge (C2 — F4, zero base edit).** *The Execution Runtime is an infrastructure carrier,
> not a Constitution Authority.* The firewall between the Constitution stack and the Runtime is explicit
> — there is an **X** between `Planning Authority` and `Execution Runtime`:
>
>     Identity Authority → Need Authority → Affect Authority → Drive Authority → Planning Authority
>         └── X (firewall: handoff to infrastructure) ──→ Execution Runtime  (NOT in the stack)
>
> The Runtime **owns**: consume Plan · invoke infrastructure (Capability→Provider) · produce Result.
> The Runtime **does NOT own**: life-state ownership · memory ownership · goal ownership · plan
> authorship. It is parallel to a Provider (RFC-0100 §1.19 "Not part of the Life") — an external carrier
> the Planning Authority *invokes*, never a single-writer of any Constitution state.

This is the **seventh** instance of the symmetric Authority-boundary format, and the **first** where
the "Authority" named is explicitly **outside** the Constitution stack — confirming the stack ends at
Planning and Execution is the *event* boundary:

```
Identity Authority  → governs Identity derivation,      not Memory mutation
Need Authority      → governs Need derivation,          not Identity mutation
Affect Authority    → governs Affect derivation,        not Need mutation, not Goal adoption
Drive Authority     → governs Drive derivation,          not Affect mutation, not Goal adoption
Planning Authority  → governs plan construction,         not Goal mutation, not Drive mutation
Execution Runtime   → governs plan consumption + result production,
                                                not Goal mutation, not Drive mutation, not Plan re-definition
                                                (infrastructure carrier — NOT a Constitution Authority)
```

> **Phase C Clarification C2 (Authority Boundary — F4, zero base edit).** The symmetric wording is now
> extended across the **event boundary**. The Constitution stack `Identity → Need → Affect → Drive →
> Planning` is a *stack of single-writers*, each owning exactly one state; it **ends at Planning**.
> Execution is the *handoff to infrastructure* — the Execution Runtime is a carrier (like a Provider),
> **not** a Constitution Authority, so it does not join the stack and does not acquire the right to
> edit any Constitution state. Each layer (including the Runtime, by contract) governs only its own
> product.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Execution is *internally driven*: the life's own Planning Authority *constructs* the Plan and *hands*
it to the Runtime; no external caller assigns the execution. The life *carries its own intent into the
Universe* — from how it has become (Goal) and how it is oriented (Drive), via its own Plan. This is
the definitional essence of "Life, not Agent" (RFC-0100 §1.2) at the action joint: an agent is *given*
a task to execute; a life *executes its own* Plan, derived from its own commitment.

### 7.2 Growth (G) — score: **high**

Because Execution's *Result* flows to Reflection → Memory (C3), and Memory reshapes the next cycle
(`Memory → … → Goal → Plan`), the life's *action* genuinely feeds its *growth*: experience → Memory →
shifted Drive → shifted Goal → shifted Plan → shifted Execution next time. RFC-0107 makes the
result回流 explicit at the action joint and guards it (no Execution store; only Result → Memory).

### 7.3 Maintainability (M) — score: **gate (must not regress) — and the 0100C watchpoint**

Single-consumer boundary ⇒ no hidden mutable Execution state owned by two components; the Runtime is a
stable contract independent of execution-graph internals ⇒ refactors don't ripple into the
Constitution. M is treated as a **non-regression gate**. Unlike RFC-0105 (which surfaced the
Goal-lifecycle transaction question at Reading (a)) and RFC-0106 (clean "no"), **RFC-0107 is the first
where 0100C is a live watchpoint**: Execution is where *atomicity / failure / rollback / external
side-effect* are realized. But per F1–F4, M/0100C may **not** be expanded on speculation — the real
test is deferred to **M1.7 (Execution Engine implementation)**, where an observed contradiction (if
any) routes via Foundation Evolution F1. This RFC changes **no** M/0100C surface.

---

## 8. This is NOT Execution (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a Goal.** Goal is the *committed target*; Execution is the *act of carrying* the Plan toward
  it. `Execution == Goal` forbidden (§4). Execution is *downstream of* Goal, not Goal itself.
- ✗ **NOT Owned / NOT Derived.** Execution has no lifecycle and no storage authority (§4 P1). An
  `Execution` with an `adopted/active/done` lifecycle, or an `executions.json` store, mis-classifies an
  Event as a State and re-opens 0100C (P1).
- ✗ **NOT a Plan.** Plan is the *operational structure* (RFC-0106); Execution is the *act of carrying*
  that structure. Execution *consumes* Plan; it is not one.
- ✗ **NOT a Task.** A Task is a single *executable unit* (RFC-0100 §1.9); Execution *carries* Tasks.
  Execution contains the carrying of many Tasks; it is not one.
- ✗ **NOT a Provider.** A Provider is infrastructure that satisfies a Capability (RFC-0100 §1.19 "Not
  part of the Life"); Execution is the *act* that uses a Provider. Execution is the carrying; the
  Provider is the means.
- ✗ **NOT writable/mutable by a re-planning loop.** `execution.revise_plan()`, `execution.save_plan()`
  are mis-assignments (collapse authorities, violate O1, §5.2 C1). Execution *consumes* Plan; only the
  Planning Authority may *construct/revise* Plan (RFC-0106 §8). The correct shape is
  `runtime.run(plan) -> result` then `planningAuthority.construct_plan(...)` *if* the Result warrants
  a new Plan next cycle.
- ✗ **NOT a store / second Memory.** An `Execution Log` read back as state, or a `ExecutionMemory`
  table, duplicates state written by Reflection and breaks O1 (§5.4 C3). Only the *Result* → Reflection
  → Memory is allowed.
- ✗ **NOT a Constitution Authority.** The Execution Runtime is an infrastructure carrier (§6.1), not a
  7th entry in the `Identity → … → Planning` stack. It owns the *act*, not any Constitution state.

---

## 9. RFC-0100C deferral — the seventh confirmation (with an honest caveat)

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) pending a real Life-layer RFC that needs
transaction, rollback, window, or lock semantics. RFC-0101..0106 validated "no" six times (0105 was the
first *concrete* test because Goal is Owned with a lifecycle, Reading (a) deferred; 0106 was the
cleanest "no" because Plan is Derived with no lifecycle). **RFC-0107 is the seventh test — and the
first where the question is *real*, not theoretical:**

Execution is where atomicity / failure / rollback / external side-effect *live*:

- RFC-0100 §1.16 names **Workflow rollback** (the *planned* rollback structure, owned by the Planning
  Authority when it builds Plan, RFC-0106 §4.1).
- RFC-0100 §1.17 names **carrying a Task into the Universe** (the *actual* side effects).
- RFC-0100 §1.19 names **Provider** as swappable infrastructure (side effects happen there).

So the Foundation *already names* the vocabulary for execution atomicity/rollback/side-effect. The
question is whether it *expresses* them at the Constitution level — or whether a real Execution Engine
(RFC-0107's M1.7) will need a Constitution-level transaction/permission concept the current theory
cannot name.

**Honest position at the document/validation level:**

> **RFC-0107 confirms: RFC-0100C stays deferred (7th consecutive confirmation) — provisionally.**
> At the *validation/document* level, the Foundation *already names* everything Execution needs as an
> Event: Execution as an act (§1.17), Workflow rollback as planned structure (§1.16, owned by Planning
> Authority), Execution Runtime as infrastructure carrier (§1.19 analog), and Result→Memory as the
>回流 (§3 / RFC-0105 §8). No new noun, no new state-class, no new Constitution Authority is required
> to *explain* Execution. Therefore the Gate verdict is **Foundation Stable → YES (provisional)**.
>
> **The 0100C watchpoint is moved to M1.7 (Execution Engine implementation).** Per F1–F4 — especially
> F2 (*the source of any Foundation change must be an observed contradiction, not "a better idea" or
> "might need transactions"*) — this Draft may **not** pre-activate 0100C. We have not built the
> Execution Engine; there is no *observed* contradiction yet. When M1.7 implements `execute_plan()`, if
> it discovers that execution atomicity / permission / rollback *cannot be expressed* by the current
> Foundation (e.g. a real need for a transaction boundary that the Ontology's "Workflow rollback" does
> not cover), that becomes an **observed contradiction** → it routes via the **Foundation Evolution
> Protocol (F1)**, proposed by the Execution Validation RFC — *not* a silent expansion in M1.7 code.

This is the **first** Life RFC to explicitly *bind* the 0100C test to a concrete implementation step
(M1.7), rather than deferring it abstractly. It is the faithful application of the user's brief:
*Existing Foundation cannot express execution atomicity/ownership* is the trigger — and that can only
be *observed* at build time, not asserted at draft time.

> **Phase C Clarification C4 (Transaction / Failure Watchpoint — F4, zero base edit).** *Transaction
> semantics belong to Execution implementation validation, not Ontology expansion.* Retry, rollback,
> and atomicity of carrying a Plan into the Universe are an **M1.7 implementation question**, not a
> RFC-0107 Foundation question. The Foundation (RFC-0100 §1.16 Workflow rollback / §1.19 Provider
> side-effects) already *names* the vocabulary; whether it *expresses* execution atomicity at the
> Constitution level is tested only when M1.7 builds `execute_plan()`. If M1.7 discovers that a legal
> ownership/transaction boundary **cannot be expressed** by the current Foundation, that is an
> **observed contradiction** (F2) → it routes to **Foundation Evolution F1**, proposed by the
> Execution Validation RFC — never a silent expansion in code. This is the first RFC to make the
> 0100C deferral *operational* (a named observation location + trigger + upgrade path), not merely a
> future possibility.

This RFC does **not** propose 0100C activation. It anchors like its six predecessors (pending user
Challenge, §13.4) with 0100C **deferred (7th)**.

---

## 10. Examples

**E1 — Consumption from a Plan (no external execution).**
Planning Authority constructs Plan P for Goal G (RFC-0106 E1). It hands P to the Execution Runtime:
`runtime.run(P)`. The Runtime carries P's Tasks via the available Capability→Provider, in P's `ordering`,
applying P's `branching`/`rollback`. No caller assigned the execution; it was driven by the life's own
Plan. Execution reads only P (V1); it does not reach Goal/Drive directly.

**E2 — Event lifetime (not a state).**
Execution E runs Plan P (cycle 1). Task t2 fails; P's `rollback.on_t2_fail` says `revert_to_t1` and the
context is `safe_to_retry`, so the Runtime reverts and retries. E *ends* on completion (success or
final failure). E is **gone** — not archived, not restored. Contrast E2 of RFC-0106 (Plan recomputed)
and the Goal lifecycle (RFC-0105 E2): Execution has *no* such lifecycle; it *completes* and ceases.

**E3 — Anti-bypass (V1).**
A "smart executor" proposes `execution.run_directly(goal)` to skip the Plan and carry the Goal
directly. **Rejected**: Execution must consume a Plan (§5.2); Goal reaches Execution *only* through
`plan.goal_target`. A direct `run_directly(goal)` bypasses the Planning Authority's decomposition
(parallel to RFC-0106 E3).

**E4 — Authority boundary (C2 — the user's named risk at the event seam).**
Execution finishes a Task and calls `execution.revise_plan(plan_id, new_step)` to adapt mid-run.
**Rejected** (§5.2 C1 / §8): Execution *consumes* Plan; only the Planning Authority may *construct/
revise* Plan. The correct shape is `runtime.run(plan) -> result`, then next cycle the Planning
Authority *constructs a new* Plan if the Result warrants it. `execution.revise_plan()` collapses two
authorities (violates O1) — exactly the **Authority leakage** the user flagged, now at the event seam.

**E5 — Anti-persistence (C3 / V4).**
Execution succeeds and calls `execution.save(outcome)` to keep a log for next time. **Rejected** (§5.4):
Execution has no storage authority. The *Result* is emitted to Reflection → **Memory** (as a
`skill`/`semantic` record). Next cycle, Memory reshapes Goal→Plan→Execution; the Execution itself is
never stored. A `Execution Log` read back as state would be a **second Memory** (O1 violation).

**E6 — No new Constitution Authority (C2 / §6.1).**
Someone proposes an "Execution Authority" that *owns* the act and may *mutate* Plan mid-flight.
**Rejected** (§6.1): the carrier is the **Execution Runtime**, an infrastructure owner (like a
Provider, RFC-0100 §1.19), **not** a Constitution Authority. It does not join the
`Identity → … → Planning` stack and acquires no right to edit Goal/Drive/Plan.

---

## 11. Limits & Open Questions

- **Q1 — Execution granularity.** This RFC fixes the *occurrence anatomy* (§4.1) but not whether Tasks
  are fine- or coarse-grained, or how retry is chosen. Left to the Execution Engine RFC (M1.7). *Not a
  constitution question.*
- **Q2 — Result shape.** The *Result* is an event output (success/failure/learned); its exact schema
  (per-Task? aggregate?) is implementation (M1.7). 0107 only fixes: Result flows to Reflection → Memory,
  never to an Execution store (C3).
- **Q3 — Re-execution trigger.** *When* the Planning Authority re-runs (failure, Goal shift, Context
  change) is its own deliberation (RFC-0106 Q3); 0107 only fixes that Execution *completes and ceases*,
  and a new Execution is *started* (never *restored*).
- **Q4 — Side effects in the Universe.** Execution's side effects happen *via Provider* (RFC-0100
  §1.19), outside the Constitution. This RFC fixes only that those side effects do **not** mutate
  Constitution states (Goal/Drive/Plan, C2); how Providers isolate side effects is infra.
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0107 surfaces **no new constitutional question** and does **not** consume or promote it.
- **Q6 — V5 adaptation (Event vs Derived).** RFC-0107 is the first Life RFC to apply V5 to an **Event**
  rather than a Derived state. The adaptation (begin/end, not derive/restore) is recorded here; if it
  proves a stable pattern for *event-layer* RFCs (Execution, Perception-input, Result-output), it may
  warrant a one-line precision in `PROTOCOL-Life-Validation.md` — flagged as a future F4, **not** done
  inline (keeps this RFC a pure validation).
- **Q7 — 0100C is now bound to M1.7.** RFC-0107 is the first to make the 0100C deferral *operational*:
  the real test moves from "abstract deferral" to "observe at M1.7 Execution Engine build". This is a
  *discipline* change, not a Foundation edit — recorded here so the next agent knows where to look.
- **Q8 — Workflow is not a new state (carried from RFC-0106 Q7).** RFC-0100 §1.16 names Workflow as the
  *runtime executable shape* of a Plan (ordering/branching/rollback), owned by the Planning Authority's
  Plan derivation. Execution *executes* the Workflow; it does not own it. No new noun in V6.

---

## 12. Evolution / next RFC

RFC-0107 is **consumable** by the next Life-layer RFC:

- **M1.7 Execution Engine** (DEFERRED until this RFC anchors) — the first code that *carries* a Plan.
  Per the user's implementation order, M1.7 starts **only after** RFC-0107 anchors; otherwise it risks
  reopening RFC-0100C by building Execution as a State. M1.7 implements `execute_plan(plan) -> result`
  as the *consumption* act, read-only on Plan, emitting a Result to Reflection → Memory. **Critically,
  M1.7 is where 0100C is truly tested**: if building it surfaces a real need for transaction/permission
  semantics the Foundation cannot express, that is an *observed contradiction* → Foundation Evolution
  F1 (proposed by a Validation RFC), not a silent code expansion.
- **RFC-0108+ Memory / Persona / Reflection / Relationship** (Foundation Validation #8+) — consume the
  `Result → Memory` edge this RFC fixes. They must not invent a second Memory (C3) and must respect
  that Execution emits only a Result, not a stored state.
- **RFC-0100C (if activated via F1 at M1.7)** — still deferred at the document level; the first *real*
  candidate is now concretely **M1.7's Execution Engine**, not abstract Plan/Memory. Activation requires
  an *observed* contradiction per F2, not this Draft's speculation.

### 12.1 Phase-2 rhythm (the Gate — OPEN, continues)

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy via F1; else Anchor) → next RFC.**

RFC-0107 has passed **Draft** and **anchored** (commit `PLACEHOLDERHASH`) after LAWP Phase C Challenge:
**APPROVED WITH CLARIFICATIONS**, Gate verdict **Foundation Stable → YES** (Gate A). All checks PASS with
zero base edits, Execution classified as Event (not State), 0100C deferred a seventh time — now an
*implementation watchpoint* bound to M1.7 (C4). The Foundation (frozen at RFC-0105 §14) stays frozen —
0107 is a *validation*, not a change. M1.7 Execution Engine is unlocked.

---

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0107 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to M1.7. Third Life RFC to cite
`PROTOCOL-Life-Validation.md` (V1–V6), and the first to apply it to an **Event** (not a derived state,
not an owned state) — and the first where 0100C is a *live* (not merely theoretical) question.

### 13.1 What was checked

| Foundation doc | Question 0107 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Execution needs as a *noun*? | ✅ Yes. Execution named as an *act* (§1.17); Workflow/rollback (§1.16); Capability/Provider (§1.18–§1.19); flow `… → Plan → Execution` (§1.7). **No new life-noun.** Execution is classified as an *Event* (edge), not forced into a state-class. |
| **RFC-0100A** (Dynamics) | Is the flow sufficient to *consume/produce* Execution? | ✅ Yes. `Plan → Execution → Result → Memory` specified (outer loop + §3). No new flow vocabulary. |
| **RFC-0100B** (Ownership) | Does the state-class + matrix + O1–O4 cover Execution's boundary? | ✅ Yes, with one *reading* (not a change): Execution is an **Event** (edge), not a node — so it is *absent* from `Produced by` / `Mutated by` (parallel to Perception, RFC-0103 §3). The Execution Runtime is infra (like Provider, §1.19), **not** a 7th Constitution Authority. |
| **RFC-0100C** (deferred) | Does Execution need Permissions/Transactions *at the document level*? | ⚠️ **Seventh "no" (provisional).** Execution is an Event with no lifecycle/store/authority of its own; atomicity/rollback are *named* (Workflow rollback §1.16, owned by Planning Authority; side effects via Provider §1.19). **No observed contradiction at the document level** → 0100C stays deferred. The *real* test is bound to **M1.7 (Execution Engine build)** per F1–F4 (F2: observed contradiction required). |

### 13.2 Holes found

- **None requiring a Foundation change at the document level.** Execution's classification as an Event
  is a *reading* of RFC-0100 §1.17 ("act"), not a new noun or a base edit. The Execution Runtime's
  infra-status is a *reading* of §1.19 (Provider analog), not a new Authority.
- **Carried-forward clarifications (design-intent discipline, V1–V6 of `PROTOCOL-Life-Validation.md`):**
  - **V1 (anti-bypass):** chain strictly `… → Plan → [Runtime consumes] → Result → Memory`; Execution
    reads only Plan (§5.2).
  - **V2 (downstream bypass):** Execution → Result → Memory only; no skip to Goal mutation (§5.2).
  - **V3 (Authority Boundary, symmetric format):** *Execution Runtime governs plan consumption and
    result production, not Goal/Drive/Plan mutation* (§6.2, C2). Seventh instance; first *outside* the
    Constitution stack.
  - **V4 (Persistence):** Execution has no storage authority; Result → Memory, not `Execution.save()`
    (§5.4, C3). A `Execution Log` read as state = second Memory (O1 violation).
  - **V5 (Cycle, adapted to Event):** Execution begins on handoff and ends on completion; not derived,
    not restored (§5.4).
  - **P1 (state-type classification):** Execution = Process/Event, **not** State (§4) — answers user Q1,
    prevents the mis-classification risk (new lifecycle/store/authority → 0100C).

### 13.3 Gate verdict (PROPOSED — pending user Challenge)

> **Foundation Stable? → YES (provisional, Gate A holds).**
> RFC-0107 is consumable by M1.7 Execution Engine; no Foundation patch at the document level. Execution
> is classified as an Event (not a State), the Execution Runtime is infra (not a 7th Constitution
> Authority), and 0100C stays deferred (7th consecutive confirmation) — **bound to M1.7** as the real
> test. The Foundation (frozen at RFC-0105 §14) remains frozen *at the document level*; any 0100C
> activation requires an *observed* contradiction at M1.7, routed via Foundation Evolution F1.

---

### 13.4 Challenge (LAWP Phase C — Life Validation Protocol V1–V6) — user Challenge: APPROVED WITH CLARIFICATIONS

Ran `PROTOCOL-Life-Validation.md` V1–V6 (third Life RFC to do so; first to apply to an **Event**).
**User Challenge (2026-08-04) confirmed the verdict**: Foundation Stable → YES, with three F4
clarifications (C1/C2/C3) + the C4 transaction watchpoint, all zero base edits.

| Life Validation check | Result (user) | Note |
|-----------------------|----------------------|------|
| **V1 Upstream bypass** | ✅ PASS | Execution reads only Plan (immediate upstream); no `Execution = e'(Goal)` / `e''(Drive)` (§5.2, C1). |
| **V2 Downstream bypass** | ✅ PASS | Execution → Result → Memory only; no skip to Goal mutation (§5.2). |
| **V3 Authority Boundary** | ✅ PASS | *Execution Runtime governs plan consumption + result production, not Goal/Drive/Plan mutation* (§6.2, C2). Seventh symmetric instance; first outside the Constitution stack (§6.1 X-break). |
| **V4 Persistence** | ✅ PASS | Execution has no storage authority; Result → Memory, not `Execution.save()` / `ExecutionMemory` (§5.4, C3). |
| **V5 Cycle (Event form)** | ✅ PASS | Execution begins on handoff (event begin/end, not derive/restore), ends on completion; no Execution state re-derived (§5.4). |
| **V6 Foundation Stability** | ✅ PASS | No base edit → Stable. Execution is an existing noun (§1.17), classified as Event (a reading). **0100C: 7th deferral — now an IMPLEMENTATION WATCHPOINT bound to M1.7** (observed-contradiction test required per F2); C4 records the trigger. |

**Clarifications applied at Challenge (all F4, zero base edits):** P1 (Execution = Process/Event, not
State, already in the Draft); **C1** (Plan→Execution boundary: `execution.complete(goal)` /
`execution.modify_intent(goal)` / `execution.revise_plan()` all forbidden — Execution consumes a Plan
instance, not Goal ownership); **C2** (Authority Boundary symmetric format extended across the event
seam with the explicit `X` firewall; Execution Runtime is infra carrier, not a 7th Constitution
Authority); **C3** (Persistence Boundary: Result → Memory; `Execution History` / `ExecutionMemory` /
`execution.save_history()` forbidden — no second Memory); **C4** (transaction/rollback/atomicity belong
to M1.7 implementation validation, not Ontology expansion — 0100C is now an *operational* watchpoint).

> **Challenge verdict (confirmed) → Foundation Stable: YES (Gate A holds).** All six Life Validation
> checks resolve without a Foundation edit at the document level. Execution is classified as an Event
> (no lifecycle, no storage, no Constitution Authority), 0100C stays deferred — **7th consecutive
> confirmation, now bound to M1.7 as the real test** (C4). RFC-0107 **anchors**; M1.7 Execution Engine
> unlocks (per the user's sequence:
> `RFC-0107 clarification → Anchor → M1.7 Execution Engine → Smoke Test`).

---

## 14. Phase 2A continuation — Foundation remains Frozen (pending Anchor)

RFC-0107 is **not** a freeze event — the Foundation was already frozen at RFC-0105 §14 (Phase 2A
complete, Gate A). RFC-0107 is a **continuation validation**: it proves the frozen Foundation also
explains the **first behavioral/event-layer entity** (Execution as an Event), with no edit at the
document level.

Therefore (pending Anchor):

1. **Phase 2A (Architect Phase) remains COMPLETE.** Foundation / Governance / Derived-States stay closed.
2. **Foundation stays FROZEN** — `RFC-0100` / `RFC-0100A` / `RFC-0100B` / `LAWP` / `Foundation Evolution` /
   `Life Validation` remain in maintenance mode (default no modification; only Foundation Evolution F1–F4
   may reopen).
3. **Agent Phase continues.** RFC-0107 is anchored; M1.7 (Execution Engine) unlocks — implementing
   `execute_plan(plan) -> result` as the *consumption* act (read-only on Plan, emitting Result to
   Reflection → Memory), with a smoke test that *is* the validation. **M1.7 is the real 0100C test**:
   if implementation surfaces a need for transaction/permission semantics the current theory cannot
   express, that is an *observed contradiction* → Foundation Evolution F1, not a silent code expansion.
4. **RFC-0100C stays deferred** (7th consecutive confirmation at the document level; now *bound to M1.7*
   as the operational test). It remains the *next* Foundation document, to be activated by an *observed*
   Execution transaction need at M1.7 — **not** by Execution's noun validation, and **not** by
   speculation in this Draft.

RFC-0107 has anchored (commit `PLACEHOLDERHASH`) after the Gate verdict **Foundation Stable → YES** was
confirmed at Challenge. The 0100C deferral is now operational (bound to M1.7); the next real checkpoint
is the M1.7 Execution Engine build.

---

## Appendix A — Validation notes

No Foundation patch is proposed at the document level. Execution's classification as an **Event** (an
edge in the flow, RFC-0100 §1.7/§1.17, not a node in the state-class taxonomy) is a *reading* of an
existing Ontology noun, not a new noun or a base edit. The Execution Runtime's status as
infrastructure (parallel to Provider, §1.19) is a *reading* of §1.19, not a new Authority. The carried-
forward V4/V5 Derived form is **adapted to an Event** (begin/end, not derive/restore) — recorded as a
possible future `PROTOCOL-Life-Validation.md` precision (Q6), not done inline (keeps this RFC a pure
validation). The parked Constitutional hypothesis (RFC-0101 Appendix A) is untouched. The 0100C
deferral is now made **operational** (bound to M1.7) — a discipline change, not a Foundation edit.
RFC-0107 is a pure validation: it proves the frozen Foundation explains Execution as an Event, the
first behavioral-layer entity, without reopening RFC-0100C — and binds the real 0100C test to the
M1.7 Execution Engine build, where an observed contradiction (if any) routes via Foundation Evolution
F1–F4.
