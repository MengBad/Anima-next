# RFC-0106: Plan (Life-6) — Foundation Validation #6

> **Status**: **Anchored** — Foundation Validation #6. Challenge verdict: **APPROVED WITH CLARIFICATIONS**
> (LAWP Phase C, 2026-08-04). This RFC is a frozen validation; it may not be modified except via the
> Foundation Evolution Protocol (F1–F4). The §13.4 Challenge table records the user verdict (PASS,
> with three F4 clarifications C1/C2/C3 applied — zero base edits).
> **Role**: **Foundation Validation #6**, *not* "Planner Design #6". Its job is to use the
> `Goal → Plan` boundary as a probe to prove whether the 0100 trilogy is sufficient to *explain*
> Plan — the **first Derived-state probe after Goal introduced an Owned state** (RFC-0105). Any
> hole found loops back via the Foundation Evolution Protocol (F1); this RFC may be the first to
> require it. Per the user's 2026-08-04 brief, the central risk at this joint is **Authority
> leakage / state-type mis-classification**, not transaction mechanics.
> **Phase**: Sixth Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0105 Goal (the
> `Goal → Plan` boundary) and transitively RFC-0101..0104.
> **Layer**: Life layer (Cognition) — RFC-0106 = Life-6
> **Depends-on**: RFC-0100 (§1.15 Planner / §1.16 Workflow / flow `Goal → Plan → Execution`),
> RFC-0100A (outer loop), RFC-0100B (state classes §3, ownership matrix §2, O1–O4), RFC-0101..0104
> (upstream, transitively), RFC-0105 (Goal = Hybrid, Planning Authority, Boundary B carried
> forward), `PROTOCOL-Life-Validation.md` (V1–V6)
> **Validates**: (a) RFC-0100 Ontology coverage for Plan (already named in §1.15/§1.16); (b)
> RFC-0100A loop sufficiency for `Goal → Plan → Execution`; (c) RFC-0100B state-class fit — is
> Plan **Derived** (like Need/Affect/Drive) or a new **Owned** state?; (d) RFC-0100C
> (Permissions/Transactions) — the deferred question, now testable a *sixth* time (Plan introduces
> no lifecycle/transaction, so expected deferral continues).
> **Scope**: The information model of Plan + its construction contract + its authority + its
> lifecycle-end — written *only* as the vehicle for that validation. **No implementation, no code,
> no new flow vocabulary, no new life noun** (Plan already exists in RFC-0100 §1.15/§1.16).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Plan gap

The project can now answer seven questions (per RFC-0105 §0 + this RFC):

- **What exists?** — Ontology (RFC-0100 §1.15): *Planner = the Cognition-layer component that
  decomposes a Goal into a Plan (graph of Tasks)*; §1.16: *Workflow = the executable shape of a
  Plan at runtime (ordering, branching, rollback)*. Plan is a **named** noun already — this RFC
  does **not** introduce it.
- **How does it flow?** — Dynamics (RFC-0100A outer loop): `Goal → Plan → Execution`; Plan is the
  decomposition of Goal that Execution consumes.
- **Who governs change?** — State Ownership (RFC-0100B): Plan's *builder* is the **Planning
  Authority** (the same authority that owns Goal, RFC-0105 §6). The question this RFC must answer
  is whether Plan is **Owned** (a second lifecycle state) or **Derived** (a transient operational
  structure of its Goal).
- **Where does it sit relative to Goal?** — RFC-0105 fixed the boundary: the Planning Authority
  *decomposes* Goal into a Plan (RFC-0105 §6.2). Goal is Owned; Plan is what Goal becomes when the
  Planning Authority reasons about *how* to reach it.
- **What is Plan *not*?** — RFC-0100 §1.15/§1.16 draw the boundary with Goal ("toward what" vs
  "how to get there") and Task (the executable unit inside a Plan). Plan is the *graph of Tasks*,
  not a single Task.
- **Does Plan need RFC-0100C?** — This is the **central continuation** of RFC-0105 §9. Plan is a
  *decomposition* (a derivation act `p`), not a commitment with a lifecycle — so it is expected to
  *not* need 0100C. But the user's brief names the real risk explicitly: **mistaking a
  short-lifecycle execution structure for an Owned state** (P1), and **Authority leakage** across
  the Goal→Plan→Execution seam (P2/P3).
- **Where does Plan end?** — The user's Q3: after Execution consumes a Plan, does it persist,
  enter Memory, or become Reflection input? This RFC answers: Plan is **transient**; its *outcome*
  sediments to Memory (via Reflection), the Plan itself does **not** persist (P3).

But RFC-0100 §1.15/§1.16 only *name* Plan/Planner/Workflow and the decomposition. They do **not**
say:

1. What actually **constitutes** a Plan record — its structure (task graph / ordering / branches)
   and how it is *born* (from Goal, not from scratch).
2. Whether Plan is **Derived or Owned** — the crux of P1. If `Plan = p(Goal, Drive, Context)` it is
   Derived (recomputed, transient); if it is given an independent lifecycle + store it would be a
   *second Owned state*, duplicating the Planning Authority's ownership scope and re-opening
   0100C prematurely.
3. Whether Plan **persists or is recomputed** — the analog of the prior four joints' "Derived, no
   storage" pattern, now applied to the *first* state downstream of an Owned state (Goal).
