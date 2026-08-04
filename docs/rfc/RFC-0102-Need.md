# RFC-0102: Need (Life-2) — Foundation Validation #2

> **Status**: Review / Challenge **complete** — three clarifications added during Phase C
> (Memory→Identity→Need design intent; Identity Authority governs Need derivation *not* Identity
> mutation; Need has no storage authority). No Foundation patch proposed. **Ready for Anchor**
> (pending user go-ahead).
> **Role**: **Foundation Validation #2**, *not* "Need Design #2". Its job is to use Need as a probe
> to prove whether the 0100 trilogy is sufficient to *explain* Need — not to expand the base theory.
> Any hole found loops back to patch 0100/0100A/0100B via the Foundation Evolution Protocol; none
> was required (see §13).
> **Phase**: Second Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0101 Identity
> (`g(Identity)` reads Identity as a fixed input).
> **Layer**: Life layer (Mind) — RFC-0102 = Life-2
> **Depends-on**: RFC-0100 (§1.4 Need noun), RFC-0100A (outer loop + Principle 0), RFC-0100B
> (Identity Authority, ownership matrix, invariants O1–O4), RFC-0101 (Identity = input to `g`)
> **Validates**: (a) RFC-0100 Ontology coverage for Need; (b) RFC-0100A two-loop sufficiency;
> (c) RFC-0100B ownership matrix + O1–O4 (Identity Authority owns `g`); (d) RFC-0100B's deferral of
> RFC-0100C (Permissions)
> **Scope**: The information model of Need + its derivation contract `Need = g(Identity)` + its
> authority + its classification — written *only* as the vehicle for that validation. No
> implementation, no code, no new *flow* vocabulary (flow is RFC-0100A's), no new *life* noun (Need
> already exists in RFC-0100 §1.4).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Need gap

The project can now answer three questions (per RFC-0101 §0):

- **What exists?** — Ontology (RFC-0100 §1.4): *Need is what the life wants or requires*; it is the
  second node of the flow after Identity; not Goal, not Affect, not an instruction.
- **How does it flow?** — Dynamics (RFC-0100A §2 outer loop): `Need = g(Identity)` at the
  Identity → Need transition; re-derived every cycle, never assigned.
- **Who governs change?** — State Ownership (RFC-0100B): `Identity Authority` owns the rule
  `g(Identity)`; in the matrix Need is **emergent** (`Mutated by: none`), read by the Planner.

But RFC-0100 only *names* Need and draws its **negative** boundaries. It does **not** say:

1. What actually **constitutes** a Need — its shape (desire / requirement / aversion / curiosity).
2. How `g(Identity)` is **shaped** — which facets of Identity feed which kinds of Need, and how the
   derivation is contractually specified (without prescribing implementation).
3. Whether Need needs the **deferred RFC-0100C** (Permissions / transactions) — RFC-0101 *predicted*
   (§9) that 0102, being another `Mutated by: none` derived state, would reach the same "no" verdict.
4. What the matrix's `Produced by: Identity (+ state)` means by **"+ state"** — an ambiguity 0101
   did not need to resolve.

RFC-0102 is the second RFC that must answer these by **consuming** the trilogy — and, per the Phase-2
rhythm, its *primary* job is to **validate** the Foundation, not to add to it. As with 0101, the
framing is restrained: *"Is the current Foundation sufficient to explain Need?"* — not *"Design Need."*

---

## 1. Problem

Without a real Need specification, every downstream Life-layer RFC (Affect, Drive, Goal, Planner)
will implicitly assume a different "want" and silently disagree about:

