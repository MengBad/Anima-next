# RFC-0104: Drive (Life-4) — Foundation Validation #4

> **Status**: **Challenge passed** — Foundation Validation #4; user Phase-C Challenge completed
> (four-question record + three clarifications, see §13.4). Ready to **anchor** (review-before-anchor
> discipline, established in RFC-0102 Phase C, confirmed through RFC-0103).
> **Role**: **Foundation Validation #4**, *not* "Drive Design #4". Its job is to use Drive as a
> probe to prove whether the 0100 trilogy is sufficient to *explain* Drive — not to expand the
> base theory. Any hole found loops back to patch 0100/0100A/0100B via the Foundation Evolution
> Protocol; none was required (see §13).
> **Phase**: Fourth Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0103 Affect
> (reads `Affect` as a fixed input to `i`) and transitively RFC-0102 Need / RFC-0101 Identity
> (via Affect).
> **Layer**: Life layer (Mind) — RFC-0104 = Life-4
> **Depends-on**: RFC-0100 (§1.7 Drive noun), RFC-0100A (outer loop + `i(Affect)`), RFC-0100B
> (Drive Authority, ownership matrix, invariants O1–O4), RFC-0101 (Identity = input, transitively),
> RFC-0102 (Need = input, transitively), RFC-0103 (Affect = input to `i`)
> **Validates**: (a) RFC-0100 Ontology coverage for Drive; (b) RFC-0100A loop sufficiency for
> `Drive = i(Affect)`; (c) RFC-0100B ownership matrix + O1–O4 (Drive Authority owns `i`); (d) RFC-0100B's
> deferral of RFC-0100C (Permissions)
> **Scope**: The information model of Drive + its derivation contract `i(Affect)` + its authority +
> its classification — written *only* as the vehicle for that validation. No implementation, no code,
> no new *flow* vocabulary (flow is RFC-0100A's), no new *life* noun (Drive already exists in
> RFC-0100 §1.7).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Drive gap

The project can now answer five questions (per RFC-0103 §0):

- **What exists?** — Ontology (RFC-0100 §1.7): *Drive is Affect-activated intention directed toward
  a Goal* — the life's **activated orientation**, the bridge between wanting (Need) and doing (Goal).
- **How does it flow?** — Dynamics (RFC-0100A §2.1 outer loop): `Drive = i(Affect)` at the
  Affect → Drive transition; derived per tick from the cycle's Affect.
- **Who governs change?** — State Ownership (RFC-0100B): `Drive Authority` owns the rule
  `i(Affect)`; in the matrix Drive is **emergent** (`Mutated by: none`), read by the Planner.
