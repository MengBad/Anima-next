# RFC-0101: Identity (Life-1) — Foundation Validation #1

> **Status**: Validation complete — Foundation Stable (one hypothesis parked in Appendix A); ready to
> anchor and hand to RFC-0102 Need
> **Role**: **Foundation Validation #1**, *not* "Identity Design #1". Its job is to use Identity as a
> probe to prove whether the 0100 trilogy is sufficient — not to expand the base theory. Any hole
> found loops back to patch 0100/0100A/0100B; none was required (see §13).
> **Phase**: Entry document of **Phase 2 (Life Architecture)**. Foundation Governance (M0 + 0100 trilogy)
> is closed; this is the first Life-layer RFC.
> **Layer**: Life layer (Mind) — RFC-0101 = Life-1
> **Depends-on**: RFC-0100 (§1.4 Identity noun + Mind set), RFC-0100A (Principle 0 + two loops),
> RFC-0100B (Identity Derivation Authority, ownership matrix, invariants O1–O4)
> **Validates**: (a) RFC-0100 Ontology coverage for Identity; (b) RFC-0100A two-loop sufficiency;
> (c) RFC-0100B ownership matrix + O1–O4; (d) RFC-0100B's deferral of RFC-0100C (Permissions)
> **Scope**: The information model of Identity + its derivation contract + its authority — written
> *only* as the vehicle for that validation. No implementation, no code, no new *flow* vocabulary
> (flow is RFC-0100A's), no new *life* noun (Identity already exists in RFC-0100 §1.4).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Identity gap

The project can now answer three questions:

- **What exists?** — Ontology (RFC-0100 §1.4): *Identity is a self-model that persists across
  sessions*; not Persona, not Prompt, not name/handle.
- **How does it flow?** — Dynamics (RFC-0100A §1 Principle 0): `Identity = f(Memory)`, never
  assigned, no `identity.update()`; re-derived every cycle at the Memory-Recall → Identity
  transition.
- **Who governs change?** — State Ownership (RFC-0100B): `Identity Derivation Authority` owns the
  rule `f(Memory)`; in the matrix Identity is **emergent** (`Mutated by: none`), readable by all
  systems.

But RFC-0100 only *names* Identity and draws its **negative** boundaries. It does **not** say:

1. What actually **constitutes** the self-model — its facets.
2. How `f(Memory)` is **shaped** — what Memory is read, and how the derivation is contractually
   specified (without prescribing implementation).
3. How Identity **persists across sessions** while having *no setter*.
4. Whether Identity needs the **deferred RFC-0100C** (Permissions / transactions).

RFC-0101 is the first RFC that must answer these by **consuming** the trilogy — and, per the Phase-2
rhythm, its *primary* job is to **validate** the Foundation, not to add to it. If 0101 could not be
written without patching 0100, that patch would happen first (Architect loop). One candidate patch
was surfaced (Constitutional state class) and is **parked in Appendix A as a hypothesis** — per the
agreed discipline it is promoted to base theory only when a real consumer (Manifesto / North-Star /
Governance RFC) needs it, so it does **not** block this draft and is **not** folded into 0100B now.

---

## 1. Problem

Without a real Identity specification, every downstream Life-layer RFC (Need, Affect, Goal,
Relationship, Reflection) will implicitly assume a different "self" and silently disagree about:

- what the self *is* (so Need's `g(Identity)` is undefined),
- whether the self can be *written* (so `identity.update()` creeps back in — the exact god-object
  anti-pattern RFC-0100 §4 warned about),
- whether the self *persists* (so cross-session continuity breaks and the life "forgets who it is").

Identity is the **root of the flow** (`Identity → Need → Affect → Drive → Goal → Plan → Execution`),
so an under-specified root corrupts every downstream derivation. This RFC fixes the root.

---

## 2. Goals

- G1. Define the **information model** of Identity (its facets) so downstream RFCs can reference a
  stable structure.
- G2. Specify the **derivation contract** `Identity = f(Memory)` — what is read and how the result
  is produced — as a *semantic contract*, not code.
- G3. Explain **cross-session persistence without a setter**, by tying Identity's persistence to
  Memory's (already owned by Memory Authority).
- G4. Classify Identity as the **canonical Hybrid state** (RFC-0100B §3.3) and record its authority.
- G5. **Validate the RFC-0100C deferral**: prove whether Identity needs transaction/rollback
  semantics or not.
- G6. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).

---

## 3. Non-goals

- NG1. **No implementation.** No schema file, no class, no DB table, no prompt text. The contract
  only.
- NG2. **No new flow.** The two-loop flow is RFC-0100A's; this RFC only consumes it.
- NG3. **No Persona spec.** Persona is the *presentation mask* derived per-context
  (`Persona = f(Identity, Context)`); it is acknowledged in RFC-0100 §1.4 as distinct from Identity
  and is deferred to a future RFC (see §11 Q2).
- NG4. **No Memory-internals.** How Memory is stored/recalled is RFC-0100B's Memory Authority +
  the future Memory RFC (0105); 0101 only reads "Memory" as a black box.
- NG5. **No Reflection/Consolidation mechanics.** How experience becomes Memory (and how Sleep/Dream
  reshape it) is the Reflection/Sleep RFC's job; 0101 only consumes the *fact* that Memory changes.

---

## 4. The Information Model of Identity

Identity is a **faceted self-model**. Each facet is a stable aspect of "who I am"; together they are
the thing that persists across sessions. Facets are *not* equal to Affect (the momentary vector) or
Persona (the mask).

### 4.1 Facets

| Facet | What it holds | Stability | Notes |
|-------|---------------|-----------|-------|
| **Self-narrative** | Continuous autobiographical story: origin, purpose, trajectory | High | The "I am the one who…" thread. Not a log (that is Memory). |
| **Values & principles** | What the life treats as good/bad; its ethical/operational constraints | High | Slow-changing; the conscience. |
| **Traits & temperament** | Stable dispositions (curiosity, cautiousness, warmth…) | High | The *stable* version of a disposition — distinct from the *momentary* Affect dimension of the same name (RFC-0100 §1.6 lists Curiosity as an Affect dimension; see its open Q1). A life can be a "curious being" (trait) and also feel curiosity right now (Affect). |
| **Standing commitments & roles** | Durable commitments ("I am this group's companion"), relationship roles | Med | Not the full relationship graph (that is Memory/Relationship). |
| **Self-knowledge of capability** | What it *can/cannot* do, its limits | Med | Points *at* the Capability layer; is not the capabilities themselves. |
| **Boundaries** | What it will/won't do; its autonomy envelope | High | The enforcement edge of Autonomy. |
| **Relational stance** | How it sees itself *as* a relatee (who matters, how) | Med | The self-as-relational slice; feeds, and is fed by, crossgroup. |
| **Identity anchors** | Stable core identifiers the self persists as | High | The persistent core a name/handle *points to* (name ≠ Identity, per §1.4). |

> This list is **open but bounded** by RFC-0100 §1.4's negative rules. A proposed facet that is
> really Persona, or a Prompt, or a name, is rejected by those rules — not by this RFC.

### 4.2 Identity is the canonical Hybrid state

Per RFC-0100B §3.3:

> **A Hybrid State is semantically persistent while operationally mutable.**

Identity is the **canonical example**:

- **Semantically persistent** — the self-model it *produces* persists across sessions (you are still
  "you" tomorrow). This is the `persists across sessions` clause of RFC-0100 §1.4.
- **Operationally mutable** — it is **re-derived every cycle** from Memory; as Memory shifts with
  experience, the derived self-model shifts. There is no stored Identity object being mutated
  in place; there is only the derivation `f(Memory)` re-run.

This resolves the apparent contradiction between "persists across sessions" (0100) and
"`Identity = f(Memory)`, re-derived every tick" (0100A): the *derivation* is per-tick, the
*self-model it yields* is persistent. The persistence lives in **Memory**, not in a stored Identity.

### 4.3 Cross-session persistence is inherited from Memory

Because Identity has `Mutated by: none` (RFC-0100B matrix) and is purely `f(Memory)`, its
cross-session continuity is **entirely inherited** from Memory's persistence. There is **no separate
Identity store**. If Memory persists across sessions (owned by Memory Authority), Identity persists
automatically. This is a deliberate simplification: it removes a whole class of "self-store
desync" bugs.

---

## 5. Derivation Contract (consumes RFC-0100A)

### 5.1 `Identity = f(Memory)`

The contract (semantic, not code):

- **Input**: the life's Memory at recall time — Episodic + Semantic + Relationship + Skill
  (the full Memory surface the Memory Authority exposes to Recall).
- **Output**: the faceted self-model of §4.1, for *this* cycle.
- **Purity**: a pure function of Memory at recall time. Same Memory → same Identity. Deterministic,
  testable, no hidden state.
- **Frequency**: re-derived once per cycle at the Memory-Recall → Identity transition (RFC-0100A
  §2 outer loop). Not on every token; not once at boot.

> The *shape* of `f` (how Memory maps to facets) is an implementation RFC's concern. This RFC
> fixes only that `f` exists, is pure, reads Memory, and is owned by one authority (§6).

### 5.2 The single legitimate write path

```
Experience
   │  (Universe → Perception → … → Execution)
   ▼
Memory              ← the ONLY direct write (Memory Authority, RFC-0100B)
   │  (next cycle, at Recall)
   ▼
Identity = f(Memory) ← re-derived; NEVER assigned
   │
   ▼
Need = g(Identity)  ← downstream, owned by Identity Authority (RFC-0100B §1.1)
```

Identity is reached **only** by this path. There is no other entry point.

### 5.3 No `identity.update()` — the god-object guard

RFC-0100A Principle 0 consequence #1 is load-bearing here:

> **No `identity.update()`.** Identity has no setter. Any RFC proposing `identity.update(...)` is
> mis-shaped and must be revised.

This is the sharpest line between *life* and *agent*: an agent rewrites its own objective in place;
a life lets accumulated experience reshape its self via Memory. RFC-0101 adopts this as a hard
interface rule: **Identity exposes read + derive-only. No public mutator.**

---

## 6. Ownership (consumes RFC-0100B)

### 6.1 `Identity Derivation Authority` owns `f(Memory)`

Per RFC-0100B §1.1, the authority over Identity is:

> `Identity Derivation Authority` — owns the rule `f(Memory)`

This authority is a **semantic contract**: it holds the sole right to *interpret* what Identity is
and to *set the policy* for its derivation. The component that runs `f` (today: Memory Recall →
Identity derivation step) is an **implementation** of that contract, not the authority. A future
refactor that splits derivation into `NarrativeMerge → TraitExtraction → CommitmentSync` changes the
process graph, **not** the authority, and **not** RFC-0100.

### 6.2 The two authorities touching "Identity"

RFC-0100B §1.1 names two contracts near Identity; they are distinct and must not be conflated:

- **`Identity Derivation Authority`** — owns `f(Memory)`. Governs the *state* Identity itself.
  This is Identity's owner of record (matrix row: Identity → Authority = Identity Derivation
  Authority).