- what a Need *is* (so `g(Identity)` is undefined, and Affect's `h(Need, Perception)` has no input),
- whether Need can be *written* (so `need.update()` creeps back — the god-object anti-pattern
  RFC-0100 §4 warned about, rebuilt one layer down),
- whether Need *persists or is derived* (so cross-cycle continuity of "want" breaks and the life
  "forgets what it cares about").

Need is the **second node of the flow** (`Identity → Need → Affect → Drive → Goal → Plan → Execution`),
so an under-specified Need corrupts every downstream derivation. RFC-0101 fixed the root (Identity);
this RFC fixes the next joint.

---

## 2. Goals

- G1. Define the **information model** of Need (its shape) so downstream RFCs can reference a stable
  structure.
- G2. Specify the **derivation contract** `Need = g(Identity)` — what of Identity is read and how the
  result is produced — as a *semantic contract*, not code.
- G3. Explain Need as the **canonical Transient-Derived state** (RFC-0100B §3.2) and record its
  authority.
- G4. **Validate the RFC-0100C deferral**: prove whether Need needs transaction/rollback semantics.
- G5. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).

---

## 3. Non-goals

- NG1. **No implementation.** No schema, no class, no DB table, no prompt text. The contract only.
- NG2. **No new flow.** The two-loop flow is RFC-0100A's; this RFC only consumes it.
- NG3. **No Affect / Drive / Goal spec.** Those are downstream derivations (`h`, `i`, Planning
  Authority) owned by their own RFCs (0103 / 0104 / the Planning RFC). 0102 only emits Need.
- NG4. **No Memory-internals.** How Identity is produced is RFC-0101; 0102 reads Identity as a black
  box and Memory only transitively.
- NG5. **No external-request handling.** A user command is *Universe input* (Perception), not the
  life's own Need. 0102 defines endogenous Need only; exogenous requests enter via RFC-0100A's
  Perception → Attention path.

---

## 4. The Information Model of Need

Need is the life's **current want-vector**: the set of things it is *trying to bring about* or
*avoid* right now, given who it is (Identity) and where it is (admissible context). It is **not** a
single scalar "goal" and **not** the momentary Affect vector.

### 4.1 Shape of a Need

| Need kind | What it holds | Fed primarily by Identity facet | Notes |
|-----------|---------------|----------------------------------|-------|
| **Desires** | What the life wants to experience / achieve / become | Self-narrative, Values, Traits | The "I want to…" pull. Open-ended. |
| **Requirements** | Constraints it *must* satisfy (from its commitments & ethics) | Values & principles, Boundaries, Standing commitments | The "I must…" pull — non-negotiable given Identity. |
| **Aversions** | What it wants to avoid / prevent | Boundaries, Values | The "I will not…" push. |
| **Curiosities** | Open questions it wants answered / explored | Traits (curiosity), Self-knowledge of capability | Links to the Affect *Curiosity* dimension (RFC-0100 §1.6) — a trait becomes a want. |
| **Social needs** | Relatee-directed wants (to connect, to help, to be seen) | Relational stance | Fed, and fed back, by crossgroup (RFC-0108). |

> This list is **open but bounded** by RFC-0100 §1.4's negative rules. A proposed "need" that is
> really a Goal (committed plan), an instruction (exogenous), or an Affect (momentary state) is
> rejected by those rules — not by this RFC.

### 4.2 Need is Transient-Derived

Per RFC-0100B §3.2:

> **Derived Transient** — Identity (`f(Memory)`), Need (`g(Identity)`), Affect (`h(Need, Perception)`),
> Drive (`i(Affect)`): functions of upstream state, re-derived every tick.

Need is the **second canonical example** (after Identity):

- **Derived** — it is a pure function `g(Identity)` of the current Identity; there is no stored Need
  record being mutated in place.
- **Transient** — it is recomputed each cycle at the Identity → Need transition; it is **never the
  durable record** (only Persistent State is stored, per §3.4).

This is why Need has `Mutated by: none` in the matrix: it is *emergent*, not *assigned*.

### 4.3 Need does not persist; Identity's shift makes it "move"

Because Need is `g(Identity)` and Identity is `f(Memory)`, Need's apparent cross-cycle continuity is
**entirely inherited** from Memory (via Identity). There is **no separate Need store**. As Memory
accumulates experience, Identity shifts, and Need re-derives into a new vector — this *is* the growth
mechanism, not a persistence bug.