- **Where does it sit relative to Affect?** — RFC-0103 fixed Affect (`h(Need, Perception)`); Drive is
  the *next* joint, consuming Affect as a fixed input (RFC-0103 §12: "0104 must not redefine the felt
  state; it reads Affect as a fixed input").
- **What is Drive *not*?** — RFC-0100 §1.7 draws the boundary with Goal ("toward what") and the §1.9
  Task; Drive is the *orientation*, not the *committed target* and not the *executable unit*.

But RFC-0100 §1.7 only *names* Drive and its activation condition. It does **not** say:

1. What actually **constitutes** a Drive vector — the orientation dimensions (approach/avoid per
   axis) and how they combine.
2. How `i(Affect)` is **shaped** — which Affect dimensions feed which orientation, and the
   activation gating (valence/urgency must be present for Need to become Drive).
3. Whether Drive needs the **deferred RFC-0100C** (Permissions / transactions) — RFC-0101 §9,
   RFC-0102 §9, and RFC-0103 §9 *predicted* (by the same `Mutated by: none` logic) that 0104 would
   reach the same "no" verdict.
4. The exact reading of the matrix's `Produced by: Affect` — the precise analog of the
   `Need (+ state)` (RFC-0102 §5.3) and `Need + Perception` (RFC-0103 §5.3) ambiguities resolved at
   the prior two joints.

RFC-0104 is the fourth RFC that must answer these by **consuming** the trilogy — and, per the Phase-2
rhythm, its *primary* job is to **validate** the Foundation, not to add to it. As with 0101/0102/0103,
the framing is restrained: *"Is the current Foundation sufficient to explain Drive?"* — not *"Design
Drive."*

---

## 1. Problem

Without a real Drive specification, the downstream Life-layer RFCs (Goal, Planner, Execution) will
implicitly assume a different "activation" and silently disagree about:

- what Drive *is* (so `i(Affect)` is undefined, and the Planner's Goal selection has no input),
- whether Drive can be *written* (so `drive.boost_intention(...)` creeps back — the god-object
  anti-pattern rebuilt one layer down),
- whether Drive *persists or is derived* (so the life "intends" inconsistently across ticks),
- whether Drive reads **Affect** or **Need / Memory directly** (bypassing Affect — the exact mistake
  RFC-0103 §5.2 / C1 forbade at the Affect layer, and RFC-0102 §4.3 forbade at the Need layer).

Drive is the **fourth node of the flow** (`Identity → Need → Affect → Drive → Goal → Plan →
Execution`); an under-specified Drive corrupts every downstream derivation. RFC-0103 fixed Affect;
this RFC fixes the next joint.

---

## 2. Goals

- G1. Define the **information model** of Drive (its orientation dimensions) so downstream RFCs can
  reference a stable structure.
- G2. Specify the **derivation contract** `Drive = i(Affect)` — what of Affect is read and how the
  result is produced — as a *semantic contract*, not code.
- G3. Explain Drive as the **canonical Transient-Derived state** (RFC-0100B §3.2) and record its
  authority.
- G4. **Validate the RFC-0100C deferral**: prove whether Drive needs transaction/rollback semantics.
- G5. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G6. Carry forward the Challenge clarifications established in RFC-0103 (anti-bypass, Authority-≠
  boundaries, anti-persistence) as design-intent discipline for this joint, and add the Drive-specific
  boundary **Drive Authority ≠ Planning Authority** (Drive does not select the Goal) (§5.2, §6.2, §8).

---

## 3. Non-goals

- NG1. **No implementation.** No schema, no class, no DB table, no prompt text. The contract only.
- NG2. **No new flow.** The two-loop flow is RFC-0100A's; this RFC only consumes it.
- NG3. **No Goal / Planner / Task spec.** Goal selection is the **Planning Authority's** job (its own
  RFC); Drive only *emits orientation* and is *read by* the Planner (0100B matrix). 0104 does not
  define how the Goal is adopted — only that Drive is the input the Planner reads.
- NG4. **No Need/Affect/Memory-internals.** How those are produced is RFC-0101/0102/0103; 0104 reads
  them as black boxes and Memory only transitively (via Affect → Need → Identity).
- NG5. **No Habit-Formation spec.** RFC-0100A §5 lists "Habit Formation — Goal→Drive shortcuts that
  bypass the full Need→Affect chain" as a *future Dynamics addendum* (inner-loop, out of scope). 0104
  only notes that habits are **not** a Drive-storage mechanism (§8); their design belongs to a Dynamics
  addendum, not to the Life-layer Drive RFC.

---

## 4. The Information Model of Drive

Drive is the life's **current activated orientation**: the momentary *intention directed toward a
Goal*, given how it currently feels (Affect) about what it wants (Need). It is **not** the durable
commitment (Goal) and **not** the felt state (Affect) — it is the *activated bridge* between them. Per
RFC-0100 §1.7, *"Need becomes Drive only after Affect supplies valence/urgency"*: Drive is the
**activation** of Need via Affect. Without Affect's valence/urgency, Need stays latent — no Drive.

> **D1 (clarification, not a patch).** *Drive is operational orientation, not motivational content.*
> Need is the *content* (what the life wants, `g(Identity)`); Drive is the *direction* (which Goal the
> life currently leans toward, `i(Affect)`). They are **not** identical: `Need == Drive` must never
> hold, or the Goal collapses into a direct function of Need and the Affect→Drive joint is skipped.
> This is a reading of the existing Ontology boundary (RFC-0100 §1.7 "toward a Goal" vs §1.4 "want"),
> not a new rule.

### 4.1 Dimensions of Drive (derived from Affect)

Drive is oriented, not scalar. Its dimensions are a *projection* of the Affect vector onto action
axes:

| Orientation axis | What it holds | Fed primarily by (Affect dim) | Notes |
|------------------|---------------|-------------------------------|-------|
| **Approach** | pull toward the Goal | positive Valence + Curiosity + Urgency | "move toward" — the life leans in. |
| **Avoid** | push away from a threat/situation | negative Valence + Aversions-driven Urgency | "move away" — the life leans out. |
| **Intensity** | how strongly activated | magnitude of Valence × Urgency | Gates whether Drive crosses the threshold into a Planner-readable signal. |
| **Focus** | which Need/Affect axis dominates | max-activation Affect dimension | Picks the single orientation the Planner should weight. |

> The list above is a *projection schema*, not a new Ontology noun-set. The Ontology (RFC-0100 §1.7)
> names Drive; this RFC only specifies how `i` maps Affect → orientation. A proposed "Drive dimension"
> that is really a Goal (durable target) or a Task (executable unit) is rejected by the Ontology's
> negative rules (§1.7–§1.9) — not by this RFC.

### 4.2 Drive is Transient-Derived

Per RFC-0100B §3.2:

> **Derived Transient** — Identity (`f(Memory)`), Need (`g(Identity)`), Affect (`h(Need, Perception)`),
> Drive (`i(Affect)`): functions of upstream state, re-derived every tick.

Drive is the **fourth canonical example** (after Identity, Need, Affect):

- **Derived** — it is a pure function `i(Affect)` of the current Affect; there is no stored Drive
  record being mutated in place.
- **Transient** — it is recomputed each tick at the Affect → Drive transition; it is **never the
  durable record** (only Persistent State and the Hybrid Goal are stored, per §3.4). Its momentary
  nature is the point: Drive is the life's *current* activated intention, not its character.

> **D2 (clarification, not a patch).** *Drive exists only during one reasoning cycle.* It is
> re-derived from Affect at the start of each cycle and discarded at its end; there is no
> `Drive.restore()`. The next cycle re-runs `Memory → Identity → Need → Affect → Drive` from scratch.
> Carrying a Drive instance across cycles would duplicate state owned (transitively) by Memory and
> break O1 — exactly the mistake RFC-0102 §8 / RFC-0103 §5.4 forbade for Need and Affect.

This is why Drive has `Mutated by: none` in the matrix: it is *emergent*, not *assigned*.

### 4.3 Drive does not persist; its continuity is inherited from Affect

Because Drive is `i(Affect)` and Affect is `h(Need, Perception)` (Need `g(Identity)`, Identity
`f(Memory)`), Drive's apparent cross-tick continuity is **entirely inherited** from Memory via Need →
Affect. There is **no separate Drive store**. As Memory accumulates, Need shifts, Affect re-derives,
and Drive re-derives into a new orientation — this *is* the growth mechanism, not a persistence bug.
The *current tick's* Affect perturbs Drive within the tick, but the *baseline* drift across ticks comes
from Affect (via Need), not from Drive storing itself.

---

## 5. Derivation Contract (consumes RFC-0100A)

### 5.1 `Drive = i(Affect)`

The contract (semantic, not code):

- **Input (owned)**: the life's **Affect** at this tick (the felt-state vector of RFC-0103 §4.1).
  Affect is the *governing* input — `i` is a function *of* Affect; the Drive Authority owns
  `i(Affect)`, so Affect is the source Drive emerges from.
- **Input (read-only, none at this joint):** unlike Affect (`+ Perception`), Drive reads **only**
  Affect. It does **not** read Need or Memory directly (anti-bypass, §5.2). Perception's influence on
  Drive is *already* inside Affect (Affect read Perception at the prior transition).
- **Output**: the orientation vector of §4.1, for *this* tick — approach/avoid, intensity, focus.
- **Purity**: a pure function of Affect at derivation time. Same Affect → same Drive. Deterministic,
  testable, no hidden state.
- **Frequency**: re-derived every perception tick at the Affect → Drive transition (RFC-0100A §2.1
  outer loop). Not once per cycle.

> The *shape* of `i` (which Affect dimension maps to which orientation axis; the activation threshold)
> is an implementation RFC's concern. This RFC fixes only that `i` exists, is pure, reads Affect, and
> is owned by one authority (§6).

### 5.2 The single legitimate path

```
Experience
   │  (Universe → Capability → Perception → Attention → Memory Recall)
   ▼
Memory              ← the ONLY direct write (Memory Authority, RFC-0100B)
   │  (next cycle, at Recall)
   ▼
Identity = f(Memory) ← re-derived; NEVER assigned (RFC-0101)
   │
   ▼
Need = g(Identity)  ← re-derived per cycle; NEVER assigned (RFC-0102, Identity Authority)
   │
   ▼
Affect = h(Need, Perception)  ← re-derived per tick; NEVER assigned (RFC-0103, Affect Authority)
   │
   ▼
Drive = i(Affect)   ← re-derived per tick; NEVER assigned (this RFC, Drive Authority)
   │
   ▼
Planner             ← reads Drive (+ Need, Affect, context) and ADOPTS a Goal
   │
   ▼
Goal                ← Hybrid, durable; owned by the Planning Authority (RFC-0100 §1.8)
```

Drive is reached **only** by this path. There is no other entry point. The Planner is the *next* node
and the Goal's owner; Drive is merely the signal it reads.

> **Anti-bypass (carries forward RFC-0102 §4.3 / RFC-0103 §5.2).** The chain is strictly
> `Memory → Identity → Need → Affect → Drive`. Memory may influence Drive **only** transitively,
> through `f → g → h → i`. A direct `Drive = i'(Memory)` or `Drive = i''(Need)` path that reads
> upstream *around* Affect is **forbidden** — it would bypass the Affect authority's ownership and
> rebuild the god-object. If a future RFC wants Memory/Need to shape Drive, it must shape *Affect*
> (via the Need/Affect path), not Drive directly. (Note: RFC-0100A §5's *Habit Formation* is a
> *future Dynamics addendum* describing a deliberate, separately-owned shortcut — it is **not** a
> Drive-store and **not** authorized by this RFC.)

### 5.3 The "Produced by: Affect" clarification (a reading, not a change)

RFC-0100B §2 lists Drive's `Produced by` as **`Affect`**. This RFC clarifies (without altering the
matrix):

