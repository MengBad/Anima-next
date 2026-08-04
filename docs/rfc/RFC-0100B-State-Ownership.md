# RFC-0100B: Digital Life State Ownership

> **Status**: Conceptually stable — final abstract tightening applied; at the agreed stop point (followed by RFC-0101 Identity)
> **Layer**: State Ownership (constitution / invariant layer) — the third "0100" document
> **Alternate name**: *Life State Constitution*
> **Depends-on**: RFC-0100 (all §1 nouns), RFC-0100A (flow + Principle 0)
> **Orthogonal to**: RFC-0100 (WHAT exists) and RFC-0100A (HOW it flows) — this document
> answers **WHO GOVERNS CHANGE**.
> **Scope**: authority assignment + ownership matrix + invariants ONLY. No new *life* vocabulary
> (reuses RFC-0100 §1), no flow description (reuses RFC-0100A §2), no code, no ADR.
> **North Star**: *Make anima-next a digital life that grows a little more, every day,
> than yesterday.*

---

## 0. Why this document exists — the Ownership gap

The project can now answer three questions:

- **Why exist?** — Manifesto (RFC-0100 §0).
- **What exists?** — Ontology (RFC-0100 §1): Identity, Need, Affect, Goal, Memory, …
- **How does it flow?** — Dynamics (RFC-0100A §2): the two loops, Attention gates, growth.

A fourth question is still unanswered, and it is the one a system **rots through fastest**:

> **Who owns each life state? Which authority governs its change?**

Without a single authority on *ownership*, every Life-layer RFC (Identity, Need, Memory, …)
will quietly grant its own component a setter it should not have. The result is exactly the
god-object anti-pattern RFC-0100 §4 warned about — rebuilt one layer up, as several smaller
modules that all call each other's `update()`.

This document closes that gap. It is the **formalization of RFC-0100A §1 Principle 0**, but
*elevated*: **mutation is a consequence of ownership, not its premise.**

- RFC-0100A says *"life never mutates itself directly."*
- This document asks *why it doesn't*: because **every state has exactly one authority**, and
  most authorities are derivation engines, not direct writers. Mutation is what an authority
  *may* permit; ownership is the primitive.

> **Boundary with the siblings:** RFC-0100 names the nouns; RFC-0100A describes the flow; this
> document assigns authority and licenses the writes. All three are orthogonal. A change to any
> one does not invalidate the others.

---

## 1. The State Ownership Principle (apex)

> **Every life state has exactly one authority.**

An **authority** is the **semantic contract** that exclusively governs a state's evolution — the
sole grant of the right to *interpret* the state and to set its evolution policy. Everything else
in this document — the matrix, the rules, the invariants — is a derivation of this one sentence.

### 1.1 An Authority is a semantic contract — not a process, class, module, or object

This is the soul of the document.

> **An Authority is the semantic contract that exclusively governs the evolution of one state.**

An Authority is therefore **not a process**. A *process* (the Planning Pipeline, the Reflection
Pipeline) is only one possible **implementation** of that contract. The contract is the stable
primitive; the process graph is the moving part.

- Not `MemoryManager owns Memory` (object ownership).
- Not `Identity owns Need` (module ownership).
- Not "the Planning Pipeline owns Goal" (process ownership) — the Pipeline *realizes* the Goal
  Authority; it is not the authority.
- An Authority answers *"who holds the sole right to interpret and evolve this state?"* —
  **never** *"which component holds the Python object?"* or *"which pipeline runs the step?"*

A future refactor may split the Planning Pipeline into `Need Analysis → Goal Selection → Conflict
Resolution → Priority`. That changes the *implementation*, not the authority: the **Goal
Authority** stays exactly the same. RFC-0100 (the nouns) is unchanged; only the realizing
component graph changed. **The authority (contract) is the stable primitive; the process graph is
the moving part.**

Authority names therefore name the *contract*, so a future rewrite of the component never forces a
rewrite of this document:

- `Identity Derivation Authority` — owns the rule `f(Memory)`
- `Identity Authority` — owns `g(Identity)` (Need)
- `Affect Authority` — owns `h(Need, Perception)`
- `Drive Authority` — owns `i(Affect)`
- `Goal / Planning Authority` — realized by the Planning Pipeline today
- `Memory Authority` — realized by the Reflection Pipeline today

```
Goal Authority
    ↑ implemented by
Planning Pipeline
```

