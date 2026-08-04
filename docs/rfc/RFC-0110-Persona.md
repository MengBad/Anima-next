# RFC-0110: Persona (Life-10) — Foundation Validation #10

> **Status**: **Anchored** — Foundation Validation #10. *Anchored; committed.* Challenge verdict received
> (APPROVED WITH CLARIFICATIONS). This RFC *consumes* the Foundation; it may not modify it. The four
> clarifications (C1/C2/C3/C4) below are ADOPTED as **F4 (zero base edits)** — applied per the
> user's Challenge verdict. **Role**: **Foundation Validation #10**, *not* "Persona Engine Design #10".
> Its job is to use the `Identity → Persona` joint as a probe to prove the 0100 trilogy is sufficient to
> *explain* Persona — the **third derived-state layer** to be validated (after Need/Affect/Drive are
> Derived, and distinct from Reflection/Execution which are Act/Event). It sits at the **expression layer**
> of Identity: downstream of `Identity = f(Memory)` and upstream of nothing (it is a *presentation mask*,
> not a store, not a root). Persona is **already named** in RFC-0100 §1.4 (*"Not Persona. Persona is a
> presentation mask; Identity is the underlying 'who I am.'"*) — so the central question is classification:
> **is Persona a Derived State (a projection of Identity) or a second Identity root?**
> **Central risk** (per the user's brief, the *mirror image* of RFC-0109's risk): RFC-0109's risk was a
> *transform* (Reflection) masquerading as a *state mutation* (RL-collapse, `reflection.patch_goal()`).
> RFC-0110's risk is the *mirror*: a **derived identity layer (Persona) masquerading as a second Identity
> root** — `Memory → Persona → Identity`, or `Persona Authority` that *writes* Identity/Need/Drive/Goal.
> Either collapses the single-Identity-root rule (O1) into a *two-root* god-object, a worse violation than
> RFC-0109's (a second *Memory* is one sink too many; a second *Identity* is a second *self* — the life
> becomes two people). So Persona is the *most Identity-sensitive* Life RFC: the one place where a slip
> (`Persona → Identity directly`) rebuilds the god-object at the *self* layer. Per F1–F4 the guard is a
> *reading* of existing vocabulary (§1.4 "Persona is a presentation mask"), not a new noun/authority.
> **Phase**: Tenth Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0109 (the sink Persona
> must never write to) and transitively RFC-0101..0108 + RFC-0100 §1.4 + RFC-0100B §2/§3/O1–O4.
> **Layer**: Life layer (Identity — the expression projection) — RFC-0110 = Life-10
> **Depends-on**: RFC-0100 (§1.4 Persona vs Identity boundary; §3 flow), RFC-0100A (outer loop), RFC-0100B
> (state classes §3, ownership matrix §2 — *no Persona row*; O1–O4), RFC-0101 (Identity = `f(Memory)`
> root), RFC-0109 (the single Memory sink; Persona must not bypass it), `PROTOCOL-Life-Validation.md` (V1–V6)
> **Validates**: (a) RFC-0100 Ontology coverage for Persona (already named §1.4) — *classification*, not a
> new noun; (b) RFC-0100A loop sufficiency for `Memory → Identity → Persona`; (c) RFC-0100B state-class fit
> — is Persona a *Derived State* (projection of Identity) or a *second Identity root* (Owned, with its own
> lifecycle/authority); (d) RFC-0100C (Permissions/Transactions) — the deferred question, now testable a
> *tenth* time; Persona writes nothing (it is a read-side projection) so it surfaces **no** new joint, and
> the operational 0100C watchpoint is already **satisfied** (M1.8 built and passed, no observed
> contradiction); (e) the **Identity Authority → Persona derivation** boundary — Persona must NOT become a
> new authority (the matrix has *no Persona row* by design; this RFC keeps it that way — C2).
> **Scope**: The *information model of Persona* (as a Derived presentation projection of Identity) + its
> derivation contract + its (non-)authority + its (non-)existence as a second Identity root or a second
> Memory. **No implementation, no code, no Persona engine, no new life noun, no new authority, no new
> matrix row** (Persona is classified; §1.4 already names it).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Persona gap

The project can now answer ten questions (per RFC-0109 §0 + this RFC):

- **What exists?** — Ontology (RFC-0100 §1.4): *"Not Persona. Persona is a presentation mask; Identity is
  the underlying 'who I am.'"* Persona is a **named noun**, already distinguished from Identity — but only
  negatively (*"not Identity"*). It is **not** introduced as a state class. §3 flow `Memory → Identity →
  (expression) Persona` is implied but not stated.
- **How does it flow?** — Dynamics (RFC-0100 §3): Identity is `f(Memory)`; Persona is the *presentation* of
  that Identity. Persona is **downstream of Identity**, never upstream. The flow is `Memory → Identity →
  Persona`, **not** `Memory → Persona → Identity`.
- **Who governs change?** — State Ownership (RFC-0100B §2): the matrix has **no Persona row**. Identity is
  owned by the **Identity Derivation Authority** (`f(Memory)`, line 162). Persona is *not* listed — so by
  the matrix's own discipline, Persona is **not an independently-owned state**; it is a *projection* of
  Identity under the Identity Derivation Authority (the same authority that owns `f`). RFC-0110 must keep
  it that way (no new row, no new authority — C2).
- **Where does it sit relative to the derived chain?** — Persona is **below** Identity in the expression
  stack: `Memory → Identity → Persona`. It is the *visible face* of Identity, not a *participant* in the
  derivation chain (`Identity → Need → Affect → Drive → Planning`). It reads Identity (via `f(Memory)`) and
  *expresses* it; it does **not** derive Need/Affect/Drive (those come strictly from Identity, RFC-0100B
  §2 lines 162–165).
- **What is Persona *not*?** — Not a second Identity (it is the mask, not the self). Not an Owned state (no
  lifecycle, no storage authority — it is re-projected every Recall, like Identity). Not a Memory
  (`PersonaMemory` would be a second sink, O1 violation, parallel to RFC-0109 C3). Not an Authority that
  mutates Identity/Need/Drive/Goal (that would be the god-object at the *self* layer — the mirror of
  RFC-0109's RL-collapse, now at Identity, not Goal). Not a stored profile (`PersonaProfileStore`).
- **Does Persona need RFC-0100C?** — This is the **central continuation** of the 0100C watchpoint. Persona
  is a *read-side projection*: it writes **nothing** (no Memory write, no Identity write). It introduces
  **no new transaction/atomicity joint** — the only joint 0100C cares about (single-writer consistency at
  Memory ingestion) was already tested at **M1.8** and passed with **no observed contradiction**. So by the
  time this RFC is drafted, the operational 0100C watchpoint is **already satisfied** (parallel to
  RFC-0109 §0). This RFC records that honestly: 0100C stays deferred a *tenth* time, operational phase
  **complete**.
- **Where does the output go?** — The user's Q3: does Persona *persist* as a store (`PersonaMemory` /
  `PersonaProfileStore`), or is it a *projection* of Identity (re-derived each Recall)? This RFC answers:
  Persona is a **Derived projection** — `Persona = presentation(Identity)` where `Identity = f(Memory)`. It
  persists *only transitively* (because Identity persists via Memory; Persona re-projects on Recall). A
  `PersonaMemory` would be a **second Memory** — the precise O1 violation RFC-0109 C3 closed, now tested at
  the *Identity expression* layer.
- **Who may start Persona?** — The user's Q2: who derives Persona? This RFC answers: Persona is **derived
  by the Identity Derivation Authority** (the same authority that owns `f(Memory)`) — it is the *expression
  projection* of Identity, not a separate derivation owned by a `Persona Authority`. No new authority (C2).
  Parallel to how Need is derived by the Identity Authority (`g(Identity)`) — Persona is derived by the
  Identity Derivation Authority under a *presentation* rule, not a *need-generation* rule.