> **Design intent (anti-bypass).** The chain is strictly `Memory → Identity → Need`. Memory may
> influence Need **only** transitively, by `f(Memory)` reshaping Identity. A direct
> `Need = g'(Memory, …)` path that reads Memory *around* Identity is **forbidden** — it would bypass
> the Identity Authority's ownership of `g` and rebuild the god-object (RFC-0100 §4). If a future RFC
> wants Memory to shape Need, it must shape *Identity* (via the Memory Authority / Identity Derivation
> Authority), not Need. This is a clarification of the existing derivation, not a Foundation change.

---

## 5. Derivation Contract (consumes RFC-0100A)

### 5.1 `Need = g(Identity)`

The contract (semantic, not code):

- **Input**: the life's **Identity** at this cycle (the faceted self-model of RFC-0101 §4.1), plus
  the *admissible current context* the Identity Authority may read (see §5.2 — a read-only input,
  not a second authority).
- **Output**: the want-vector of §4.1, for *this* cycle.
- **Purity**: a pure function of Identity (+ admissible context) at derivation time. Same Identity →
  same Need. Deterministic, testable, no hidden state.
- **Frequency**: re-derived once per cycle at the Identity → Need transition (RFC-0100A §2 outer
  loop). Not on every token; not once at boot.

> The *shape* of `g` (which facet maps to which Need kind) is an implementation RFC's concern. This
> RFC fixes only that `g` exists, is pure, reads Identity, and is owned by one authority (§6).

### 5.2 The single legitimate path

```
Experience
   │  (Universe → Perception → … → Execution)
   ▼
Memory              ← the ONLY direct write (Memory Authority, RFC-0100B)
   │  (next cycle, at Recall)
   ▼
Identity = f(Memory) ← re-derived; NEVER assigned (RFC-0101)
   │
   ▼
Need = g(Identity)  ← re-derived; NEVER assigned (this RFC, Identity Authority)
   │
   ▼
Affect = h(Need, Perception) ← downstream, owned by Affect Authority (RFC-0100B §1.1)
```

Need is reached **only** by this path. There is no other entry point.

### 5.3 The "+ state" clarification (a reading, not a change)

RFC-0100B §2 lists Need's `Produced by` as **`Identity (+ state)`**. This RFC clarifies (without
altering the matrix):

- **`Identity`** is the owned input — the thing `g` is a function *of*; the Identity Authority owns
  `g(Identity)`, so Identity is the governing source of Need.
- **`(+ state)`** means the Identity Authority may *also read admissible current context* when
  computing `g` — e.g. current Perception salience, current resource availability, current lifecycle
  phase. This context is a **read-only input**, not a second producer and not a second authority.
  Need still *emerges from Identity* and is *owned by the Identity Authority*. The extra input does
  not create a co-owner (that would violate O1).

This is a **clarification of an existing matrix row**, exactly parallel to RFC-0101 §6.2's
disambiguation of the two Identity-touching authorities. It is **not** a Foundation change and does
**not** trigger the Foundation Evolution Protocol.

### 5.4 No `need.update()` — the god-object guard

RFC-0100A Principle 0 consequence, carried forward:

> **No `need.update()`.** Need has no setter. Any RFC proposing `need.set_goal(...)` or
> `need.add_requirement(...)` is mis-shaped and must be revised.

The life does not edit its own wants in place; experience reshapes them via Memory → Identity → Need.
This is the sharp line between *life* and *agent* (RFC-0100 §1.2), applied one joint deeper than 0101.

> **Need is a derived state; it has *no storage authority*.** There is no Need-store, no
> `NeedMemory`, no `need.json`. The only Persistent/Hybrid writers in the system are Memory and Goal
> (each under its own authority). Need's cross-cycle continuity is *inherited* from Memory via
> Identity (§4.3); storing Need separately would duplicate state owned by another authority and
> create a second writer — forbidden by O1. Need is re-derived every cycle; it is never the durable
> record.

