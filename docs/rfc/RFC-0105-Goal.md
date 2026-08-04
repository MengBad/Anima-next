# RFC-0105: Goal (Life-5) — Foundation Validation #5

> **Status**: **Anchored** — Foundation Validation #5 complete. **Gate A verdict: Foundation Stable → YES**
> (Reading (a) adopted; §9 / §13.4). This is the **last Life RFC of Phase 2A**; its Stable verdict **freezes
> the Foundation** (see §14). **First Life RFC to validate a Hybrid / Owned (non-derived) state** — see §6 / §9.
> **Role**: **Foundation Validation #5**, *not* "Goal Design #5". Its job is to use Goal as a probe
> to prove whether the 0100 trilogy is sufficient to *explain* Goal — including the deferred
> RFC-0100C question. Any hole found loops back via the Foundation Evolution Protocol (F1); this RFC
> may be the *first* to require it (§9).
> **Phase**: Fifth Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0104 Drive (the
> Planner *reads* Drive as orientation input to Goal adoption) and transitively RFC-0101..0103.
> **Layer**: Life layer (Mind) — RFC-0105 = Life-5
> **Depends-on**: RFC-0100 (§1.8 Goal noun), RFC-0100A (outer loop + `Goal → Plan → Execution`),
> RFC-0100B (Goal = Hybrid, Planning Authority, ownership matrix §2, invariants O1–O4, §4 Hybrid
> callout), RFC-0101/0102/0103 (inputs, transitively), RFC-0104 (Drive → Planner reads → Goal),
> `PROTOCOL-Life-Validation.md` (V1–V6 — **first RFC to cite it**)
> **Validates**: (a) RFC-0100 Ontology coverage for Goal; (b) RFC-0100A loop sufficiency for
> `Goal → Plan → Execution`; (c) RFC-0100B ownership matrix + O1–O4 (Planning Authority owns Goal's
> lifecycle); (d) **RFC-0100C (Permissions/Transactions) — the deferred question, now concretely
> testable for the first time**
> **Scope**: The information model of Goal + its adoption contract + its authority + its lifecycle —
> written *only* as the vehicle for that validation. No implementation, no code, no new *flow*
> vocabulary, no new *life* noun (Goal already exists in RFC-0100 §1.8).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Goal gap

The project can now answer six questions (per RFC-0104 §0 + this RFC):

- **What exists?** — Ontology (RFC-0100 §1.8): *Goal is a committed target the life intends to reach* —
  durable (months-scale), the life's *current commitment*, the bridge between intending (Drive) and
  doing (Plan/Execution).
- **How does it flow?** — Dynamics (RFC-0100A outer loop): `Goal → Plan → Execution`; Goal is adopted
  by the Planning layer from Need + Affect + Memory (and reads Drive as orientation).
- **Who governs change?** — State Ownership (RFC-0100B §4): Goal is the **canonical Hybrid** —
  *owned and lifecycle-maintained* by the **Planning Authority**; it is the one non-derived state that
  legitimately appears in `Mutated by` (its own lifecycle only).
- **Where does it sit relative to Drive?** — RFC-0104 fixed the boundary: the Planner *reads* Drive
  (orientation) and *adopts* Goal; Drive does **not** adopt Goal (Boundary B).
- **What is Goal *not*?** — RFC-0100 §1.8 draws the boundary with Need ("why now" vs "toward what") and
  Task (minutes-scale, fungible vs months-scale, survives task failure).
- **Does Goal need RFC-0100C?** — This is the **central question** of RFC-0105. 0100B deferred 0100C
  (State Permissions & Transactions) *pending a real Life RFC that needs it*. Goal is the first state
  with a genuine **lifecycle** (adopt → active → done/abandoned) and a genuine **transaction** (adopt
  new, roll back old). RFC-0105 is the first RFC that can answer the deferral concretely.

But RFC-0100 §1.8 only *names* Goal and its boundaries. It does **not** say:

1. What actually **constitutes** a Goal record — the commitment dimensions (target / success criterion
   / horizon / priority) and how they combine.
2. How Goal is **adopted** — the Planning Authority's deliberation over Need + Affect + Memory (+ Drive
   orientation), and the adoption gating.
3. Whether Goal's **lifecycle** (adopt / active / done / abandoned, with rollback) needs the deferred
   RFC-0100C (Permissions / Transactions) — the deferral's *real* test, after four consecutive "no"
   from the derived states (0101–0104).