4. The exact reading of the matrix's `Produced by: Planning Authority (from Goal)` for Plan — the
   analog of the `Need (+ state)` / `Need + Perception` / `Produced by: Affect` / `Planning
   Authority governs Goal adoption` clarifications, *plus* the new wrinkle that Plan is Derived
   **from an Owned state** (Goal), so the Authority boundary must name both Goal and Drive as
   things the Planning Authority may *read* but never *mutate* (P2).

RFC-0106 is the sixth RFC that must answer these by **consuming** the trilogy — and, per the
Phase-2 rhythm, its *primary* job is to **validate** the Foundation, not to add to it. The framing
is restrained: *"Is the current Foundation sufficient to explain Plan?"* — not *"Design the
Planner."* The new wrinkle: Plan is the **first state that is Derived *from* an Owned state**
(Goal). This tests whether the Derived classification survives contact with an Owned upstream
without leaking Authority.

---

## 1. Problem

Without a real Plan specification, the downstream Life-layer RFCs (Execution, Memory) will
implicitly assume a different "operational structure" and silently disagree about:

- what Plan *is* (so Execution has no structure to run, and the Planning Authority's decomposition
  is undefined),
- whether Plan can be **mutated in place** by Execution (the god-object anti-pattern, rebuilt at the
  operational layer — e.g. `execution.revise_plan()` reaching into Plan state it shouldn't own),
- whether Plan is **Owned or Derived** (P1) — if 0106 silently re-applies the "Owned, with a
  lifecycle" pattern from Goal, it would *mis-classify* Plan and create a second lifecycle state,
  breaking the 0100B Derived/Owned distinction and re-opening 0100C),
- whether Plan **persists or is recomputed** (P3) — if 0106 provisions a `Plan` store, it duplicates
  the single-writer rule (O1) and opens 0100C for no constitutional reason.

Plan is the **sixth node of the flow** (`Identity → Need → Affect → Drive → Goal → Plan →
Execution`); an under-specified Plan corrupts Execution. RFC-0105 fixed Goal (Owned); this RFC
fixes the next joint — and, for the first time, the next joint is **Derived *from* an Owned state**.

---

## 2. Goals

- G1. Define the **information model** of Plan (its structure: task graph / ordering / branches) so
  downstream RFC-0107 Execution can reference a stable structure.
- G2. Specify the **construction contract** — how the Planning Authority forms Plan from Goal + Drive
  + Context — as a *semantic contract*, not code.
- G3. **Classify Plan correctly**: prove it is **Transient-Derived** (like Need/Affect/Drive), *not*
  a new Owned state — the central answer to the user's Q1/P1.
- G4. **Close the RFC-0100C question a sixth time**: show Plan introduces no lifecycle/transaction/
  permission-window, so 0100C stays deferred (continuing the five consecutive "no"s from 0101–0105).
- G5. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G6. Carry forward the Challenge discipline via `PROTOCOL-Life-Validation.md` V1–V6: apply V1
  (anti-bypass), V2 (downstream bypass), V3 (Authority Boundary, symmetric format, P2), V4
  (Persistence, P3), V5 (Cycle), V6 (Foundation Stability). This is the *second* Life RFC to run the
  Protocol (after 0105), and the *first* to apply it to a Derived state downstream of an Owned one.

---

## 3. Non-goals

- NG1. **No implementation.** No schema, no class, no `planner.py`, no prompt text. The contract only.
  M1.6 (Planner Interface) is **explicitly deferred** until this RFC anchors (per the user's
  implementation order: `RFC-0106 Draft → Challenge → Anchor → M1.6 Planner Interface → Smoke Test`).
- NG2. **No new flow.** The `Goal → Plan → Execution` flow is RFC-0100A's; this RFC only consumes it.
- NG3. **No Execution / Task spec.** Task execution is the **Execution Authority's** job (its own RFC,
  RFC-0107); Plan only *is the structure* Execution runs. 0106 does not define how a Task is carried
  out — only that Plan is the input the Execution Authority consumes.
- NG4. **No Goal/Need/Affect/Memory/Drive-internals.** How those are produced is RFC-0101..0105; 0106
  reads them as black boxes. Drive is read *as orientation* per RFC-0104 Boundary B / RFC-0105 §5.2.
- NG5. **No 0100C text.** If 0106 concludes 0100C is needed, it *proposes* its activation via the
  Foundation Evolution Protocol (F1) — it does **not** write 0100C inline.