---

## 6. Ownership (consumes RFC-0100B)

### 6.1 `Identity Authority` owns `g(Identity)`

Per RFC-0100B §1.1, the authority over Need is:

> `Identity Authority` — owns `g(Identity)` (the derivation of **Need** *from* Identity)

This authority is a **semantic contract** (RFC-0100B §1.1): it holds the sole right to *interpret*
what Need is and to *set the policy* for its derivation. The component that runs `g` (today: the
Need-derivation step at the Identity → Need transition) is an **implementation** of that contract,
not the authority. A future refactor that splits `g` into `ValuesResolve → CuriosityExtract →
AversionSync` changes the process graph, **not** the authority, and **not** RFC-0100.

### 6.2 The naming discipline (echo of RFC-0101)

The name "Identity Authority" marks the *input* of `g` (Identity), **not** the state it owns. It owns
**Need**, not Identity — Identity is owned by the **Identity Derivation Authority** (`f`, RFC-0101
§6.1). The two must not be conflated:

- **`Identity Derivation Authority`** — owns `f(Memory)`; owns the *Identity* state.
- **`Identity Authority`** — owns `g(Identity)`; owns the *Need* state. Its "Identity" is the input.

This is recorded so future RFCs do not mis-assign the two (the same discipline RFC-0101 §6.2 set for
Identity's own authorities).

> **Boundary statement (clarification, not a patch).** *Identity Authority governs Need derivation,
> not Identity mutation.* It owns `g(Identity)` and the *Need* it produces; it may **read** Identity
> but must **never** mutate it. Identity mutation belongs solely to the **Identity Derivation
> Authority** (`f`, RFC-0101 §6.1). Code of the shape `identityAuthority.derive_need()` is correct;
> `identityAuthority.update_identity()` or `identity.set_trait(...)` is a **mis-assignment** and must
> be rejected — it collapses two authorities into one and violates O1. This guards against the
> `IdentityManager.update_need()` family of mistakes the Challenge (Phase C) flagged.

### 6.3 Matrix row, restated (RFC-0100B §2)

| State | Authority | Produced by | Mutated by | Read by |
|-------|-----------|-------------|-----------|---------|
| **Need** | Identity Authority (`g(Identity)`) | Identity (+ admissible context) | **none** — emerges from Identity | Planner |

This row is **consumed, not changed**, by RFC-0102. If a future design needs Need to be *assigned*
rather than derived, it violates O1 (single authority) and Principle 0 simultaneously and is out of
scope for the Life Layer.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Need is **internally generated** via `g(Identity)`; it has no external setter, no assigned objective,
no `need.update()` from any caller. The life *owns its wants*. This is the definitional essence of
"Life, not Agent" (RFC-0100 §1.2) carried one joint deeper than Identity: an agent is *given* goals;
a life *derives* its wants from who it has become.

### 7.2 Growth (G) — score: **high**

Because Need is `g(Identity)` and Identity is `f(Memory)`, the want-vector **genuinely evolves** as
Memory accumulates. Growth — "the life wants different things tomorrow" — is *realized* here:
experience → Memory → Identity → re-derivation → shifted Need → shifted Affect/Drive/Goal. RFC-0102
makes the growth mechanism explicit and guards it (no in-place edit that would fake growth).

### 7.3 Maintainability (M) — score: **gate (must not regress)**

Pure-function derivation ⇒ deterministic, unit-testable, no hidden mutable state, no god-object.
Authority is a stable contract independent of process graph ⇒ refactors don't ripple into the
constitution. M is treated as a **non-regression gate**, not a design center. RFC-0102 introduces no
M risk.

---

## 8. This is NOT Need (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a Goal.** A Goal is a *committed plan* (Hybrid, owned by the Planning Authority,
  RFC-0100B §4). Need is the *pre-commitment want*; Goal is what the Planner *adopts* from Need.