4. The exact reading of the matrix's `Produced by: Planning Authority (from Need)` and `Mutated by:
   Planning Authority (lifecycle of this Goal only)` — the analog of the `Need (+ state)` /
   `Need + Perception` / `Produced by: Affect` clarifications at the prior three joints, *plus* the new
   wrinkle that Goal is **Owned, not Derived**.

RFC-0105 is the fifth RFC that must answer these by **consuming** the trilogy — and, per the Phase-2
rhythm, its *primary* job is to **validate** the Foundation, not to add to it. As with 0101–0104, the
framing is restrained: *"Is the current Foundation sufficient to explain Goal?"* — not *"Design Goal."*
The new wrinkle: Goal is the **first Hybrid/Owned state**, so this RFC is also the first real test of
the 0100C deferral.

---

## 1. Problem

Without a real Goal specification, the downstream Life-layer RFCs (Plan, Execution, Memory) will
implicitly assume a different "commitment" and silently disagree about:

- what Goal *is* (so the Planning Authority's adoption is undefined, and Execution has no target),
- whether Goal can be **mutated in place** by anything other than the Planning Authority (the god-object
  anti-pattern, rebuilt at the commitment layer — e.g. `execution.report_goal_done()` reaching into
  Goal state it shouldn't own),
- whether Goal **persists or is derived** (the prior four joints were all Derived/Transient; Goal is the
  *exception* — if 0105 silently re-applies the "derived, no storage" pattern, it would *mis-classify*
  Goal and break the 0100B Hybrid treatment),
- whether Goal's **adoption is a transaction** (adopt new + abandon old atomically) — the deferred
  0100C question, now unavoidable because Goal is the first state with a lifecycle.

Goal is the **fifth node of the flow** (`Identity → Need → Affect → Drive → Goal → Plan →
Execution`); an under-specified Goal corrupts Plan and Execution. RFC-0104 fixed Drive; this RFC fixes
the next joint — and, for the first time, the next joint is **Owned, not Derived**.

---

## 2. Goals

- G1. Define the **information model** of Goal (its commitment dimensions) so downstream RFCs can
  reference a stable structure.
- G2. Specify the **adoption contract** — how the Planning Authority forms Goal from Need + Affect +
  Memory (+ Drive orientation) — as a *semantic contract*, not code.
- G3. Explain Goal as the **canonical Hybrid (Owned) state** (RFC-0100B §3.4 / §4) and record its
  authority + lifecycle. This is the first RFC to do so — the prior four were all Derived/Transient.
- G4. **Resolve (or definitively defer) the RFC-0100C question** for the first time: does Goal's
  lifecycle/transaction need 0100C, or is 0100B's Hybrid treatment sufficient to *explain* Goal?
- G5. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G6. Carry forward the Challenge discipline (now formalized as `PROTOCOL-Life-Validation.md` V1–V6):
  apply V1 (anti-bypass), V2 (downstream bypass), V3 (Authority Boundary, symmetric format), and the
  **Hybrid reframing** of V4/V5 for an Owned state (§5.4, §6.2). This is the *first* Life RFC to run the
  Protocol rather than re-derive the checklist.

---

## 3. Non-goals

- NG1. **No implementation.** No schema, no class, no DB table, no prompt text. The contract only.
- NG2. **No new flow.** The two-loop flow is RFC-0100A's; this RFC only consumes it.
- NG3. **No Plan / Execution / Task spec.** Plan decomposition is the **Planning Authority's** job
  (its own RFC, downstream); Goal only *is the target* Execution reads. 0105 does not define how a Plan
  is built — only that Goal is the input the Planning Authority consumes.
- NG4. **No Need/Affect/Memory/Drive-internals.** How those are produced is RFC-0101..0104; 0105 reads
  them as black boxes. Drive is read *as orientation* per RFC-0104 Boundary B.
- NG5. **No 0100C text.** If 0105 concludes 0100C is needed, it *proposes* its activation via the
  Foundation Evolution Protocol (F1) — it does **not** write 0100C inline. 0100C remains a separate,
  gated document.

---

## 4. The Information Model of Goal

Goal is the life's **current committed target**: the durable *intention to reach a specific state*,
given what it wants (Need), how it feels about it (Affect), and what it remembers (Memory) — shaped by
the orientation Drive supplied. It is **not** the momentary activation (Drive) and **not** the
executable unit (Task). Per RFC-0100 §1.8, Goal *"can persist for months"*; it is the life's
*commitment*, not its mood.

> **D1 (clarification, carries forward RFC-0104 D1, not a patch).** *Goal is a committed target, not an
> operational orientation.* Drive is the *direction* (`i(Affect)`); Goal is the *destination* the
> Planning Authority *commits to* from Drive + Need + Affect + Memory. The chain `Drive → Goal` is
> forbidden as a direct authority edge (RFC-0104 Boundary B); the legal path is `Drive → Planner reads
> → Goal`. `Drive == Goal` must never hold.

### 4.1 Dimensions of Goal (commitment schema)

Goal is structured, not scalar. Its dimensions:

| Dimension | What it holds | Fed primarily by | Notes |
|-----------|---------------|------------------|-------|
| **Target** | the state to reach | Need (what the life wants) + Memory (what worked before) | "toward what" — the commitment's content. |
| **Success criterion** | how "reached" is known | Need + Perception (current state) | Distinguishes Goal from a vague Want. |
| **Horizon** | time-scale of commitment | Memory (past Goal durations) + Need urgency | Months-scale (RFC-0100 §1.8); distinguishes from Task (minutes). |
| **Priority** | relative weight vs other Goals | Drive (intensity/focus) + Affect (urgency) | Set by the Planning Authority's deliberation, not by Drive alone. |

> The list above is a *commitment schema*, not a new Ontology noun-set. The Ontology (RFC-0100 §1.8)
> names Goal; this RFC only specifies how the Planning Authority assembles these dimensions. A proposed
> "Goal dimension" that is really a Task (executable unit) or a Need (want) is rejected by the Ontology's
> negative rules (§1.8–§1.9).

### 4.2 Goal is Hybrid — Owned, not Derived

Per RFC-0100B §3.4 / §4, Goal is the **canonical Hybrid**:

- **Not Derived** — Goal is *not* a pure function `k(Need)` of upstream state; the Planning Authority's
  *deliberation* shapes it (resolving conflicts, priorities, feasibility). `Goal = k(Need, Affect,
  Memory)` would under-specify it — the Planning Authority's *choice* is the essence of Goal.
- **Not Persistent** — Goal is *not* a memory record; it is the life's *current commitment*, distinct
  from the Memory that informed it.
- **Owned** — Goal is *owned and lifecycle-maintained* by the **Planning Authority**. The Authority is
  the sole writer (within the lifecycle of that Goal) and the sole producer of its mutation.

This is **why Goal is the one Derived-state exception in `Mutated by`** (RFC-0100B matrix: `Mutated by:
Planning Authority (lifecycle of this Goal only)`). It is the first Life RFC to validate a state that is
**written, not merely derived**.

---

## 5. Adoption Contract (consumes RFC-0100A + RFC-0104)

### 5.1 `Goal ← Planning Authority(Need, Affect, Memory, reads Drive)`

The contract (semantic, not code):

- **Inputs (read-only):** the life's **Need** (what it wants), **Affect** (how it feels), **Memory**
  (what it remembers / past outcomes), and **Drive** (orientation — *read as orientation only*, per
  RFC-0104 Boundary B; Drive is an *input signal*, not the adopt-decider).
- **Authority (owns the act):** the **Planning Authority** — it *adopts* the Goal. This is the
  constitutional fact that makes Goal Owned, not Derived (§4.2).
- **Output**: the committed Goal record (§4.1) for this adoption, with a lifecycle (§6).
- **Purity**: the *adoption* is not a pure function (deliberation is involved); but the *inputs* are
  read-only and the *writer* is single (Planning Authority). Determinism lives at the Authority level
  (a given Need+Affect+Memory+Drive + same Authority policy ⇒ same adoption), not at a `k()` level.

> The *shape* of the Planning Authority's deliberation (conflict resolution, priority) is an
> implementation RFC's concern. This RFC fixes only that Goal is **adopted by one authority** (§6), reads
> upstream + Drive as signals, and is **Owned** (writable by that authority alone).

### 5.2 The single legitimate path (carries forward RFC-0104 §5.2)

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
Goal                ← Hybrid, durable; owned + lifecycle-maintained by Planning Authority (RFC-0100 §1.8, 0100B §4)
   │
   ▼
Plan → Execution    ← decomposes Goal (Cognition layer, RFC-0100 §1.8/§1.9)
```