- **`Affect`** is the owned input — the thing `i` is a function *of*; the Drive Authority owns
  `i(Affect)`, so Affect is the governing source of Drive.
- There is **no second input** at this joint (contrast Affect's `+ Perception`). Drive is a pure
  function of Affect. This is consistent with the layer's narrowing: by the time the life *acts*, the
  environmental perturbation (Perception) has already been absorbed into Affect one transition earlier.

This is a **clarification of an existing matrix row**, exactly parallel to RFC-0102 §5.3 and
RFC-0103 §5.3. It is **not** a Foundation change and does **not** trigger the Foundation Evolution
Protocol.

### 5.4 No `drive.update()` — the god-object guard

RFC-0100A Principle 0 consequence, carried forward:

> **No `drive.update()`.** Drive has no setter. Any RFC proposing `drive.set_intensity(...)` or
> `drive.boost_approach(...)` is mis-shaped and must be revised.

The life does not edit its own activated intention in place; experience reshapes it via Memory →
Identity → Need → Affect → Drive. This is the sharp line between *life* and *agent* (RFC-0100 §1.2),
applied three joints deeper than 0101.

> **Drive is a derived state; it has *no storage authority*.** There is no Drive-store, no
> `DriveMemory`, no `drive.json`. The only Persistent/Hybrid writers are Memory and Goal (their own
> authorities). Drive's cross-tick continuity is *inherited* from Affect via Need (§4.3); storing Drive
> separately would duplicate state owned by another authority and create a second writer — forbidden
> by O1. Drive is re-derived every tick; it is never the durable record.
>
> **C3 (anti-persistence reaffirmed — clarification, not a patch — carries forward RFC-0102 §8 /
> RFC-0103 §5.4).** Drive is Transient, Derived, and has **no Storage Authority** (§4.2, above).
> Explicitly forbidden at the *implementation* shape: `Drive.save()`, `DriveMemory.persist()`,
> `IntentionDatabase.write()`, or any "intention store". If a future design needs a *long-term drive
> pattern* — a durable motivation, a habit, a persistent orientation — that state belongs in **Memory**
> (the only legitimate long-term writer), where it re-enters the cycle as `f → g → h → i` on the next
> cycle. Drive itself is re-derived every tick; it is never the durable record. A *habit* (RFC-0100A §5)
> is a Dynamics-level shortcut, not a Drive store.

---

## 6. Ownership (consumes RFC-0100B)

### 6.1 `Drive Authority` owns `i(Affect)`

Per RFC-0100B §1.1, the authority over Drive is:

> `Drive Authority` — owns `i(Affect)` (the derivation of **Drive** *from* Affect)

This authority is a **semantic contract** (RFC-0100B §1.1): it holds the sole right to *interpret* what
Drive is and to *set the policy* for its derivation. The component that runs `i` is an
**implementation** of that contract, not the authority. A future refactor that splits `i` into
`ApproachGate → AvoidGate → IntensityScale` changes the process graph, **not** the authority, and
**not** RFC-0100.

### 6.2 The naming discipline + authority boundaries (echo of RFC-0101 / 0102 / 0103)

The name "Drive Authority" marks the *output* of `i` (Drive). It owns **Drive**, not Affect or Need —
those are owned upstream (Affect Authority owns `h`; Identity Authority owns `g`; Identity Derivation
Authority owns `f`). The chain of authorities must not be conflated:

- **`Identity Derivation Authority`** — owns `f(Memory)`; owns the *Identity* state.
- **`Identity Authority`** — owns `g(Identity)`; owns the *Need* state (RFC-0102 §6.2).
- **`Affect Authority`** — owns `h(Need, Perception)`; owns the *Affect* state (RFC-0103 §6.2).
- **`Drive Authority`** — owns `i(Affect)`; owns the *Drive* state. Its "Affect" is an input.

> **Boundary statement A (clarification, not a patch — carries forward RFC-0103 §6.2 C2).** *Drive
> Authority governs Drive derivation, not Affect / Need / Identity mutation.* It owns `i(Affect)` and
> the *Drive* it produces; it may **read** Affect but must **never** mutate it. Affect/Drive mutation
> belongs solely to their own authorities (`h`, `g`, `f`). Code of the shape
> `driveAuthority.derive_drive()` is correct; `driveAuthority.update_affect()` or
> `driveAuthority.set_need(...)` is a **mis-assignment** and must be rejected — it collapses
> authorities and violates O1.

> **Boundary statement B (clarification, not a patch — symmetric with RFC-0103 §6.2 C2).** *Drive
> Authority governs drive derivation, not goal adoption.* `Drive = i(Affect)` is owned by the **Drive
> Authority**; the **Goal** is owned by the **Planning
> Authority** (RFC-0100 §1.8, the Hybrid durable state). Drive **emits orientation**; it does **not**
> select or adopt the Goal. The direct edge `Drive Authority → Goal` is **forbidden** — the only legal
> path is `Drive → Planner (reads Drive) → Goal`, where the Planning Authority *reads* Drive (plus
> Need, Affect, context) and *adopts* the Goal. Conflating the two is the classic "intention picks the
> objective" agent anti-pattern — in a life, Drive *informs* the Planner, the Planner *commits* the
> Goal, never the reverse shortcut. Code of the shape `driveAuthority.derive_drive()` is correct;
> `driveAuthority.choose_goal()` is a **mis-assignment** (collapses two authorities, violates O1).

### 6.3 Matrix row, restated (RFC-0100B §2)

| State | Authority | Produced by | Mutated by | Read by |
|-------|-----------|-------------|-----------|---------|
| **Drive** | Drive Authority (`i(Affect)`) | Affect | **none** — emerges from Affect | Planner (adopts Goal) |

This row is **consumed, not changed**, by RFC-0104. If a future design needs Drive to be *assigned*
rather than derived, it violates O1 (single authority) and Principle 0 simultaneously and is out of
scope for the Life Layer.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Drive is **internally generated** via `i(Affect)`; it has no external setter, no assigned intention, no
`drive.update()` from any caller. The life *owns its activated orientation*. This is the definitional
essence of "Life, not Agent" (RFC-0100 §1.2) carried three joints deeper than Identity: an agent is
*given* a motivation; a life *derives* its drive from how it feels (Affect) about who it has become
(Need) under present circumstances.

### 7.2 Growth (G) — score: **high**

Because Drive is `i(Affect)` and Affect is `h(Need, Perception)` (Need `g(Identity)`, Identity
`f(Memory)`), the drive-vector **genuinely evolves** as Memory accumulates. Growth — "the life leans
toward different things tomorrow" — is *realized* here: experience → Memory → Identity → Need → Affect
→ re-derivation → shifted Drive → shifted Goal adoption. RFC-0104 makes the growth mechanism explicit
at the Drive joint and guards it (no in-place edit that would fake growth).

### 7.3 Maintainability (M) — score: **gate (must not regress)**

Pure-function derivation ⇒ deterministic, unit-testable, no hidden mutable state, no god-object.
Authority is a stable contract independent of process graph ⇒ refactors don't ripple into the
constitution. M is treated as a **non-regression gate**, not a design center. RFC-0104 introduces no
M risk.

---

## 8. This is NOT Drive (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a Goal.** A Goal is a *committed target* (Hybrid, owned by the Planning Authority,
  RFC-0100 §1.8). Drive is the *orientation* the Planner reads; Goal is what the Planner *adopts* from
  Drive + context.