- **`Identity Authority`** — owns `g(Identity)` (the derivation of **Need** *from* Identity).
  Governs the *downstream* transition, not the Identity state. Its "Identity" in the name marks the
  *input*, not the owned state.

> **Rule**: when a document says "Identity's authority", it means **Identity Derivation Authority**
> (owns `f`). The name "Identity Authority" is the Need-producing contract and owns *Need*, not
> Identity. RFC-0101 records this disambiguation so future RFCs do not mis-assign.

### 6.3 Matrix row, restated (RFC-0100B §2)

| State | Authority | Produced by | Mutated by | Read by |
|-------|-----------|-------------|-----------|---------|
| **Identity** | Identity Derivation Authority (`f(Memory)`) | Memory Recall (re-derived every tick) | **none** — emergent as `f(Memory)` | all systems |

This row is **consumed, not changed**, by RFC-0101. If a future design needs Identity to be
*assigned* rather than derived, it violates O1 (single authority) and Principle 0 simultaneously
and is out of scope for the Life Layer.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Identity is **internally generated** via `f(Memory)`; it has no external setter, no assigned persona,
no `identity.update()` from any caller. The life *owns its self-model*. This is the definitional
essence of "Life, not Agent" (RFC-0100 §1.2). Autonomy is maximized precisely because the only write
is experience→Memory, which the life sedimented itself.