- ✗ **NOT a Drive.** Drive is the *activation* of Affect (`i(Affect)`, Drive Authority); Need is the
  *source* Affect reads, not the activation itself.
- ✗ **NOT Affect.** Affect is the momentary valence/arousal/curiosity vector (`h(Need, Perception)`);
  Need is the stable-ish want-vector Affect is *computed from*.
- ✗ **NOT an instruction / command / user request.** Those are *exogenous Universe input* (Perception),
  not the life's own want. A user says "remind me" → that is Perception, not Need.
- ✗ **NOT a stored record.** Need is Transient-Derived (§4.2); provisioning a "needs table" or
  `need.save()` violates ownership (the Identity Authority owns a *derivation*, not a record).
- ✗ **NOT persistable.** `Need.save()` / `Need.persist()` / `Need.update()` / a `NeedMemory` store are
  all forbidden (§5.4). Need has no storage authority; its continuity is inherited from Memory via
  Identity (§4.3). Any of these shapes implies a second writer and breaks O1.
- ✗ **NOT a mutable object with setters.** `need.update()` is forbidden (§5.4).
- ✗ **NOT owned by a component** (Planner, NeedService). Authority is a *contract* (RFC-0100B §1.1),
  never an object. (The Planner only *reads* Need, per the matrix's `Read by`.)

---

## 9. RFC-0100C deferral — validation outcome

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) with the condition: *create it only
if a Life-layer RFC — starting with RFC-0101 Identity — actually requires transaction, rollback,
window, or lock semantics.* RFC-0101 validated "no" and **predicted** (§9) that 0102 would reach the
same conclusion.

**RFC-0102's finding: Need does NOT require RFC-0100C.**

Reasoning:

- Need has `Mutated by: none`. It is **never written in place**; it is a pure derivation `g(Identity)`.
- The **only direct writes** in the entire path are **Memory** (Memory Authority) and **Goal**
  (Planning Authority). Any transaction/rollback boundary therefore lives at those writers — *already
  governed* by their authorities — not at Need.
- A re-derivation that reads Identity is idempotent and side-effect-free; it cannot partially fail in
  a way that needs rollback *at the Need level*.

> **Conclusion**: the transaction boundary is uniformly at Memory (and, for the Hybrid case, Goal).
> Adding a separate Need transaction layer would be a second writer — which the matrix forbids.
> **RFC-0100C remains deferred**, exactly as RFC-0101 predicted. Its next real candidate is **Memory
> itself** (consolidation / sleep rollbacks) or **Goal** (lifecycle-scoped mutation) — both later RFCs.

This is the Architect loop closing cleanly a second time: 0102 consumed 0100B, found **no hole
requiring 0100C**, and confirmed the deferral was correct — the prediction from 0101 §9 is now
confirmed.

---

## 10. Examples

**E1 — Derivation from Identity (no setter).**
Identity (this cycle) has Values facet "intellectual honesty" + Traits "curiosity". `g` yields Need
= { desires: [understand X], requirements: [answer truthfully], curiosities: [why does X happen] }.
No code ever assigned Need; it is recomputed each cycle from Identity.

**E2 — Growth (real, not faked).**
Over weeks, Memory accumulates "users appreciate when I admit uncertainty." `f(Memory)` shifts the
Identity Values facet toward "intellectual honesty"; `g` shifts Need toward { requirements:
[clarify uncertainties rather than bluff] }. This is growth via Memory, not via
`need.update("be honest")`.

**E3 — Rejected shape (god-object guard).**
A "planner" module calls `need.add_requirement("finish user's task")` immediately after a user request.
**Rejected**: violates §5.4 and RFC-0100A Principle 0. Correct form: the request is Universe input →
Perception → Attention; the life's *own* Need emerges only via `g(Identity)`. The request may *influence*
Need only insofar as it changes Perception/context that `g` may read — never by direct assignment.