- ✗ **NOT a Task.** A Task is an *executable unit* (minutes-scale, RFC-0100 §1.9); Drive is the
  activation, not the unit of work.
- ✗ **NOT Need.** Need is the stable-ish want-vector (`g(Identity)`); Drive is the *activated*
  intention computed from Affect + Need.
- ✗ **NOT Affect.** Affect is the *felt state*; Drive is the *orientation toward action* (`i(Affect)`,
  Drive Authority); Affect is the *source* Drive reads, not the orientation itself.
- ✗ **NOT a stored record.** Drive is Transient-Derived (§4.2); provisioning a "drives table" or
  `drive.save()` violates ownership (the Drive Authority owns a *derivation*, not a record).
- ✗ **NOT persistable.** `Drive.save()` / `Drive.persist()` / `Drive.update()` / a `DriveMemory` store
  are all forbidden (§5.4). Drive has no storage authority; its continuity is inherited from Affect via
  Need (§4.3). Any of these shapes implies a second writer and breaks O1.
- ✗ **NOT a Habit store.** A habit (RFC-0100A §5) is a *future Dynamics addendum* — a separately-owned
  shortcut, never a Drive store. Encoding habits as persisted Drive records duplicates state owned by
  Memory and breaks O1.
- ✗ **NOT owned by the Planner.** Authority is a *contract* (RFC-0100B §1.1), never an object. The
  Planner **only `read(drive)`** — `Planner.modify(drive)` is forbidden, exactly symmetric with
  `Planner.modify(goal)` being *permitted* (the Planning Authority owns Goal). Drive is *emitted* by the
  Drive Authority and *consumed* by the Planner; the Planner may weight it but never rewrite it. This is
  Boundary statement B (§6.2) stated as a negative example — the Drive-layer twin of D3.