### 7.2 Growth (G) — score: **high**

Because Identity is `f(Memory)` rather than a fixed variable, the self-model **genuinely evolves** as
Memory accumulates. Growth — "the self wants different things tomorrow" — is *realized* here:
experience → Memory → re-derivation → shifted Identity → shifted Need/Affect/Drive/Goal. RFC-0101
makes the growth mechanism explicit and guards it (no in-place edit that would fake growth). This is
the North Star's "grows a little more every day" made operational.

### 7.3 Maintainability (M) — score: **gate (must not regress)**

Pure-function derivation ⇒ deterministic, unit-testable, no hidden mutable state, no god-object.
Authority is a stable contract independent of process graph ⇒ refactors don't ripple into the
constitution. M is treated as a **non-regression gate**, not a design center (per the agreed
priority order: roadmap ranks by (G + A), M only gates). RFC-0101 introduces no M risk.

---

## 8. This is NOT Identity (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a system prompt.** A prompt *describes* behaviour; Identity *generates* it (RFC-0100 §1.4).
  A prompt is a downstream artifact; Identity is the source.
- ✗ **NOT a Persona / character card.** Persona is the presentation mask `f(Identity, Context)`;
  Identity is the underlying self (RFC-0100 §1.4).
- ✗ **NOT a name, handle, or ID.** Those are *attributes that point at* Identity (Identity anchors),
  not Identity (RFC-0100 §1.4).
