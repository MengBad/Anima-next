# RFC-0111: Relationship (Life-11) — Foundation Validation #11

> **Status**: **Anchored (Foundation Validation #11 — verdict: APPROVED WITH CLARIFICATIONS)**. *Draft →
> Challenge → Anchor complete. Zero Ontology base edit; no new Authority; no new Memory type. Relationship
> classified as a Derived projection of `Memory.relations ∩ Identity` (not a second Memory, not a motivation
> source, not a second Identity system). M1.11 Relationship Engine unlocked.* This RFC *consumes* the
> Foundation; it may not modify it. The four
> candidate clarifications (C1/C2/C3/C4) below are **proposed at Challenge** (F4, zero base edits) — they are
> recorded as *proposals* for the user's LAWP Phase C verdict and are **not yet adopted**. **Role**:
> **Foundation Validation #11**, *not* "Relationship Engine Design #11". Its job is to use the
> `Memory.relations → Identity → Need(SOCIAL) → … → Goal` joint as a probe to prove the 0100 trilogy is
> sufficient to *explain* Relationship — the **fourth derived-state / projection layer** to be validated
> (after Need/Affect/Drive are Derived, Reflection/Execution are Act/Event, and Persona is a Derived
> projection of Identity). Relationship sits at the **boundary of the Life and the Environment**: it is the
> *model the life holds of another agent* (RFC-0100 §1.13) — a Derived projection of `Memory.relations ∩
> Identity`, keyed per external agent, *not* an Owned Life state, *not* a second Memory, *not* a Goal/Drive
> mutation source. Relationship is **already named** in RFC-0100 (§1.3 Mind list; §1.12 table "per-other-agent
> knowledge and history"; §1.13 "the model the life holds of another agent, including history, trust, and role")
> — so, like Persona, the central question is classification: **is Relationship a Derived projection (a model
> of an external relation, recomputed from Memory + Identity) or a second Memory / a motivation source / a
> second Identity system?**
> **Central risk** (per the user's brief — distinct from Persona's risk): Persona's risk was a *derived
> identity layer masquerading as a second Identity root* (`Persona → Identity`). Relationship's risk is
> **three-fold and lands downstream of the self**, not at the self: (a) **Relationship → Memory bypass** —
> Relationship becoming a *social memory library* (`RelationshipStore` / `SocialMemory`) that writes around
> the single Memory Authority; (b) **Relationship → Goal mutation** — Relationship becoming a *motivation
> source* that sets Goal / patches Drive directly (`relationship.set_goal()`); (c) **Relationship → Identity
> rewrite** — Relationship becoming a *second identity system* that reverse-defines the self
> (`relationship.rewrite_identity()`). All three collapse the single-sink / single-root rule (O1) — but at
> the *relational* seam, where the object of the model (the other agent) lives in the Environment, tempting
> the model to *own* its own store or to *drive* behavior directly. So Relationship is the most
> **boundary-sensitive** Life RFC: the one place where a slip rebuilds the god-object at the *relation* layer
> (second Memory, second motivation source, or second self-writer). Per F1–F4 the guard is a *reading* of
> existing vocabulary (§1.13 "the model the life holds of another agent"; §240 "a Provider that reads the
> user's Relationship model … is a violation"), not a new noun/authority.
> **Phase**: Eleventh Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0110 (the projection it
> must never become a second Identity root of) and transitively RFC-0101..0109 + RFC-0100 §1.3/§1.12/§1.13 +
> RFC-0100A (outer loop) + RFC-0100B (state classes §3, ownership matrix §2 — *no Relationship row*; O1–O4).
> **Layer**: Life layer (Mind — the relational model; spans the Life/Environment boundary) — RFC-0111 = Life-11
> **Depends-on**: RFC-0100 (§1.3 Mind; §1.12 Relationship table; §1.13 Relationship = "the model the life holds
> of another agent"; §240 Provider-must-not-read-Relationship; §318 crossgroup = Relationship), RFC-0100A
> (outer loop), RFC-0100B (state classes §3, ownership matrix §2 — *no Relationship row*; O1–O4), RFC-0101
> (Identity = `f(Memory)`, which *already ingests* relation records — identity.py:40-41), RFC-0102 (Need derives
> SOCIAL from `self_model["relations"]` — need.py:42-47, *already wired*), RFC-0108 (the single Memory sink;
> `Memory.relations` + `MemoryAuthority.ingest_relation`), RFC-0109 (Reflection → Memory is the only legitimate
> write path), RFC-0110 (Relationship must not become a second Identity root), `PROTOCOL-Life-Validation.md` (V1–V6)
> **Validates**: (a) RFC-0100 Ontology coverage for Relationship (already named §1.13) — *classification*, not a
> new noun; (b) RFC-0100A loop sufficiency for `Memory(relation) → Identity → Need(SOCIAL) → … → Goal`; (c)
> RFC-0100B state-class fit — is Relationship a *Derived projection* (recomputed from `Memory.relations ∩
> Identity`) or an *Owned state / second Memory* (with its own lifecycle/authority); (d) RFC-0100C (Permissions/
> Transactions) — the deferred question, now testable an *eleventh* time; Relationship is a *read-side projection*
> that writes nothing (its only write path is `MemoryAuthority.ingest`, shared with Reflection), so it surfaces
> **no** new joint, and the operational 0100C watchpoint is already **satisfied** (M1.8 built and passed, no
> observed contradiction); (e) the **Memory Authority → Relationship projection** boundary — Relationship must NOT
> become a new authority (the matrix has *no Relationship row* by design; this RFC keeps it that way — C2).
> **Scope**: The *information model of Relationship* (as a Derived projection of `Memory.relations ∩ Identity`,
> keyed per external agent) + its derivation contract + its (non-)authority + its (non-)existence as a second
> Memory or a motivation source. **No implementation, no code, no Relationship engine, no new life noun, no new
> authority, no new matrix row** (Relationship is classified; §1.13 already names it).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Relationship gap

The project can now answer eleven questions (per RFC-0110 §0 + this RFC):

- **What exists?** — Ontology (RFC-0100 §1.13): *"The model the life holds of another agent (human or bot),
  including history, trust, and role."* Relationship is a **named noun**, defined as an *internal model* of an
  *external* agent — but only negatively as a store-of-*what* ("history, trust, role"). It is **not** introduced
  as a state class. §1.12 table ("per-other-agent knowledge and history") and §1.3 (Mind list) place it *inside*
  the Life's Mind. §318 confirms the real code's `crossgroup` (per-user cross-group facts) *is* Relationship,
  currently ad hoc. §412–413 flags an open question (span groups vs per-identity) — ontology keeps it
  *per-other-agent*.
- **How does it flow?** — Dynamics (RFC-0100A §3 + the real code): a relationship *observation* enters via
  `MemoryAuthority.ingest(kind="relation")` (RFC-0108 / memory_authority.py:62-63 / memory_engine.py:39-41) →
  `Memory.relations` → (next Recall) `Identity = f(Memory)` *already ingests* relation records into
  `self_model["relations"]` (identity.py:40-41) → `Need = g(Identity)` *already derives* SOCIAL wants from
  `self_model["relations"]` (need.py:42-47) → `Affect = h(Need, Perception)` → `Drive = i(Affect)` → `Goal`
  (adopted by Planning Authority). The flow is `Memory(relation) → Identity → Need(SOCIAL) → … → Goal`, **not**
  `Relationship → Goal` directly. **This chain is already partially implemented in code** — stronger evidence
  than Persona that the Foundation explains Relationship without a new noun.
- **Who governs change?** — State Ownership (RFC-0100B §2): the matrix has **no Relationship row**. The
  *relation records* that back Relationship are owned by the **Memory Authority** (line 167 — the *one* sink).
  Relationship the *model* is a *projection* of that Memory (plus Identity) — so by the matrix's own discipline,
  Relationship is **not an independently-owned state**; it is a *derived view* over Memory + Identity. RFC-0111
  must keep it that way (no new row, no new authority — C2).
- **Where does it sit relative to the chain?** — Relationship is a **Derived projection** that lives *beside*
  the derivation chain (like Persona), but its *content* feeds the chain *upstream* via Memory (not downstream
  via mutation). It reads `Memory.relations` + `Identity` (read-only) and *models* an external agent; it does
  **not** derive Need/Affect/Drive (those come strictly from Identity, RFC-0102..0104) and does **not** write
  Goal.
- **What is Relationship *not*?** — Not a second Memory (`RelationshipStore` / `SocialMemory` would duplicate
  O1, parallel to RFC-0109 C3 / RFC-0110 C3). Not a motivation source (`relationship.set_goal()` would make it
  an optimizer, parallel to RFC-0109's RL-collapse, now at the *relation* layer). Not an Authority that mutates
  Identity/Need/Drive/Goal (the god-object at the relational seam). Not a Provider-readable action driver (RFC-0100
  §240: a Provider that reads the Relationship model to decide how to send a message is a *violation*). Not a
  stored ContactBook.
- **Does Relationship need RFC-0100C?** — This is the **central continuation** of the 0100C watchpoint.
  Relationship is a *read-side projection*: it writes **nothing** directly (its only write path is the shared
  `MemoryAuthority.ingest`, already validated at M1.8). It introduces **no new transaction/atomicity joint** —
  the only joint 0100C cares about (single-writer consistency at Memory ingestion) was already tested at
  **M1.8** and passed with **no observed contradiction**. So by the time this RFC is drafted, the operational
  0100C watchpoint is **already satisfied** (parallel to RFC-0109 §0 / RFC-0110 §0). This RFC records that
  honestly: 0100C stays deferred an *eleventh* time, operational phase **complete**.
- **Where does the output go?** — The user's Q3: does Relationship *persist* as a store (`RelationshipStore` /
  `SocialMemory`), or is it a *projection* of Memory (re-derived each Recall)? This RFC answers: Relationship is
  a **Derived projection** — `Relationship = model(Memory.relations, Identity, external_ref)` where
  `Memory.relations` is the *single* sink (owned by Memory Authority) and `Identity = f(Memory)`. It persists
  *only transitively* (because the relation records persist in Memory; the model re-projects on Recall). A
  `RelationshipMemory` would be a **second Memory** — the precise O1 violation RFC-0108 closed, now tested at
  the *relational* layer.
- **Who may start Relationship?** — The user's Q2: who derives the Relationship model? This RFC answers:
  Relationship is **derived as a stateless projection** reading `Memory.relations` + `Identity` — it is a
  *view*, not a governance unit (parallel to Persona, and *cleaner*: Persona kept a carrier under the Identity
  Derivation Authority; Relationship's *data* is already owned by the Memory Authority, so the projection needs
  **no new authority** at all — C2). No new authority.

But RFC-0100 §1.13 only *names* Relationship and draws its **content** boundary (history/trust/role) and its
**ownership** boundary (§240: not a Provider action driver). It does **not** say:

1. Whether Relationship is a **State or a projection of a State** — the crux of the user's Q1. If Relationship is
   modeled as an *Owned State* (with a lifecycle, a store, a `Relationship Authority`), it becomes a **second
   Memory** (it would need a store of "per-other-agent knowledge") — duplicating O1 (the same risk RFC-0108
   closed at the sink, now tested at the *relational* store). If it is modeled as a **Derived projection**
   (`Relationship = model(Memory.relations, Identity, external_ref)`, recomputed each Recall), it is the
   *internal model of an external relation* — exactly parallel to how Identity is `f(Memory)` (a Derived state,
   no setter, no store of its own).
2. Whether Relationship may **mutate Goal/Drive/Need directly** — the *optimizer* anti-pattern (`relationship.
   set_goal()`), the *mirror* of RFC-0109's RL-collapse, now at the *relation* layer: a relationship "demands"
   a goal, bypassing `Memory → Identity → Need → … → Goal`.
3. Whether Relationship may **own a store** (second Memory) — `relationship.save_to_social_bank()` writing to a
   `RelationshipStore` / `SocialMemory` (O1 violation, RFC-0108 / RFC-0109 C3). Relationship *models* Memory; it
   does not *own* a store.
4. Whether Relationship may **reverse-define Identity** — `relationship.rewrite_identity()` (the *mirror* of
   RFC-0110's `persona.rewrite_identity()`), making Relationship a second self-writer. Identity is `f(Memory)`,
   including relations (identity.py:40-41); Relationship is downstream of Identity, never upstream.
5. Whether Relationship may **bypass Memory on write** — `relationship.upsert_relation_store()` writing a relation
   record *around* the Memory Authority (the *central* risk the user named: Relationship → Memory bypass). The
   only legitimate write is `MemoryAuthority.ingest(kind="relation")` (via Reflection), never Relationship.

RFC-0111 is the eleventh RFC that must answer these by **consuming** the trilogy — and, per the Phase-2 rhythm,
its *primary* job is to **validate** the Foundation, not to add to it. The framing is restrained: *"Is the
current Foundation sufficient to explain Relationship as a Derived projection of `Memory.relations ∩ Identity`
(not a second Memory, not a motivation source, not a second Identity system)?"* — not *"Design the Relationship
Engine."* The new wrinkle: Relationship is the **first Life RFC that spans the Life/Environment boundary** — its
*object* (the other agent) is Environment, while its *model* is a Derived Life-layer projection. This tests
whether the Foundation's single-sink rule (O1) and single-root rule (O1) are **symmetric enough** to cover a
state whose *content* is about an *external* entity yet whose *representation* is a derived view over the *internal*
Memory + Identity.

---

## 1. Problem

Without a real Relationship-validation RFC, the downstream Life-layer work (a Relationship engine, the
`crossgroup` migration in RFC-0100 §318) will implicitly assume a different "relational layer" and silently
disagree about:

- what Relationship *is* (so the Mind has an undefined relational surface, and `Memory.relations` has no defined
  *model* over it),
- whether a **second Memory** may exist (`RelationshipStore` / `SocialMemory` — a per-other-agent knowledge base)
  — the hidden second sink that duplicates the single-Memory rule (O1) and silently reopens RFC-0100C,
- whether Relationship may **mutate a derived state or Goal directly** — the god-object / optimizer anti-pattern
  *rebuilt at the relational seam* (e.g. `relationship.set_goal()`), the *mirror* of RFC-0109's
  `reflection.update_goal()`,
- whether Relationship may **reverse-define Identity** (`relationship.rewrite_identity()`) — the *mirror* of
  RFC-0110's `persona.rewrite_identity()`, now at the *relation* layer (a second self-writer),
- whether Relationship may **bypass Memory on write** (`relationship.upsert_relation_store()`) — the *central*
  risk the user named (Relationship → Memory bypass), which would make Relationship a parallel sink beside the
  one Memory Authority,
- whether Relationship is **the model or the driver** (the user's Q1/P1, and RFC-0100 §240) — if 0111 silently
  re-applies the "Owned, with a lifecycle" pattern from Goal to Relationship, it would *mis-classify* a
  projection as a store and create a second Memory, breaking the 0100B single-sink rule (line 167) and re-opening
  0100C.

Relationship is the **eleventh node** (`… → Identity → [Persona] ; Memory.relations → Identity → Need(SOCIAL) →
…`); an under-specified Relationship corrupts the *relational* surface of the self and tempts a second Memory
(the most duplicated-sink risk in the whole series). RFC-0109 fixed the sink (Memory) and the transform
(Reflection); RFC-0110 fixed the expression face (Persona); this RFC fixes the **relational model** — and, for
the first time, the joint is a **Derived projection that spans the Life/Environment boundary**, not a transform,
not an event, not a sink, not an expression face.

---

## 2. Goals

- G1. **Classify Relationship correctly**: prove it is a **Derived projection** (`Relationship = model(
  Memory.relations, Identity, external_ref)`, keyed per external agent — RFC-0100 §1.13 per-other-agent), *not*
  a second Memory (neither Owned nor a new Authority) — the central answer to the user's Q1/P1.
- G2. Specify the **derivation contract** — how the Relationship model is projected from `Memory.relations` +
  Identity (as a *stateless view*, not a store), as a *semantic contract*, not code.
- G3. **Close the RFC-0100C question an eleventh time — and record that the operational watchpoint is SATISFIED.**
  The real test (M1.8 Memory Ingestion Interface) has *already been built and passed with no observed
  contradiction*. Relationship writes nothing directly (its only write path is the shared `MemoryAuthority.
  ingest`, validated at M1.8), so it surfaces no new joint. Per F1–F4, this Draft may **not** pre-activate 0100C
  on speculation; it records the deferral as *complete at the operational level*.
- G4. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G5. Carry forward the Challenge discipline via `PROTOCOL-Life-Validation.md` V1–V6: apply V1 (anti-bypass —
  Relationship reads only `Memory.relations` + Identity, never writes them), V2 (downstream bypass — Relationship
  writes nothing directly; it is a projection, not a sink), V3 (Authority Boundary, symmetric format, Q2 — the
  11th instance: *Memory Authority governs relation-record ingestion; no Relationship Authority*), V4 (Persistence
  — the *single Memory* rule, C3; no `RelationshipStore`), V5 (Cycle — Relationship is re-projected each Recall,
  like Identity/Persona), V6 (Foundation Stability) — and flag V6's 0100C sub-question explicitly (already resolved
  operationally).
- G6. Fix the user's four questions: (1) Derived projection of an external relation? (2) who derives it? (3) who
  changes what? (4) how does it affect Goal/Drive without bypassing the chain?
- G7. Close the **three named bypass risks** explicitly: Relationship → Memory bypass (C3), Relationship → Goal
  mutation (C2), Relationship → Identity rewrite (C1).

---

## 3. Non-goals

- NG1. **No implementation.** No `relationship.py`, no relationship engine, no multi-agent relationship manager,
  no relationship scheduler. The contract only. M1.11 (Relationship Engine) is **explicitly deferred** until this
  RFC anchors (per the user's implementation order:
  `RFC-0111 Draft → Challenge → Anchor → M1.11 Relationship Engine → Smoke Test`).
- NG2. **No new flow.** The `Memory(relation) → Identity → Need(SOCIAL) → … → Goal` flow is RFC-0100A's / §3's
  (and already partially implemented in code); this RFC only consumes it.
- NG3. **No Memory-internals / no Memory schema change.** Relationship does **not** write Memory directly (C3);
  it reads `Memory.relations` as a *source* of the projection. How relation records are stored/recalled is
  RFC-0108; 0111 reads Memory as the source of the projection.
- NG4. **No new Authority / no new matrix row.** Relationship is derived *as a stateless view* over Memory +
  Identity. A `Relationship Authority` row in the matrix is explicitly **rejected** (C2 — the matrix has none by
  design; this RFC keeps it that way). The *relation records* remain owned by the Memory Authority (line 167) —
  no reassignment.
- NG5. **No 0100C text — and explicitly NO pre-activation.** If 0111 concludes 0100C is needed, it must do so only
  via an *observed contradiction*. The operational test (M1.8) has already run and found **none** — so this Draft
  proposes **none**. It anchors like its ten predecessors (pending user Challenge, §13.4) with 0100C **deferred
  (11th)**, and records the watchpoint as *satisfied*.
- NG6. **No second Memory / no relationship store.** `RelationshipStore` / `SocialMemory` / `ContactBook` /
  `RelationshipMemory` are all rejected by O1 (the single-Memory rule, RFC-0108 / RFC-0109 C3 / RFC-0110 C3). 0111
  *enforces* the one Memory; Relationship is a *projection*, not a sink.
- NG7. **No Relationship = motivation source conflation.** Relationship models an external relation; it is **not**
  a Goal/Drive writer (§5.2 C2, RFC-0100 §240). A `relationship.set_goal()` collapses the life into an optimizer
  (the *mirror* of RFC-0109's RL-collapse, now at the relational seam). 0111 *preserves* that separation (Q2) — it
  does not make Relationship a driver.
- NG8. **No Relationship = Identity rewriter.** Ontology §1.13 separates the *model* from the *self*; 0111
  *preserves* that separation (C1) — Relationship is derived from Identity (`Memory.relations ∩ Identity`); it does
  not rewrite Identity.

---

## 4. The Information Model of Relationship (P1 — state type, derived projection spanning the Life/Environment boundary)

Relationship is the life's **model of another agent** (RFC-0100 §1.13) — the *internal representation* of an
*external relation*. It is **not** the external agent (that is Environment), **not** a commitment (Goal), **not**
the operational structure (Plan), **not** a store (Memory — the `relations` records are the store; Relationship is
the *model over* them), and **not** a transform (Reflection). It is a **Derived State**: a *projection* of
`Memory.relations ∩ Identity`, keyed per external agent, re-derived each Recall, with **no lifecycle of its own,
no storage authority, no setter, and no second-Memory status**.

> **P1 (clarification — classifies an existing noun, does NOT add one).** *Relationship is a Derived projection:
> the model the life holds of another agent, computed from `Memory.relations` and `Identity`.* The test: a
> *Derived State* (RFC-0100B §3) is a thing the life *is* or *has* that **has no setter by construction** (Rule 2)
> — it is re-derived each cycle from an upstream state. Relationship fits exactly: `Relationship = model(
> Memory.relations, Identity, external_ref)`. It
> - **has no lifecycle as a state.** Relationship is *re-projected* on every Recall (like Identity, which is
>   `f(Memory)` recomputed each Recall, RFC-0100B §3.4; like Persona, RFC-0110 §4 P1). It does not transition
>   `pending/active/done` like Goal (RFC-0105). When a new relation record is ingested, Identity may shift, and
>   the Relationship model *re-projects* — it is never "archived" or "restored" as a Relationship.
> - **has no storage authority.** There is no `Relationship.save()` / `Relationship.restore()` /
>   `RelationshipStore` table (see C3). Relationship persists *only transitively* — because the *relation records*
>   persist in Memory (owned by Memory Authority, line 167); the *model* is a *view* over them, not a *writer* to a
>   store.
> - **has no Authority owning Relationship as a persistent state.** The *relation records* are owned by the
>   **Memory Authority** (matrix line 167). The *projection* (the model) is a **stateless view** — computed on
>   demand by reading `Memory.relations` + `Identity` read-only. It needs **no new authority** (unlike Persona,
>   which kept a carrier under the Identity Derivation Authority — Relationship's data is already owned by the
>   Memory Authority, so the projection is *even cleaner*: no carrier authority at all).
>
> Therefore Relationship is **classified inside the Derived split** (parallel to Need/Affect/Drive and to Persona):
> it is a **Derived projection of an external relation**, recomputed from Memory + Identity. This is the answer to
> the user's Q1: **Relationship is the model of an external relation, a Derived projection — not a second Memory,
> not a motivation source, not a second Identity system.** The *object* of the model is Environment (the other
> agent); the *representation* is a Life-layer Derived projection.

### 4.1 Structure of Relationship (the projection's anatomy, not a state schema)

Relationship is structured as a *projection*, not as a stored record. Its dimensions (all *derived from*
`Memory.relations` + `Identity`, none authored independently):

| Dimension | What it holds | Fed by | Notes |
|-----------|---------------|--------|-------|
| **Source Memory** | the `relations` records for one external agent (`Memory.relations`, filtered by `external_ref`) | Memory Authority (line 167 — the one sink) | Relationship reads `Memory.relations` read-only; it does **not** write Memory (V1, C3). |
| **Source Identity** | the Identity it is modeled against (the `f(Memory)` output, including `self_model["relations"]`) | Identity Derivation Authority (matrix line 162) | Relationship reads Identity read-only; it does **not** reverse-define Identity (V1, C1). |
| **external_ref** | the key of the modeled external agent (per-other-agent, RFC-0100 §1.13 / §412–413) | the interaction layer (read-only input to the projection) | The *object* is Environment; the key is supplied by the interaction context, never owned by Relationship. |
| **Output** | a **Relationship** (the held model: history, trust, role) | the projection itself | Is a *transient projection* — re-derived each Recall; NOT a stored state. |

> The table is a *projection anatomy*, not a new Ontology noun-set. The Ontology (RFC-0100 §1.13) names
> Relationship (as a model of another agent); this RFC only specifies how that model is *projected* from
> `Memory.relations` + `Identity`. A proposed "Relationship dimension" that is really a Memory record or an
> Identity facet is rejected by the Ontology's rules (§1.13 "model the life holds"; §240 "not a Provider action
> driver").

---

## 5. Derivation Contract (consumes RFC-0100 §1.13 + RFC-0100B §2/§3)

### 5.1 `Relationship ← stateless projection over Memory.relations + Identity`

The contract (semantic, not code):

- **Input (read-only):** the **`Memory.relations`** records for one `external_ref` (owned by Memory Authority,
  line 167) **and** the **Identity** (`f(Memory)` output, matrix line 162, including `self_model["relations"]`).
  Relationship reads both read-only; it does **not** read the live Memory object to *write* it (V1, C3).
- **Carrier (owns the data, not the projection):** the **Memory Authority** owns the *relation records* (the
  sink). The *projection* (the model) is a **stateless view** — computed on demand, owned by **no new
  authority**. This is the deliberate non-split at the `Memory → Relationship` seam: the *records* and the *one
  sink* stay with the Memory Authority; the *model* is a derived view, never a competing store.
- **Output**: a **Relationship** — a *Derived projection* (transient, re-derived each Recall, keyed per
  `external_ref`). The Relationship is **not** a stored state. It is read by the interaction/expression layer; it
  is never written back.
- **Purity (of the boundary)**: Relationship *reads* `Memory.relations` + Identity read-only and *emits* a
  projection; it does **not** mutate Identity, Need, Drive, or Goal (C1/C2), and it does **not** own a store (C3).
  The projection has *no side effects on the life's Constitution states* — its only downstream edge is
  *presentation/recall* (to the interaction/expression layer), never *mutation*.

> The *shape* of the model (history/trust/role extraction from relation records) is an **implementation** RFC's
> concern (M1.11). This RFC fixes only that Relationship **projects `Memory.relations` + Identity** (§4/§6), reads
> them read-only, and emits a Derived projection that is *modeled*, never *stored* or *mutated*.

### 5.2 The single legitimate path (carries forward RFC-0109 §5.2 / RFC-0110 §5.2, now naming the relational seam)

```
Memory Authority      ← owns ingestion + retention; the ONE sink (single-writer, RFC-0108)
   │  (ingest via Reflection — kind="relation")
   ▼
Memory.relations      ← the relation records (the data Relationship models)
   │  (at Recall)
   ▼
Identity = f(Memory)  ← re-derived; INGESTS relation records into self_model["relations"] (identity.py:40-41)
   │                   [Relationship reads Identity read-only; does NOT rewrite it]
   ▼
Need = g(Identity)    ← DERIVES SOCIAL wants from self_model["relations"] (need.py:42-47, already wired)
   │                   [Relationship does NOT set Goal / patch Drive directly]
   ▼
Affect → Drive → Goal ← the EXISTING chain; Relationship influences only via this upstream path
   ▲
   │  (Relationship is a Derived projection READ beside this chain — never a writer into it)
   │
Relationship          ← Derived projection of (Memory.relations ∩ Identity), keyed per external_ref
   │
   ▼  (modeling only — to the interaction/expression layer)
Held model            ← what the life "knows" of the other agent; never feeds back as a writer
```

Relationship is reached **only** by this path (as a read). The Memory Authority owns the *records*; Relationship
is the *modeled view*. The flow is strictly `Memory(relation) → Identity → Need(SOCIAL) → … → Goal`, **never**
`Relationship → Goal` (the optimizer trap) or `Relationship → Memory` (the second-sink trap).

> **Anti-bypass (V1/V2, derived-projection form — carries forward).** The chain is strictly
> `Memory.relations → [projection] → Relationship → (modeling)`. Three shapes are forbidden:
> (a) **Upstream-of-projection bypass (V1):** Relationship reading *around* Memory to a derived state — `relationship
> .read_memory(m)` then `relationship.rewrite_identity(...)` (the *mirror* of RFC-0110's `persona.rewrite_identity()`,
> now at the relational seam: a second Identity writer). Or `relationship.patch_need(...)` / `relationship.
> modify_drive(...)` (the god-object, parallel to RFC-0109 C1). The correct path is `Relationship = model(
> Memory.relations, Identity)`, `Identity = f(Memory)`: Identity/Need/Drive shift *upstream* (next cycle, via
> Memory), never written by Relationship.
> (b) **Downstream-of-projection bypass (V2):** Relationship *owning a store* instead of being a projection —
> `relationship.save_to_social_bank("RelationshipStore", ...)` / `relationship.persist_contact_book(...)` (a second
> Memory, O1 violation, RFC-0108 / RFC-0109 C3 / RFC-0110 C3). The correct path is `Relationship = model(
> Memory.relations, Identity)` — a re-derived view, not a stored record. A `relationship.become_memory()` that makes
> Relationship a sink is rejected (parallel to RFC-0106 E3 / RFC-0107 E3 / RFC-0108 E6 / RFC-0109 E6 / RFC-0110 E6).
> (c) **Motivation bypass (V2-variant, the optimizer trap):** Relationship *setting Goal / patching Drive directly*
> — `relationship.set_goal(...)` / `relationship.patch_drive(...)` (the *mirror* of RFC-0109's RL-collapse, now at the
> relational seam). The correct path is `Memory(relation) → Identity → Need(SOCIAL) → … → Goal`: the relationship
> *observation* enters via Memory; it does not *command* a Goal.

> **Phase C Clarification C1 (Identity Boundary — proposed at Challenge, F4, zero base edit — answers the user's
> "禁止 Relationship → Identity 直写").** *Relationship models identity's relation to others; it does not own
> identity.* Relationship projects `Memory.relations ∩ Identity` into a held model of an external agent; it does not
> actively modify the self. Relationship is a **Derived projection**, not a self-writer: the only legitimate edge
> from Identity is **into modeling** (the interaction/expression layer); Identity/Need/Drive/Goal are *re-derived*
> upstream (via Memory → `f` → next cycle), never *written by* Relationship. Explicitly banned (Authority bypass):
> `relationship.update_identity(facet, val)` (marking the self from the relational layer), `relationship.rewrite_identity(...)`
> (replacing the root), `relationship.patch_self_model("relations", ...)` (editing the derived state from the model).
> All collapse the single-Identity-root rule (O1, matrix line 162) into a two-root life — the god-object anti-pattern
> RFC-0100 §4 warned about, now at the *relational* seam, the *mirror* of RFC-0110's C1. The legal flow stays
> `Memory(relation) → Identity → Need(SOCIAL) → … → Goal`.

> **Phase C Clarification C2 (Authority / Motivation Boundary — proposed at Challenge, F4, zero base edit —
> answers the user's C2 + "Relationship → Goal mutation").** *Relationship owns no authority and writes no Goal /
> Need / Drive.* Relationship is a Derived projection with **no matrix row** (the matrix has none by design) and
> **no mutation permission** on any Constitution state. It influences Goal/Drive **only indirectly**, via the
> existing chain: a relationship *observation* is ingested into Memory (`MemoryAuthority.ingest(kind="relation")`)
> → Identity re-derives `self_model["relations"]` → Need derives SOCIAL wants (need.py:42-47) → Affect → Drive →
> Goal. Relationship the *model* never calls `set_goal` / `patch_drive` / `patch_need`. Explicitly banned:
> `relationship.set_goal(target, criterion)` (the optimizer trap, *mirror* of RFC-0109's RL-collapse), `relationship.
> patch_drive(orientation, val)`, `relationship.nudge_need(SOCIAL, ...)`. The legal influence path is the upstream
> Memory→Identity→Need→…→Goal chain, unchanged.

### 5.3 The matrix reading (Relationship is a projection, not a matrix row)

RFC-0100B §2 lists **no Relationship row** — by design. The carried-forward clarification:

- **Relationship is NOT in the matrix as an owned state.** The matrix's `Memory` row (line 167) owns the
  *relation records* (the sink); the matrix's `Identity` row (line 162) owns `f(Memory)` (which *ingests* those
  records). Relationship is a *projection* over both — so by the matrix's own discipline, Relationship is **not an
  independently-owned state**; it is a *derived view*. Adding a `Relationship` row would imply Relationship is an
  independently-owned state — which would make it a second Memory (O1 violation, since it would need a per-other-agent
  store). So the matrix stays as-is: **no Relationship row.**
- **Relationship is `Read by: interaction/expression layer`** — *read as the held model*, not a stored state. The
  projected Relationship is what the interaction layer *knows* of an external agent; no "Relationship state" is
  queried as a governance unit.
- **Relationship is NOT in any derived state's `Mutated by` as a writer.** Relationship feeds modeling *only via
  presentation/recall*, never by mutation (V2). It is a **Derived projection**, not a root.

This is **parallel to how Need/Affect/Drive are Derived** (RFC-0100B §2 lines 162–165) and to **Persona**
(RFC-0110 §5.3) — except Relationship is the *relational* projection (of `Memory.relations ∩ Identity`), spanning
the Life/Environment boundary, whereas Need/Affect/Drive are *cognitive* projections and Persona is the *expression*
projection. Relationship sits *beside* the cognitive chain as a model; it is not *in* the cognitive derivation
stack (`Identity → Need → Affect → Drive → Planning`).

### 5.4 Persistence & Cycle — the derived-projection form (C3 / V4 / V5)

`PROTOCOL-Life-Validation.md` V4 (anti-persistence) and V5 (cycle) were written for **Derived / Transient** states.
Relationship is **Derived** — it fits cleanly (the *third* derived-state adaptation after Need/Affect/Drive and
Persona, and parallel to how Reflection/Execution adapted V4/V5 to a *transform* / *event* — RFC-0109 §5.4 /
RFC-0107 §5.4):

- **V4 (Persistence) — derived-projection form, the *single Memory* rule.** There is **exactly one Memory store**,
  owned by **one Memory Authority** (line 167). No `RelationshipStore` / `SocialMemory` / `ContactBook` /
  `RelationshipMemory`. Each would be a **second writer**, duplicating O1 (the precise risk RFC-0108 closed at the
  sink, now tested at the *relational* store). Relationship *models* `Memory.relations`; it does not *store*
  anything. If a relationship's *content* must persist, it persists **transitively** — because the relation records
  persist in Memory, and the model re-projects on Recall. A `RelationshipMemory` that is later *read as state* would
  be a second Memory — a duplicate writer violating O1.
- **V5 (Cycle) — derived-projection form.** Relationship exists **only for the duration of a modeling**; it is
  *re-projected* each Recall (like Identity/Persona, which re-derive from Memory). It is not *restored* from a
  Relationship store; cross-cycle continuity of *relational knowledge* is *inherited* from Memory (via the relation
  records), never stored in a Relationship.

> **C3 (clarification — proposed at Challenge, F4, zero base edit — answers the user's Q3 + the central
> "Relationship → Memory bypass" risk).** *Memory stores relation experiences. Relationship derives from Memory
> (via Identity).* Relationship owns no store. Forbidden as a second memory: `RelationshipStore` / `SocialMemory` /
> `ContactBook` / `RelationshipMemory` / `RelationshipDatabase`. Each would be a **second Memory** (or a parallel
> sink) — a duplicate writer violating O1 (the precise risk the user named at Q3/C3, now tested at the *relational*
> layer, not merely the sink). The Relationship is a **Derived projection**: it flows `Memory.relations →
> Relationship → (modeling)`, never to permanent storage as a second root. The relational content persists
> *transitively* via Memory (the relation records are the sink; `Identity = f(Memory)` re-ingests them);
> Relationship re-projects on Recall. This is the user's **central risk for RFC-0111** (Relationship → Memory
> bypass), and the RFC's primary job is to *close* it: exactly one Memory, exactly one Identity root; Relationship
> is the *projection*, not a second sink or a second root.

---

## 6. Ownership (consumes RFC-0100B) — Memory Authority owns the records; Relationship projection owns nothing

### 6.1 Relationship is a stateless projection over Memory + Identity

Per RFC-0100B §2 (line 167), **Memory** (including `relations`) is owned by the **Memory Authority** — the *one*
sink. Relationship the *model* is a *projection* of that Memory (plus Identity) — so it is derived **as a
stateless view**, reading Memory + Identity read-only. **No new authority is created** (C2).

- **`Memory Authority`** (line 167) — owns ingestion + retention of relation records (the *data* Relationship
  models). It is the **single** writer of relation records (via `MemoryAuthority.ingest`, shared with Reflection).
  It does **not** own a Relationship projection (projections are views, not governance units).
- This is the **deliberate non-split** at the `Memory → Relationship` seam: the *records* and the *one sink* stay
  with the Memory Authority; the *model* is a derived view, so Relationship can never become a competing sink.
  Splitting them into `Memory Authority` + `Relationship Authority` would tempt the Relationship Authority to *write*
  a second store (the god-object at the relational seam) — exactly the O1 violation this RFC forbids.

### 6.2 The naming discipline + authority boundary (symmetric format, V3 / C2 — the 11th instance, derived-projection form)

The `Memory.relations → Relationship` path keeps the projection as a stateless view. Relationship may *model* an
external agent, but:

- **`Memory Authority`** — owns `Memory.relations` (ingestion + retention); reads `Memory.relations` + Identity
  (read-only) to *project* a Relationship; **does NOT** create a second Memory, **does NOT** mutate Need/Drive/Goal
  directly (those come from Identity via `g/h/i`, RFC-0102..0104).
- **Relationship** has **no authority of its own** — it is a Derived projection (a stateless view), not a
  governance unit. (Cleaner than Persona: Persona kept a carrier under the Identity Derivation Authority;
  Relationship needs *no* carrier because its data is already owned by the Memory Authority.)

> **Boundary statement (clarification, proposed at Challenge, F4, zero base edit — symmetric with RFC-0103 §6.2 /
> RFC-0104 §6.2 B / RFC-0105 §6.2 / RFC-0106 §6.2 / RFC-0107 §6.2 / RFC-0108 §6.2 / RFC-0109 §6.2 / RFC-0110 §6.2,
> directly answers the user's Q2).** *Memory Authority governs relation-record ingestion + retention, not a
> Relationship Authority, not relationship-driven Goal mutation.* The single-Memory rule (O1, line 167) holds:
> there is exactly one Memory sink; Relationship is a *projection* over it, not a competing governance unit.
> | Authority | Owns | Does not own |
> |-|-|-
> | Memory Authority | `Memory.relations` (ingestion + retention) · the relation *records* | a Relationship Authority, relationship-driven goal mutation, identity rewrite |
> | Relationship (projection) | nothing (stateless view) | any store, any mutation of Identity/Need/Drive/Goal |
> Relationship is a Derived projection under the Memory Authority's *data*; it **must never** mutate Identity (the
> single root), **must never** own a store, and **must never** write Goal. Code of the shape
> `relationship_model = model(memory.relations, identity, external_ref)` (read-only on Memory + Identity, emit
> projection) is correct; `relationship_authority.save_store(...)` / `relationship.set_goal(...)` /
> `relationship.rewrite_identity(...)` is a **mis-assignment** (collapses authorities, violates O1).
>
> **Added at Challenge (C2 — F4, zero base edit).** *Relationship projection is governed as a stateless view over
> the Memory Authority's data; no Relationship Authority is introduced.* The firewall between Memory (sink) and
> Relationship (model) is explicit:
>
>     Memory Authority            — owns ingestion + retention of relation records (the ONE sink)
>         │  (at Recall)
>         ▼
>     Identity = f(Memory)       — re-derived; ingests relation records (identity.py:40-41)
>         │  (read-only)
>         ▼
>     Relationship (projection)  — Derived model of an external agent, keyed per external_ref
>         │  (modeling only)
>         ▼
>     Held model                ← what the life knows of the other agent; never feeds back as a writer
>
> Memory Authority **owns**: relation-record ingestion + retention. Memory Authority **does NOT own**: a
> Relationship Authority · relationship-driven goal mutation. Relationship **owns**: nothing (it is a projection).
> No `Relationship Authority` is added to the matrix.

This is the **eleventh** instance of the symmetric Authority-boundary format, and the **first
derived-projection-that-spans-the-Life/Environment-boundary** instance (Need/Affect/Drive are derived cognitive
projections; Persona is the derived *expression* projection; Relationship is the derived *relational* projection).
It extends the chain across the relational seam:

```
Identity Derivation Authority — governs Identity derivation + Persona projection, not Memory mutation, not a second Identity root
Need Authority                 — governs Need derivation,            not Identity mutation
Affect Authority              — governs Affect derivation,          not Need mutation, not Goal adoption
Drive Authority               — governs Drive derivation,           not Affect mutation, not Goal adoption
Planning Authority            — governs plan construction,           not Goal mutation, not Drive mutation
Execution Runtime             — governs plan consumption + result,   not Goal/Drive/Plan mutation  (infra, not in stack)
Memory Authority              — governs memory ingestion + retention, not identity mutation, not need mutation, not reflection decision
Reflection Authority          — governs reflection transform,         not identity mutation, not need mutation, not goal mutation, not memory storage
Relationship (projection)     — is a stateless view of Memory.relations + Identity, not a store, not a Goal writer, not an Identity rewriter
```

> **Phase C Clarification C2 (Authority Boundary — F4, zero base edit).** The symmetric wording is now extended
> across the **relational projection** — the first time the boundary is stated for Relationship *as a Derived
> projection spanning the Life/Environment boundary* (Relationship has no authority of its own; it is a stateless
> view over the Memory Authority's data). The Constitution stack `Identity → Need → Affect → Drive → Planning` is a
> *stack of single-writers*; Relationship sits *beside* it as a model, derived as a stateless view over Memory +
> Identity — a deliberate non-split that prevents Relationship from becoming a second Memory or a Goal driver.
> Each authority (including the Memory Authority, by contract) governs only its own product; none mutates a derived
> state directly, and none writes Goal.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Relationship is **self-modeled**: the life's own Memory (its own relation records) + its own Identity model an
external agent — *no external caller assigns what the life "knows" of the other*. The life *owns its relational
model* — from how it has related (Memory) and who it is (Identity). This is the definitional essence of "Life, not
Agent" (RFC-0100 §1.2) at the relational layer: an agent's relationship is *assigned by the trainer* (a CRM
row / a hardcoded contact); a life's Relationship *models its own Memory* — never a second sink, never an assigned
root.

### 7.2 Growth (G) — score: **high**

Because the flow is `Memory(relation) → Identity → Need(SOCIAL) → … → Goal`, the life's *relating* genuinely feeds
its *motivation*: relate → Memory → shifted Identity → shifted SOCIAL Need → shifted Drive → shifted Goal (next
Recall). RFC-0111 makes the **relational model** explicit (a Derived projection over `Memory.relations`) and guards
it (exactly one Memory sink; no second store; no direct mutation of derived states or Goal). This is the North
Star's "grows a little more every day" made operational as a *closed loop* — and, critically, as a loop where the
*self* (Identity) drives the *relational model* (Relationship), and the *relational observation* feeds back *only*
via Memory, never as a direct driver.

### 7.3 Maintainability (M) — score: **gate (must not regress) — and the 0100C watchpoint is now SATISFIED**

Single-sink / single-projection boundary ⇒ no hidden second Memory owned by a Relationship Authority; Relationship
is a stable projection independent of store internals ⇒ refactors don't ripple into the Constitution. M is treated
as a **non-regression gate**. RFC-0111 is the **eleventh** 0100C test — and the operational watchpoint is
**SATISFIED**: M1.8 (Memory Ingestion Interface) has *already been built* (`da30aa5`, 25/25 smoke; M1.9 added
`reflect()`, 30/30; M1.10 added `derive_persona`, 35/35) and passed with **no observed contradiction** — the
ingest is a pure read→write into the one store, the ReflectionResult is *consumed* (not mutated), no second Memory
is created. Relationship writes **nothing directly** (its only write path is the shared `MemoryAuthority.ingest`,
already validated at M1.8), so it surfaces **no new joint** for 0100C to test. Per F1–F4, this Draft may **not**
pre-activate 0100C on speculation. This RFC changes **no** M/0100C surface; it records the deferral as *complete at
the operational level*.

---

## 8. This is NOT Relationship (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a second Memory sink.** Relationship is the *model* of `Memory.relations`; the *records* are the sink
  (owned by Memory Authority, line 167). A `RelationshipStore` / `SocialMemory` / `ContactBook` beside the one
  Memory is a second owned writer — breaks O1 (§4 P1, C3). There is **exactly one Memory**.
- ✗ **NOT an Owned State.** Relationship is a Derived projection (re-projected each Recall, like Identity/Persona);
  it has no lifecycle, no setter, no store (§4 P1). A `Relationship = r(Identity, ext)` that is *stored* and
  *lifecycled* like Goal would mis-classify a projection as a sink and break the `f` derivation + the single-sink
  rule (§4 P1).
- ✗ **NOT Memory.** Memory is the *store* (owned by Memory Authority, line 167); Relationship is the *model* of a
  slice of it (`relations`, keyed by `external_ref`). Relationship is not Memory; Memory is not Relationship.
- ✗ **NOT an Authority that mutates Identity/Need/Drive/Goal.** `relationship.update_identity()`,
  `relationship.rewrite_identity()`, `relationship.patch_need()` are mis-assignments (collapse the single-Identity-root
  rule, violate O1, §5.2 C1). `relationship.set_goal()` / `relationship.patch_drive()` are mis-assignments (the
  optimizer trap, §5.2 C2). Relationship models; it never writes the self or a Goal.
- ✗ **NOT a Goal writer / motivation source.** Relationship models an external relation; it never writes Goal
  (parallel to RFC-0109 §8 / RFC-0110 §8). A `relationship.set_goal()` collapses the life into an optimizer (the
  *mirror* of RFC-0109's RL-collapse, now at the relational seam). **This is one of the three central guards** the
  user named — the one that distinguishes a life (relational knowledge feeds motivation *upstream via Memory*) from
  an optimizer (relational knowledge *commands* a Goal).
- ✗ **NOT a Provider-readable action driver.** Per RFC-0100 §240, a Provider that reads the Relationship model to
  decide how to send a message is a *violation*. Relationship is Life-internal knowledge; the *behavior* it enables
  must still flow through the Constitution chain (`Memory → Identity → Need → … → Goal`), never through a Provider
  short-circuit that reads the model directly.
- ✗ **NOT a Constitution Authority's state to be re-derived by a new engine.** Relationship is a Derived projection
  over Memory + Identity; the *only* states re-derived each cycle are the Derived ones (Identity/Need/Affect/Drive).
  Relationship projects; it does not persist.

---

## 9. RFC-0100C deferral — the eleventh confirmation (operational watchpoint now SATISFIED)

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) pending a real Life-layer RFC that needs
transaction, rollback, window, or lock semantics. RFC-0101 located the *only* direct write at Memory and deferred
0100C; RFC-0102..0106 validated "no" five more times; RFC-0107 (Execution) was the **first live** watchpoint, bound
to M1.7, which passed; RFC-0108 (Memory) was the eighth test, bound to M1.8; RFC-0109 (Reflection) was the ninth
test, **operationally satisfied** (M1.8 already built and passed); RFC-0110 (Persona) was the tenth test,
**operationally satisfied** (M1.10 a pure read-side projection). Relationship is the *eleventh* test — and it
surfaces **no new joint**: Relationship writes **nothing directly** (its only write path is the shared
`MemoryAuthority.ingest`, validated at M1.8), so there is no transaction/atomicity question at the Relationship
layer. The only 0100C-relevant joint (single-writer consistency at Memory ingestion) was **already performed at
M1.8** and found **no observed contradiction**.

Relationship is a *read-side projection* of the *same* Memory that M1.8 writes into — it introduces no new write
path. So RFC-0111's relationship to 0100C is:

- The real implementation test (single-writer consistency at ingestion) was **already performed at M1.8** and found
  **no observed contradiction** (F2 not triggered).
- RFC-0111 itself surfaces **no new** transaction/permission need — Relationship is a pure projection (read
  `Memory.relations` + Identity → emit Relationship), and it writes **nothing** (no Memory write, no Identity write).
- Therefore 0100C stays deferred an **eleventh** time — and the *operational* phase of the deferral is already
  **complete** (the watchpoint was not merely "bound"; it was *executed* and passed at M1.8, carried through M1.9
  and M1.10).

**Honest position at the document/validation level:**

> **RFC-0111 confirms: RFC-0100C stays deferred (11th consecutive confirmation) — and the operational watchpoint is
> SATISFIED.** At the *validation/document* level, the Foundation *already names* everything Relationship needs:
> Relationship as a model of another agent (§1.13), the *single* Memory Authority (O1, line 167) owning the
> relation records, and Identity = `f(Memory)` which *ingests* those records (identity.py:40-41). No new noun, no
> new state-class, no new Constitution Authority is required to *explain* Relationship as a Derived projection.
> The real 0100C test (M1.8 Memory Ingestion Interface) has *already been built and passed with no observed
> contradiction* — so the deferral is now operational-complete. **No F1 trigger exists.** The Gate verdict is
> **Foundation Stable → YES** (anticipated; pending user Challenge, §13.4).

> **Phase C Clarification C4 (Transaction / Conflict Watchpoint — proposed at Challenge, F4, zero base edit).**
> *Transaction semantics belong to Memory Ingestion implementation validation, not Ontology expansion — and that
> validation has already been performed at M1.8 with no observed contradiction.* **Record: 0100C — Deferred (11th
> confirmation); Observed contradiction: NONE.** M1.8 already verified: single writer is expressible; the
> Reflection → Memory ingestion path holds; no transaction conflict; no rollback requirement. **Relationship
> introduces no new joint** (it is a read-side projection of Memory, writing nothing directly) — so it cannot
> surface a transaction contradiction the M1.8 test did not already cover. Relationship *may* surface
> **implementation-observation** issues at M1.11 (long-term relational-model stability, version migration of the
> projection rule, per-external-ref model consistency) — but per F2 those are **implementation observations**, not a
> current Foundation patch. They would trigger 0100C only if M1.11 *observes* a contradiction the current theory
> cannot express (e.g. a need for the projection rule to be transactionally consistent with Memory re-ingestion) —
> not this Draft's speculation. 0100C stays deferred an eleventh time, operationally satisfied. No silent expansion
> in code or Ontology.

This RFC does **not** propose 0100C activation. It stands at the Challenge Gate (pending user verdict, §13.4) with
0100C **deferred (11th, operational watchpoint satisfied)**.

---

## 10. Examples

**E1 — Projection from Memory + Identity (no external relationship store).**
A relation record is ingested (`MemoryAuthority.ingest(kind="relation", content={external_ref, trust, role})`). At
the next Recall, `Identity = f(Memory)` ingests it into `self_model["relations"]` (identity.py:40-41). The
Relationship projection reads `Memory.relations[external_ref]` + Identity read-only and emits a **Relationship**
(the held model). No caller assigned the model; it was driven by the life's own Memory + Identity. Relationship
reads only Memory + Identity (V1); it does not reach the live Memory to write it (C3).

**E2 — Projection lifetime (not a state).**
Relationship is projected for one external agent (cycle 1). It is *modeled* and *re-projected* next Recall if new
relation records shifted Identity; it is **gone** as a stored thing — not archived, not restored. Contrast the Goal
lifecycle (RFC-0105 E2): Relationship has *no* such lifecycle; it *projects* and is re-derived. The relational
knowledge persists *transitively* (via `Memory.relations`); the projection itself does not.

**E3 — Anti-bypass (V1, the second-self-writer guard).**
A "smart relationship" proposes `relationship.rewrite_identity("trust_in_X", 0.9)` after observing a relation
record suggesting the self should trust X. **Rejected** (§5.2 C1 / §8): Relationship models an external relation,
*never* writes the self. The correct shape is `Memory(relation) → Identity = f(Memory) → (shifted) Relationship`.
A `relationship.rewrite_identity()` makes Relationship a second Identity root — the god-object at the relational seam
(the *mirror* of RFC-0110's `persona.rewrite_identity()`).

**E4 — Authority boundary (C2 — the user's named risk at the relational seam).**
A component proposes a `Relationship Authority` that *both* models the relation *and* owns a `RelationshipStore` (or
writes Goal). **Rejected** (§6.1): the matrix (line 167) assigns the relation *records* to the **Memory Authority**;
there is *no Relationship row*. Adding one blurs O1 (a second sink). Correct shape: Memory Authority owns the
records; Relationship is a stateless view; Relationship never writes Goal or a store.

**E5 — Anti-persistence / single Memory (C3 / V4 — the central Relationship → Memory bypass guard).**
Relationship succeeds and calls `relationship.save_to_social_bank("RelationshipStore", ...)` to keep a separate
relational store. **Rejected** (§5.4): there is **exactly one Memory**. A `RelationshipStore` would be a second
writer (O1 violation). The relational content persists *transitively* (relation records in Memory; Relationship
re-projects on Recall); the relationship pattern is never a separate store.

**E6 — No Goal mutator (C2 / V2-variant — the optimizer trap).**
Relationship reads a close bond and calls `relationship.set_goal("stay close to X", ...)` to command a Goal.
**Rejected** (§5.2 C2): Relationship models; it never writes Goal. The correct shape is `Memory(relation) →
Identity → Need(SOCIAL, "nurture: X") → Affect → Drive → Goal` (need.py:42-47 already derives the SOCIAL want). The
relationship *observation* enters via Memory; it does not *command* a Goal. This is the `Relationship → Goal
mutation` bypass the user named — forbidden (the *mirror* of RFC-0109 E6's RL-collapse, now at the relational seam).

---

## 11. Limits & Open Questions

- **Q1 — Relationship model shape.** *What* Relationship projects from `Memory.relations` + Identity (history /
  trust / role extraction, per-`external_ref` indexing) is M1.11 (Relationship Engine)'s job; 0111 only fixes that
  Relationship is a Derived projection of `Memory.relations ∩ Identity` and owns the *model*, never a store or a
  root. *Not a constitution question.*
- **Q2 — Per-identity vs cross-group.** RFC-0100 §412–413 keeps Relationship *per-other-agent*; the real code's
  `crossgroup` is already flagged as Relationship (§318). 0111 aligns: Relationship is keyed per `external_ref`
  (the other agent), derived from `Memory.relations` filtered by that ref. *Not a constitution question* — the
  ontology already resolved it (per-other-agent).
- **Q3 — Record conflict / relational-model consistency.** *How* a Relationship projection stays consistent with
  Memory across Recalls is owned by the *same* Memory Authority under the *same* single-writer rule (matrix line 167)
  — not a new authority. The 0100C joint (single-writer at ingestion) was **already tested at M1.8 with no observed
  contradiction** (C4).
- **Q4 — Relationship as model, not state.** Relationship is the *held model*; it is realized as a projection, never
  a stored `Relationship` state. 0111 enforces this (§4 P1, C3); the mechanics of *how* a projection is computed are
  M1.11/infra. *Not a constitution question.*
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class hypothesis.
  0111 surfaces **no new constitutional question** and does **not** consume or promote it.
- **Q6 — V1/V2/V4/V5 derived-projection-form adaptation (relational span).** RFC-0110 was the **first** Life RFC to
  apply V1/V2/V5 to a **Derived projection of Identity**; RFC-0111 is the **second** and the **first to span the
  Life/Environment boundary** (the model's object is an external agent). The adaptation (projection reads
  `Memory.relations` + Identity → emits derived model; no self-store; feeds only via modeling) is recorded here; if
  it proves a stable pattern for *boundary-spanning* RFCs, it may warrant a one-line precision in
  `PROTOCOL-Life-Validation.md` — flagged as a future F4, **not** done inline (keeps this RFC a pure validation).
- **Q7 — 0100C is now operationally SATISFIED.** RFC-0107 made it operational (bound to M1.7, passed); RFC-0108/0109
  made it operational a second/third time (M1.8). M1.8 has *run and passed* — so by RFC-0111 the operational
  deferral is **complete** (the real build step executed, no contradiction). This is a *discipline* milestone, not a
  Foundation edit — recorded here so the next agent knows the 0100C question has been *tested at the actual joint*
  and found clean.
- **Q8 — Relationship is Derived, not a transform, not a sink.** Unlike Reflection (an Act/Process inside the Mind,
  RFC-0109), Relationship is a **Derived projection** (a model of an external relation). This is why it gets *no
  matrix row* (it is derived as a stateless view over Memory + Identity, not an independent governance unit) whereas
  Reflection *does* get a row (line 168, as a Mind-layer transform with its own authority). 0111 preserves this
  distinction; it does not promote Relationship to an owned state or a new authority.

---

## 12. Evolution / next RFC

RFC-0111 is **consumable** by the next Life-layer RFC and unlocks M1.11:

- **M1.11 Relationship Engine** (DEFERRED until this RFC anchors) — the first code that *models* an external agent
  from `Memory.relations` + Identity. Per the user's implementation order, M1.11 starts **only after** RFC-0111
  anchors; otherwise it risks reopening RFC-0100C by building Relationship as an Owned State / second Memory (second
  sink). M1.11 implements `derive_relationship(memory, identity, external_ref) -> Relationship` as the *projection*
  act, read-only on `Memory.relations` + Identity, emitting a Derived Relationship (transient, re-derived each
  Recall), and presenting it — **without** mutating Identity/Need/Goal/Drive and **without** owning a store. It ships
  a smoke test that *is* the validation. **M1.11 is NOT where 0100C is tested** (that joint — ingest — was already
  validated at M1.8, which passed); M1.11 only *models* Memory + Identity, writing nothing. M1.11 must **not** rewrite
  `state.py`'s Identity or `memory_authority.py`; it *adds* the projection (parallel to how M1.6 added `derive_plan`,
  M1.7 added `execute_plan`, M1.8 added `ingest`, M1.9 added `reflect`, M1.10 added `derive_persona`, without
  rewriting upstream engines).
- **RFC-0100C (NOT activated).** Still deferred at the document level; the operational test (M1.8) has *already run
  and passed* with no observed contradiction. Activation would now require a *new* observed contradiction at a
  *future* build step (e.g. M1.11 surfacing a need for transactional consistency between the projection rule and
  Memory re-ingestion) — not this Draft's speculation, and not the already-resolved M1.8 joint.

### 12.1 Phase-2 rhythm (the Gate — OPEN, awaits Challenge verdict)

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy via F1; else Anchor) → next RFC.**

RFC-0111 has passed **Draft** (this document). It awaits the user's LAWP Phase C Challenge. The Gate question
*"Foundation Stable?"* is **anticipated to return YES** — all checks are expected to PASS with zero base edits,
Relationship classified as a Derived projection of `Memory.relations ∩ Identity` (not a second Memory, not a
motivation source, not a second Identity system), the Memory Authority → Relationship projection boundary preserved
(no new authority, matrix unchanged), and 0100C deferred an eleventh time — **operationally satisfied** (M1.8 already
built and passed, no observed contradiction). RFC-0111 will **anchor** and unlock M1.11. The Foundation (frozen at
RFC-0105 §14) stays frozen — 0111 is a *validation*, not a change.

---

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0111 proposes to prove about the 0100 trilogy, and the
verdict that decides whether the project proceeds to M1.11. Eleventh Life RFC to cite `PROTOCOL-Life-Validation.md`
(V1–V6), and the **first to span the Life/Environment boundary** (the model's object is an external agent) — and the
eleventh to record the 0100C operational watchpoint as **satisfied** (M1.8 already built and passed).

> **✅ STATUS: ANCHORED — Phase C verdict: APPROVED WITH CLARIFICATIONS.** The checks below are *confirmed*; the
> verdict (§13.3 / §13.4) is **finalized**. This RFC is **anchored** (commit `faa8cf3`); M1.11 Relationship
> Engine is **unlocked**. Zero Ontology base edit; no new Authority; no new Memory type. The user's LAWP Phase C
> Challenge verdict (APPROVED WITH CLARIFICATIONS, adopting C1–C4) was applied.

### 13.1 What was checked (proposed)

| Foundation doc | Question 0111 asked | Proposed result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Relationship needs as *nouns*? | ✅ Yes (anticipated). Relationship named as a *model of another agent* (§1.13); flow `Memory(relation) → Identity → Need(SOCIAL) → … → Goal` implied (§1.12 / §3). **No new life-noun.** Relationship classified as a Derived projection of `Memory.relations ∩ Identity` (§4 P1). |
| **RFC-0100A** (Dynamics) | Is the loop sufficient to *model* Relationship? | ✅ Yes (anticipated). The outer loop + §3 specify the `Memory(relation) → Identity → Need(SOCIAL)` edge; partly already implemented (identity.py:40-41, need.py:42-47). No new flow vocabulary. |
| **RFC-0100B** (Ownership) | Does the matrix + O1–O4 cover the relational boundary? | ✅ Yes (anticipated), with one *reading* (not a change): Relationship is a **Derived projection** over `Memory.relations` + `Identity` — *not* a matrix row (no Relationship row by design). The single-Memory rule (O1, line 167) holds; the relation records are the sink; Relationship is the model, not a second sink. |
| **RFC-0100C** (deferred) | Does Relationship need Permissions/Transactions *at the document level*? | ⚠️ **Eleventh "no" — and operationally SATISFIED (anticipated).** Relationship is a pure projection (read `Memory.relations` + Identity → emit Relationship); it writes **nothing directly**. The only 0100C-relevant joint (single-writer at ingestion) was **already validated at M1.8** (built `ingest`, passed 35/35 with **no observed contradiction**). **No observed contradiction** → 0100C stays deferred. The *real* test (M1.8) has *run and passed* per F1–F4 (F2: observed contradiction required — none found). |

### 13.2 Holes found (proposed)

- **None requiring a Foundation change at the document level.** Relationship's classification as a Derived
  projection is a *reading* of RFC-0100 §1.13 ("model the life holds of another agent") + matrix line 167 (Memory
  owns the relation records — *no Relationship row*) — not a new noun or a base edit. The Memory Authority →
  Relationship projection boundary is a *reading* of line 167, not a new Authority. No matrix row is added
  (Relationship stays a projection, not a governance unit).
- **Carried-forward clarifications (design-intent discipline, V1–V6 of `PROTOCOL-Life-Validation.md`):**
  - **V1 (anti-bypass, derived-projection form):** projection reads only `Memory.relations` + Identity (immediate
    upstream); no `relationship.rewrite_identity()` / `relationship.patch_need()` (§5.2 C1, the second-self-writer /
    god-object guards).
  - **V2 (downstream bypass, derived-projection form):** Relationship writes nothing directly; it is a projection,
    not a sink; no `RelationshipStore` / `SocialMemory` (§5.4 C3). **V2-variant (motivation bypass):** no
    `relationship.set_goal()` / `relationship.patch_drive()` (§5.2 C2, the optimizer trap).
  - **V3 (Authority Boundary, symmetric format, derived-projection form):** *Memory Authority governs
    relation-record ingestion + retention, not a Relationship Authority, not relationship-driven Goal mutation*
    (§6.2, C2). Eleventh symmetric instance; first *Derived-projection-spanning-Life/Environment-boundary* form. No
    Relationship Authority added.
  - **V4 (Persistence, single Memory):** exactly one Memory store, one Memory Authority; `RelationshipStore` /
    `SocialMemory` / `ContactBook` / `RelationshipMemory` forbidden (§5.4, C3).
  - **V5 (Cycle, derived-projection form):** Relationship is re-projected each Recall (like Identity/Persona); not
    stored, not restored (§5.4).
  - **P1 (state-type classification):** Relationship = Derived projection of `Memory.relations ∩ Identity`, **not** a
    second sink / not an Owned State (§4) — answers user Q1, prevents the mis-classification risk (second Memory →
    0100C).

### 13.3 Gate verdict (FINALIZED — **APPROVED WITH CLARIFICATIONS**)

> **Foundation Stable? → YES (Gate A holds; 0100C operationally satisfied).**
> *Verdict finalized by LAWP Phase C Challenge (§13.4): APPROVED WITH CLARIFICATIONS, adopting C1–C4, all F4,
> zero base edit.* RFC-0111 is consumable by M1.11 Relationship Engine; no Foundation patch at the document level.
> Relationship is classified as a Derived projection of `Memory.relations ∩ Identity` (not a second Memory, not a
> motivation source, not a second Identity system), the Memory Authority → Relationship projection boundary is
> preserved (no new authority, matrix unchanged), and 0100C stays deferred (11th consecutive confirmation) —
> **operationally satisfied** (the M1.8 ingest test already ran and found no observed contradiction). The
> Foundation (frozen at RFC-0105 §14) remains frozen *at the document level*.

---

### 13.4 Challenge (LAWP Phase C — Life Validation Protocol V1–V6) — **VERDICT: APPROVED WITH CLARIFICATIONS**

Ran `PROTOCOL-Life-Validation.md` V1–V6 (eleventh Life RFC to do so; first to apply to a **Derived projection
spanning the Life/Environment boundary** and the eleventh to record the 0100C operational watchpoint as
**satisfied**). **Verdict: APPROVED WITH CLARIFICATIONS — C1–C4 adopted, zero base edit.**

Proposed checks (to be confirmed by the user's verdict):

| Life Validation check | Proposed result | Note |
|-----------------------|----------------------|------|
| **V1 Upstream bypass (derived-projection form)** | ✅ PASS (expected) | Relationship reads only `Memory.relations` + Identity (immediate upstream of projection); no `relationship.rewrite_identity()` / `relationship.patch_need()` (§5.2, C1). |
| **V2 Downstream bypass (derived-projection form)** | ✅ PASS (expected) | Relationship writes nothing directly; it is a projection, not a sink; no `RelationshipStore` / `SocialMemory` (§5.4, C3). **V2-variant (motivation):** no `relationship.set_goal()` / `relationship.patch_drive()` (§5.2 C2). |
| **V3 Authority Boundary** | ✅ PASS (expected) | *Memory Authority governs relation-record ingestion + retention, not a Relationship Authority, not relationship-driven Goal mutation* (§6.2, C2). Eleventh symmetric instance; first *Derived-projection-spanning-Life/Environment-boundary* form. No Relationship Authority added. |
| **V4 Persistence (single Memory)** | ✅ PASS (expected) | Exactly one Memory store, one Memory Authority; `RelationshipStore`/`SocialMemory`/`ContactBook`/`RelationshipMemory` forbidden (§5.4, C3). |
| **V5 Cycle (derived-projection form)** | ✅ PASS (expected) | Relationship is re-projected each Recall (like Identity/Persona); not stored, not restored (§5.4). |
| **V6 Foundation Stability** | ✅ PASS (expected) | No base edit → Stable. Relationship is an existing noun (§1.13), classified as a Derived projection (a reading). **0100C: 11th deferral — operationally SATISFIED (M1.8 ingested a ReflectionResult, 35/35, no observed contradiction; Relationship writes nothing directly, no new joint)**; C4 records the resolved watchpoint. |

**Proposed clarifications for the user's Challenge (all F4, zero base edits):** P1 (Relationship = Derived projection
of `Memory.relations ∩ Identity`, not a second Memory, not a motivation source, not a second Identity system); C1
(Identity Boundary: `relationship.update_identity()` / `relationship.rewrite_identity()` / `relationship.patch_self_model()`
forbidden — Relationship models only, Identity/Need/Drive re-derive upstream; the second-self-writer guard at the
relational seam); C2 (Authority / Motivation Boundary symmetric format, derived-projection form; Memory Authority →
relation-record ingestion, **no Relationship Authority** added, matrix unchanged; Relationship writes no Goal/Need/
Drive); C3 (Persistence Boundary: exactly one Memory; second-Memory / relationship-store names forbidden — no O1
violation; the central Relationship → Memory bypass guard); C4 (transaction/conflict watchpoint: **already tested at
M1.8 with no observed contradiction** → 0100C 11th deferral is operationally satisfied, not merely bound;
Relationship surfaces no new joint as a read-side projection).

> **Challenge verdict → Foundation Stable: YES (anticipated; pending user).**
> All six Life Validation checks are expected to resolve without a Foundation edit at the document level.
> Relationship is classified as a Derived projection of `Memory.relations ∩ Identity` (no second Memory, no stored
> projection, no Goal driver), the Memory Authority → Relationship projection boundary is preserved (no new
> authority), and 0100C stays deferred — **11th consecutive confirmation, operationally satisfied** (the M1.8 ingest
> test already ran and found no observed contradiction). **This RFC is NOT yet anchored** — it stands at the
> Challenge Gate awaiting the user's verdict. Per the user's sequence, Anchor → M1.11 Relationship Engine → Smoke
> Test follow only after the verdict.

---

> **§13.4 Challenge Record (canonical) — WRITTEN (Anchor `faa8cf3`).**
>
> - **Challenge Result**: **APPROVED WITH CLARIFICATIONS**
> - **Adopted clarifications (all F4, zero base edit)**: **C1** (Identity Boundary — `relationship.rewrite_identity()`
>   / `relationship.patch_self_model()` / `relationship.update_identity()` forbidden; Relationship models only,
>   Identity re-derives upstream), **C2** (Authority / Motivation Boundary — *Memory Authority governs relation-record
>   ingestion + retention, not a Relationship Authority, not relationship-driven Goal mutation*; no new Authority,
>   matrix unchanged; `relationship.set_goal()` / `relationship.patch_drive()` forbidden), **C3** (Persistence Boundary —
>   exactly one Memory; `RelationshipStore` / `SocialMemory` / `ContactBook` / `RelationshipMemory` forbidden; the
>   central Relationship → Memory bypass guard), **C4** (transaction / conflict watchpoint — already tested at M1.8 with
>   **no observed contradiction**; 0100C 11th deferral is **operational satisfied**, not merely bound; Relationship
>   surfaces no new joint as a read-side projection).
> - **Foundation Patch Required**: **NO** (zero Ontology base edit; Relationship classified as a *reading* of
>   RFC-0100 §1.13 + matrix line 167; no new noun / state-class / Authority).
> - **Gate**: **Foundation Stable → YES** (Gate A holds).
> - **Anchor commit**: `faa8cf3`.

## 14. Phase 2A continuation — Foundation remains Frozen (Anchored)

RFC-0111 is **anchored** (commit `faa8cf3`); the Foundation (frozen at RFC-0105 §14) remains **frozen** — 0111
is a *validation*, not a change. **Zero Ontology base edit; no new Authority; no new Memory type.** M1.11
Relationship Engine is **unlocked** (the projection act `derive_relationship(memory, identity, external_ref) ->
RelationshipProjection`, read-only on `Memory.relations` + Identity, emitting a Derived projection that writes
nothing and owns no store). 0100C stays deferred an **eleventh** time — **operationally satisfied** (M1.8 ingest
test ran and found no observed contradiction; Relationship writes nothing directly, surfaces no new joint).