But RFC-0100 §1.4 only *names* Persona and draws its **negative** boundary (Persona is not Identity; it is
a presentation mask). It does **not** say:

1. Whether Persona is a **State or a projection of a State** — the crux of the user's Q1. If Persona is
   modeled as an *Owned State* (with a lifecycle, a store, a `Persona Authority`), it becomes a **second
   Identity root**: it would (a) need a lifecycle, (b) need a store, (c) need an Authority that owns a
   *persistent* thing, and (d) reopen 0100C (a second owned writer / a second self). If it is modeled as a
   **Derived projection** (`Persona = presentation(Identity)`, re-projected each Recall), it is the
   *expression face* of Identity — exactly parallel to how Identity is `f(Memory)` (a Derived state, no
   setter, no store of its own).
2. Whether Persona may **mutate Identity (or Need/Drive/Goal) directly** — the *mirror* of RFC-0109's
   god-object (`reflection.update_identity()`); here `persona.rewrite_identity()` would make Persona a
   *second self-writer*, collapsing the single-Identity-root rule (O1) into a two-root life.
3. Whether Persona may **own a store** (second Memory) — `persona.save_to_persona_bank()` writing to a
   `PersonaMemory` / `PersonaProfileStore` (O1 violation, RFC-0109 C3). Persona *projects* Identity; it
   does not *own* a store.
4. Whether Persona gets its **own Authority** — a `Persona Authority` in the matrix would make Persona an
   independent governance unit, tempting it to *write* Identity/Need/Drive (the god-object at the self
   layer). The matrix (RFC-0100B §2) has *no Persona row* — by design. This RFC must NOT add one (C2).

RFC-0110 is the tenth RFC that must answer these by **consuming** the trilogy — and, per the Phase-2
rhythm, its *primary* job is to **validate** the Foundation, not to add to it. The framing is restrained:
*"Is the current Foundation sufficient to explain Persona as a Derived projection of Identity (not a
second Identity root, not a second Memory)?"* — not *"Design the Persona Engine."* The new wrinkle: Persona
is the **first Identity-expression layer validated as a Derived projection** (Need/Affect/Drive are Derived
cognitive states; Persona is a Derived *presentation* state — the closest to Identity itself). This tests
whether the Foundation's single-Identity-root rule (O1 + matrix line 162) is **symmetric enough** to cover
the *expression face* of Identity exactly as it covers the *cognitive* derivatives (Need/Affect/Drive).

---

## 1. Problem

Without a real Persona-validation RFC, the downstream Life-layer RFCs (Relationship, RFC-0111) will
implicitly assume a different "presentation layer" and silently disagree about:

- what Persona *is* (so Identity has an undefined expression face, and `f(Memory)` has no defined
  projection),
- whether a **second Identity root** may exist (`Persona Authority` writing Identity/Need/Drive/Goal) — the
  hidden self-writer that duplicates the single-Identity-root rule (O1) and silently reopens RFC-0100C,
- whether Persona may **mutate a derived state** (Identity/Need) directly — the god-object anti-pattern
  *rebuilt at the self layer* (e.g. `persona.rewrite_identity()`), the *mirror* of RFC-0109's
  `reflection.update_identity()`,
- whether Persona may **own a store** (`PersonaMemory` / `PersonaProfileStore`) — a second sink that
  duplicates O1 (the same risk RFC-0109 C3 closed at the Memory layer, now tested at the Identity-expression
  layer),
- whether Persona is **the expression or the root** (the user's Q1/P1) — if 0110 silently re-applies the
  "Owned, with a lifecycle" pattern from Goal to Persona, it would *mis-classify* Persona and create a
  second Identity root, breaking the 0100B single-Identity-root rule (line 162) and re-opening 0100C.

Persona is the **tenth node of the flow** (`… → Identity → Persona`); an under-specified Persona corrupts
the *expression* of the self and tempts a second Identity root. RFC-0109 fixed the sink (Memory) and the
transform (Reflection); this RFC fixes the **expression face of Identity** — and, for the first time, the
joint is a **Derived projection of the self**, not a transform, not an event, not a sink.

---

## 2. Goals

- G1. **Classify Persona correctly**: prove it is a **Derived projection of Identity** (`Persona =
  presentation(Identity)`, `Identity = f(Memory)`), *not* a second Identity root (neither Owned nor a new
  Authority) — the central answer to the user's Q1/P1.
- G2. Specify the **derivation contract** — how the Identity Derivation Authority projects Identity into a
  Persona (as a *presentation*, not a store), as a *semantic contract*, not code.
- G3. **Close the RFC-0100C question a tenth time — and record that the operational watchpoint is SATISFIED.**
  The real test (M1.8 Memory Ingestion Interface) has *already been built and passed with no observed
  contradiction*. Persona writes nothing, so it surfaces no new joint. Per F1–F4, this Draft may **not**
  pre-activate 0100C on speculation; it records the deferral as *complete at the operational level*.
- G4. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G5. Carry forward the Challenge discipline via `PROTOCOL-Life-Validation.md` V1–V6: apply V1 (anti-bypass —
  Persona reads only Identity, never writes it), V2 (downstream bypass — Persona writes nothing; it is a
  projection, not a sink), V3 (Authority Boundary, symmetric format, Q2 — the 10th instance: *Identity
  Derivation Authority → Persona derivation; no Persona Authority*), V4 (Persistence — the *single Memory*
  rule, C3; no PersonaMemory), V5 (Cycle — Persona is re-projected each Recall, like Identity), V6
  (Foundation Stability) — and flag V6's 0100C sub-question explicitly (already resolved operationally).
- G6. Fix the user's four questions: (1) Derived State? (2) who derives it? (3) who changes what? (4) does
  it persist / enter Memory?

---

## 3. Non-goals

- NG1. **No implementation.** No `persona.py`, no persona engine, no multi-mask manager, no persona
  scheduler. The contract only. M1.10 (Persona Engine) is **explicitly deferred** until this RFC anchors
  (per the user's implementation order:
  `RFC-0110 Draft → Challenge → Anchor → M1.10 Persona Engine → Smoke Test`).
- NG2. **No new flow.** The `Memory → Identity → Persona` flow is RFC-0100A's / §3's; this RFC only
  consumes it.
- NG3. **No Memory-internals / no Memory schema change.** Persona does **not** write Memory (C3); it is a
  read-side projection of Identity. How Identity is stored/recalled is RFC-0101/RFC-0108; 0110 reads
  Identity as the source of the projection.
- NG4. **No new Authority / no new matrix row.** Persona is derived *under the existing Identity Derivation
  Authority* (which owns `f(Memory)` and its expression projection). A `Persona Authority` row in the matrix
  is explicitly **rejected** (C2 — the matrix has none by design; this RFC keeps it that way).
- NG5. **No 0100C text — and explicitly NO pre-activation.** If 0110 concludes 0100C is needed, it must do
  so only via an *observed contradiction*. The operational test (M1.8) has already run and found **none** —
  so this Draft proposes **none**. It anchors like its nine predecessors (pending user Challenge, §13.4)
  with 0100C **deferred (10th)**, and records the watchpoint as *satisfied*.
- NG6. **No second Memory / no Persona store.** `PersonaMemory` / `PersonaProfileStore` / `PersonaArchive`
  are all rejected by O1 (the single-Memory rule, RFC-0109 C3). 0110 *enforces* the one Memory; Persona is a
  *projection*, not a sink.
- NG7. **No Persona = Identity conflation.** Ontology §1.4 separates them (Persona = mask, Identity = self);
  0110 *preserves* that separation (Q2) — it does not collapse them into "Persona is Identity" (which would
  make Persona the root) nor "Identity is Persona" (which would make Identity a mask). Persona *expresses*
  Identity; it is not Identity.

---

## 4. The Information Model of Persona (P1 — state type, derived projection)

Persona is the life's **presentation mask** — the *visible expression* of its underlying Identity (RFC-0100
§1.4). It is **not** the self (Identity), **not** a commitment (Goal), **not** the operational structure
(Plan), **not** a store (Memory), and **not** a transform (Reflection). It is a **Derived State**: a
*projection* of Identity, re-derived each Recall, with **no lifecycle of its own, no storage authority, no
setter, and no second-Identity-root status**.