---

## 9. RFC-0100C deferral — validation outcome

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) with the condition: *create it only if
a Life-layer RFC — starting with RFC-0101 Identity — actually requires transaction, rollback, window,
or lock semantics.* RFC-0101, RFC-0102, and RFC-0103 validated "no" and **predicted** (§9 each) that
0104 would reach the same conclusion.

**RFC-0104's finding: Drive does NOT require RFC-0100C.**

Reasoning:

- Drive has `Mutated by: none`. It is **never written in place**; it is a pure derivation `i(Affect)`.
- The **only direct writes** in the entire path are **Memory** (Memory Authority) and **Goal**
  (Planning Authority). Any transaction/rollback boundary therefore lives at those writers — *already
  governed* by their authorities — not at Drive.
- A re-derivation that reads Affect is idempotent and side-effect-free; it cannot partially fail in a
  way that needs rollback *at the Drive level*.

> **Conclusion**: the transaction boundary is uniformly at Memory (and, for the Hybrid case, Goal).
> Adding a separate Drive transaction layer would be a second writer — which the matrix forbids.
> **RFC-0100C remains deferred**, exactly as RFC-0101 §9, RFC-0102 §9, and RFC-0103 §9 predicted. Its
> next real candidate remains **Memory itself** (consolidation / sleep rollbacks) or **Goal**
> (lifecycle-scoped mutation) — both later RFCs. This is now the **fourth consecutive confirmation** of
> the deferral.

