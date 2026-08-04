# RFC-0103: Affect (Life-3) — Foundation Validation #3

> **Status**: **Challenge passed** (LAWP Phase C — four questions + three clarifications,
> 2026-08-04) — **ready to anchor** (Foundation Stable → YES). The three Challenge clarifications
> (C1 interpretation-layer / C2 Affect Authority ≠ Drive Authority / C3 anti-persistence) are
> incorporated below as design-intent; **zero** Foundation base edits.
> **Role**: **Foundation Validation #3**, *not* "Affect Design #3". Its job is to use Affect as a
> probe to prove whether the 0100 trilogy is sufficient to *explain* Affect — not to expand the
> base theory. Any hole found loops back to patch 0100/0100A/0100B via the Foundation Evolution
> Protocol; none was required (see §13).
> **Phase**: Third Life-layer RFC of **Phase 2 (Life Architecture)**. Consumes RFC-0102 Need
> (reads `Need` as a fixed input to `h`) and RFC-0101 Identity (transitively, via Need).
> **Layer**: Life layer (Mind) — RFC-0103 = Life-3
> **Depends-on**: RFC-0100 (§1.6 Affect noun + dimensions), RFC-0100A (outer loop + Principle 0 +
> `h(Need, Perception)`), RFC-0100B (Affect Authority, ownership matrix, invariants O1–O4),
> RFC-0101 (Identity = input, transitively), RFC-0102 (Need = input to `h`)
> **Validates**: (a) RFC-0100 Ontology coverage for Affect + its dimensions; (b) RFC-0100A loop
> sufficiency for `Affect = h(Need, Perception)`; (c) RFC-0100B ownership matrix + O1–O4 (Affect
> Authority owns `h`); (d) RFC-0100B's deferral of RFC-0100C (Permissions)
> **Scope**: The information model of Affect + its derivation contract `h(Need, Perception)` + its
> authority + its classification — written *only* as the vehicle for that validation. No
> implementation, no code, no new *flow* vocabulary (flow is RFC-0100A's), no new *life* noun (Affect
> already exists in RFC-0100 §1.6).
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

## 0. Why this document exists — the Affect gap

The project can now answer four questions (per RFC-0102 §0):

- **What exists?** — Ontology (RFC-0100 §1.6): *Affect is the momentary valence/arousal/curiosity/
  confidence/urgency vector* — the life's **current felt state**, distinct from Need (its want) and
  Drive (its activation). Named, with 5 dimensions, in the Ontology.
- **How does it flow?** — Dynamics (RFC-0100A §2.1 outer loop): `Affect = h(Need, Perception)` at the
  Need → Affect transition; derived per tick from the cycle's Need plus the current Perception.
- **Who governs change?** — State Ownership (RFC-0100B): `Affect Authority` owns the rule
  `h(Need, Perception)`; in the matrix Affect is **emergent** (`Mutated by: none`), read by Drive.