- NG6. **No new Authority.** Plan's builder is the *existing* Planning Authority (RFC-0105 §6). This
  RFC does **not** introduce a "Plan Authority" (rejected per the user's Q2).

---

## 4. The Information Model of Plan (P1 — state type)

Plan is the life's **operational structure for reaching its committed Goal**: the decomposition of
Goal into an ordered graph of Tasks, shaped by the orientation Drive supplied and the current
Context. It is **not** the commitment (Goal) and **not** the executable unit (Task). Per RFC-0100
§1.15/§1.16, Plan is the *graph of Tasks*; Workflow is its *runtime executable shape* (ordering,
branching, rollback) — Workflow is **not a separate state-class**, it is the Plan's runtime form,
owned by the same Planning Authority derivation.

> **P1 (clarification — classifies an existing noun, does NOT add one).** *Plan is Transient-Derived,
> not Owned.* The test: a state is Owned (Hybrid) iff it has an independent *lifecycle* (adopt →
> active → done/abandoned, with rollback) and a *storage authority* of its own (RFC-0100B §3.4 / §4).
> Plan has **neither**:
> - **No independent lifecycle.** Plan exists *for the duration of its Goal's pursuit*; it is
>   constructed when the Planning Authority decides *how* to pursue the active Goal, and it is
>   *consumed* by Execution. When the Goal is retired or the pursuit fails, the Plan is *discarded*,
>   not *archived* — a new Plan is *constructed* next time, never the old one *restored*.
> - **No storage authority.** There is no `Plan.save()` / `Plan.persist()` / `Plan.update()` /
>   `Plan.restore()`. Any *recurring* plan-pattern (a strategy that works) sediments into **Memory**
>   (as a `skill` / `semantic` record) and re-enters the cycle as Goal context next time — exactly
>   like the prior four Derived states (RFC-0102 §8 / 0103 §5.4 / 0104 §5.4).
>
> Therefore `Plan = p(Goal, Drive, Context)` is a **derivation** (`p`), parallel to `g`/`h`/`i`. The
> Planning Authority is Plan's **Derivation Authority** (it owns the act `p`), but Plan itself is
> **Derived/Transient** — the same state-class as Need/Affect/Drive (RFC-0100B §3.2). This is the
> answer to the user's Q1: **Plan is Goal's derived execution structure, not a new Owned state.**

### 4.1 Structure of Plan (decomposition schema)

Plan is structured, not scalar. Its dimensions (all *derived from* Goal, not authored independently):

| Dimension | What it holds | Fed primarily by | Notes |
|-----------|---------------|------------------|-------|
| **Task graph** | the set of Tasks to perform | Goal (target/success) decomposed | "what to do" — the graph of work. |
| **Ordering** | sequence / dependencies among Tasks | Goal (success criterion) + Context | Distinguishes Plan from a bag of Tasks. |
| **Branching** | conditional / fallback paths | Context (available capabilities) + Drive (orientation) | RFC-0100 §1.16 "Workflow" = runtime form of this. |
| **Rollback** | how to unwind on failure | Context (safe-to-retry?) + Goal (horizon) | RFC-0100 §1.16 "Workflow" rollback; *not* a Plan lifecycle. |

> The list above is a *decomposition schema*, not a new Ontology noun-set. The Ontology (RFC-0100
> §1.15/§1.16) names Plan/Planner/Workflow; this RFC only specifies how the Planning Authority assembles
> these dimensions from Goal. A proposed "Plan dimension" that is really a Goal (target) or a Task
> (executable unit) is rejected by the Ontology's negative rules (§1.8–§1.9).

---

## 5. Construction Contract (consumes RFC-0100A + RFC-0105)

### 5.1 `Plan ← Planning Authority(Goal, reads Drive, reads Context)`

The contract (semantic, not code):

- **Inputs (read-only):** the life's **Goal** (the committed target — the *Owned* state the Planning
  Authority already owns), **Drive** (orientation — *read as orientation only*, per RFC-0104 Boundary
  B / RFC-0105 §5.2; Drive is an *input signal*, not the plan-decider), and **Context** (the current
  situational snapshot — available capabilities, current environment, resources — a *read-only*
  instantaneous input, parallel to how Affect reads Perception in RFC-0103).
- **Authority (owns the act):** the **Planning Authority** — it *constructs* the Plan. This is the
  constitutional fact that makes Plan Derived-but-owned-by-one-authority (§4 P1): the Planning
  Authority is the sole *constructor* of Plan, just as it is the sole *adopter* of Goal (RFC-0105 §6).
- **Output**: the Plan record (§4.1) for this construction — transient, consumed by Execution.
- **Purity**: the *construction* is a derivation `p(Goal, Drive, Context)`; it reads Goal/Drive/Context
  read-only and emits a fresh Plan. It is **not** a mutation of Goal (P2), and the Plan it emits is
  **not** persisted (P3).

> The *shape* of the Planning Authority's decomposition (task-graph algorithm, branching policy) is an
> implementation RFC's concern (M1.6). This RFC fixes only that Plan is **constructed by one authority**
> (§6), reads Goal/Drive/Context as signals, and is **Derived/Transient** (recomputed, never restored).

### 5.2 The single legitimate path (carries forward RFC-0105 §5.2)

```
Memory              ← the ONLY other direct write (Memory Authority, RFC-0100B)
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
Goal                ← Hybrid, durable; owned + lifecycle-maintained by Planning Authority (RFC-0105)
   │
   ▼  (decomposed — Planning Authority constructs Plan, reads Goal + Drive + Context)
Plan = p(Goal, Drive, Context)   ← Transient-Derived; constructed by Planning Authority; consumed by Execution
   │
   ▼
Execution → Tasks   ← runs the Plan's task graph (Cognition/Infra layer, RFC-0100 §1.15–§1.17)
```

Plan is reached **only** by this path. The Planning Authority is the *decomposition* node; Goal/Drive/
Context are merely the inputs it reads.

> **Anti-bypass (V1, carries forward).** The chain is strictly `… → Goal → [Planning Authority
> constructs] → Plan`. Plan reads **only** Goal (its immediate upstream Owned state) + Drive
> (orientation) + Context (situational snapshot). A direct `Plan = p'(Memory)` or `Plan = p''(Need)`
> that reads around Goal/Drive/Context is **forbidden** — it would bypass the Planning Authority's
> decomposition and rebuild the god-object. (Note: RFC-0100A §5's *Habit Formation* is a future
> Dynamics addendum describing a deliberate, separately-owned shortcut — it is **not** a Plan store and
> **not** authorized by this RFC.)