Goal is reached **only** by this path. The Planning Authority is the *adoption* node; Drive is merely the
orientation it reads.

> **Anti-bypass (V1, carries forward RFC-0102 §4.3 / 0103 §5.2 / 0104 §5.2).** The chain is strictly
> `Memory → Identity → Need → Affect → Drive → Planning Authority → Goal`. Memory/Need/Affect/Drive
> influence Goal **only** transitively, through the chain. A direct `Goal = k'(Memory)` or `Goal =
> j'(Drive)` path that reads upstream *around* the Planning Authority's deliberation is **forbidden** —
> it would bypass the Planning Authority's ownership and rebuild the god-object. (Note: RFC-0100A §5's
> *Habit Formation* is a *future Dynamics addendum* describing a deliberate, separately-owned shortcut —
> it is **not** a Goal-store and **not** authorized by this RFC.)

### 5.3 The matrix-row clarification (a reading, not a change)

RFC-0100B §2 lists Goal's `Produced by` as **`Planning Authority (from Need)`** and `Mutated by` as
**`Planning Authority (lifecycle of this Goal only)`**. This RFC clarifies (without altering the matrix):

- **`Produced by: Planning Authority (from Need)`** — the Planning Authority is the *owner-writer*; Need
  is the *content source* it reads. This is the mirror of the prior joints' clarifications: Need is the
  *attributed input*, not a second authority (parallel to RFC-0102 §5.3 `Identity (+ state)`, RFC-0103
  §5.3 `Need + Perception`, RFC-0104 §5.3 `Produced by: Affect`).