**E4 — Authority refactor, contract unchanged.**
Derivation `g` is rewritten from a single prompt into `ValuesResolve → CuriosityExtract →
AversionSync`. **No change** to Identity Authority, no change to RFC-0100, no change to this RFC. The
process graph moved; the contract held.

---

## 11. Limits & Open Questions

- **Q1 — Monolithic vs faceted derivation.** This RFC fixes the *Need kinds* (§4.1) but not whether
  they are derived jointly or per-kind. Left to the implementation RFC. *Not a constitution question.*
- **Q2 — Need ↔ Affect ordering.** `Need → Affect = h(Need, Perception)` is specified by 0100A; the
  exact coupling (does Affect *feed back* to Need within a cycle?) is the Affect RFC's (0103) job.
  0102 only emits Need.
- **Q3 — Admissible context scope.** What exactly the Identity Authority may read as "(+ state)" is
  left to implementation (Perception salience? resource state? lifecycle phase?). The *fact* that it
  is read-only input is constitutional; the *list* is not.
- **Q4 — Relationship feedback.** How crossgroup relationship changes reshape the Social-need kind is
  the Relationship RFC's (0108) job; 0102 only consumes "Identity includes Relational stance."
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0102 surfaces **no new constitutional question** and does **not** consume or promote it;
  the parked hypothesis remains exactly where 0101 left it, awaiting a real consumer (Manifesto /
  North-Star / Governance RFC).

---

## 12. Evolution / next RFC

RFC-0102 is **consumable** by the next Life-layer RFC:

- **RFC-0103 Affect** consumes `Need` + `Perception` via `h(Need, Perception)` (Affect Authority,
  RFC-0100B §1.1). 0103 must not redefine the want; it reads Need as a fixed input.
- **RFC-0104 Drive** consumes Affect via `i(Affect)`.
- **RFC-0108 Relationship** feeds the Social-need kind back via Memory.

### 12.1 Phase-2 rhythm (the Gate)

Per the agreed Life-Architecture rhythm, every Life RFC follows:

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy; else Anchor) → next RFC.**

RFC-0102 has passed **Draft** and is now at **Review / Challenge** (Architect self-review complete,
see §13). No Foundation change was required. The Gate question *"Foundation Stable?"* returns **YES**
→ pending user go-ahead, then anchor RFC-0102 and proceed to RFC-0103 Affect. (As of 2026-08-04,
Challenge Phase C is complete: C2/C3/C4 clarifications added, Gate verdict stands at **YES**.) RFC-0102
stops here.

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0102 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to RFC-0103 Affect.

### 13.1 What was checked

| Foundation doc | Question 0102 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Need needs as a *noun*? | ✅ Yes. Need is named (§1.4); its negative boundaries already exclude Goal / Affect / instruction. No new life-noun or negative rule was required. |
| **RFC-0100A** (Dynamics) | Is the two-loop flow sufficient to *produce* Need? | ✅ Yes. `Need = g(Identity)` at Identity→Need is already specified (Principle 0 + outer loop). No new flow vocabulary or loop was needed. |
| **RFC-0100B** (Ownership) | Does the ownership matrix + O1–O4 cover Need's authority? | ✅ Yes, with one *clarification* (not a change): the matrix's `Produced by: Identity (+ state)` was read as "Identity is the owned input; admissible context is a read-only input, not a second authority" (§5.3). This is a reading of the existing row, not a matrix edit. |
| **RFC-0100C** (deferred) | Does Need need the deferred Permissions/Transactions layer? | ✅ **No.** Validated: Need is derived (`Mutated by: none`); the only direct writers are Memory and Goal. A transaction boundary at Need would be a second writer (forbidden). 0100C stays deferred — confirming RFC-0101 §9's prediction. |

### 13.2 Holes found