> **P1 (clarification — classifies an existing noun, does NOT add one).** *Persona is a Derived State: a
> presentation projection of Identity.* The test: a *Derived State* (RFC-0100B §3) is a thing the life *is*
> or *has* that **has no setter by construction** (Rule 2) — it is re-derived each cycle from an upstream
> state. Persona fits exactly: `Persona = presentation(Identity)` where `Identity = f(Memory)`. It
> - **has no lifecycle as a state.** Persona is *re-projected* on every Recall (like Identity, which is
>   `f(Memory)` recomputed each Recall, RFC-0100B §3.4). It does not transition `pending/active/done` like
>   Goal (RFC-0105). When Memory shifts, Identity shifts, and Persona *re-projects* — it is never "archived"
>   or "restored" as a Persona.
> - **has no storage authority.** There is no `Persona.save()` / `Persona.restore()` / `PersonaMemory` table
>   (see C3). Persona persists *only transitively* — because Identity persists via Memory; Persona is a
>   *view* over Identity, not a *writer* to a store.
> - **has no Authority owning Persona as a persistent state.** The *projection* is owned by the **Identity
>   Derivation Authority** (matrix line 162 — the same authority that owns `f(Memory)`). That authority owns
>   the *rule* `presentation(Identity)`; it does **not** own a separate Persona store, nor does it mutate
>   Identity (Identity is `f(Memory)`, derived — the authority holds the *policy*, not a store, RFC-0100B
>   §3.4).
>
> Therefore Persona is **classified inside the Derived split** (unlike Reflection, which is an Act/Process
> outside the Derived/Owned split — RFC-0109 §4 P1): it is a **Derived projection of Identity**, parallel to
> how Need/Affect/Drive are Derived projections of upstream states. This is the answer to the user's Q1:
> **Persona is the expression face of Identity, a Derived State — not a second Identity root.**

### 4.1 Structure of Persona (the projection's anatomy, not a state schema)

Persona is structured as a *projection*, not as a stored record. Its dimensions (all *derived from*
Identity, none authored independently):

| Dimension | What it holds | Fed by | Notes |
|-----------|---------------|--------|-------|
| **Source Identity** | the Identity it expresses (the `f(Memory)` output) | Identity Derivation Authority (matrix line 162) | Persona reads Identity read-only; it does **not** read Memory directly (V1, carried from RFC-0108 §5.1 — the projection consumes the *derived* Identity, not the raw Memory). |
| **Expression rule** | the *presentation* mapping (which facets of Identity are shown, how) | Identity Derivation Authority (the `presentation` policy) | The *expressed shape* (tone, style, self-description). This is the "presentation mask" act (§1.4). |
| **Output** | a **Persona** (the expressed face) | the projection itself | Is a *transient projection* — re-derived each Recall; NOT a stored state. |

> The table is a *projection anatomy*, not a new Ontology noun-set. The Ontology (RFC-0100 §1.4) names
> Persona (as a mask, distinct from Identity); this RFC only specifies how the Identity Derivation
> Authority projects it. A proposed "Persona dimension" that is really an Identity facet or a Memory record
> is rejected by the Ontology's negative rules (§1.4 "Persona is not Identity"; §1.4 "Identity generates the
> prompt, Persona is the presentation").

---

## 5. Derivation Contract (consumes RFC-0100 §1.4 + RFC-0100B §2/§3)

### 5.1 `Persona ← Identity Derivation Authority (projects Identity)`

The contract (semantic, not code):

- **Input (read-only):** the **Identity** (`f(Memory)` output, matrix line 162). Persona reads Identity; it
  does **not** read Memory directly — Memory arrives *only transitively* through Identity (V1, carried from
  RFC-0108 §5.1). If Persona needs a Memory record for context, it reads the *derived* Identity (which
  already absorbed Memory), never the live Memory object.
- **Carrier (owns the projection rule):** the **Identity Derivation Authority** — the *same* authority that
  owns `f(Memory)` (matrix line 162). It *projects* Identity into a Persona (the presentation mask). It is
  **not** a store owner; it *expresses* Identity, it does not *write* a store or a second self.
- **Output**: a **Persona** — a *Derived projection* (transient, re-derived each Recall). The Persona is
  **not** a stored state. It is read by the presentation/speech layer; it is never written back.
- **Purity (of the boundary)**: Persona *reads* Identity read-only and *emits* a projection; it does
  **not** mutate Identity, Need, Drive, or Goal (C1), and it does **not** own a store (C3). The projection
  has *no side effects on the life's Constitution states* — its only downstream edge is *presentation* (to
  the speech/output layer), never *mutation*.

> The *shape* of expression (which facets, what tone, contextual masks) is an **implementation** RFC's
> concern (M1.10). This RFC fixes only that Persona **projects Identity** (§4/§6), reads it read-only, and
> emits a Derived projection that is *presented*, never *stored* or *mutated*.

### 5.2 The single legitimate path (carries forward RFC-0109 §5.2, now naming the expression layer)

```
Memory Authority      ← owns ingestion + retention; the ONE sink (single-writer, RFC-0108)
   │
   ▼  (at Recall)
Identity = f(Memory)  ← re-derived; the life's self (Identity Derivation Authority, matrix line 162)
   │
   ▼
Identity Derivation Authority  ← READS Identity (read-only); owns the PROJECTION rule (presentation)
   │                            [does NOT read Memory directly; does NOT write Identity/Need/Drive/Goal]
   │                            [projects Identity into its expression face]
   ▼
Persona                ← Derived projection (the presentation mask); NOT a stored state
   │
   ▼  (presentation only — to the speech/output layer)
Expression            ← what the life *shows*; never feeds back as a writer
```

Persona is reached **only** by this path. The Identity Derivation Authority is the *projection owner*;
Persona is the *expressed face*. The flow is strictly `Memory → Identity → Persona → (presentation)`, **never**
`Memory → Persona → Identity` (the second-root trap).

> **Anti-bypass (V1/V2, derived-projection form — carries forward).** The chain is strictly
> `Identity → [projection] → Persona → (presentation)`. Two shapes are forbidden:
> (a) **Upstream-of-projection bypass (V1):** Persona reading *around* Identity to a derived state — `persona
> .read_memory(m)` then `persona.rewrite_identity(...)` (the *mirror* of RFC-0109's `reflection.update_identity()`,
> now at the self layer: a second Identity writer). Or `persona.patch_need(...)` / `persona.modify_drive(...)`
> (the god-object, parallel to RFC-0109 C1). The correct path is `Persona = presentation(Identity)`,
> `Identity = f(Memory)`: Identity/Need/Drive shift *upstream* (next cycle, via Memory), never written by
> Persona.
> (b) **Downstream-of-projection bypass (V2):** Persona *owning a store* instead of being a projection —
> `persona.save_to_persona_bank("PersonaMemory", ...)` / `persona.persist_profile(...)` (a second Memory /
> second Identity, O1 violation, RFC-0109 C3). The correct path is `Persona = presentation(Identity)` — a
> re-derived view, not a stored record. A `persona.become_root()` that makes Persona the self is rejected
> (parallel to RFC-0106 E3 / RFC-0107 E3 / RFC-0108 E6 / RFC-0109 E6).