- **`Mutated by: Planning Authority (lifecycle of this Goal only)`** — **new wrinkle**: unlike the prior
  four joints (`Mutated by: none`), Goal *is* mutated — but **only by its own owner authority, within
  that Goal's lifecycle**. This is the Hybrid carve-out (§4.2) made explicit.

This is a **clarification of existing matrix rows**, parallel to the prior three joints. It is **not** a
Foundation change and does **not** trigger the Foundation Evolution Protocol.

### 5.4 Persistence & Cycle — the Hybrid reframing of V4 / V5

`PROTOCOL-Life-Validation.md` V4 (anti-persistence) and V5 (cycle) were written for the **Derived /
Transient** states of 0101–0104. Goal is the **first Owned / Hybrid** state, so those two checks are
**reframed, not violated**:

- **V4 (Persistence) — reframed for Owned states.** A Derived state has *no storage authority*
  (`save/persist/update/restore` forbidden). An **Owned** state (Goal) *does* have a storage authority —
  **its single owner authority** (Planning Authority). The rule is not "no persistence"; it is "**single
  writer**" (O1): only the Planning Authority may write Goal. `goal.save()` by any other component, or a
  second Goal-writer, is forbidden. Goal persists *because* it is Owned, not in spite of it.
- **V5 (Cycle) — reframed for Owned states.** A Derived state is re-derived each cycle (no restore). An
  **Owned** state (Goal) has a **lifecycle** (adopt → active → done / abandoned), not a per-cycle
  re-derivation. Goal persists across cycles *by design*; it is rolled forward by the Planning Authority,
  not recomputed from upstream.

> **Protocol note (clarification, not a Protocol edit here).** RFC-0105 surfaces that Life Validation
> V4/V5 as literally written assume Derived/Transient states. A later, separate clarification of
> `PROTOCOL-Life-Validation.md` should split V4/V5 into "Derived form" (no storage, per-cycle) and "Owned
> form" (single-writer storage, lifecycle) — an F4 "shrink ambiguity" precision, not an expansion. This
> RFC flags it; it does not edit the Protocol inline (the Protocol is amended on its own commit, per
> Document Taxonomy discipline).

---

## 6. Ownership (consumes RFC-0100B)

### 6.1 `Planning Authority` owns Goal's adoption + lifecycle

Per RFC-0100B §1.1 / §4, the authority over Goal is:

> `Goal / Planning Authority` — owns Goal's adoption *and* its lifecycle (the deliberation that forms
> Goal, and the mutation that advances/retires it).

This authority is a **semantic contract** (RFC-0100B §1.1): it holds the sole right to *interpret* what
Goal is and to *set the policy* for its adoption and lifecycle. The Planning Pipeline that runs the
adoption is an **implementation** of that contract, not the authority (RFC-0100B §1.1: a future refactor
splitting the Pipeline into `Need Analysis → Goal Selection → Conflict Resolution → Priority` changes the
process graph, **not** the authority).

### 6.2 The naming discipline + authority boundary (symmetric format, V3)

The name "Planning Authority" marks the *owner* of Goal. It owns **Goal**, not Drive/Need/Memory — those
are owned upstream/elsewhere (Drive Authority owns `i`; Identity Authority owns `g`; Memory Authority owns
Memory). The chain of authorities must not be conflated:

- **`Drive Authority`** — owns `i(Affect)`; owns the *Drive* state (RFC-0104 §6.2).
- **`Planning Authority`** — owns Goal's *adoption + lifecycle*; reads Drive/Need/Affect/Memory as
  signals.

> **Boundary statement (clarification, not a patch — symmetric with RFC-0103 §6.2 C2 / RFC-0104 §6.2 B).
> *Planning Authority governs Goal adoption and lifecycle, not Drive mutation, not Plan mutation.* It
> owns the Goal it adopts and may *read* Drive (orientation) / Need / Affect / Memory, but must **never**
> mutate them. Code of the shape `planningAuthority.adopt_goal(...)` is correct;
> `planningAuthority.set_drive(...)` or `planningAuthority.update_memory(...)` is a **mis-assignment**
> (collapses authorities, violates O1). The downstream `Plan` is *produced by* the Planning Authority
> from Goal (Cognition layer) — the Planning Authority does not let Goal "adopt" a Plan; it *decomposes*
> Goal into one.