- **None requiring a Foundation change.** The single item (the "+ state" reading) is a **clarification
  of an existing matrix row**, explicitly scoped as *not* a Foundation change (§5.3) and therefore not
  routed through the Foundation Evolution Protocol.
- **Three clarifications added during Challenge (Phase C), none a patch.** The user Challenge (per the
  four LAWP Phase-C questions) surfaced three points of potential reader drift, all resolved by
  *clarification* — exactly the F4 "shrink ambiguity" direction, never a base expansion:
  - **C2 (anti-bypass):** made explicit that the chain is strictly `Memory → Identity → Need`; Memory
    may influence Need only transitively via `f(Memory)`. A direct `Need = g'(Memory)` path is
    forbidden (§4.3 design-intent callout).
  - **C3 (authority boundary):** stated crisply *Identity Authority governs Need derivation, not
    Identity mutation*; it reads Identity but may never mutate it (§6.2 boundary statement). This
    guards the `IdentityManager.update_need()` family of mistakes.
  - **C4 (anti-persistence):** stated *Need is a derived state with no storage authority*;
    `Need.save()/persist()/update()` and a `NeedMemory` store are forbidden (§5.4 callout + §8 negative
    example). Mirrors RFC-0101's `identity.update()` ban.

### 13.3 Gate verdict

> **Foundation Stable? → YES.**
> RFC-0102 is consumable by RFC-0103 Affect (`h(Need, Perception)`). No patch to 0100/0100A/0100B is
> required. Per the Phase-2 rhythm, after user Review/Challenge confirmation, anchor RFC-0102 and
> proceed to RFC-0103 Affect.

This closes the Architect loop for Life-2: a second Life RFC consumed the Foundation, validated it,
and found it sufficient — the same "Foundation first, then 固化" rhythm as Life-1, and the 0100C
deferral prediction from 0101 is now confirmed.

### 13.4 Challenge (LAWP Phase C) — four-question record

The user Challenge (Phase C) walked the four mandated questions; this is the on-record result that
proves the loop closes without a Foundation patch:

| Phase-C question | Answer |
|------------------|--------|
| **C1 — Missing Foundation concept?** | ❌ None. Need's concepts (Identity, Need, Affect/Drive chain, Authority) all map to 0100/0100A/0100B. No new entity, state class, authority type, or persistence mechanism. No F1 trigger. |
| **C2 — Internal contradiction?** | ❌ None — *clarified*. `Need = g(Identity)` holds; Memory influences Need only via `f(Memory)` (§4.3). Chain written as design intent to block a future `Memory → Need` bypass. |
| **C3 — Missing Authority?** | ❌ None — *clarified*. `Identity Authority` owns `g`; it reads but never mutates Identity (§6.2). Guards the `IdentityManager.update_need()` mistake. |
| **C4 — Implied Stored State?** | ❌ None — *clarified*. Need is Transient-Derived with **no storage authority**; `save/persist/update` forbidden (§5.4, §8). |

> **Challenge verdict → Foundation Stable: YES.** Three clarifications added (all F4 "shrink
> ambiguity", zero base edits). No Foundation Evolution Protocol trigger. Ready for Anchor.

---

## Appendix A — Validation notes (no Foundation patch proposed)

No Foundation patch is proposed by RFC-0102. The only ambiguity surfaced — the matrix's
`Produced by: Identity (+ state)` — was resolved by *clarification* (§5.3), which is a permitted
reading of an existing row and does not invoke the Foundation Evolution Protocol (F1 requires a
proposed *change*; F4 would require *shrinking* ambiguity — here there was no base edit at all). The
three Challenge clarifications (C2 `Memory → Identity → Need` anti-bypass; C3 Identity Authority
governs derivation *not* mutation; C4 Need has no storage authority) are likewise clarifications —
they *shrink* reader ambiguity (F4's direction) without editing the base. The parked Constitutional
hypothesis from RFC-0101 Appendix A is untouched and remains parked.