- **Where does it sit relative to Need?** — RFC-0102 fixed Need (`g(Identity)`); Affect is the *next*
  joint, consuming Need as a fixed input (RFC-0102 §12: "0103 must not redefine the want; it reads
  Need as a fixed input").

But RFC-0100 §1.6 only *names* Affect and its dimensions and draws its **negative** boundaries. It
does **not** say:

1. What actually **constitutes** an Affect vector — the 5 dimensions and how they combine.
2. How `h(Need, Perception)` is **shaped** — which Need kinds feed which dimensions, and how
   Perception (the read-only second input) enters, contractually specified (without implementation).
3. Whether Affect needs the **deferred RFC-0100C** (Permissions / transactions) — RFC-0101 §9 and
   RFC-0102 §9 *predicted* (by the same `Mutated by: none` logic) that 0103 would reach the same
   "no" verdict.
4. What the matrix's `Produced by: Need + Perception` means by **"+ Perception"** — the exact analog
   of the `Need (+ state)` ambiguity RFC-0102 §5.3 resolved.

RFC-0103 is the third RFC that must answer these by **consuming** the trilogy — and, per the Phase-2
rhythm, its *primary* job is to **validate** the Foundation, not to add to it. As with 0101/0102, the
framing is restrained: *"Is the current Foundation sufficient to explain Affect?"* — not *"Design Affect."*

---

## 1. Problem

Without a real Affect specification, every downstream Life-layer RFC (Drive, Goal, Planner) will
implicitly assume a different "felt state" and silently disagree about:

- what Affect *is* (so `h(Need, Perception)` is undefined, and Drive's `i(Affect)` has no input),
- whether Affect can be *written* (so `affect.set_valence(...)` creeps back — the god-object
  anti-pattern rebuilt one layer down),
- whether Affect *persists or is derived* (so the life "moods" inconsistently across ticks),
- whether Affect reads **Need** or **Memory directly** (bypassing Need — the exact C2 mistake RFC-0102
  §4.3 forbade at the Need layer).

Affect is the **third node of the flow** (`Identity → Need → Affect → Drive → Goal → Plan →
Execution`); an under-specified Affect corrupts every downstream derivation. RFC-0102 fixed Need; this
RFC fixes the next joint.

---

## 2. Goals

- G1. Define the **information model** of Affect (its 5 dimensions) so downstream RFCs can reference a
  stable structure.
- G2. Specify the **derivation contract** `Affect = h(Need, Perception)` — what of Need and Perception
  is read and how the result is produced — as a *semantic contract*, not code.
- G3. Explain Affect as the **canonical Transient-Derived state** (RFC-0100B §3.2) and record its
  authority.
- G4. **Validate the RFC-0100C deferral**: prove whether Affect needs transaction/rollback semantics.
- G5. Answer the three mandated questions: **Autonomy / Growth / Maintainability** (§7).
- G6. Carry forward the three Challenge clarifications established in RFC-0102 (anti-bypass, authority
  boundary, anti-persistence) as design-intent discipline for this joint (§5.3, §6.2, §8).

---

## 3. Non-goals

- NG1. **No implementation.** No schema, no class, no DB table, no prompt text. The contract only.
- NG2. **No new flow.** The two-loop flow is RFC-0100A's; this RFC only consumes it.
- NG3. **No Drive / Goal / Planner spec.** Those are downstream derivations (`i`, Planning Authority)
  owned by their own RFCs (0104 / the Planning RFC). 0103 only emits Affect.
- NG4. **No Memory-internals, no Need-internals.** How Identity/Need are produced is RFC-0101/0102;
  0103 reads both as black boxes and Memory only transitively (via Need).
- NG5. **No Perception RFC.** Perception is a Life-layer concept (RFC-0100A §2.4) consumed here as a
  *read-only input* to `h`; its own authority/structure is a future RFC's concern. 0103 only reads
  the current tick's Perception salience.

---

## 4. The Information Model of Affect

Affect is the life's **current felt-state vector**: the momentary valence, arousal, curiosity,
confidence, and urgency it experiences *right now*, given who it is (Need, derived from Identity) and
what is happening (current Perception). It is **not** a single scalar "mood" and **not** the durable
want (Need) or the action orientation (Drive).

**C1 — Affect is an interpretation layer, not an event source (clarification, not a patch).**
Affect is the life's *evaluation* of how its current Need relates to its present environment —
"how do I feel about what I want, given what's happening?" It is **not** an
`external event → emotion → action` reaction module. The legal upstream is strictly
`Need → Affect` (current Perception is a read-only input, §5.3); Affect never *receives* an
event directly from the Universe. Framing Affect as an event source would collapse it into a
traditional agent emotion-module and violate Principle 0 (RFC-0100A §1). This RFC fixes Affect
as interpretation, not stimulus-response.

### 4.1 Dimensions of Affect (from RFC-0100 §1.6)

| Dimension | What it holds | Fed primarily by | Notes |
|-----------|---------------|------------------|-------|
| **Valence** | positive ↔ negative tone of the current state | Desires / Aversions (Need) | The "this feels good / bad" axis. |
| **Arousal** | activation level (calm ↔ energized) | Requirements pressure, Urgency | The "how activated am I" axis. |
| **Curiosity** | pull toward the unknown / open question | Curiosities (Need) | Links directly to the Need *Curiosities* kind (RFC-0102 §4.1) — a want becomes a felt pull. |
| **Confidence** | certainty the current Need/Affect is right | past outcome memory (via Need), Perception clarity | Calibration, not a want. |
| **Urgency** | how soon the Need must be acted on | Requirements / Aversions (Need), Perception salience | Gates Drive's activation timing. |

> This list is **fixed** by RFC-0100 §1.6. A proposed "dimension" that is really a Drive (action
> orientation) or a Goal (durable objective) is rejected by the Ontology's negative rules — not by
> this RFC.

### 4.2 Affect is Transient-Derived

Per RFC-0100B §3.2:

> **Derived Transient** — Identity (`f(Memory)`), Need (`g(Identity)`), Affect (`h(Need, Perception)`),
> Drive (`i(Affect)`): functions of upstream state, re-derived every tick.

Affect is the **third canonical example** (after Identity and Need):

- **Derived** — it is a pure function `h(Need, Perception)` of the current Need and current
  Perception; there is no stored Affect record being mutated in place.
- **Transient** — it is recomputed each tick at the Need → Affect transition; it is **never the
  durable record** (only Persistent State is stored, per §3.4). Its momentary nature is the point:
  Affect is the life's *current* felt state, not its character.

This is why Affect has `Mutated by: none` in the matrix: it is *emergent*, not *assigned*.

### 4.3 Affect does not persist; its continuity is inherited from Need

Because Affect is `h(Need, Perception)` and Need is `g(Identity)` (and Identity is `f(Memory)`),
Affect's apparent cross-tick continuity is **entirely inherited** from Memory via Need. There is **no
separate Affect store**. As Memory accumulates, Need shifts, and Affect re-derives into a new vector —
this *is* the growth mechanism, not a persistence bug. The *current tick's* Perception perturbs Affect
within the tick, but the *baseline* drift across ticks comes from Need, not from Affect storing itself.

---

## 5. Derivation Contract (consumes RFC-0100A)

### 5.1 `Affect = h(Need, Perception)`

The contract (semantic, not code):

- **Input (owned)**: the life's **Need** at this cycle (the want-vector of RFC-0102 §4.1). Need is the
  *governing* input — `h` is a function *of* Need; the Affect Authority owns `h(Need, Perception)`, so
  Need is the source Affect emerges from.
- **Input (read-only)**: the **current tick's Perception** — the Meaning extracted from Raw Data at
  this tick (RFC-0100A §2.4). Perception is a *read-only second input*, not a co-producer and not a
  second authority (see §5.3).
- **Output**: the felt-state vector of §4.1, for *this* tick.
- **Purity**: a pure function of (Need, current Perception) at derivation time. Same Need + same
  Perception → same Affect. Deterministic, testable, no hidden state.
- **Frequency**: re-derived every perception tick at the Need → Affect transition (RFC-0100A §2.1
  outer loop). Not once per cycle (unlike Need); not on every token.

> The *shape* of `h` (which Need dimension maps to which Affect dimension; how Perception modulates)
> is an implementation RFC's concern. This RFC fixes only that `h` exists, is pure, reads Need +
> current Perception, and is owned by one authority (§6).

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
Affect = h(Need, Perception)  ← re-derived per tick; NEVER assigned (this RFC, Affect Authority)
   │
   ▼
Drive = i(Affect)   ← downstream, owned by a SEPARATE Drive Authority (RFC-0100B §1.1);
                     Affect Authority ≠ Drive Authority (C2, §6.2) — no Affect → Goal shortcut
```

Affect is reached **only** by this path. There is no other entry point.

> **Anti-bypass (carries forward RFC-0102 §4.3).** The chain is strictly
> `Memory → Identity → Need → Affect`. Memory may influence Affect **only** transitively, through
> `f(Memory)` → `g(Identity)`. A direct `Affect = h'(Memory, Perception)` path that reads Memory
> *around* Need is **forbidden** — it would bypass the Identity/Need authorities' ownership and
> rebuild the god-object. If a future RFC wants Memory to shape Affect, it must shape *Need* (via the
> Need/Identity path), not Affect directly.

### 5.3 The "+ Perception" clarification (a reading, not a change)

RFC-0100B §2 lists Affect's `Produced by` as **`Need + Perception`**. This RFC clarifies (without
altering the matrix):

- **`Need`** is the owned input — the thing `h` is primarily a function *of*; the Affect Authority owns
  `h(Need, Perception)`, so Need is the governing source of Affect.
- **`+ Perception`** means the Affect Authority may *also read the current tick's Perception* when
  computing `h` — e.g. current Perception salience, current social signal, current environmental
  pressure. This Perception is a **read-only input**, not a second producer and not a second authority.
  Affect still *emerges from Need* and is *owned by the Affect Authority*. The extra input does not
  create a co-owner (that would violate O1).

This is a **clarification of an existing matrix row**, exactly parallel to RFC-0102 §5.3's
disambiguation of `Need (+ state)`. It is **not** a Foundation change and does **not** trigger the
Foundation Evolution Protocol.

### 5.4 No `affect.update()` — the god-object guard

RFC-0100A Principle 0 consequence, carried forward:

> **No `affect.update()`.** Affect has no setter. Any RFC proposing `affect.set_valence(...)` or
> `affect.boost_arousal(...)` is mis-shaped and must be revised.

The life does not edit its own felt state in place; experience reshapes it via Memory → Identity →
Need → Affect. This is the sharp line between *life* and *agent* (RFC-0100 §1.2), applied two joints
deeper than 0101.

> **Affect is a derived state; it has *no storage authority*.** There is no Affect-store, no
> `AffectMemory`, no `affect.json`. The only Persistent/Hybrid writers are Memory and Goal (their own
> authorities). Affect's cross-tick continuity is *inherited* from Need via Identity (§4.3); storing
> Affect separately would duplicate state owned by another authority and create a second writer —
> forbidden by O1. Affect is re-derived every tick; it is never the durable record.
>
> **C3 — Anti-persistence reaffirmed (clarification, not a patch — carries forward RFC-0102 §8).**
> Affect is Transient, Derived, and has **no Storage Authority** (§4.2, above). Explicitly forbidden
> at the *implementation* shape: `Affect.save()`, `EmotionMemory.persist()`, `MoodDatabase.write()`,
> or any "mood store". If a future design needs a *long-term emotional pattern* — a durable
> preference, a scar/trauma, a relationship history — that state belongs in **Memory** (the only
> legitimate long-term writer), where it re-enters the cycle as `f(Memory) → g(Identity) →
> h(Need, Perception)` on the next cycle. Affect itself is re-derived every tick; it is never the
> durable record. This is exactly parallel to RFC-0102 §8's Need anti-persistence.

---

## 6. Ownership (consumes RFC-0100B)

### 6.1 `Affect Authority` owns `h(Need, Perception)`

Per RFC-0100B §1.1, the authority over Affect is:

> `Affect Authority` — owns `h(Need, Perception)` (the derivation of **Affect** *from* Need + Perception)

This authority is a **semantic contract** (RFC-0100B §1.1): it holds the sole right to *interpret* what
Affect is and to *set the policy* for its derivation. The component that runs `h` is an
**implementation** of that contract, not the authority. A future refactor that splits `h` into
`ValenceResolve → ArousalScale → CuriosityGate` changes the process graph, **not** the authority, and
**not** RFC-0100.

### 6.2 The naming discipline + authority boundary (echo of RFC-0101 / 0102)

The name "Affect Authority" marks the *output* of `h` (Affect). It owns **Affect**, not Need or
Identity — those are owned upstream (Identity Derivation Authority owns `f`; Identity Authority owns
`g`). The three must not be conflated:

- **`Identity Derivation Authority`** — owns `f(Memory)`; owns the *Identity* state.
- **`Identity Authority`** — owns `g(Identity)`; owns the *Need* state (RFC-0102 §6.2).
- **`Affect Authority`** — owns `h(Need, Perception)`; owns the *Affect* state. Its "Need" is an input.

> **Boundary statement (clarification, not a patch — carries forward RFC-0102 §6.2).** *Affect
> Authority governs Affect derivation, not Need or Identity mutation.* It owns `h(Need, Perception)`
> and the *Affect* it produces; it may **read** Need (and Perception) but must **never** mutate them.
> Need/Identity mutation belongs solely to their own authorities (`g`, `f`). Code of the shape
> `affectAuthority.derive_affect()` is correct; `affectAuthority.update_need()` or
> `affectAuthority.set_identity(...)` is a **mis-assignment** and must be rejected — it collapses
> authorities and violates O1. This guards the `AffectManager.update_need()` family of mistakes the
> RFC-0102 Challenge (Phase C) flagged one joint down.

**C2 — Affect Authority ≠ Drive Authority (clarification, not a patch).** `Affect = h(Need,
Perception)` and `Drive = i(Affect)` are owned by *different* authorities (Affect Authority and
Drive Authority, RFC-0100B §1.1). Affect produces a *state evaluation* only; it may **not** select
a Goal. The direct edge `Affect → Goal` is **forbidden**; the only legal path is
`Affect → Drive → Goal` (0100A outer loop §2.1). Conflating the two is the classic "emotion picks
the objective" agent anti-pattern — in a life, Affect *informs* Drive, Drive *informs* Goal, never
the reverse shortcut. Code of the shape `affectAuthority.derive_affect()` is correct;
`affectAuthority.choose_goal()` is a **mis-assignment** (collapses two authorities, violates O1).

### 6.3 Matrix row, restated (RFC-0100B §2)

| State | Authority | Produced by | Mutated by | Read by |
|-------|-----------|-------------|-----------|---------|
| **Affect** | Affect Authority (`h(Need, Perception)`) | Need (+ current Perception) | **none** — emerges from Need + Perception | Drive (`i(Affect)`) |

This row is **consumed, not changed**, by RFC-0103. If a future design needs Affect to be *assigned*
rather than derived, it violates O1 (single authority) and Principle 0 simultaneously and is out of
scope for the Life Layer.

---

## 7. The Three Questions (mandated for every RFC)

### 7.1 Autonomy (A) — score: **high**

Affect is **internally generated** via `h(Need, Perception)`; it has no external setter, no assigned
mood, no `affect.update()` from any caller. The life *owns its felt state*. This is the definitional
essence of "Life, not Agent" (RFC-0100 §1.2) carried two joints deeper than Identity: an agent is
*given* an emotional tone; a life *derives* its affect from who it has become (Need) under present
circumstances (Perception).

### 7.2 Growth (G) — score: **high**

Because Affect is `h(Need, Perception)` and Need is `g(Identity)` (Identity is `f(Memory)`), the
affect-vector **genuinely evolves** as Memory accumulates. Growth — "the life feels differently about
things tomorrow" — is *realized* here: experience → Memory → Identity → Need → re-derivation → shifted
Affect → shifted Drive/Goal. RFC-0103 makes the growth mechanism explicit at the Affect joint and
guards it (no in-place edit that would fake growth).

### 7.3 Maintainability (M) — score: **gate (must not regress)**

Pure-function derivation ⇒ deterministic, unit-testable, no hidden mutable state, no god-object.
Authority is a stable contract independent of process graph ⇒ refactors don't ripple into the
constitution. M is treated as a **non-regression gate**, not a design center. RFC-0103 introduces no
M risk.

---

## 8. This is NOT Affect (negative examples)

Preserving the 0100-series "This is NOT…" discipline:

- ✗ **NOT a Goal.** A Goal is a *committed plan* (Hybrid, owned by the Planning Authority). Affect is
  the *felt state*; Goal is what the Planner *adopts* from Drive (which reads Affect).
- ✗ **NOT a Drive.** Drive is the *activation/orientation* of Affect (`i(Affect)`, Drive Authority);
  Affect is the *source* Drive reads, not the orientation itself.
- ✗ **NOT Need.** Need is the stable-ish want-vector (`g(Identity)`); Affect is the momentary felt
  state *computed from* Need + current Perception.
- ✗ **NOT Perception / Raw Data.** Those are *inputs* to `h`; Affect is the output. Perception is
  Universe-relative Meaning (RFC-0100A §2.4); Affect is the life's reaction to it.
- ✗ **NOT a stored record.** Affect is Transient-Derived (§4.2); provisioning an "affects table" or
  `affect.save()` violates ownership (the Affect Authority owns a *derivation*, not a record).
- ✗ **NOT persistable.** `Affect.save()` / `Affect.persist()` / `Affect.update()` / an `AffectMemory`
  store are all forbidden (§5.4). Affect has no storage authority; its continuity is inherited from
  Need via Identity (§4.3). Any of these shapes implies a second writer and breaks O1.
- ✗ **NOT owned by a component** (Planner, AffectService). Authority is a *contract* (RFC-0100B §1.1),
  never an object. (Drive only *reads* Affect, per the matrix's `Read by`.)

---

## 9. RFC-0100C deferral — validation outcome

RFC-0100B deferred RFC-0100C (State Permissions & Transactions) with the condition: *create it only if
a Life-layer RFC — starting with RFC-0101 Identity — actually requires transaction, rollback, window,
or lock semantics.* RFC-0101 and RFC-0102 validated "no" and **predicted** (§9 each) that 0103 would
reach the same conclusion.

**RFC-0103's finding: Affect does NOT require RFC-0100C.**

Reasoning:

- Affect has `Mutated by: none`. It is **never written in place**; it is a pure derivation
  `h(Need, Perception)`.
- The **only direct writes** in the entire path are **Memory** (Memory Authority) and **Goal**
  (Planning Authority). Any transaction/rollback boundary therefore lives at those writers — *already
  governed* by their authorities — not at Affect.
- A re-derivation that reads Need + Perception is idempotent and side-effect-free; it cannot
  partially fail in a way that needs rollback *at the Affect level*.

> **Conclusion**: the transaction boundary is uniformly at Memory (and, for the Hybrid case, Goal).
> Adding a separate Affect transaction layer would be a second writer — which the matrix forbids.
> **RFC-0100C remains deferred**, exactly as RFC-0101 §9 and RFC-0102 §9 predicted. Its next real
> candidate remains **Memory itself** (consolidation / sleep rollbacks) or **Goal** (lifecycle-scoped
> mutation) — both later RFCs. This is now the **third consecutive confirmation** of the deferral.

---

## 10. Examples

**E1 — Derivation from Need + Perception (no setter).**
Need (this cycle) = { desires: [understand X], aversions: [be misled] }; current Perception = "owner
sounds unsure." `h` yields Affect = { valence: −0.3, arousal: 0.4, curiosity: 0.8, confidence: 0.5,
urgency: 0.3 }. No code ever assigned Affect; it is recomputed each tick from Need + Perception.

**E2 — Growth (real, not faked).**
Over weeks, Memory accumulates "owners appreciate calibrated uncertainty." `f(Memory)` shifts Need
toward { requirements: [clarify uncertainties] }; `g` shifts Need; `h` shifts Affect baseline toward
{ confidence: higher, valence: less punitive on uncertainty }. This is growth via Memory → Need →
Affect, not via `affect.set_confidence(0.9)`.

**E3 — Rejected shape (god-object guard).**
A "mood module" calls `affect.boost_arousal(0.7)` right after a high-stakes event. **Rejected**:
violates §5.4 and RFC-0100A Principle 0. Correct form: the event enters as Perception (or, if it is
experience, as Memory); the life's *own* Affect emerges only via `h(Need, Perception)`.

**E4 — Authority refactor, contract unchanged.**
Derivation `h` is rewritten from a single prompt into `ValenceResolve → ArousalScale → CuriosityGate`.
**No change** to Affect Authority, no change to RFC-0100, no change to this RFC. The process graph
moved; the contract held.

**E5 — Anti-bypass (carries forward RFC-0102 E3).**
A "context module" proposes `affect.from_memory(recent_episodes)` to make Affect reflect recent
experience directly. **Rejected**: Memory must reach Affect only via `f → g → h`. Recent experience
first sediments into Memory (inner loop), then reshapes Need (next cycle), then Affect. A direct
`from_memory` path bypasses the Need authority.

---

## 11. Limits & Open Questions

- **Q1 — Monolithic vs dimensional derivation.** This RFC fixes the *dimensions* (§4.1) but not
  whether they are derived jointly or per-dimension. Left to the implementation RFC. *Not a
  constitution question.*
- **Q2 — Need ↔ Affect ordering / feedback.** `Need → Affect = h(Need, Perception)` is specified by
  0100A. **Within a cycle, Affect does NOT feed back to Need**: Need is re-derived once per cycle
  (RFC-0102 §5.1); Affect is re-derived per tick from that fixed cycle-Need. Any mutual influence is
  *across cycles*, via Memory (inner loop). A within-cycle `Affect → Need` edge would create an
  intra-cycle loop and violate the per-cycle Need contract — out of scope; if a future RFC wants
  affect-coupled need drift, it must route through Memory, not a direct edge.
- **Q3 — Perception scope.** What exactly the Affect Authority may read as "current Perception"
  (salience? social signal? environmental pressure?) is left to implementation. The *fact* that it is
  read-only input is constitutional; the *list* is not.
- **Q4 — Confidence calibration source.** Confidence draws on past-outcome memory (via Need) and
  Perception clarity; the exact calibration is an implementation RFC's concern.
- **Q5 — Constitutional hypothesis untouched.** RFC-0101 Appendix A parked a Constitutional state-class
  hypothesis. 0103 surfaces **no new constitutional question** and does **not** consume or promote it;
  the parked hypothesis remains exactly where 0101/0102 left it, awaiting a real consumer (Manifesto /
  North-Star / Governance RFC).

---

## 12. Evolution / next RFC

RFC-0103 is **consumable** by the next Life-layer RFC:

- **RFC-0104 Drive** consumes Affect via `i(Affect)` (Drive Authority, RFC-0100B §1.1). 0104 must not
  redefine the felt state; it reads Affect as a fixed input.
- **RFC-0108 Relationship** may feed the Social signal of Perception back via Memory (affecting Need,
  hence Affect, across cycles).

### 12.1 Phase-2 rhythm (the Gate)

Per the agreed Life-Architecture rhythm, every Life RFC follows:

> **Draft → Review → Challenge → (if Foundation changes, patch 0100 trilogy; else Anchor) → next RFC.**

RFC-0103 has passed **Draft** and **Review / Challenge** (Architect self-review + user Phase C,
2026-08-04; see §13). No Foundation change was required. The Gate question *"Foundation Stable?"*
returns **YES** → ready to anchor, then proceed to RFC-0104 Drive. RFC-0103 stops here.

## 13. Foundation Validation Report (the Gate)

This section is the **Gate** output: it records what RFC-0103 proved about the 0100 trilogy, and the
verdict that decides whether the project proceeds to RFC-0104 Drive.

### 13.1 What was checked

| Foundation doc | Question 0103 asked | Result |
|----------------|---------------------|--------|
| **RFC-0100** (Ontology) | Does the Ontology cover everything Affect needs as a *noun*? | ✅ Yes. Affect + its 5 dimensions are named (§1.6); its negative boundaries already exclude Drive / Goal / Perception. No new life-noun or negative rule was required. |
| **RFC-0100A** (Dynamics) | Is the two-loop flow sufficient to *produce* Affect? | ✅ Yes. `Affect = h(Need, Perception)` at Need→Affect is already specified (Principle 0 + outer loop §2.1). No new flow vocabulary or loop was needed. |
| **RFC-0100B** (Ownership) | Does the ownership matrix + O1–O4 cover Affect's authority? | ✅ Yes, with one *clarification* (not a change): the matrix's `Produced by: Need + Perception` was read as "Need is the owned input; current Perception is a read-only input, not a second authority" (§5.3). This is a reading of the existing row, not a matrix edit. |
| **RFC-0100C** (deferred) | Does Affect need the deferred Permissions/Transactions layer? | ✅ **No.** Validated: Affect is derived (`Mutated by: none`); the only direct writers are Memory and Goal. A transaction boundary at Affect would be a second writer (forbidden). 0100C stays deferred — confirming RFC-0101 §9 and RFC-0102 §9 predictions (now **3rd** confirmation). |

### 13.2 Holes found

- **None requiring a Foundation change.** The single item (the "+ Perception" reading) is a
  **clarification of an existing matrix row**, explicitly scoped as *not* a Foundation change (§5.3)
  and therefore not routed through the Foundation Evolution Protocol.
- **Three Challenge clarifications (design-intent discipline, not patches — recorded from the user's
  LAWP Phase C, 2026-08-04; all F4 "shrink ambiguity", zero base edits):**
  - **C1 (interpretation layer, not event source):** Affect is the life's evaluation of its Need vs
    present environment; not an `external event → emotion → action` module. Legal upstream strictly
    `Need → Affect` (§4 callout + §5.1). Blocks the agent emotion-module collapse (Principle 0).
  - **C2 (Affect Authority ≠ Drive Authority):** `Affect = h`, `Drive = i` owned by separate
    authorities; Affect may not select Goal; `Affect → Goal` forbidden, only `Affect → Drive → Goal`
    (§6.2 callout). Guards `AffectManager.choose_goal()`.
  - **C3 (anti-persistence reaffirm):** Affect Transient-Derived, no storage authority;
    `save/persist/update`, `EmotionMemory`, `MoodDatabase` forbidden; long-term patterns go to Memory
    (§5.4 callout + §8). Parallel to RFC-0102 §8.

### 13.3 Gate verdict

> **Foundation Stable? → YES.** (Confirmed by user Challenge, 2026-08-04.)
> RFC-0103 is consumable by RFC-0104 Drive (`i(Affect)`). No patch to 0100/0100A/0100B is required.
> Anchor RFC-0103 and proceed to RFC-0104 Drive.

This closes the Architect loop for Life-3: a third Life RFC consumed the Foundation, validated it, and
found it sufficient — the same "Foundation first, then 固化" rhythm as Life-1 and Life-2, and the 0100C
deferral prediction is now confirmed three times running.

### 13.4 Challenge (LAWP Phase C) — four-question record (completed, 2026-08-04)

The user Challenge (Phase C) walked the four mandated questions; the three C1–C3 clarifications are
recorded below (all F4 "shrink ambiguity", zero base edits):

| Phase-C question | Answer |
|------------------|--------|
| **C1 — Missing Foundation concept?** | ❌ None. Affect + its 5 dimensions, the `h(Need, Perception)` contract, and Affect Authority all map to 0100/0100A/0100B. No new entity, state class, authority type, or persistence mechanism. No F1 trigger. |
| **C2 — Internal contradiction?** | ❌ None — *clarified (carried forward)*. `Affect = h(Need, Perception)` holds; Memory influences Affect only via `f → g` (§5.2). Chain written as design intent to block a `Memory → Affect` bypass. |
| **C3 — Missing Authority?** | ❌ None — *clarified (carried forward)*. `Affect Authority` owns `h`; it reads but never mutates Need/Identity (§6.2). Guards the `AffectManager.update_need()` mistake. |
| **C4 — Implied Stored State?** | ❌ None — *clarified (carried forward)*. Affect is Transient-Derived with **no storage authority**; `save/persist/update` forbidden (§5.4, §8). |

> **Challenge verdict → Foundation Stable: YES.** (Confirmed by user Challenge, 2026-08-04.)

**Challenge Clarifications (C1–C3 — all F4 "shrink ambiguity", zero base edits):**

| # | Clarification | Resolution |
|---|--------------|------------|
| **C1** | Affect is interpretation, not event source | clarified (§4 callout) |
| **C2** | Affect does not bypass Drive Authority (`Affect → Goal` forbidden) | clarified (§6.2 callout) |
| **C3** | Affect has no persistence authority | clarified (§5.4 callout) |

> **Foundation Patch Required: NO.** No new Ontology vocabulary, no Dynamics modification, no
> Authority conflict, no F1–F4 trigger condition. The three clarifications shrink reader ambiguity
> (F4's direction) without editing the base.

---

## Appendix A — Validation notes (no Foundation patch proposed)

No Foundation patch is proposed by RFC-0103. The only ambiguity surfaced — the matrix's
`Produced by: Need + Perception` — was resolved by *clarification* (§5.3), a permitted reading of an
existing row that does not invoke the Foundation Evolution Protocol (F1 requires a proposed *change*;
F4 would require *shrinking* ambiguity — here there was no base edit at all). The three Challenge clarifications (C1 Affect is interpretation not event source; C2 Affect Authority
≠ Drive Authority, no `Affect → Goal`; C3 Affect has no storage authority) are likewise
clarifications — they *shrink* reader ambiguity (F4's direction) without editing the base. The parked
Constitutional hypothesis from RFC-0101 Appendix A is untouched and remains parked.