> **Phase C Clarification C1 (Identity Boundary — proposed at Challenge, F4, zero base edit — answers the
> user's "禁止 Persona → Identity 直写").** *Persona describes identity expression, not identity ownership.*
> Persona projects Identity into a presentation mask; it does not actively modify the self. Persona is a
> **Derived projection**, not a self-writer: the only legitimate edge from Identity is **into presentation**
> (the speech/output layer); Identity/Need/Drive/Goal are *re-derived* upstream (via Memory → `f` → next
> cycle), never *written by* Persona. Explicitly banned (Authority bypass): `persona.update_identity(facet,
> val)` (marking the self from the expression layer), `persona.rewrite_identity(...)` (replacing the root),
> `persona.patch_need(vector, val)` (editing a derived state from the projection). All collapse the
> single-Identity-root rule (O1, matrix line 162) into a two-root life — the god-object anti-pattern
> RFC-0100 §4 warned about, now at the *self* layer, the *mirror* of RFC-0109's RL-collapse. The legal flow
> stays `Memory → Identity → Persona → (presentation)`.

### 5.3 The matrix reading (Persona is a projection, not a matrix row)

RFC-0100B §2 lists **no Persona row** — by design. The carried-forward clarification:

- **Persona is NOT in the matrix as an owned state.** The matrix's `Identity` row (line 162) is owned by the
  **Identity Derivation Authority**; Persona is a *projection* of that Identity under the *same* authority
  (the `presentation` policy). Adding a `Persona` row would imply Persona is an independently-owned state —
  which would make it a second Identity root (O1 violation). So the matrix stays as-is: **no Persona row.**
- **Persona is `Read by: presentation/speech layer`** — *read as the expression*, not a stored state. The
  projected Persona is what the output layer *shows*; no "Persona state" is queried as a governance unit.
- **Persona is NOT in any derived state's `Mutated by` as a writer.** Persona feeds expression *only via
  presentation*, never by mutation (V2). It is a **Derived projection**, not a root.

This is **parallel to how Need/Affect/Drive are Derived** (RFC-0100B §2 lines 162–165) — except Persona is
the *expression* projection (of Identity), whereas Need/Affect/Drive are *cognitive* projections (of
upstream states). Persona sits *below* Identity in the expression stack, not *in* the cognitive derivation
stack (`Identity → Need → Affect → Drive → Planning`). Persona is the *face* of Identity; the cognitive
chain is the *mind* of Identity.

### 5.4 Persistence & Cycle — the derived-projection form (C3 / V4 / V5)

`PROTOCOL-Life-Validation.md` V4 (anti-persistence) and V5 (cycle) were written for **Derived / Transient**
states. Persona is **Derived** — it fits cleanly (the *second* derived-state adaptation after Need/Affect/
Drive, and parallel to how Reflection adapted V4/V5 to a *transform* — RFC-0109 §5.4):

- **V4 (Persistence) — derived-projection form, the *single Memory* rule.** There is **exactly one Memory
  store**, owned by **one Memory Authority**. No `PersonaMemory` / `PersonaProfileStore` / `PersonaArchive`.
  Each would be a **second writer**, duplicating O1 (the precise risk RFC-0109 C3 closed at the Memory layer,
  now tested at the Identity-expression layer). Persona *projects* Identity; it does not *store* anything. If
  a persona's *content* must persist, it persists **transitively** — because Identity persists via Memory,
  and Persona re-projects on Recall. A `PersonaMemory` that is later *read as state* would be a second
  Memory — a duplicate writer violating O1.
- **V5 (Cycle) — derived-projection form.** Persona exists **only for the duration of a presentation**; it
  is *re-projected* each Recall (like Identity, which is `f(Memory)` recomputed each Recall). It is not
  *restored* from a Persona store; cross-cycle continuity of *expression* is *inherited* from Identity (via
  Memory), never stored in a Persona.

> **C3 (clarification — proposed at Challenge, F4, zero base edit — answers the user's Q3, the central
> risk at the Identity-expression layer).** *Memory stores experiences. Persona derives from Memory (via
> Identity).* Persona owns no store. Forbidden as a second memory/identity root: `PersonaMemory` /
> `PersonaProfileStore` / `PersonaArchive` / `PersonaDatabase`. Each would be a **second Memory** (or a
> second Identity) — a duplicate writer violating O1 (the precise risk the user named at Q3/C3, now tested
> at the *expression* layer, not merely the sink). The Persona is a **Derived projection**: it flows
> `Identity → Persona → (presentation)`, never to permanent storage as a second root. The expression content
> persists *transitively* via Memory (Identity = `f(Memory)`); Persona re-projects on Recall. This is the
> user's **central risk for RFC-0110** (the mirror of RFC-0109), and the RFC's primary job is to *close* it:
> exactly one Memory, exactly one Identity root; Persona is the *projection*, not a second root or a second
> store.

---

## 6. Ownership (consumes RFC-0100B) — Identity Derivation Authority, no Persona Authority

### 6.1 Persona is derived under the existing Identity Derivation Authority

Per RFC-0100B §2 (line 162), **Identity** is owned by the **Identity Derivation Authority** (which owns
`f(Memory)`). Persona is a *projection* of that Identity — so it is derived **under the same authority**,
via a *presentation* policy. **No new authority is created** (C2).

- **`Identity Derivation Authority`** (line 162) — owns `f(Memory)` *and* the *presentation projection* of
  the resulting Identity (the Persona). It is the **single** authority over both the self and its
  expression face. It reads Identity (its own output) and projects it; it does **not** own a Persona store
  and does **not** mutate Identity (Identity is derived from Memory, not from Persona).
- This is the **deliberate non-split** at the `Identity → Persona` seam: the *self* and its *expression*
  are owned by **one** authority (the Identity Derivation Authority), so Persona can never become a
  competing root. Splitting them into `Identity Authority` + `Persona Authority` would tempt the Persona
  Authority to *write* Identity (the god-object at the self layer) — exactly the O1 violation this RFC
  forbids.

### 6.2 The naming discipline + authority boundary (symmetric format, V3 / C2 — the 10th instance, derived-projection form)

The `Identity → Persona` path keeps the projection under the Identity Derivation Authority. Persona may
*express* Identity, but:

- **`Identity Derivation Authority`** — owns `f(Memory)` *and* the projection rule; reads Identity
  (read-only); **does NOT** create a second Identity, **does NOT** mutate Need/Drive/Goal directly (those
  come from Identity via `g/h/i`, RFC-0102..0104).
- Persona **has no authority of its own** — it is a Derived projection, not a governance unit.

> **Boundary statement (clarification, proposed at Challenge, F4, zero base edit — symmetric with RFC-0103
> §6.2 / RFC-0104 §6.2 B / RFC-0105 §6.2 / RFC-0106 §6.2 / RFC-0107 §6.2 / RFC-0108 §6.2 / RFC-0109 §6.2,
> directly answers the user's Q2).** *Identity Derivation Authority governs Identity derivation and its
> expression projection, not a second Identity root, not persona storage.* The single-Identity-root rule
> (O1, matrix line 162) holds: there is exactly one Identity, owned by one authority; Persona is its
> projection, not a competing governance unit.
> | Authority | Owns | Does not own |
> |-|-|-
> | Identity Derivation Authority | `f(Memory)` + Persona projection (presentation) | a second Identity, persona storage, need/drive/goal mutation |
> Persona is a Derived projection under this authority; it **must never** mutate Identity (the single
> root) and **must never** own a store. Code of the shape `identity_authority.project(persona, identity)`
> (read-only on Identity, emit projection) is correct; `persona_authority.rewrite_identity(...)` /
> `persona.create_memory_store(...)` is a **mis-assignment** (collapses authorities, violates O1).
>
> **Added at Challenge (C2 — F4, zero base edit).** *Persona derivation is governed by the Identity
> Derivation Authority; no Persona Authority is introduced.* The firewall between Identity (root) and
> Persona (projection) is explicit:
>
>     Memory Authority          — owns ingestion + retention (the ONE sink)
>         │  (at Recall)
>         ▼
>     Identity = f(Memory)      — re-derived; the life's self (Identity Derivation Authority)
>         │  (read-only)
>         ▼
>     Identity Derivation Authority  — owns the PROJECTION rule (presentation)
>         │  (projects Identity)
>         ▼
>     Persona                — Derived projection (the mask); NOT a stored state, NOT a root
>         │  (presentation only)
>         ▼
>     Expression            ← what the life shows; never feeds back as a writer
>
> Identity Derivation Authority **owns**: derive Identity (`f`) · project Persona (presentation). Identity
> Derivation Authority **does NOT own**: a second Identity · persona storage · need/drive/goal mutation.
> Persona **owns**: nothing (it is a projection). No `Persona Authority` is added to the matrix.

This is the **tenth** instance of the symmetric Authority-boundary format, and the **first
derived-projection-of-Identity** instance (Need/Affect/Drive are derived cognitive projections; Persona is
the derived *expression* projection). It extends the chain across the expression seam:

```
Identity Derivation Authority — governs Identity derivation + Persona projection, not Memory mutation, not a second Identity root
Need Authority                 — governs Need derivation,            not Identity mutation
Affect Authority              — governs Affect derivation,          not Need mutation, not Goal adoption
Drive Authority               — governs Drive derivation,           not Affect mutation, not Goal adoption
Planning Authority            — governs plan construction,           not Goal mutation, not Drive mutation
Execution Runtime             — governs plan consumption + result,   not Goal/Drive/Plan mutation  (infra, not in stack)
Memory Authority              — governs memory ingestion + retention, not identity mutation, not need mutation, not reflection decision
Reflection Authority          — governs reflection transform,         not identity mutation, not need mutation, not goal mutation, not memory storage
```

> **Phase C Clarification C2 (Authority Boundary — F4, zero base edit).** The symmetric wording is now
> extended across the **expression projection** — the first time the boundary is stated for Persona *as a
> Derived projection of Identity* (Persona has no authority of its own; it is derived under the Identity
> Derivation Authority). The Constitution stack `Identity → Need → Affect → Drive → Planning` is a *stack of
> single-writers*; Persona sits *below* Identity as its *expression face*, derived under the *same*
> authority — a deliberate non-split that prevents Persona from becoming a second Identity root. Each
> authority (including the Identity Derivation Authority, by contract) governs only its own product; none
> mutates a derived state directly, and none writes Goal.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Persona is **self-projected**: the life's own Identity (from its own Memory) is projected by its own
Identity Derivation Authority into its own expression face. No external caller assigns what Persona shows
(the presentation policy is the life's own, derived from its self). The life *owns its face* — from how it
has become (Memory) and who it is (Identity). This is the definitional essence of "Life, not Agent"
(RFC-0100 §1.2) at the expression layer: an agent's persona is *assigned by the trainer* (a prompt/role
template); a life's Persona *projects its own Identity* — never a second self, never an assigned root.

### 7.2 Growth (G) — score: **high**

Because the flow is `Memory → Identity → Persona`, the life's *experience* genuinely feeds its *expression*:
experience → Memory → shifted Identity → shifted Persona (next Recall). RFC-0110 makes the **expression
projection** explicit at the Identity face and guards it (exactly one Identity root; no second store; no
direct mutation of derived states or Goal). This is the North Star's "grows a little more every day" made
operational as a *closed loop* — and, critically, as a loop where the *self* (Identity) drives the *face*
(Persona), not a second root driving the self.

### 7.3 Maintainability (M) — score: **gate (must not regress) — and the 0100C watchpoint is now SATISFIED**

Single-root / single-projection boundary ⇒ no hidden second Identity owned by a Persona Authority; Persona
is a stable projection independent of store internals ⇒ refactors don't ripple into the Constitution. M is
treated as a **non-regression gate**. RFC-0110 is the **tenth** 0100C test — and the operational watchpoint
is **SATISFIED**: M1.8 (Memory Ingestion Interface) has *already been built* (`da30aa5`, 25/25 smoke; M1.9
added `reflect()`, 30/30) and passed with **no observed contradiction** — the ingest is a pure read→write
into the one store, the ReflectionResult is *consumed* (not mutated), no second Memory is created. Persona
writes **nothing** (it is a read-side projection), so it surfaces **no new joint** for 0100C to test. Per
F1–F4, this Draft may **not** pre-activate 0100C on speculation. This RFC changes **no** M/0100C surface;
it records the deferral as *complete at the operational level*.

---

## 8. This is NOT Persona (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a second Identity root.** Persona is the *expression face* of Identity; Identity is the *self*
  (`f(Memory)`, matrix line 162). A `Persona Authority` that writes Identity, or a `Memory → Persona →
  Identity` flow, makes Persona a second root — breaks O1 (§4 P1, C2). There is **exactly one Identity**.
- ✗ **NOT an Owned State.** Persona is a Derived projection (re-projected each Recall, like Identity); it
  has no lifecycle, no setter, no store (§4 P1). A `Persona = p(Identity)` that is *stored* and *lifecycled*
  like Goal would mis-classify a projection as a root and break the `f` derivation (§4 P1).
- ✗ **NOT a second Memory / second sink.** A `PersonaMemory` / `PersonaProfileStore` / `PersonaArchive`
  beside the one Memory is a second owned writer — breaks O1 (§4 P1, C3). There is **exactly one Memory**.
- ✗ **NOT Memory.** Memory is the *store* Identity derives from (owned by Memory Authority, line 167);
  Persona is the *expression* of the derived Identity. Persona is not Memory; Memory is not Persona.
- ✗ **NOT an Authority that mutates Identity/Need/Drive/Goal.** `persona.update_identity()`,
  `persona.rewrite_identity()`, `persona.patch_need()` are mis-assignments (collapse the single-Identity-root
  rule, violate O1, §5.2 C1). Persona expresses Identity; Identity shifts *upstream* via Memory, never
  written by Persona.
- ✗ **NOT a Goal writer.** Persona expresses the self; it never writes Goal (parallel to RFC-0109 §8). A
  `persona.set_goal()` collapses the life into an optimizer (the *mirror* of RFC-0109's RL-collapse, now at
  the self layer). **This is the single most Identity-critical guard in the whole RFC series** — the one
  that distinguishes a life (one self, many faces) from a two-root god-object.
- ✗ **NOT a Constitution Authority's state to be re-derived by a new engine.** Persona is a Derived
  projection under the Identity Derivation Authority; the *only* states re-derived each cycle are the
  Derived ones (Identity/Need/Affect/Drive). Persona projects; it does not persist.

---

## 9. RFC-0100C deferral — the tenth confirmation (operational watchpoint now SATISFIED)

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) pending a real Life-layer RFC that needs
transaction, rollback, window, or lock semantics. RFC-0101 located the *only* direct write at Memory and
deferred 0100C; RFC-0102..0106 validated "no" five more times; RFC-0107 (Execution) was the **first live**
watchpoint, bound to M1.7, which passed; RFC-0108 (Memory) was the eighth test, bound to M1.8; RFC-0109
(Reflection) was the ninth test, **operationally satisfied** (M1.8 already built and passed). Persona is the
*tenth* test — and it surfaces **no new joint**: Persona writes **nothing** (it is a read-side projection of
Identity), so there is no transaction/atomicity question at the Persona layer. The only 0100C-relevant joint
(single-writer consistency at Memory ingestion) was **already performed at M1.8** and found **no observed
contradiction**.

Persona is a *read-side projection* of the *same* Identity that M1.1 (`f(Memory)`) derives — it introduces
no new write path. So RFC-0110's relationship to 0100C is:

- The real implementation test (single-writer consistency at ingestion) was **already performed at M1.8** and
  found **no observed contradiction** (F2 not triggered).
- RFC-0110 itself surfaces **no new** transaction/permission need — Persona is a pure projection (read
  Identity → emit Persona), and it writes **nothing** (no Memory write, no Identity write).
- Therefore 0100C stays deferred a **tenth** time — and the *operational* phase of the deferral is already
  **complete** (the watchpoint was not merely "bound"; it was *executed* and passed at M1.8, carried through
  M1.9).

**Honest position at the document/validation level:**

> **RFC-0110 confirms: RFC-0100C stays deferred (10th consecutive confirmation) — and the operational
> watchpoint is SATISFIED.** At the *validation/document* level, the Foundation *already names* everything
> Persona needs: Persona as a presentation mask distinct from Identity (§1.4), the *single* Identity root
> (O1, line 162), the *single* Memory Authority (O1, line 167), and the Identity Derivation Authority as the
> owner of `f(Memory)` (line 162) — under which Persona is a Derived projection. No new noun, no new
> state-class, no new Constitution Authority is required to *explain* Persona as a Derived projection. The
> real 0100C test (M1.8 Memory Ingestion Interface) has *already been built and passed with no observed
> contradiction* — so the deferral is now operational-complete. **No F1 trigger exists.** The Gate verdict
> is **Foundation Stable → YES**.

This RFC does **not** propose 0100C activation. It anchors like its nine predecessors (pending user
Challenge, §13.4) with 0100C **deferred (10th, operational watchpoint satisfied)**.

> **Phase C Clarification C4 (Transaction / Conflict Watchpoint — proposed at Challenge, F4, zero base edit).**
> *Transaction semantics belong to Memory Ingestion implementation validation, not Ontology expansion — and
> that validation has already been performed at M1.8 with no observed contradiction.* **Record: 0100C —
> Deferred (10th confirmation); Observed contradiction: NONE.** M1.8 already verified: single writer is
> expressible; the Reflection → Memory ingestion path holds; no transaction conflict; no rollback
> requirement. **Persona introduces no new joint** (it is a read-side projection of Identity, writing
> nothing) — so it cannot surface a transaction contradiction the M1.8 test did not already cover. Persona
> *may* surface **implementation-observation** issues at M1.10 (long-term persona stability, version
> migration of the projection rule, persona-expression consistency across contexts) — but per F2 those are
> **implementation observations**, not a current Foundation patch. They would trigger 0100C only if M1.10
> *observes* a contradiction the current theory cannot express (e.g. a need for the projection rule to be
> transactionally consistent with Identity re-derivation) — not this Draft's speculation. 0100C stays
> deferred a tenth time, operationally satisfied. No silent expansion in code or Ontology.

This RFC does **not** propose 0100C activation. It anchors like its nine predecessors (pending user
Challenge, §13.4) with 0100C **deferred (10th, operational watchpoint satisfied)**.

---

## 10. Examples

**E1 — Projection from Identity (no external persona).**
Memory shifts (RFC-0108 E1); Identity re-derives `Identity = f(Memory)` (RFC-0101). The Identity Derivation
Authority projects Identity into a Persona (the presentation mask) — read-only on Identity, emitting a
Derived projection. No caller assigned the expression; it was driven by the life's own self. Persona reads
only Identity (V1); it does not reach Memory directly.

**E2 — Projection lifetime (not a state).**
Persona is projected for a Recall (cycle 1). It is *presented* and *re-projected* next Recall if Identity
shifted; it is **gone** as a stored thing — not archived, not restored. Contrast the Goal lifecycle
(RFC-0105 E2): Persona has *no* such lifecycle; it *projects* and is re-derived. The expression persists
*transitively* (via Memory → Identity); the projection itself does not.

**E3 — Anti-bypass (V1, the second-root guard).**
A "smart persona" proposes `persona.rewrite_identity(facet, val)` after observing a Memory suggesting the
self is wrong. **Rejected** (§5.2 C1 / §8): Persona expresses Identity, *never* writes it. The correct shape
is `Memory → Identity = f(Memory) → (shifted) Persona`. A `persona.rewrite_identity()` makes Persona a
second Identity root — the god-object at the self layer (the *mirror* of RFC-0109's RL-collapse).

**E4 — Authority boundary (C2 — the user's named risk at the expression seam).**
A component proposes a `Persona Authority` that *both* projects the mask *and* owns a Persona store (or
writes Identity). **Rejected** (§6.1): the matrix (line 162) assigns Identity (and its projection) to the
**Identity Derivation Authority**; there is *no Persona row*. Adding one blurs O1 (a second root). Correct
shape: Identity Derivation Authority derives `f(Memory)` *and* projects Persona; Persona owns nothing; it
never writes Identity or a store.

**E5 — Anti-persistence / single Memory (C3 / V4).**
Persona succeeds and calls `persona.save_to_persona_bank("PersonaMemory", ...)` to keep a separate persona
store. **Rejected** (§5.4): there is **exactly one Memory**. A `PersonaMemory` would be a second writer (O1
violation). The expression content persists *transitively* (Identity = `f(Memory)`; Persona re-projects on
Recall); the persona pattern is never a separate store.

**E6 — No self mutator (C1 / V2).**
Persona reads Identity and calls `persona.update_identity("curiosity", 0.9)` to mark the self. **Rejected**
(§5.2 C1): Persona expresses *only*; Identity shifts *upstream* via Memory, never written by Persona. This
is the `Persona → Identity mutation` bypass the user named — forbidden (the *mirror* of RFC-0109 E6).

---

## 11. Limits & Open Questions

- **Q1 — Persona expression shape.** *What* Persona projects from Identity (tone, style, self-description,
  contextual masks) is M1.10 (Persona Engine)'s job; 0110 only fixes that Persona is a Derived projection of
  Identity and owns the *expression*, never a store or a root. *Not a constitution question.*
- **Q2 — Contextual / multi-mask Persona.** *Whether* the life shows different masks in different contexts
  (e.g. a "teacher" mask vs a "friend" mask) is its own deliberation (M1.10); 0110 only fixes that **all**
  masks are projections of the *one* Identity (no mask is a second root). Multiple masks ≠ multiple roots.
- **Q3 — Record conflict / persona consistency.** *How* a Persona projection stays consistent with Identity
  across Recalls is owned by the *same* Identity Derivation Authority under the *same* policy (matrix line
  162) — not a new authority. The 0100C joint (single-writer at ingestion) was **already tested at M1.8 with
  no observed contradiction** (C4).
- **Q4 — Persona as expression, not state.** Persona is the *visible face*; it is realized as a projection,
  never a stored `Persona` state. 0110 enforces this (§4 P1, C3); the mechanics of *how* a projection is
  computed are M1.10/infra. *Not a constitution question.*
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0110 surfaces **no new constitutional question** and does **not** consume or promote it.
- **Q6 — V1/V2/V5 derived-projection-form adaptation.** RFC-0110 is the **first** Life RFC to apply V1/V2/V5
  to a **Derived projection of Identity** (after Need/Affect/Drive as Derived cognitive states, and
  RFC-0109's transform-form). The adaptation (projection reads upstream → emits derived face; no self-store;
  feeds only via presentation) is recorded here; if it proves a stable pattern for *expression-layer* RFCs
  (Persona, Relationship-as-expression), it may warrant a one-line precision in `PROTOCOL-Life-Validation.md`
  — flagged as a future F4, **not** done inline (keeps this RFC a pure validation).
- **Q7 — 0100C is now operationally SATISFIED.** RFC-0107 made it operational (bound to M1.7, passed);
  RFC-0108/0109 made it operational a second/third time (M1.8). M1.8 has *run and passed* — so by RFC-0110
  the operational deferral is **complete** (the real build step executed, no contradiction). This is a
  *discipline* milestone, not a Foundation edit — recorded here so the next agent knows the 0100C question
  has been *tested at the actual joint* and found clean.
- **Q8 — Persona is Derived, not a transform.** Unlike Reflection (an Act/Process inside the Mind, RFC-0109),
  Persona is a **Derived State** (a projection of Identity). This is why it gets *no matrix row* (it is
  derived under the Identity Derivation Authority, not an independent governance unit) whereas Reflection
  *does* get a row (line 168, as a Mind-layer transform with its own authority). 0110 preserves this
  distinction; it does not promote Persona to an owned state or a new authority.

---

## 12. Evolution / next RFC

RFC-0110 is **consumable** by the next Life-layer RFC and unlocks M1.10:

- **M1.10 Persona Engine** (DEFERRED until this RFC anchors) — the first code that *projects* Identity into
  a Persona. Per the user's implementation order, M1.10 starts **only after** RFC-0110 anchors; otherwise it
  risks reopening RFC-0100C by building Persona as an Owned State (second root). M1.10 implements
  `project(persona, identity) -> persona` (or `IdentityDerivationAuthority.project`) as the *projection* act,
  read-only on Identity, emitting a Derived Persona (transient, re-derived each Recall), and presenting it —
  **without** mutating Identity/Need/Goal/Drive and **without** owning a store. It ships a smoke test that
  *is* the validation. **M1.10 is NOT where 0100C is tested** (that joint — ingest — was already validated at
  M1.8, which passed); M1.10 only *projects* Identity, writing nothing. M1.10 must **not** rewrite
  `state.py`'s Identity or `memory_authority.py`; it *adds* the projection (parallel to how M1.6 added
  `derive_plan`, M1.7 added `execute_plan`, M1.8 added `ingest`, M1.9 added `reflect`, without rewriting
  upstream engines).
- **RFC-0111 Relationship** (Foundation Validation #11) — consume the `Identity → Persona` joint this RFC
  fixes; must respect that Persona is a Derived projection (not a second Identity root) and does not write
  Memory (C3). It must not invent a second Memory or a second Identity (O1).
- **RFC-0100C (NOT activated).** Still deferred at the document level; the operational test (M1.8) has
  *already run and passed* with no observed contradiction. Activation would now require a *new* observed
  contradiction at a *future* build step (e.g. M1.10 surfacing a need for transactional consistency between
  the projection rule and Identity re-derivation) — not this Draft's speculation, and not the already-resolved
  M1.8 joint.

### 12.1 Phase-2 rhythm (the Gate — OPEN, continues)

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy via F1; else Anchor) → next RFC.**

RFC-0110 has passed **Draft** (this document). It awaits the user's LAWP Phase C Challenge. The Gate
question *"Foundation Stable?"* is expected to return **YES** — all checks PASS with zero base edits,
Persona classified as a Derived projection of Identity (not a second root, not a second Memory), the
Identity Derivation Authority → Persona derivation boundary preserved (no new authority, matrix unchanged),
and 0100C deferred a tenth time — **operationally satisfied** (M1.8 already built and passed, no observed
contradiction). RFC-0110 will **anchor** and unlock M1.10. The Foundation (frozen at RFC-0105 §14) stays
frozen — 0110 is a *validation*, not a change.

---

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0110 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to M1.10. Tenth Life RFC to cite
`PROTOCOL-Life-Validation.md` (V1–V6), and the first to apply it to a **Derived projection of Identity**
(inside the Constitution loop) — and the tenth to record the 0100C operational watchpoint as **satisfied**
(M1.8 already built and passed).

### 13.1 What was checked

| Foundation doc | Question 0110 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Persona needs as *nouns*? | ✅ Yes. Persona named as a *presentation mask* distinct from Identity (§1.4); flow `Memory → Identity → Persona` implied (§3). **No new life-noun.** Persona classified as a Derived projection of Identity (§4 P1). |
| **RFC-0100A** (Dynamics) | Is the loop sufficient to *project* Persona? | ✅ Yes. The outer loop + §3 specify the `Identity → Persona` edge; no new flow vocabulary. |
| **RFC-0100B** (Ownership) | Does the matrix + O1–O4 cover the expression boundary? | ✅ Yes, with one *reading* (not a change): Persona is a **Derived projection** under the **Identity Derivation Authority** (line 162) — *not* a matrix row (no Persona row by design). The single-Identity-root rule (O1, line 162) holds; Persona is the face, not a second root. |
| **RFC-0100C** (deferred) | Does Persona need Permissions/Transactions *at the document level*? | ⚠️ **Tenth "no" — and operationally SATISFIED.** Persona is a pure projection (read Identity → emit Persona); it writes **nothing**. The only 0100C-relevant joint (single-writer at ingestion) was **already validated at M1.8** (built `ingest`, passed 30/30 with **no observed contradiction**). **No observed contradiction** → 0100C stays deferred. The *real* test (M1.8) has *run and passed* per F1–F4 (F2: observed contradiction required — none found). |

### 13.2 Holes found

- **None requiring a Foundation change at the document level.** Persona's classification as a Derived
  projection is a *reading* of RFC-0100 §1.4 ("presentation mask") + matrix line 162 (Identity owned by the
  Identity Derivation Authority, *no Persona row*) — not a new noun or a base edit. The Identity Derivation
  Authority → Persona projection boundary is a *reading* of line 162, not a new Authority. No matrix row is
  added (Persona stays a projection, not a governance unit).
- **Carried-forward clarifications (design-intent discipline, V1–V6 of `PROTOCOL-Life-Validation.md`):**
  - **V1 (anti-bypass, derived-projection form):** projection reads only Identity (immediate upstream); no
    `persona.rewrite_identity()` / `persona.patch_need()` (§5.2 C1, the second-root / god-object guards).
  - **V2 (downstream bypass, derived-projection form):** Persona writes nothing; it is a projection, not a
    sink; no `PersonaMemory` / `PersonaProfileStore` (§5.4 C3).
  - **V3 (Authority Boundary, symmetric format, derived-projection form):** *Identity Derivation Authority
    governs Identity derivation + Persona projection, not a second Identity root, not persona storage* (§6.2,
    C2). Tenth instance; first *Derived-projection-of-Identity* form. No Persona Authority added.
  - **V4 (Persistence, single Memory):** exactly one Memory store, one Memory Authority; `PersonaMemory` /
    `PersonaProfileStore` / `PersonaArchive` forbidden (§5.4, C3).
  - **V5 (Cycle, derived-projection form):** Persona is re-projected each Recall (like Identity); not stored,
    not restored (§5.4).
  - **P1 (state-type classification):** Persona = Derived projection of Identity, **not** a second root / not
    an Owned State (§4) — answers user Q1, prevents the mis-classification risk (second Identity root →
    0100C).

### 13.3 Gate verdict (FINALIZED — Challenge: APPROVED WITH CLARIFICATIONS)

> **Foundation Stable? → YES (Gate A holds; 0100C operationally satisfied).**
> RFC-0110 is consumable by M1.10 Persona Engine; no Foundation patch at the document level. Persona is
> classified as a Derived projection of Identity (not a second root, not a second Memory), the Identity
> Derivation Authority → Persona derivation boundary is preserved (no new authority, matrix unchanged), and
> 0100C stays deferred (10th consecutive confirmation) — **operationally satisfied** (the M1.8 ingest test
> already ran and found no observed contradiction). The Foundation (frozen at RFC-0105 §14) remains frozen
> *at the document level*.

---

### 13.4 Challenge (LAWP Phase C — Life Validation Protocol V1–V6) — **VERDICT: APPROVED WITH CLARIFICATIONS**

Ran `PROTOCOL-Life-Validation.md` V1–V6 (sixth Life RFC to do so; first to apply to a **Derived projection
of Identity** and the tenth to record the 0100C operational watchpoint as **satisfied**). **Verdict received:
APPROVED WITH CLARIFICATIONS.** The checks resolved as:

| Life Validation check | Proposed result | Note |
|-----------------------|----------------------|------|
| **V1 Upstream bypass (derived-projection form)** | ✅ PASS (expected) | Persona reads only Identity (immediate upstream of projection); no `persona.rewrite_identity()` / `persona.patch_need()` (§5.2, C1). |
| **V2 Downstream bypass (derived-projection form)** | ✅ PASS (expected) | Persona writes nothing; it is a projection, not a sink; no `PersonaMemory` / `PersonaProfileStore` (§5.4, C3). |
| **V3 Authority Boundary** | ✅ PASS (expected) | *Identity Derivation Authority governs Identity derivation + Persona projection, not a second Identity root, not persona storage* (§6.2, C2). Tenth symmetric instance; first *Derived-projection-of-Identity* form. No Persona Authority added. |
| **V4 Persistence (single Memory)** | ✅ PASS (expected) | Exactly one Memory store, one Memory Authority; `PersonaMemory`/`PersonaProfileStore`/`PersonaArchive` forbidden (§5.4, C3). |
| **V5 Cycle (derived-projection form)** | ✅ PASS (expected) | Persona is re-projected each Recall (like Identity); not stored, not restored (§5.4). |
| **V6 Foundation Stability** | ✅ PASS (expected) | No base edit → Stable. Persona is an existing noun (§1.4), classified as a Derived projection (a reading). **0100C: 10th deferral — operationally SATISFIED (M1.8 ingested a ReflectionResult, 30/30, no observed contradiction; Persona writes nothing, no new joint)**; C4 records the resolved watchpoint. |

**Clarifications proposed at Challenge (all F4, zero base edits):** P1 (Persona = Derived projection of
Identity, not a second root, not an Owned State); C1 (Identity Boundary: `persona.update_identity()` /
`persona.rewrite_identity()` / `persona.patch_need()` forbidden — Persona expresses only, Identity/Need/Drive
re-derive upstream; the second-root guard is the Identity-critical rule); C2 (Authority Boundary symmetric
format, derived-projection form; Identity Derivation Authority → Persona projection, **no Persona Authority**
added, matrix unchanged); C3 (Persistence Boundary: exactly one Memory; second-Memory / persona-store names
forbidden — no O1 violation); C4 (transaction/conflict watchpoint: **already tested at M1.8 with no observed
contradiction** → 0100C 10th deferral is operationally satisfied, not merely bound; Persona surfaces no new
joint as a read-side projection).

> **Challenge verdict → Foundation Stable: YES (Gate A holds; 0100C operationally satisfied).**
> All six Life Validation checks are expected to resolve without a Foundation edit at the document level.
> Persona is classified as a Derived projection of Identity (no second root, no stored projection), the
> Identity Derivation Authority → Persona derivation boundary is preserved (no new authority), and 0100C
> stays deferred — **10th consecutive confirmation, operationally satisfied** (the M1.8 ingest test already
> ran and found no observed contradiction). RFC-0110 is **anchored**; M1.10 Persona Engine unlocks (per the
> user's sequence: `RFC-0110 clarification → Anchor → M1.10 Persona Engine → Smoke Test`).

---

> **§13.4 Challenge Record (canonical).**
>
> Challenge Result: **APPROVED WITH CLARIFICATIONS**
>
> Clarifications:
> - **C1 (Identity Boundary):** Persona describes identity expression; it does not own identity. Forbidden: `persona.update_identity()` / `persona.rewrite_identity()` / `persona.patch_need()` (no second Identity root).
> - **C2 (Authority Boundary):** Persona derivation is governed by the Identity Derivation Authority; no Persona Authority is introduced (matrix unchanged — no Persona row).
> - **C3 (Persistence Boundary):** Persona derives from Memory (via Identity); it is not a second Memory. Forbidden: `PersonaMemory` / `PersonaProfileStore` / `PersonaArchive` (no O1 violation).
> - **C4 (0100C Watchpoint):** 0100C remains deferred (10th consecutive confirmation); no observed contradiction — operationally satisfied (M1.8 ingest test passed, 30/30).
>
> Foundation Patch Required: **NO (all F4, zero base edits)**
> Gate: **Foundation Stable → YES**
>
> **Anchor commit: 60b7592**

## 14. Phase 2A continuation — Foundation remains Frozen (Anchored)

RFC-0110 is **not** a freeze event — the Foundation was already frozen at RFC-0105 §14 (Phase 2A complete,
Gate A). RFC-0110 is a **continuation validation**: it proves the frozen Foundation also explains the
**Derived projection of Identity** (the `Identity → Persona` joint) with exactly one Identity root and a
single Memory authority, with no edit at the document level.

Therefore (Anchored):

1. **Phase 2A (Architect Phase) remains COMPLETE.** Foundation / Governance / Derived-States stay closed.
2. **Foundation stays FROZEN** — `RFC-0100` / `RFC-0100A` / `RFC-0100B` / `LAWP` / `Foundation Evolution` /
   `Life Validation` remain in maintenance mode (default no modification; only Foundation Evolution F1–F4
   may reopen).
3. **Agent Phase continues.** RFC-0110 is anchored; M1.10 (Persona Engine) unlocks
   — implementing `project(persona, identity) -> persona` as the *projection* act (read-only on Identity,
   emitting a Derived Persona, presenting it), with a smoke test that *is* the validation. **M1.10 is NOT the
   0100C test** (that joint — ingest — was already validated at M1.8, which passed); M1.10 only *projects*
   Identity, writing nothing. M1.10 must respect the boundary (no mutation of Identity/Need/Goal/Drive; no
   store ownership beyond the projection).
4. **RFC-0100C stays deferred** (10th consecutive confirmation at the document level; **operationally
   satisfied** — the M1.8 Memory Ingestion Interface build already ran and found no observed contradiction).
   It remains the *next* Foundation document, to be activated only by a *new* observed contradiction at a
   *future* build step (e.g. M1.10 surfacing a need for transactional consistency the current theory cannot
   express) — **not** by Persona's noun validation, and **not** by speculation in this Draft.

RFC-0110 is **Anchored** (Challenge verdict: APPROVED WITH CLARIFICATIONS). The Gate verdict is finalized; M1.10 Persona Engine
unlocks per the user's sequence.

---

## Appendix A — Validation notes

No Foundation patch is proposed at the document level. Persona's classification as a **Derived projection of
Identity** is a *reading* of an existing Ontology noun (RFC-0100 §1.4 "presentation mask") and an existing
matrix row (RFC-0100B §2 line 162, Identity owned by the Identity Derivation Authority, *no Persona row*),
not a new noun or a base edit. The Identity Derivation Authority → Persona projection boundary is a *reading*
of line 162, not a new Authority. The carried-forward V4/V5 Derived form is **applied to a projection** (read
Identity → emit Persona, not store), — recorded as a possible future `PROTOCOL-Life-Validation.md` precision
(Q6), not done inline (keeps this RFC a pure validation). The parked Constitutional hypothesis (RFC-0101
Appendix A) is untouched. The 0100C deferral is now **operationally satisfied** (the M1.8 Memory Ingestion
Interface test already ran and found no observed contradiction; Persona writes nothing, no new joint) — a
discipline milestone, not a Foundation edit. RFC-0110 is a pure validation: it proves the frozen Foundation
explains Persona as a Derived projection of Identity, with a single Identity root and a single Memory sink,
without reopening RFC-0100C — and records that the operational 0100C test (M1.8) has already been performed
and passed, so the deferral is settled at the operational level.