> **Phase C Clarification C1 (Upstream Boundary — F4, zero base edit).** *Drive provides operational
> orientation; Goal provides owned target identity; Plan requires Goal context.* The only legitimate
> reading is `Goal + Drive + Context → Plan`. Two paths are forbidden: (a) `Drive → Plan` that reads
> around Goal — it would make Goal a dispensable option and degrade the Owned intent into an afterthought;
> (b) `Drive → Plan → Goal` — Goal would become a derivative of orientation, an Owned state reversed out
> of a Derived one; this is exactly the Authority-leak the user named as the real risk at this joint. Drive
> is an *input signal* to `p`, never its *decider*; Goal's `target` / `success_criterion` remain the required
> content source (§5.3).

### 5.3 The matrix-row clarification (a reading, not a change)

RFC-0100B §2 lists Goal's `Produced by` as `Planning Authority (from Need)`. For Plan, the analogous
reading is:

- **`Produced by: Planning Authority (from Goal)`** — the Planning Authority is the *sole constructor*
  of Plan; Goal is the *content source* it decomposes. This mirrors the prior joints' clarifications:
  Goal is the *attributed input*, not a second authority (parallel to RFC-0102 §5.3 `Identity (+ state)`,
  RFC-0103 §5.3 `Need + Perception`, RFC-0104 §5.3 `Produced by: Affect`, RFC-0105 §5.3 `Planning
  Authority (from Need)`). Plan is the *next* step down: Goal is the input the Planning Authority reads
  to build Plan.
- Plan does **not** appear in `Mutated by` with a lifecycle (unlike Goal). Plan is Derived/Transient:
  its only "mutation" is *re-construction* (a fresh `p`), which is a derivation act, not a lifecycle
  mutation.

This is a **clarification of an implied matrix row**, parallel to the prior five joints. It is **not** a
Foundation change and does **not** trigger the Foundation Evolution Protocol.

### 5.4 Persistence & Cycle — the Derived form, applied to a post-Owned state (P3 / V4 / V5)

`PROTOCOL-Life-Validation.md` V4 (anti-persistence) and V5 (cycle) were written for the **Derived /
Transient** states. Goal (RFC-0105) was the *Owned* reframing. Plan is the **clean Derived case after an
Owned upstream** — and it confirms the V4/V5 Derived form holds:

- **V4 (Persistence) — Derived form, confirmed.** Plan has **no storage authority**. No `Plan.save()` /
  `Plan.persist()` / `Plan.update()` / `Plan.restore()`. Any long-term pattern that *looks* like a Plan
  (a strategy that works) belongs in **Memory** (the only legitimate long-term writer), where it
  re-enters the cycle as Goal context next cycle. This is the answer to the user's Q3 / P3: **Execution
  result → Memory, not Plan.save().**
- **V5 (Cycle) — Derived form, confirmed.** Plan exists **only for the duration of its Goal's pursuit**;
  it is *constructed* (not restored) each time the Planning Authority decides how to pursue the active
  Goal, and *discarded* when Execution consumes it (or when the Goal is retired). Cross-cycle continuity
  of *strategy* is *inherited* from Memory (via the sedimented skill/semantic record), never stored in
  Plan.