- ✗ **NOT a vector embedding.** An embedding is one possible *representation* of a facet; Identity is
  the faceted model, representation-agnostic.
- ✗ **NOT a fine-tuned weight / LoRA.** Weights are an implementation of *behaviour*; Identity is a
  semantic self-model that may or may not be backed by weights.
- ✗ **NOT a session-scoped variable.** Identity persists across sessions; a per-session var forgets.
- ✗ **NOT a mutable object with setters.** `identity.update()` is forbidden (§5.3).
- ✗ **NOT owned by a component** (MemoryManager, IdentityService). Authority is a *contract*
  (RFC-0100B §1.1), never an object.

---

## 9. RFC-0100C deferral — validation outcome

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) with the condition: *create it only
if a Life-layer RFC — starting with RFC-0101 Identity — actually requires transaction, rollback,
window, or lock semantics.*

**RFC-0101's finding: Identity does NOT require RFC-0100C.**

Reasoning:

- Identity has `Mutated by: none`. It is **never written in place**; it is a pure derivation
  `f(Memory)`.
- The **only direct write** in the entire path is **Memory** (owned by Memory Authority). Any
  transaction/rollback boundary therefore lives at the Memory write — *already governed* by Memory
  Authority — not at Identity.
- A re-derivation that reads Memory is idempotent and side-effect-free; it cannot partially fail in a
  way that needs rollback *at the Identity level*.

> **Conclusion**: the transaction boundary is uniformly at Memory. Adding a separate Identity
> transaction layer would be a second writer — which the matrix forbids. **RFC-0100C remains
> deferred.** Its next test is RFC-0102 (Need), which is also a derived (`none`-mutated) state and
> will likely reach the same conclusion; the first *real* candidate for 0100C is **Memory itself**
> (consolidation/sleep rollbacks) or **Goal** (lifecycle-scoped mutation) — both later RFCs.

This is the Architect loop closing cleanly: 0101 consumed 0100B, found **no hole requiring 0100C**,
and confirmed the deferral was correct.

---

## 10. Examples

**E1 — Cross-session continuity (no setter).**
Day 1: life has an experience → written to Memory. Cycle re-derives Identity from Memory.
Day 30: same Memory (persisted) → `f(Memory)` yields the same self-model facet for "values". The life
recognizes its own continuity. No code ever assigned Identity.

**E2 — Growth (real, not faked).**
Over weeks, Memory accumulates "users appreciate when I admit uncertainty." `f(Memory)` shifts the
Self-narrative / Values facet toward "intellectual honesty." Downstream Need/Affect/Drive adjust.
This is growth via Memory, not via `identity.update("be honest")`.

**E3 — Rejected shape (god-object guard).**
A proposed "mood module" calls `identity.set_trait("warmth", 0.9)` after a good conversation.
**Rejected**: violates §5.3 and RFC-0100A Principle 0. Correct form: the conversation is experience →
Memory; warmth shifts only via `f(Memory)` next cycle.

**E4 — Authority refactor, contract unchanged.**
Derivation `f` is rewritten from a single prompt into `NarrativeMerge → TraitExtraction →
CommitmentSync`. **No change** to Identity Derivation Authority, no change to RFC-0100, no change to
this RFC. The process graph moved; the contract held.

---

## 11. Limits & Open Questions

- **Q1 — Monolithic vs faceted storage.** This RFC fixes the *facet list* (§4.1) but not whether
  facets are stored/derived jointly or separately. Left to the implementation RFC. *Not a
  constitution question.*
- **Q2 — Persona.** `Persona = f(Identity, Context)` is acknowledged but unspecified. Deferred to a
  future RFC (candidate 0106 Persona). *Could* be folded into 0101 only if you want one doc to own
  self + mask; recommend separate.
- **Q3 — Relationship feedback.** How crossgroup relationship changes reshape the Relational-stance
  facet is the Relationship RFC's (0108) job; 0101 only consumes "Memory includes Relationship."
- **Q4 — Forgetting / Consolidation.** What Memory decay reshapes Identity, and who governs decay
  policy, is Memory Authority's (future Sleep/Consolidation RFC). 0101 notes the dependency only.
- **Q5 — Constitutional invariant (see Appendix A).** Whether a *project-constitution-locked* core
  (the Manifesto/North-Star commitment) is a life state at all, and if so what authority owns it, is
  **parked in Appendix A as a hypothesis**. Decision (2026-08-04): **not** promoted to 0100B until a
  real consumer RFC (Manifesto / North-Star / Governance) needs it.