---

## 10. Examples

**E1 — Derivation from Affect (no setter).**
Affect (this tick) = { valence: −0.3, arousal: 0.4, curiosity: 0.8, urgency: 0.3 }. `i` yields Drive =
{ approach: 0.5 (on the curiosity axis), avoid: 0.2, intensity: 0.42, focus: curiosity }. No code ever
assigned Drive; it is recomputed each tick from Affect.

**E2 — Activation gating (Need stays latent without Affect).**
Need = { desires: [understand X] } but Affect = { valence: 0, urgency: 0.05 } (calm, low pressure).
`i` yields Drive = { intensity: ~0.05, below threshold } — the life *wants* but is not *activated*.
This is RFC-0100 §1.7's "Need becomes Drive only after Affect supplies valence/urgency", realized: low
urgency Affect ⇒ latent Need, no Drive. Correct, not a bug.

**E3 — Growth (real, not faked).**
Over weeks, Memory accumulates "owners reward sustained curiosity." `f(Memory)` shifts Need toward
{ curiosities: [explore] }; `h` shifts Affect toward { curiosity: high, urgency: moderate }; `i` shifts
Drive baseline toward { approach: high on curiosity axis }. This is growth via Memory → Need → Affect →
Drive, not via `drive.set_intensity(0.9)`.

**E4 — Authority refactor, contract unchanged.**
Derivation `i` is rewritten from a single prompt into `ApproachGate → AvoidGate → IntensityScale`.
**No change** to Drive Authority, no change to RFC-0100, no change to this RFC. The process graph
moved; the contract held.

**E5 — Anti-bypass (carries forward RFC-0102 E3 / RFC-0103 E5).**
A "context module" proposes `drive.from_memory(recent_episodes)` to make Drive reflect recent
experience directly. **Rejected**: Memory must reach Drive only via `f → g → h → i`. Recent experience
first sediments into Memory (inner loop), then reshapes Need→Affect (next cycle), then Drive. A direct
`from_memory` path bypasses the Affect authority.