> **P3 (clarification — answers the user's Q3).** *Plan does not enter Memory; its outcome does.* When
> Execution finishes (success or failure), the **result** (what worked, what failed, what was learned)
> is reported to the Planning Authority, which forwards it to **Reflection**, which writes it to
> **Memory** (RFC-0100 §3 / RFC-0105 §8). Plan itself is never the direct input to Reflection, and there
> is no `plan.save()`. Reflection → Plan directly would be a single-writer violation (O1) — only the
> Planning Authority may construct Plan; Reflection may only inform Memory, which reshapes Goal → Plan
> next cycle.

> **Phase C Clarification C3 (Persistence Boundary — F4, zero base edit).** *Plan has no storage
> authority. Execution **results**, not Plans, enter Memory.* There is no `plan.save()` / `plan.persist()`
> / `PlanMemory` table. A "Plan archive" built by Execution would be a *hidden Memory* — a second writer
> duplicating the single-writer rule (O1) and re-opening RFC-0100C for no constitutional reason. Any
> strategy that proves reusable sediments into **Memory** as a `skill`/`semantic` record and re-enters the
> cycle as Goal context next time (§5.4 V4/V5).

---

## 6. Ownership (consumes RFC-0100B)

### 6.1 `Planning Authority` constructs Plan (it is Plan's Derivation Authority)

Per RFC-0100B §1.1 / §4 and RFC-0105 §6, the authority over Plan is the **Planning Authority** — the
*same* authority that owns Goal. It owns the act `p(Goal, Drive, Context)` that *constructs* Plan. Plan
is Derived, but its derivation is owned by exactly one authority (the single-writer rule, O1): only the
Planning Authority may build a Plan.

This authority is a **semantic contract** (RFC-0100B §1.1): it holds the sole right to *interpret* what
Plan is and to *set the policy* for its construction. The Planning Pipeline / Planner that runs the
construction is an **implementation** of that contract, not the authority (RFC-0100B §1.1).

### 6.2 The naming discipline + authority boundary (symmetric format, V3 / P2)

The name "Planning Authority" marks the *owner* of Goal **and** the *constructor* of Plan. It constructs
Plan from Goal (Owned) but must never mutate Goal, Drive, or Context to do so. The chain of authorities
must not be conflated:

- **`Goal Authority` (= Planning Authority)** — owns Goal's *adoption + lifecycle* (RFC-0105 §6.2).
- **`Drive Authority`** — owns `i(Affect)`; owns the *Drive* state (RFC-0104 §6.2).
- **`Planning Authority` (as Plan's Derivation Authority)** — owns Plan's *construction*; reads Goal
  (owned) / Drive (orientation) / Context (situational) as signals.

> **Boundary statement (clarification, not a patch — symmetric with RFC-0103 §6.2 C2 / RFC-0104 §6.2 B /
> RFC-0105 §6.2, and directly answers the user's P2).** *Planning Authority governs plan construction,
> not Goal mutation, not Drive mutation.* It constructs the Plan it derives and may *read* Goal (target) /
> Drive (orientation) / Context (situation), but must **never** mutate them. Code of the shape
> `planningAuthority.construct_plan(goal, drive, context)` is correct;
> `planningAuthority.set_goal(...)` or `planningAuthority.update_drive(...)` is a **mis-assignment**
> (collapses authorities, violates O1). The downstream `Execution` *consumes* Plan — the Planning
> Authority does not let Plan "adopt" an Execution; it *hands* Plan to the Execution Authority (RFC-0107).

This is the **sixth** instance of the symmetric Authority-boundary format, now extended across the
Owned→Derived seam (Plan is Derived *from* the Owned Goal):

```
Planning Authority governs plan construction,
not Goal mutation,
not Drive mutation.
```

The prior five (Identity / Need / Affect / Drive / Goal) all said "governs X derivation/adoption"; Plan
says "governs plan *construction*" — the Derived-wording the post-Owned case requires, and the explicit
guard against the **Authority leakage** the user named as the real risk at this joint.

> **Phase C Clarification C2 (Authority Boundary — F4, zero base edit).** The symmetric wording is now
> *fixed* across the whole derivation stack. Each Authority governs **only its own product**; it may read
> its upstream as a signal but must never mutate it:
>
> ```
> Identity Authority  → governs Identity derivation,  not Memory mutation
> Need Authority      → governs Need derivation,      not Identity mutation
> Affect Authority    → governs Affect derivation,    not Need mutation
> Drive Authority     → governs Drive derivation,     not Affect mutation
> Planning Authority  → governs plan construction,    not Goal mutation, not Drive mutation
> ```
>
> The chain `Identity → Need → Affect → Drive → Planning` is a *stack of single-writers*, each owning
> exactly one state. No layer may reach across to edit another's product (single-writer rule O1). The
> Planning Authority's two jobs — adopt Goal (RFC-0105 §6) and construct Plan (this RFC) — are both *its
> own*; owning both does not grant it the right to edit Identity/Need/Affect/Drive.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Plan is **internally constructed** by the Planning Authority from the life's own Goal + Drive + Context;
it has no external setter, no assigned plan from any caller. The life *works out its own how*, from how
it has become (Goal) and how it is oriented (Drive). This is the definitional essence of "Life, not
Agent" (RFC-0100 §1.2) at the operational joint: an agent is *given* a plan; a life *derives* its own,
from its own commitment.

### 7.2 Growth (G) — score: **high**

Because Plan is constructed from Goal (which evolves as Memory accumulates — RFC-0105 §7.2) + Drive
(orientation) + Context, the operational structure *genuinely evolves* as the life grows — "the life
reaches its goals differently tomorrow." Growth is *realized* here: experience → Memory → … → shifted
Drive → shifted Goal → shifted Plan construction. RFC-0106 makes the growth mechanism explicit at the
operational joint and guards it (only the Planning Authority may construct Plan; no external
`plan.set()`).

### 7.3 Maintainability (M) — score: **gate (must not regress)**

Single-constructor ownership ⇒ no hidden mutable Plan state owned by two components; authority is a
stable contract independent of Planner graph ⇒ refactors don't ripple into the constitution. M is treated
as a **non-regression gate**. RFC-0106 introduces **no** new M consideration (unlike RFC-0105, which
surfaced the Goal-lifecycle transaction question): Plan's construction is a pure derivation act with no
transaction boundary, so 0100C stays deferred and M is unchanged from the prior four Derived joints.

---

## 8. This is NOT Plan (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a Goal.** Goal is the *committed target* ("toward what"); Plan is the *operational structure*
  ("how to get there"). `Plan == Goal` forbidden (§4). Plan is *derived from* Goal, not Goal itself.
- ✗ **NOT Owned.** Plan has no independent lifecycle and no storage authority (§4 P1). A `Plan` with an
  `adopted/active/done` lifecycle, or a `plans.json` store, mis-classifies a Derived state as Owned and
  re-opens 0100C (P1).
- ✗ **NOT a Task.** A Task is a single *executable unit* (RFC-0100 §1.9); Plan is the *graph of Tasks*
  (RFC-0100 §1.15). Plan contains Tasks; it is not one.
- ✗ **NOT a memory record.** Plan is *transient*; the *strategy* it embodies sediments to **Memory**, not
  the Plan (§5.4 P3). But Plan itself is *recomputed*, never persisted.
- ✗ **NOT writable/mutable by Execution.** `execution.revise_plan()`, `execution.report_plan_done()` are
  mis-assignments (collapse authorities, violate O1). Execution *consumes* Plan (runs its Tasks); only the
  Planning Authority may *construct/revise* Plan. The correct shape is `planningAuthority.construct_plan(...)`
  after Execution reports outcome *to* the Planning Authority (RFC-0107).
- ✗ **NOT writable by Reflection.** Reflection writes to **Memory**, never to Plan (RFC-0100 §3). If a
  strategy is worth keeping, it sediments to Memory; the Planning Authority later constructs a new Plan
  *if* Memory reshapes Goal→Plan. `reflection.save_plan()` is the canonical single-writer violation.
- ✗ **NOT a second writer's store.** A `PlanDB` written by Execution, or a `plan_cache` owned by
  Reflection, duplicates state constructed by the Planning Authority and breaks O1.

---

## 9. RFC-0100C deferral — the sixth confirmation

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) pending a real Life-layer RFC that needs
transaction, rollback, window, or lock semantics. RFC-0101..0105 validated "no" five times (0105 was the
first *concrete* test, because Goal is Owned with a lifecycle; Reading (a) deferred 0100C because Goal's
owner is already named). **RFC-0106 is the sixth test — and it is the *cleanest* "no" yet:**

Plan is **Transient-Derived** (§4 P1). It has:

- **No lifecycle** — constructed and consumed, never archived/restored (§5.4 V5).
- **No storage authority** — no `Plan.save()` (§5.4 V4/P3).
- **No transaction boundary** — construction is a pure derivation act `p(Goal, Drive, Context)`,
  recomputed fresh; it does **not** participate in Goal's adoption transaction (RFC-0105 §9), and it
  introduces no new transaction of its own.

The only transaction in this region remains Goal's adoption (Reading (a) → deferred at 0105). Plan
construction is *inside* the Planning Authority's remit but *outside* any transaction syntax need.
Therefore:

> **RFC-0106 confirms: RFC-0100C stays deferred (6th consecutive confirmation).** Plan needs no
> permission/transaction/lock semantics the current theory cannot express. If a future RFC (Execution,
> RFC-0107) proves a need at the *Execution* boundary, it may be the first to activate 0100C via F1 — but
> Plan itself does not.

This RFC does **not** propose 0100C activation. It anchors like its five predecessors (pending user
Challenge, §13.4).

---

## 10. Examples

**E1 — Construction from signals (no external plan).**
Goal = { target: understand X, success: can explain X, horizon: weeks, priority: high }. Drive = {
approach: high on curiosity axis }. Context = { capability `knowledge` available, no `browser` }. Planning
Authority constructs Plan = { task_graph: [read X docs, summarize, explain], ordering: sequential,
branching: if summarize fails → retry via `knowledge`, rollback: revert to read }. No caller assigned the
Plan; it was derived from internal Goal + Drive + Context.

**E2 — Transience (Derived, not Owned).**
Plan P constructed for Goal G (cycle 1). Execution fails at task 2. Planning Authority *constructs a new*
Plan P' (cycle 2) — this is a *fresh derivation* `p(G, Drive, Context')`, **not** a *restore* of P. P is
discarded. Contrast E2 of RFC-0105 (Goal retired/adopted via lifecycle) — Plan does **not** behave that
way; it is recomputed, never rolled forward across cycles.

**E3 — Anti-bypass (V1).**
A "context module" proposes `plan.from_recent_episodes(recent)` to make Plan reflect recent experience
directly. **Rejected**: experience first sediments into Memory, reshapes Need→Affect→Drive→Goal, then the
Planning Authority constructs Plan from Goal. A direct `from_recent_episodes` path bypasses the Planning
Authority's decomposition.

**E4 — Authority boundary (P2 — the user's named risk).**
Execution finishes a Task and calls `execution.revise_plan(plan_id, new_step)`. **Rejected** (§8):
Execution *consumes* Plan (runs Tasks); only the Planning Authority may *construct/revise* Plan. The
correct shape is `planningAuthority.construct_plan(goal, drive, context')` after Execution reports outcome
*to* the Planning Authority. `execution.revise_plan()` collapses two authorities (violates O1) — this is
exactly the **Authority leakage** the user flagged as the real risk at this joint.

**E5 — Anti-persistence (P3 / V4).**
Execution succeeds and calls `plan.save(outcome)` to keep the plan for next time. **Rejected** (§5.4):
Plan has no storage authority. The *outcome* is reported to the Planning Authority → Reflection →
**Memory** (as a `skill`/`semantic` record). Next cycle, Memory reshapes Goal→Plan; the Plan itself is
never stored. `plan.save()` would open 0100C for no constitutional reason.

---

## 11. Limits & Open Questions

- **Q1 — Plan granularity.** This RFC fixes the *decomposition dimensions* (§4.1) but not whether Tasks
  are fine- or coarse-grained, or how branching is chosen. Left to the Planner RFC (M1.6 / RFC-0107).
  *Not a constitution question.*
- **Q2 — Plan ↔ Drive feedback.** `Drive → Plan` is allowed *as a read* (orientation at construction);
  `Plan → Drive` is **forbidden** (would re-open the intra-cycle loop RFC-0103 Q2 / RFC-0104 Q2 forbade).
  Cross-cycle influence is via Memory (outcome → Memory → Goal → Plan).
- **Q3 — Re-plan trigger.** *When* the Planning Authority constructs a new Plan (execution failure, Goal
  shift, Context change) is its own deliberation (implementation RFC), not 0106's. 0106 only fixes:
  Planning Authority is the **sole constructor**; Plan is **never restored** (§5.4 V5).
- **Q4 — Multiple / sub-Plans.** How the Planning Authority nests or prioritizes Plans is its own
  deliberation (implementation RFC); 0106 only fixes that the Planning Authority is the sole constructor
  of all of them.
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0106 surfaces **no new constitutional question** and does **not** consume or promote it.
- **Q6 — Life Validation V4/V5 Derived-vs-Owned split (carried forward from RFC-0105 Q6).** RFC-0106 is
  the clean **Derived** case after an Owned upstream (Goal). It *confirms* that V4/V5 Derived form holds
  for Plan and that the split (Derived: no storage/per-cycle; Owned: single-writer/lifecycle) is correct.
  No new Protocol action; the split remains a separate F4 precision (flagged at 0105 Q6).
- **Q7 — Workflow is not a new state.** RFC-0100 §1.16 names "Workflow" as the *runtime executable shape*
  of a Plan (ordering/branching/rollback). 0106 clarifies Workflow is the Plan's runtime form, **not** a
  separate state-class beyond Plan — owned by the same Planning Authority derivation. No new noun in V6.

---

## 12. Evolution / next RFC

RFC-0106 is **consumable** by the next Life-layer RFC:

- **RFC-0107 Execution** (Foundation Validation #7) consumes Plan as the structure the Execution
  Authority runs. 0107 must not redefine the operational structure; it reads Plan as a fixed input to
  Task execution and must respect the Authority boundary (Execution *consumes*, does not *construct* or
  *mutate* Plan — E4).
- **M1.6 Planner Interface** (DEFERRED until this RFC anchors) — the first code that touches `Goal +
  Plan + Drive + Execution`. Per the user's implementation order, M1.6 starts **only after** RFC-0106
  anchors; otherwise it risks reopening RFC-0100C by building Plan as Owned. M1.6 implements
  `construct_plan(goal, drive, context)` as the `p` derivation, single-writer, transient.
- **RFC-0100C (if activated via F1)** — still deferred; the first *real* candidate remains Memory
  (consolidation / sleep rollback) or a future Execution transaction need — **not** Plan.

### 12.1 Phase-2 rhythm (the Gate — OPEN, continues)

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy via F1; else Anchor) → next RFC.**

RFC-0106 has passed **Draft** and **Challenge** (LAWP Phase C, four-question + V1–V6). The Gate
question *"Foundation Stable?"* returned **YES** (all checks PASS with zero base edits, Plan
classified Derived, 0100C deferred a sixth time). The user Challenge confirmed **APPROVED WITH CLARIFICATIONS**;
RFC-0106 **anchors** and unlocks M1.6. The Foundation (frozen at RFC-0105 §14) stays frozen — 0106 is a
*validation*, not a change.

---

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0106 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to RFC-0107 / M1.6. Second Life RFC to cite
`PROTOCOL-Life-Validation.md` (V1–V6), and the first to apply it to a Derived state *derived from an
Owned state* (Goal).

### 13.1 What was checked

| Foundation doc | Question 0106 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Plan needs as a *noun*? | ✅ Yes. Plan/Planner/Workflow named (§1.15/§1.16); flow `Goal → Plan → Execution` (§1.15/§130); negative rules exclude Goal/Task. **No new life-noun.** |
| **RFC-0100A** (Dynamics) | Is the flow sufficient to *produce/consume* Plan? | ✅ Yes. `Goal → Plan → Execution` specified (outer loop). No new flow vocabulary. |
| **RFC-0100B** (Ownership) | Does the state-class + matrix + O1–O4 cover Plan's authority? | ✅ Yes, with one *clarification* (not a change): `Produced by: Planning Authority (from Goal)` read as sole-constructor + content-source (§5.3). Plan = Transient-Derived (§4 P1), the mirror of Need/Affect/Drive. |
| **RFC-0100C** (deferred) | Does Plan need Permissions/Transactions? | ✅ **Sixth "no".** Plan is Derived/Transient: no lifecycle, no storage, no transaction (§4 P1 / §5.4 / §9). 0100C stays deferred. |

### 13.2 Holes found

- **None requiring a Foundation change.** The matrix clarification (§5.3) is a reading of an implied row,
  not an edit. Plan's classification as Derived is a *classification of an existing noun*, not a new noun.
- **Carried-forward clarifications (design-intent discipline, V1–V6 of `PROTOCOL-Life-Validation.md`):**
  - **V1 (anti-bypass):** chain strictly `… → Goal → [Planning Authority constructs] → Plan`; Plan reads
    only Goal + Drive + Context (§5.2).
  - **V2 (downstream bypass):** Plan → Execution only; no skip (§5.2).
  - **V3 (Authority Boundary, symmetric format):** *Planning Authority governs plan construction, not Goal
    mutation, not Drive mutation* (§6.2, P2). Sixth instance; first across the Owned→Derived seam.
  - **V4 (Persistence):** Plan has no storage authority; outcome → Memory, not `Plan.save()` (§5.4, P3).
  - **V5 (Cycle):** Plan constructed (not restored) per pursuit; discarded after Execution (§5.4).
  - **P1 (state-type classification):** Plan = Transient-Derived, not Owned (§4) — answers user Q1, prevents
    the mis-classification risk.

### 13.3 Gate verdict (CONFIRMED — Foundation Stable: YES)

> **Foundation Stable? → YES (proposed, Gate A holds).**
> RFC-0106 is consumable by RFC-0107 Execution and unlocks M1.6 Planner; no Foundation patch. Plan is
> classified Derived (not Owned), 0100C stays deferred (6th consecutive confirmation), and the Planning
> Authority's boundary is fixed against the Authority leakage the user named. The Foundation (frozen at
> RFC-0105 §14) remains frozen.

---

### 13.4 Challenge (LAWP Phase C — Life Validation Protocol V1–V6) — **user Challenge: APPROVED WITH CLARIFICATIONS**

Ran `PROTOCOL-Life-Validation.md` V1–V6 (second Life RFC to do so; first to apply to a Derived state
*from* an Owned upstream). **User Challenge (2026-08-04): APPROVED WITH CLARIFICATIONS** — all six checks
PASS; three F4 clarifications (C1/C2/C3) applied with zero base edits.

| Life Validation check | Result (user Challenge) | Clarification applied (F4) | Note |
|-----------------------|--------------------------|----------------------------|------|
| **V1 Upstream bypass** | ✅ PASS | **C1** | Plan reads only Goal (immediate upstream) + Drive (orientation) + Context; `Drive → Plan` and `Drive → Plan → Goal` forbidden (§5.2). |
| **V2 Downstream bypass** | ✅ PASS | — | Plan → Execution only; no skip (§5.2). |
| **V3 Authority Boundary** | ✅ PASS | **C2** | *Planning Authority governs plan construction, not Goal mutation, not Drive mutation* (§6.2, P2). Sixth symmetric instance; full authority stack fixed. |
| **V4 Persistence** | ✅ PASS | **C3** | Plan has no storage authority; outcome → Memory, not `Plan.save()` (§5.4, P3). |
| **V5 Cycle** | ✅ PASS | — | Plan constructed (not restored) per pursuit; discarded after Execution (§5.4). |
| **V6 Foundation Stability** | ✅ PASS | — | No base edit → Stable. Plan is an existing noun (RFC-0100 §1.15); classification is a reading, not a change. |

**Clarifications applied (all F4, zero base edits):** P1 (Plan = Transient-Derived, not Owned); P2 (Authority
Boundary symmetric format across Owned→Derived seam); P3 (outcome → Memory, not `Plan.save()`); §5.3 matrix-row
reading; **plus three Phase C clarifications** — **C1** Upstream Boundary (`Drive → Plan → Goal` forbidden),
**C2** Authority Boundary (full authority stack fixed, each governs only its own product), **C3** Persistence
Boundary (Plan has no storage authority; results, not Plans, enter Memory). All three are F4: they shrink
future interpretation space, add no theory, and modify no Foundation text.

> **Challenge verdict (CONFIRMED) → Foundation Stable: YES (Gate A holds).** All six Life Validation checks
> resolve without a Foundation edit. Plan is classified Derived (no lifecycle, no storage), 0100C stays
> deferred (6th consecutive confirmation), and the Authority-boundary guard closes the leakage risk the
> user named at this joint. RFC-0106 **anchors**; M1.6 Planner unlocks (per the user's sequence:
> `RFC-0106 clarification → Anchor → M1.6 Planner Interface → RFC-0107 Execution Validation`).

---

## 14. Phase 2A continuation — Foundation remains Frozen

RFC-0106 is **not** a freeze event — the Foundation was already frozen at RFC-0105 §14 (Phase 2A complete,
Gate A). RFC-0106 is a **continuation validation**: it proves the frozen Foundation also explains the
*first Derived state downstream of an Owned state* (Plan from Goal), with no edit.

Therefore:

1. **Phase 2A (Architect Phase) remains COMPLETE.** Foundation / Governance / Derived-States stay closed.
2. **Foundation stays FROZEN** — `RFC-0100` / `RFC-0100A` / `RFC-0100B` / `LAWP` / `Foundation Evolution` /
   `Life Validation` remain in maintenance mode (default no modification; only Foundation Evolution F1–F4
   may reopen).
3. **Agent Phase continues.** RFC-0106 is anchored; M1.6 (Planner Interface) unlocks — implementing
   `construct_plan(goal, drive, context)` as the `p` derivation, single-writer, transient, with a smoke
   test that *is* the validation (per M1 discipline). Foundation changes only if *implementation* surfaces a
   contradiction no Life RFC foresaw — then via Foundation Evolution F1–F4.
4. **RFC-0100C stays deferred** (6th consecutive confirmation). It remains the *next* Foundation document,
   to be activated by a concrete Execution/Memory transaction need, not by Plan's noun validation.

RFC-0106's user Challenge is confirmed (APPROVED WITH CLARIFICATIONS); it is now **anchored** (Gate A → Foundation Stable: YES), unlocking M1.6 per the user's sequence.

---

## Appendix A — Validation notes

No Foundation patch is proposed. The matrix clarification (§5.3) is a permitted reading of an implied row;
Plan's classification as Transient-Derived (§4 P1) is a *classification of an existing Ontology noun*
(RFC-0100 §1.15/§1.16), not a new noun or a base edit. The carried-forward V4/V5 Derived form (§5.4) confirms
the Derived/Owned split flagged at RFC-0105 Q6 — no new Protocol action. The parked Constitutional hypothesis
(RFC-0101 Appendix A) is untouched. RFC-0106 is a pure validation: it proves the frozen Foundation explains
Plan, the first Derived state *derived from* an Owned state, without reopening RFC-0100C.