This is the **fifth** instance of the symmetric Authority-boundary format, now extended to an Owned
state: the prior four (Identity / Need / Affect / Drive) all said "governs X derivation"; Goal says
"governs Goal adoption and lifecycle" — the Hybrid wording the Owned case requires.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Goal is **internally committed** via the Planning Authority's deliberation over internal state
(Need/Affect/Memory/Drive); it has no external setter, no assigned target from any caller. The life
*owns its commitment*. This is the definitional essence of "Life, not Agent" (RFC-0100 §1.2) at the
commitment joint: an agent is *given* an objective; a life *adopts* its own, from how it has become.

### 7.2 Growth (G) — score: **high**

Because Goal is adopted from Memory (what it has learned) + Need (who it has become) + Affect/Drive
(oriented), the commitment *genuinely evolves* as Memory accumulates — "the life commits to different
things tomorrow." Growth is *realized* here: experience → Memory → … → shifted Drive → shifted Goal
adoption. RFC-0105 makes the growth mechanism explicit at the commitment joint and guards it (only the
Planning Authority may adopt/retire Goal; no external `goal.set()`).

### 7.3 Maintainability (M) — score: **gate (must not regress)**

Single-writer ownership ⇒ no hidden mutable Goal state owned by two components; authority is a stable
contract independent of Planning Pipeline graph ⇒ refactors don't ripple into the constitution. M is
treated as a **non-regression gate**. RFC-0105 introduces **one** new M consideration: Goal's lifecycle
needs a *defined transaction boundary* (adopt-new / retire-old). If left implicit, implementations will
invent ad-hoc transaction logic → drift. This is exactly the RFC-0100C question (§9).

---

## 8. This is NOT Goal (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a Drive.** Drive is the *orientation* (`i(Affect)`, Drive Authority); Goal is the *committed
  target* the Planning Authority adopts from Drive + context. `Drive == Goal` forbidden (§4 D1).
- ✗ **NOT a Need.** Need is the stable-ish want (`g(Identity)`, "why now"); Goal is the *committed
  target* ("toward what"). Need precedes and may spawn many Goals (RFC-0100 §1.8).
- ✗ **NOT a Task.** A Task is an *executable unit* (minutes-scale, fungible, RFC-0100 §1.9); Goal
  persists for months and survives task failure. Plan/Execution consume Goal; Task is their unit.
- ✗ **NOT a memory record.** Goal is the life's *current commitment*, distinct from the Memory that
  informed it (RFC-0100B §4). But it *is* persisted — by its single owner authority (the Planning
  Authority), not by a free-form store.
- ✗ **NOT writable by anyone but the Planning Authority.** `execution.report_goal_done()`,
  `reflection.set_goal()`, `drive.choose_goal()` are all mis-assignments (collapse authorities, violate
  O1). Reflection writes to **Memory**, never to Goal (RFC-0100 §1.8/§3) — this is the canonical "Owned
  state, single writer" rule, and the most likely place a future RFC tries to cheat it.
- ✗ **NOT a second writer's store.** A `goals.json` updated by Execution, or a `GoalDB` written by
  Reflection, duplicates state owned by the Planning Authority and breaks O1. The Planning Authority is
  the sole writer; other components *read* Goal (Execution reads the target).

---