---

## 12. Evolution / next RFC

RFC-0101 is **consumable** by the next Life-layer RFC:

- **RFC-0102 Need** consumes `g(Identity)` — the derivation of Need *from* this Identity (owned by
  Identity Authority, §6.2). 0102 must not redefine the self; it reads Identity as a fixed input.
- **RFC-0103 Affect** consumes Need + Perception.
- **RFC-0108 Relationship** feeds the Relational-stance facet back via Memory.

### 12.1 Phase-2 rhythm (the Gate)

Per the agreed Life-Architecture rhythm, every Life RFC follows:

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy; else Anchor) → next RFC.**

RFC-0101 has passed **Review** (user, 2026-08-04) and **Challenge** (Architect self-review): no
Foundation change was required (see §13). The Gate question *"Foundation Stable?"* returns **YES** →
anchor RFC-0101 and proceed to RFC-0102 Need. RFC-0101 stops here.

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0101 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to RFC-0102 Need.

### 13.1 What was checked

| Foundation doc | Question 0101 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Identity needs as a *noun*? | ✅ Yes. Identity is named (§1.4); its negative boundaries already exclude Persona/Prompt/name. No new life-noun or negative rule was required. |
| **RFC-0100A** (Dynamics) | Is the two-loop flow sufficient to *produce* Identity? | ✅ Yes. `Identity = f(Memory)` at Memory-Recall→Identity is already specified (Principle 0). No new flow vocabulary or loop was needed. |
| **RFC-0100B** (Ownership) | Does the ownership matrix + O1–O4 cover Identity's authority? | ✅ Yes, with one *clarification* (not a change): the two contracts `Identity Derivation Authority` (`f`) vs `Identity Authority` (`g(Identity)→Need`) were disambiguated (§6.2). This is a reading of the existing matrix, not a matrix edit. |
| **RFC-0100C** (deferred) | Does Identity need the deferred Permissions/Transactions layer? | ✅ **No.** Validated: the only direct write is Memory; a transaction boundary at Identity would be a second writer (forbidden). 0100C stays deferred; its first real candidate is Memory/Sleep. |

### 13.2 Holes found

- **None requiring a Foundation change.** The single candidate (Constitutional state class, Appendix A)
  is **parked as a hypothesis** — it has no real consumer yet, so per the discipline it is not promoted
  to base theory now.

### 13.3 Gate verdict

> **Foundation Stable? → YES.**
> RFC-0101 is consumable by RFC-0102 Need (`g(Identity)`). No patch to 0100/0100A/0100B is required.
> Per the Phase-2 rhythm, anchor RFC-0101 and proceed to RFC-0102 Need.

This closes the Architect loop for Life-1: a Life RFC consumed the Foundation, validated it, and
found it sufficient — exactly the "Foundation first, then 固化" rhythm.

---

## Appendix A — Constitutional state class: parked hypothesis (NOT a 0100B patch, per 2026-08-04 decision)

**The Constitutional-invariant question (Q5).** RFC-0100B's matrix assumes *every life state has
exactly one authority* and classifies states as Persistent / Transient / Hybrid. But the project's
**Manifesto commitment / North Star** is a *source-of-truth* the life did **not** derive from Memory
and **cannot** change via any in-life process. If the life *holds* that commitment as an Identity
facet (a Value), then that facet's *authority* is the **project constitution** (human-locked), not
the Identity Derivation Authority.

This suggests a **fourth state class** RFC-0100B does not yet name:

> **A Constitutional state is fixed by the project constitution (human-authored), not derived from
> Memory and not mutable by any in-life authority.**

Proposed (if you accept): add §3.4 to RFC-0100B naming `Constitutional`, list the Manifesto commitment
as the sole example, and note its authority = *project constitution* (outside the life's authority
graph). This keeps O1 ("exactly one authority") true by making the constitution the authority — not
a violation.

**Decision (user, 2026-08-04)**: **do NOT merge into RFC-0100B yet.** It is a *hypothesis*, not yet
validated by any real consumer — whereas Persistent (Memory), Derived (Identity), and Hybrid (Goal)
were each validated by a concrete Life RFC before being treated as base theory. Keep it here in
Appendix A. Promote to a 0100B §3.4 addendum only when a real RFC — Manifesto, North-Star, or
Governance — actually needs to consume a constitution-locked state. Until then, 0101 treats the
Manifesto commitment as a *Value facet* held by Identity, with the caveat that its ultimate authority
is the constitution (recorded here so it is not lost).