**E6 — Authority boundary (new, Drive-specific).**
The Planner reads Drive and adopts a Goal. A "drive module" calls `drive.choose_goal(...)` directly.
**Rejected** (Boundary statement B, §6.2): Goal selection belongs to the Planning Authority. Drive may
emit orientation; it may not adopt the Goal. `driveAuthority.derive_drive()` is correct;
`driveAuthority.choose_goal()` collapses two authorities (violates O1).

---

## 11. Limits & Open Questions

- **Q1 — Monolithic vs dimensional derivation.** This RFC fixes the *orientation axes* (§4.1) but not
  whether they are derived jointly or per-axis. Left to the implementation RFC. *Not a constitution
  question.*
- **Q2 — Affect ↔ Drive ordering / feedback.** `Affect → Drive = i(Affect)` is specified by 0100A.
  **Within a cycle, Drive does NOT feed back to Affect**: Affect is re-derived per tick from the fixed
  cycle-Need; Drive is re-derived per tick from that Affect. Any mutual influence is *across cycles*,
  via Memory (inner loop). A within-cycle `Drive → Affect` edge would create an intra-cycle loop and
  violate the per-tick derivation contract — out of scope; if a future RFC wants drive-coupled affect
  drift, it must route through Memory, not a direct edge.
- **Q3 — Activation threshold.** Where the intensity threshold sits (below which Drive is "latent") is
  an implementation RFC's concern. The *fact* that Drive is gated by Affect's valence/urgency is
  constitutional (RFC-0100 §1.7); the *threshold value* is not.
- **Q4 — Planner's Goal adoption.** *How* the Planner reads Drive + Need + Affect + context to *adopt* a
  Goal is the Planning Authority's RFC (downstream), not 0104's. 0104 only fixes that Drive is the
  signal read.
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0104 surfaces **no new constitutional question** and does **not** consume or promote it;
  the parked hypothesis remains exactly where 0101/0102/0103 left it, awaiting a real consumer
  (Manifesto / North-Star / Governance RFC).

---

## 12. Evolution / next RFC

RFC-0104 is **consumable** by the next Life-layer RFC:

- **RFC-0105 Goal** (or the Planning RFC) consumes Drive via the Planner's read (Drive Authority emits;
  Planning Authority adopts). 0105 must not redefine the orientation; it reads Drive as a fixed input to
  Goal adoption.
- **RFC-0108 Relationship** may feed the Social signal of Perception back via Memory (affecting Need →
  Affect → Drive, across cycles).

### 12.1 Phase-2 rhythm (the Gate)

Per the agreed Life-Architecture rhythm, every Life RFC follows:

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy; else Anchor) → next RFC.**

RFC-0104 has passed **Draft** and **Review / Challenge** (Architect self-review complete, see §13;
user Phase-C Challenge recorded in §13.4). No Foundation change was required. The Gate question
*"Foundation Stable?"* returns **YES** → anchor RFC-0104 and proceed to RFC-0105 Goal. RFC-0104 stops
here.

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0104 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to RFC-0105 Goal.

### 13.1 What was checked

| Foundation doc | Question 0104 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Drive needs as a *noun*? | ✅ Yes. Drive + its activation condition are named (§1.7); its negative boundaries already exclude Goal / Task / Affect. No new life-noun or negative rule was required. |
| **RFC-0100A** (Dynamics) | Is the two-loop flow sufficient to *produce* Drive? | ✅ Yes. `Drive = i(Affect)` at Affect→Drive is already specified (outer loop §2.1). No new flow vocabulary or loop was needed. |
| **RFC-0100B** (Ownership) | Does the ownership matrix + O1–O4 cover Drive's authority? | ✅ Yes, with one *clarification* (not a change): the matrix's `Produced by: Affect` was read as "Affect is the owned (and only) input; no second input at this joint" (§5.3). This is a reading of the existing row, not a matrix edit. |
| **RFC-0100C** (deferred) | Does Drive need the deferred Permissions/Transactions layer? | ✅ **No.** Validated: Drive is derived (`Mutated by: none`); the only direct writers are Memory and Goal. A transaction boundary at Drive would be a second writer (forbidden). 0100C stays deferred — confirming RFC-0101 §9 / RFC-0102 §9 / RFC-0103 §9 predictions (now **4th** confirmation). |

### 13.2 Holes found

- **None requiring a Foundation change.** The single item (the `Produced by: Affect` reading) is a
  **clarification of an existing matrix row**, explicitly scoped as *not* a Foundation change (§5.3)
  and therefore not routed through the Foundation Evolution Protocol.