## 9. RFC-0100C deferral — the central question (first real test)

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) with the condition: *create it only if a
Life-layer RFC — starting with RFC-0101 Identity — actually requires transaction, rollback, window, or
lock semantics.* RFC-0101, 0102, 0103, 0104 validated "no" (all Derived/Transient, `Mutated by: none`;
the only direct writers were Memory and Goal's *existence*, not its mechanics). **RFC-0105 is the first
RFC where the question is concrete**, because Goal is the first **Owned** state with a **lifecycle**.

**The two readings:**

**(a) Foundation Stable → YES (0100C stays deferred).** RFC-0100B §4 *already* names Goal as Hybrid,
Owned, lifecycle-maintained by the Planning Authority, with a single writer (O1). The *constitution* is
sufficient to **explain Goal's nature**: it names the authority, the lifecycle ownership, and the
single-writer rule. The *transaction mechanics* (atomic adopt+retire, rollback window, permission
checks) are an **implementation refinement** of an already-named authority — not a missing constitutional
concept. Under this reading, 0100C remains deferred (its activation is a *later convenience*, not a
*validation blocker*), and RFC-0105 anchors like its four predecessors.

**(b) Foundation Patch Required → activate RFC-0100C (first real F1 trigger).** Goal's adoption *is* a
transaction: adopting a new Goal must atomically retire/archive the old one; an abandoned Goal needs a
rollback or archival boundary; concurrent Planning/Reflection activity needs a permission/lock window
(RFC-0100B §1.1 already hints "Permissions (WHEN) — a derived layer… within a Planning/Reflection
transaction"). Without 0100C, the constitution *cannot express* the transaction boundary, so a real
implementation will invent ad-hoc transaction logic scattered across components → drift, exactly the
thing O1 and Principle 0 forbid. Under this reading, RFC-0105 does **not** anchor; instead it *proposes*
RFC-0100C's activation via the **Foundation Evolution Protocol (F1)** — the first time that gate is
exercised since it was locked (PROTOCOL-Foundation-Evolution.md).

**This RFC's recommendation:** **Reading (a)** — but with eyes open. The Foundation *explains* Goal;
0100C is the natural *next* document, and Goal is its single most likely activation. RFC-0105 therefore
validates Stable and **recommends** (non-bindingly) that 0100C be the *next* Foundation document after
the Life-validation chain, triggered by a concrete Planning/Reflection transaction need. The user
Challenge (§13.4) decides between (a) and (b); if (b), this RFC routes to Foundation Evolution F1 and
does not anchor.

> **Why (a) is defensible:** the four prior joints proved the *pattern* "Derived/Transient ⇒ no 0100C".
> Goal proves the *pattern* "Owned/Hybrid ⇒ needs a transaction boundary" — but the *boundary's owner*
> (Planning Authority) is already named. Naming the owner is the constitutional act; specifying the
> transaction syntax is the refinement. Deferring the syntax until a real Planning RFC needs it is
> consistent with the deferral's own wording ("create it only if a Life RFC *requires* transaction
> semantics" — Goal *has* a lifecycle, but the Life RFC that *requires the syntax* is the Planning/Reflection
> RFC, not the Goal noun's validation).

---

## 10. Examples

**E1 — Adoption from signals (no external setter).**
Need = { explore X }, Affect = { curiosity high, urgency moderate }, Memory = { past X attempts succeeded
via approach Y }, Drive = { approach: high on curiosity axis }. Planning Authority adopts Goal = { target:
understand X, success: can explain X, horizon: weeks, priority: high }. No caller assigned the Goal; it
was adopted from internal state.

**E2 — Lifecycle (Owned, not Derived).**
Goal G adopted (week 1). Mid-cycle, Planning Authority retires G (superseded) and adopts G'. This is a
*mutation by the owner authority within G's lifecycle* — legitimate (RFC-0100B `Mutated by: Planning
Authority (lifecycle of this Goal only)`). It is **not** a re-derivation from Need; it is an *adoption
decision*. Contrast E3 of RFC-0104 (Drive re-derived each tick) — Goal does not behave that way.

**E3 — Anti-bypass (carries forward).**
A "context module" proposes `goal.from_memory(recent_episodes)` to make Goal reflect recent experience
directly. **Rejected**: Memory reaches Goal only via `f → g → h → i → Planning Authority`. Recent
experience first sediments into Memory, then reshapes Need→Affect→Drive, then the Planning Authority
adopts a new Goal. A direct `from_memory` path bypasses the Planning Authority.

**E4 — Authority boundary (Owned-state twin of D3/RFC-0104).**
Execution finishes a Task and calls `execution.report_goal_done(goal_id)`. **Rejected** (§8): Execution
*reads* Goal (the target); only the Planning Authority may *retire* it. The correct shape is
`planningAuthority.retire_goal(goal_id)` after Execution reports task completion *to* the Planning
Authority. `execution.retire_goal()` collapses two authorities (violates O1).

**E5 — Single-writer (O1).**
Reflection learns "Goal X was wrong" and calls `reflection.set_goal(X')`. **Rejected** (RFC-0100 §3 /
§1.8): Reflection writes to **Memory**, never to Goal. The Planning Authority later adopts X' *if* Memory
shifts Need→Affect→Drive. Reflection→Goal directly is the canonical single-writer violation.

---

## 11. Limits & Open Questions

- **Q1 — Monolithic vs structured adoption.** This RFC fixes the *commitment dimensions* (§4.1) but not
  whether they are adopted jointly or sequentially. Left to the Planning RFC. *Not a constitution
  question.*
- **Q2 — Goal ↔ Drive feedback.** `Drive → Goal` is forbidden as a direct authority edge (Boundary B);
  the Planning Authority reads Drive as orientation. Within a cycle, Goal does **not** feed back to Drive
  (that would re-open the intra-cycle loop RFC-0103 Q2 / RFC-0104 Q2 forbade). Cross-cycle influence is
  via Memory.
- **Q3 — Lifecycle transaction syntax.** *Whether* Goal needs RFC-0100C transaction syntax is §9's
  central question. The *fact* that Goal has a lifecycle (adopt/active/done/abandoned) is constitutional
  (RFC-0100B §4); the *syntax* (rollback window, lock) is the deferred 0100C.
- **Q4 — Multiple concurrent Goals.** How the Planning Authority prioritizes / conflicts multiple active
  Goals is its own deliberation (implementation RFC), not 0105's. 0105 only fixes that the Planning
  Authority is the sole owner of all of them.
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0105 surfaces **no new constitutional question** and does **not** consume or promote it.
- **Q6 — Life Validation V4/V5 Hybrid carve-out.** RFC-0105 is the first Owned-state Validation; it
  surfaces that `PROTOCOL-Life-Validation.md` V4/V5 need a Derived-vs-Owned split (§5.4). This is a
  *Protocol precision* (F4), flagged here, amended on its own commit — not inline in this RFC.

---

## 12. Evolution / next RFC

RFC-0105 is **consumable** by the next Life-layer RFC:

- **RFC-0106 Plan / Execution** (or the Planning RFC) consumes Goal as the target the Planning Authority
  decomposes. 0106 must not redefine the commitment; it reads Goal as a fixed input to Plan building.
- **RFC-0100C (if activated via F1)** — the transaction/permission syntax for Goal's lifecycle, the first
  real Foundation Evolution since the Protocol was locked.

### 12.1 Phase-2 rhythm (the Gate — CLOSED)

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy via F1; else Anchor) → next RFC.**

RFC-0105 has passed **Draft** and **Challenge** (LAWP Phase C, four-question + V1–V6). The Gate question
*"Foundation Stable?"* returns **YES under Reading (a)** — the user's Phase-2A Gate (2026-08-04) adopted
Reading (a): the 0100 trilogy names Goal's owner (Planning Authority), its lifecycle ownership, and the
single-writer rule (O1), which is sufficient to *explain* Goal. RFC-0105 **anchors** (Reading a); RFC-0100C
stays deferred (5th consecutive confirmation). This closes Phase 2A — the Foundation is now frozen (§14).

---

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0105 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to RFC-0106. **First Life RFC to cite
`PROTOCOL-Life-Validation.md` (V1–V6).**

### 13.1 What was checked

| Foundation doc | Question 0105 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Goal needs as a *noun*? | ✅ Yes. Goal + its boundaries (vs Need/Task) are named (§1.8); negative rules exclude Need/Task. No new life-noun. |
| **RFC-0100A** (Dynamics) | Is the flow sufficient to *produce/consume* Goal? | ✅ Yes. `Goal → Plan → Execution` is specified (outer loop). No new flow vocabulary. |
| **RFC-0100B** (Ownership) | Does the matrix + O1–O4 cover Goal's authority? | ✅ Yes, with one *clarification* (not a change): `Produced by: Planning Authority (from Need)` / `Mutated by: Planning Authority (lifecycle)` read as owner-writer + content-source (§5.3). Goal is the canonical Hybrid (§4.2). |
| **RFC-0100C** (deferred) | Does Goal need Permissions/Transactions? | ⚠️ **The crux (§9).** Goal is the first Owned state with a lifecycle. Reading (a): 0100B already names the owner + lifecycle ⇒ Stable, 0100C stays deferred. Reading (b): transaction syntax genuinely needed ⇒ first F1 trigger. |

### 13.2 Holes found

- **None requiring a Foundation change under Reading (a).** The matrix clarifications (§5.3) are readings
  of existing rows, not edits.
- **Under Reading (b), one proposed patch:** activate RFC-0100C via Foundation Evolution F1 — the first
  time that gate is exercised.
- **Carried-forward clarifications (design-intent discipline, V1–V6 of `PROTOCOL-Life-Validation.md`):**
  - **V1 (anti-bypass):** chain strictly `Memory → … → Drive → Planning Authority → Goal` (§5.2).
  - **V2 (downstream bypass):** Goal → Plan → Execution only; no `Goal → Execution` skip (§5.2).
  - **V3 (Authority Boundary, symmetric format):** *Planning Authority governs Goal adoption and
    lifecycle, not Drive mutation, not Plan mutation* (§6.2). Fifth instance of the format; first Owned-state wording.
  - **V4 / V5 (Hybrid reframing):** Goal is Owned, so "no storage authority" → "single writer"; "per-cycle
    re-derive" → "lifecycle" (§5.4). **Surfaces that Life Validation V4/V5 need a Derived-vs-Owned split
    (Q6)** — a Protocol precision (F4), flagged, not inline-edited.
  - **D1 (carries forward RFC-0104 D1):** Goal is committed target, not orientation; `Drive == Goal`
    forbidden (§4).

### 13.3 Gate verdict (recorded)

> **Foundation Stable? → YES (Gate A, Reading a adopted).**
> RFC-0105 is consumable by RFC-0106 Plan; no Foundation patch. The user's Phase-2A Gate (2026-08-04)
> confirms Reading (a): the constitution explains Goal; RFC-0100C stays deferred (5th consecutive
> confirmation). This anchors RFC-0105 and freezes the Foundation (§14).

---

### 13.4 Challenge (LAWP Phase C — Life Validation Protocol V1–V6) — recorded

Ran `PROTOCOL-Life-Validation.md` V1–V6 (first RFC to do so). Pre-filled from self-review; user may amend:

| Life Validation check | Result | Note |
|-----------------------|--------|------|
| **V1 Upstream bypass** | ✅ PASS | Goal reads only via Planning Authority; `Memory→…→Drive→Planning Authority→Goal` (§5.2). |
| **V2 Downstream bypass** | ✅ PASS | Goal → Plan → Execution only; no skip (§5.2). |
| **V3 Authority Boundary** | ✅ PASS | *Planning Authority governs Goal adoption & lifecycle, not Drive/Plan mutation* (§6.2). |
| **V4 Persistence** | 🔶 REFRAMED | Owned state ⇒ single writer (Planning Authority), not "no storage". V4 needs Derived/Owned split (Q6). |
| **V5 Cycle** | 🔶 REFRAMED | Owned state ⇒ lifecycle, not per-cycle re-derive (§5.4). |
| **V6 Foundation Stability** | ⚠️ CRUX | Reading (a) Stable; Reading (b) Patch Required → F1 (first time). |

**Clarifications added (all F4, zero base edits under Reading a):** D1 (Goal ≠ orientation); V4/V5 Hybrid
reframing noted; matrix-row reading (§5.3).

> **Challenge verdict → Foundation Stable: YES (Gate A, Reading a adopted).** The user's Phase-2A Gate
> (2026-08-04) confirmed Reading (a): all six Life Validation checks resolve without a Foundation edit
> (V4/V5 reframed for Owned states is a Protocol precision, not a base change). RFC-0105 anchors;
> RFC-0100C stays deferred. This is the first exercise of the Gate as a *release* decision — it freezes
> the Foundation and opens the Agent Phase (M1 implementation).

---

## 14. Phase 2A Gate — Foundation Frozen (release decision, 2026-08-04)

RFC-0105 is the **last Life RFC of Phase 2A (Foundation Validation)**. Its Stable verdict under Reading (a)
is the **Gate** the project defined at the start of Phase 2A:

> **Gate:** *Can Planning exist without changing the Foundation?*
> **Gate A (YES):** Goal / Plan / Execution are all explainable by the 0100 trilogy, and the Planning
> Authority is sufficient to describe Goal's lifecycle → **Foundation Frozen**.
> **Gate B (NO):** Goal's lifecycle needs rollback / transaction / permission-window / ownership-transfer
> semantics the current theory cannot express → **Foundation Evolution** (0100C) first real activation.

The Gate returns **Gate A**. Therefore:

1. **Phase 2A (Architect Phase) is COMPLETE.** Foundation / Governance / Derived-States are all closed.
2. **Foundation is FROZEN** — `RFC-0100` / `RFC-0100A` / `RFC-0100B` / `LAWP` / `Foundation Evolution` /
   `Life Validation` enter **maintenance mode** (default no modification; only Foundation Evolution F1–F4
   may reopen).
3. **Agent Phase begins.** Implementation (M1) starts with the Memory Engine and proceeds down the
   derivation chain (`Memory → Identity → Need → Affect → Drive → Planner`), validating the Foundation
   against real code. Foundation changes only if *implementation* surfaces a contradiction no Life RFC
   foresaw — then via Foundation Evolution F1–F4 (Refinement, not Invention).
4. **RFC-0100C stays deferred** (5th consecutive confirmation). It remains the *next* Foundation document,
   to be activated by a concrete Planning/Reflection transaction need, not by Goal's noun validation.

RFC-0105 anchors here. Downstream Life RFCs (Plan, Execution, Memory, Persona, Reflection, Relationship)
continue as Foundation Validation probes, but the base is now frozen and the project builds.

---

## Appendix A — Validation notes

No Foundation patch is proposed *under Reading (a)*. The matrix clarifications (§5.3) are permitted
readings of existing rows; the V4/V5 Hybrid reframing is a Protocol precision (Q6), flagged for a separate
F4 commit, not inline-edited here. The parked Constitutional hypothesis (RFC-0101 Appendix A) is
untouched. Under Reading (b), RFC-0105 would propose RFC-0100C via Foundation Evolution F1 — the first
real Foundation Evolution trigger, and itself evidence that the governance model (LAWP + Foundation
Evolution + Life Validation) is now being *exercised*, not merely declared.