If the Planning Pipeline is later split into a dozen components, the Authority is unchanged and
the Ontology does not move.

### 1.2 The layering: WHO → WHEN → HOW

Ownership is the top of a three-step derivation. **RFC-0100B stabilizes only the first step.**

```
Ontology          →  WHAT exists
Dynamics          →  HOW it flows
State Ownership   →  WHO  — which AUTHORITY governs a state      ← this document
State Permissions →  WHEN — within a transaction/lifecycle, when a write is permitted
                          (rollback, multi-agent coordination)   ← derived layer
State Mutation    →  HOW  — how the permitted change is physically applied  ← derived layer
```

- **Ownership (WHO)** — this document. The semantic authority over a state.
- **Permissions (WHEN)** — a *derived* layer: within a Planning/Reflection transaction, when a
  write is permitted, when it is forbidden, when it rolls back. **Deferred.** It will be created
  *only if* a Life-layer RFC — starting with **RFC-0101 Identity** — actually requires
  transaction, rollback, window, or lock semantics. Until then it is not assumed to exist.
- **Mutation (HOW)** — the physical application of a permitted change.

Because Ownership is the primitive and Permissions/Mutation are derived, **RFC-0100B does not
need to change when transactions, rollback, or multi-agent coordination are added** — those
extend the WHEN/HOW layers, not the WHO layer. This is the document's stability guarantee.

The hard rules formalize this hierarchy:

- **Rule 1 — Only the authority may decide how a state evolves.** No other component may
  introduce a writer for the authority's state.
- **Rule 2 — Most states expose no mutation interface.** A *derived* state (see §3) has no
  setter by construction. Attempting to add one is a category error.