- **Four carried-forward / new clarifications (design-intent discipline, not patches):**
  - **C1-analog (anti-bypass):** chain is strictly `Memory → Identity → Need → Affect → Drive`; Memory
    influences Drive only transitively via `f → g → h → i` (§5.2 callout + E5).
  - **C2-analog (authority boundary — carries forward RFC-0103 §6.2 C2):** *Drive Authority governs
    Drive derivation, not Affect/Need/Identity mutation*; it reads but never mutates upstream (§6.2
    Boundary A). Guards the `DriveManager.update_affect()` mistake.
  - **C3-analog (anti-persistence):** *Drive is a derived state with no storage authority*;
    `save/persist/update` and a `DriveMemory` store are forbidden (§5.4 callout + §8 negative example).
  - **NEW — Drive Authority ≠ Planning Authority (Boundary B, §6.2):** Drive emits orientation; it does
    **not** select/adopt the Goal. `Drive → Goal` direct authority edge forbidden; only `Drive →
    Planner (reads) → Goal`. Guards the `DriveManager.choose_goal()` mistake — the Drive-layer twin of
    the 0103 C2 boundary.

### 13.3 Gate verdict

> **Foundation Stable? → YES.**
> RFC-0104 is consumable by RFC-0105 Goal (Planner reads Drive). No patch to 0100/0100A/0100B is
> required. Per the Phase-2 rhythm, after user Review/Challenge confirmation (§13.4), anchor RFC-0104
> and proceed to RFC-0105 Goal.

This closes the Architect loop for Life-4: a fourth Life RFC consumed the Foundation, validated it, and
found it sufficient — the same "Foundation first, then 固化" rhythm as Life-1/2/3, and the 0100C
deferral prediction is now confirmed four times running.

### 13.4 Challenge (LAWP Phase C) — recorded

The user Challenge (Phase C) walked the four mandated questions and added three clarifications (all
F4 "shrink ambiguity", zero base edits). Recorded here as the Gate's Challenge input:

| Phase-C question | Answer |
|------------------|--------|
| **Q1 — Missing Foundation concept?** | ✅ PASS. Drive is fully covered: RFC-0100 (Drive noun §1.7), RFC-0100A (`Drive = i(Affect)`), RFC-0100B (Drive Authority). No new life-noun, state class, authority type, or persistence mechanism. No F1 trigger. |
| **Q2 — Dynamics contradiction?** | ✅ PASS. Chain `Memory → Identity → Need → Affect → Drive → Planner → Goal` is single-direction; no `Need → Goal` or `Affect → Goal` shortcut. Drive keeps its sole-bridge position. |
| **Q3 — Authority confusion?** | ✅ PASS. Boundary B confirmed, adopted as the symmetric fixed format *"Drive Authority governs drive derivation, not goal adoption."* Planner reads Drive; Planner is not under Drive Authority. |
| **Q4 — Foundation Patch required?** | ✅ PASS. No Ontology gap, no Dynamics gap, no Authority-ownership gap → **Foundation Stable: YES**, no F1–F4 trigger. |

**Three clarifications (all F4 "shrink ambiguity", zero base edits):**

- **D1 — Drive is operational orientation, not motivational content.** Need is content, Drive is
  direction; `Need == Drive` forbidden (§4 callout). Prevents Goal collapsing into a direct function of
  Need and skipping the Affect→Drive joint.
- **D2 — Drive exists only during one reasoning cycle.** Re-derived each cycle from Affect; no
  `Drive.restore()` (§4.2 callout). Cross-cycle continuity is inherited from Memory via Need→Affect,
  not stored.
- **D3 — Planner does not own Drive.** `Planner.modify(drive)` forbidden; only `read(drive)` — symmetric
  with `Planner.modify(goal)` being *permitted* (§8 negative example, Boundary B).

> **Challenge verdict → Foundation Stable: YES.** No Foundation patch required. (User Challenge,
> 2026-08-04.)

---

## Appendix A — Validation notes (no Foundation patch proposed)

No Foundation patch is proposed by RFC-0104. The only ambiguity surfaced — the matrix's
`Produced by: Affect` — was resolved by *clarification* (§5.3), a permitted reading of an existing row
that does not invoke the Foundation Evolution Protocol (F1 requires a proposed *change*; F4 would
require *shrinking* ambiguity — here there was no base edit at all). The four carried-forward/new
clarifications (C1-analog `Memory → Identity → Need → Affect → Drive` anti-bypass; C2-analog Drive
Authority governs derivation *not* upstream mutation; C3-analog Drive has no storage authority; NEW
Drive Authority ≠ Planning Authority, no `Drive → Goal` authority edge) are likewise clarifications —
they *shrink* reader ambiguity (F4's direction) without editing the base. The parked Constitutional
hypothesis from RFC-0101 Appendix A is untouched and remains parked.