- **Rule 3 — Mutation, when it exists, is indirect.** The only legitimate direct writes are
  Memory (via Memory Authority / Reflection) and Goal (via Planning Authority's lifecycle). Both
  are *owned*, intentional, and indirect relative to "the life editing itself."
- **Rule 0 (retained consequence).** *Life never mutates itself directly.* Every change to what
  the life *is* or *wants* passes through experience first sedimented into Memory
  (RFC-0100A §1). No exemption.

### 1.3 This is NOT "ownership" (negative examples)

The word *ownership* is overloaded in software. To stop newcomers from reading program semantics
into this document, the following are **explicitly out of scope** of an Authority:

- **Not Python object ownership** — `self.identity = …` is an implementation detail, not an
  Authority.
- **Not reference ownership** — who holds a pointer or handle to a state says nothing about who
  may evolve it.
- **Not mutex / lock ownership** — concurrency control is a *mechanism*, not a governance right.
- **Not memory (RAM) ownership** — which process allocates the buffer is irrelevant to semantic
  authority.
- **Not file / storage ownership** — which module writes the bytes is irrelevant; the Authority
  governs the *state's evolution policy*, never its bytes.

An Authority is a **life-semantic** contract. The moment a newcomer reaches for a setter
"because my component owns this object," they have confused program ownership with semantic
authority — exactly the god-object pattern this document exists to forbid.

---

## 2. The Ownership Matrix

For each state: **which authority owns it**, **who produces it each cycle**, **who alone may
mutate it**, and **who may read it**.

| State | Authority | Produced by | Mutated by | Read by |
|-------|-----------|-------------|-----------|---------|
| **Identity** | Identity Derivation Authority (owns `f(Memory)`) | Memory Recall (re-derived every tick) | **none** — emergent as `f(Memory)` | all systems |
| **Need** | Identity Authority (owns `g(Identity)`) | Identity (+ state) | **none** — emerges from Identity | Planner |
| **Affect** | Affect Authority (owns `h(Need, Perception)`) | Need + Perception | **none** — emerges | Planner |
| **Drive** | Drive Authority (owns `i(Affect)`) | Affect | **none** — emerges | Planner |
| **Goal** | Planning Authority (Planning Pipeline) | Planning Authority (from Need) | Planning Authority (lifecycle of this Goal only) | Execution |
| **Memory** (Episodic / Semantic / Skill / Relationship) | Memory Authority (Reflection Pipeline) | Reflection | Memory Authority (+ future Consolidation / Sleep, *by the same policy*) | Recall |
| **Reflection** | Reflection Authority | Execution Experience | Reflection Authority | all systems |

Five columns encode the constitution:

- **Authority** — the single semantic contract that exclusively governs the state's evolution
  policy. This is the primitive. For *derived* states (Identity, Need, Affect, Drive) the
  authority owns the *rule* (`f`, `g`, `h`, `i`) even though it never writes the state directly.
  **Authority is permanent — it does not change when a new writer (process) is added** (see §5).
- **Produced by** — where the state *originates* each cycle (upstream state or mechanism).
  Emergent states have no producer component; they are derived functions.
- **Mutated by** — the **sole** direct writer. Most Life states have `none`: they are *derived*,
  never assigned. Only `Memory` (via Memory Authority) and `Goal` (via Planning Authority) have a
  legitimate writer.
- **Read by** — which components may *observe* the state. Fixes the coupling direction: a reader
  depends on the state, never the reverse.

> **The key invariant:** every row whose `Mutated by` is `none` is a *derived* state owned by a
> derivation authority. A Life-layer RFC that introduces a setter for any such row is mis-shaped
> and MUST be revised. This is the Ownership Principle (§1), made checkable.

---

## 3. State Classification — Persistent / Transient / Hybrid

Life states fall into three classes. Confusing them is the root cause of *"let me just save
Identity to the DB"* — the exact mistake this document exists to prevent.

### 3.1 Persistent State (Stored)
Actually persisted across sessions:
- Episodic Memory
- Semantic Memory
- Skill Memory
- Relationship Memory

These are the durable substrate. *(Note: RFC-0100 §1.12 lists "Identity" as a Memory sub-type;
read that as the **persisted self-model substrate** from which `f` reads — the live Identity
state is Transient-derived, not a stored record. This document supersedes that sub-type's
implication; the Ontology will be aligned in a future maintenance pass.)*

### 3.2 Transient State
Recomputed or re-evaluated each cycle; never the durable record:
- **Derived Transient** — Identity (`f(Memory)`), Need (`g(Identity)`), Affect
  (`h(Need, Perception)`), Drive (`i(Affect)`): functions of upstream state, re-derived every tick.
- **Control Transient** — Attention: a transient gating signal (RFC-0100A §4), not derived from
  memory but from the current salience field; still Transient, not Persistent.

`Derived` is *one implementation* of `Transient`. If a future `Sleep Dream Buffer` appears, it is
Transient (a transient workspace) — not Derived and not Stored. The classification holds without
change.

### 3.3 Hybrid State

> **A Hybrid State is semantically persistent while operationally mutable.**

It persists as a *commitment* — owned for the lifecycle that the deliberation produced it — yet it
is not a stored record, and its authority legitimately writes it during that lifecycle.

- **Goal** — the canonical Hybrid. Owned and lifecycle-maintained by the Goal / Planning Authority;
  neither purely derived (the Planning Authority's deliberation shapes it) nor a stored memory
  record (it is the life's current commitment, not persisted as Episodic/Semantic Memory). See §4.

### 3.4 The persistence rule

> **Persist only Persistent State. Never persist a Transient (Derived or Control) state.**

Identity is **not a database field**. It is the output of `f(Memory)` recomputed at every Memory
Recall (RFC-0100A §2.2). A Life-layer RFC that provisions an "identity table" or an
`identity.save()` violates ownership: the Identity Derivation Authority owns a *derivation*, not a
record. The same holds for Need, Affect, Drive — their authorities hold the *policy*, not a store.

---

## 4. Goal is a Hybrid (Owned) State

Goal does not fit the Persistent / Transient split cleanly, so it is called out explicitly.

```
Need
  │
  ▼
Goal / Planning Authority   ← Goal's owner (semantic contract, not the Planner object)
  │
  ▼
Goal                    ← produced and lifecycle-maintained by the Planning Authority
```

- **Not Derived**: Goal is not a pure function of Need; the Planning Authority's deliberation
  shapes it.
- **Not Persistent**: Goal is not a memory record; it is the life's current commitment.
- **Owned State**: Goal is *owned and lifecycle-maintained* by the Planning Authority. The
  Authority is the sole writer (within the lifecycle of that Goal) and the sole producer for
  Execution.

This is why Goal is the one Derived-state exception that legitimately appears in `Mutated by`.
Its authority is a Cognition-layer contract (realized by a process), not a derivation engine — the
only such case in the matrix.

**Why this framing future-proofs change:** adding `Consolidation → Memory` or
`Identity Reconstruction → Identity` does **not** break any rule, because the **Authority** is
unchanged (Memory Authority / Identity Derivation Authority). New writers are permitted *by the
authority's policy*, not by rewriting the constitution. Ownership is the stable primitive;
mutation is the moving part.

---

## 5. Ownership Invariants

The architecture discipline, formalized. A design that violates any invariant is an *agent*, not
a *life*.

- **O1 — Single authority.** Every life state has exactly one authority. No state is co-owned;
  no authority shares a write right over another's state.
- **O2 — Ownership ≠ visibility.** An authority may read any state it is granted, but reading
  confers no ownership and no write right. Ownership and visibility are orthogonal.
- **O3 — Runtime execution never changes semantic authorities.** No execution path within the
  *current lifecycle* may alter which authority governs a state: the Planning Authority cannot hand
  Goal to the Reflection Authority mid-run; the Memory Authority cannot cede Memory to the Planner.
  This is scoped to the current lifecycle only — a future Lifecycle Restart, Fork, Clone, or
  Migration may re-establish authorities deliberately. What O3 forbids is *ad-hoc transfer during a
  run*, not the existence of future lifecycle-changing operations.
- **O4 — Authorities communicate only through states.** No authority calls another's mutator.
  Not `Reflection.modify(identity)`; only `Reflection → Memory → Identity = f(Memory)`. **State is
  the only inter-authority channel.** This is the largest discipline of the whole life
  architecture, and it is mechanically checkable in code.

---

## 6. Conflict resolution

When two Life-layer RFCs disagree on *who owns / may change what* (e.g. "should the Planner also
adjust Affect?" — no, per Rule 1 and RFC-0100A §4: Attention owns no state), the answer is
resolved **in this document**, not in either RFC. This is the single authority on *ownership and
the WHO assignment*, just as RFC-0100 is the authority on *what* and RFC-0100A on *how*.

Questions of *when* a permitted write may occur (lifecycle window, rollback, multi-agent
coordination) belong to the **derived Permissions layer**, which is **deferred** (not yet
specified). If RFC-0101 Identity proves it needs such semantics, a companion will be opened then —
and it will **not** change the WHO assignment here.

Any Life-layer RFC whose design requires a component to write a state outside its `Mutated by`
column, or to claim an authority it does not hold, MUST either (a) be revised to obey the matrix,
or (b) propose a change to this document via a new RFC / ADR — never by silently adding a setter.

---

## 7. Relationship to the sibling "0100" documents

| Document | Answers | This document adds |
|----------|---------|--------------------|
| RFC-0100 Ontology | *what exists* | — (nouns imported) |
| RFC-0100A Dynamics | *how it flows* | — (flow imported) |
| **RFC-0100B State Ownership** | **who governs change (WHO)** | the authority assignment + matrix + Rules 0–3 + Invariants O1–O4 |

The three are disjoint: a change to vocabulary (0100) does not alter the flow (0100A) or the
ownership (0100B); a new loop stage (0100A) does not grant new write rights (0100B) unless this
document is also amended.

> **Shared meta-principle (cited across the 0100 trilogy):** Ontology defines what exists.
> Dynamics defines how life flows. State Ownership defines who governs change.

> **Stability note:** RFC-0100B owns the **WHO** layer only. The derived **WHEN** (Permissions)
> and **HOW** (Mutation) layers are intentionally *not* formalized here. Per the agreed stop
> point, **RFC-0100C is not being written now**: the theory already forms a complete closed loop
> across Ontology (WHAT) → Dynamics (HOW) → State Ownership (WHO). A Permissions companion will be
> opened *only if* RFC-0101 Identity — the first Life-layer RFC to consume all three foundations —
> demonstrates a concrete need. Until then, transactions / rollback / multi-agent coordination are
> an *assumed future extension* that does not alter this document.

---

## 8. Non-goals

- Does not define interfaces, schemas, or storage (those belong to the respective Life-layer
  RFCs: Identity RFC-0101, Memory RFC-0106, …).
- Does not prescribe *how* Affect dimensions are computed (RFC-0103) or *how* Memory is stored
  (RFC-0106).
- Does not record a one-off decision (that is ADR); it states standing invariants.
- Does not introduce new *life* nouns into RFC-0100 §1. (`Authority` is a constitutional meta-term
  about ownership, not a life state.)
- Does **not** formalize the Permissions (WHEN) or Mutation (HOW) layers — those are derived and
  left to a future companion precisely so this document stays stable.
