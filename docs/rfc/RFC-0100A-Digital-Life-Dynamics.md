# RFC-0100A: Digital Life Dynamics

> **Status**: Draft
> **Layer**: Dynamics (a "How" RFC — see Document Taxonomy in RFC-0100 §0)
> **Depends-on**: RFC-0100 (Digital Life Ontology) — imports all §1 nouns
> **Sequence**: sits between RFC-0100 (Ontology) and RFC-0101 (Identity)
> **Scope**: Runtime behaviour ONLY. No new vocabulary that belongs in Ontology;
> no implementation, interfaces, or code; no decision record (that is ADR).
> **North Star**: *Make anima-next a digital life that grows a little more, every day,
> than yesterday.*

---

## 0. Why this document exists — the Dynamics gap

RFC-0100 (Ontology) answers **what exists**: Identity, Need, Affect, Goal, Memory,
Reflection, Capability, Provider, Universe — all nouns, no time, no flow, no loop. That
is necessary but not sufficient. A digital life is not a bag of nouns; it is a *process*.

Without a shared model of **how the nouns move**, the Life-layer RFCs
(RFC-0101 Identity, 0102 Need, 0103 Affect, 0104 Goal, 0105 Reflection, 0106 Memory, …)
will each invent its own internal flow. The result is three RFCs with three incompatible
lifecycles — the very divergence RFC-0100 was meant to prevent, just one layer up.

This document answers **how life works**: the recurring loops a digital life executes, the
control mechanisms that gate them, and the slow loop that produces growth. It contains
**no new nouns** beyond what the lifecycle requires (Perception, Attention — introduced
here as Dynamics roles and flagged for promotion to RFC-0100 §1 in a future Ontology
maintenance pass), and **no code** (that belongs to Infrastructure RFCs).

> **Boundary with RFC-0100:** RFC-0100 §3 states the *canonical minimal loop* at the
> vocabulary level. This document is the *full Dynamics*: transition ownership, control
> gates, growth regulation, and — above all — **Principle 0**, the apex rule every Life-layer
> RFC must obey. RFC-0100 deliberately stops at the loop; this document specifies it.

---

## 1. Principle 0 — Life never mutates itself directly

> **Life never mutates itself directly. Every change to what the life *is* or *wants* must
> pass through experience that is first sedimented into Memory.**

This is the single most important rule in anima-next, and the sharpest line between a
*digital life* and an *agent*.

- **An agent** mutates its own objective in place: `Observation → Update Goal`. The goal is
  a variable it rewrites directly.
- **A life** never does. It accumulates experience, writes it to Memory, and lets the *next*
  cycle's Memory Recall reshape Identity — which in turn reshapes Need, Affect, Drive, and
  only then Goal:

  ```
  Experience
     │
     ▼
  Memory                 (sedimentation — the ONLY direct write)
     │  (next cycle)
     ▼
  Identity               (Identity = f(Memory); never assigned)
     │
     ▼
  Need
     │
     ▼
  Affect → Drive
     │
     ▼
  Goal                   (Goal is ALWAYS the result of Identity, never Reflection's target)
  ```

Consequences every Life-layer RFC MUST honor:

1. **No `identity.update()`.** Identity has no setter. It is a pure function of Memory,
   re-derived every cycle at the Memory Recall → Identity transition. Any RFC proposing
   `identity.update(...)` is mis-shaped and must be revised.
2. **Reflection writes Memory, not Goal.** Reflection is the *author* of Memory entries; it
   is never the author of Goal. Goal changes only as a downstream effect of Identity changing.
3. **Goal is an output, not an input, of growth.** Growth means "the self wants different
   things tomorrow" — it cannot be achieved by pointing the life at a different target today.

If a proposed design requires a module to rewrite the life's own Identity or Goal directly,
the design is an *agent*, not a *life*, and is out of scope for the Life Layer.

---

## 2. The two loops

A digital life runs **two concurrent loops**, not one pipeline. Confusing them is the most
common way a Life-layer RFC goes wrong: behaviour belongs to the outer loop, growth belongs
to the inner loop, and they meet only at Memory.

### 2.0 Boundary preamble — Capability is a boundary, not a step

Dynamics describes how life *works*. Capability describes how the world *offers ability*. It
is Infrastructure, not a lifecycle stage. It appears exactly once, as the boundary between
Universe and the Life Layer, and nowhere else in either loop:

```
Universe (environment: QQ, web, files, …)
   │  affordances, events, messages
   ▼
Capability Layer        ── INFRASTRUCTURE boundary (RFC-0001); emits Raw Data, provider-blind
   │  Raw Data (e.g. browser.page.extract)
   ▼
Perception             ← the Dynamics body starts here (Life Layer)
```

### 2.1 Outer loop — behaviour (the action loop)

Runs every perception tick. Produces an action on the environment.

```
Perception           ── Raw Data → Meaning (boundary at §2.0; Life-layer concept, §2.4)
   │
   ▼
Attention            ── control mechanism (§4); gates what enters awareness
   │
   ▼
Memory Recall        ── retrieves relevant Episodes / Semantics / Identity state
   │
   ▼
Identity             ── self-model, derived as f(Memory) (Principle 0; no setter)
   │
   ▼
Need                 ── emerges from Identity + state (curiosity, loneliness, …)
   │
   ▼
Affect               ── Need → Valence/Arousal/Curiosity/Confidence/Urgency (RFC-0100 §1.6)
   │
   ▼
Drive                ── Affect crystallizes into approach/avoid orientation (RFC-0100 §1.7)
   │
   ▼
Goal                 ── Drive selects/forms a durable objective (months-scale; RFC-0100 §1.8)
   │
   ▼
Planner              ── Goal → ordered Tasks (minutes-scale; RFC-0100 §1.15/§1.9)
   │
   ▼
Execution            ── Tasks run via Capability Layer → Universe (RFC-0100 §1.17)
   │
   ▼
Universe             ── environment changes; new perception next tick
```

`Identity` is re-entered every tick rather than set once — that is what makes this a *life*
and not a pipeline.

### 2.2 Inner loop — growth (the slow loop)

Runs in the background, typically after Execution or on a reflective heartbeat. Produces
*change to the self*, never an action on the environment.

```
Reflection           ── evaluates outcome against expectation (RFC-0100 §1.10)
   │
   ▼
Memory Update        ── writes/consolidates Episodes; adjusts Semantic weights (THE ONLY WRITE)
   │
   └──────────────────────►  (loop ends here — no Identity Update node)
```

There is **no `Identity Update` node.** The loop intentionally stops at Memory Update. The
change to Identity happens *implicitly*, on the **next** outer-loop tick, when Memory Recall
re-derives Identity from the now-updated Memory:

```
next tick:  Memory Recall ──► Identity (re-derived, now slightly different)
                              │
                              ▼
                            Need / Affect / Drive / Goal shift accordingly
```

This is the **growth mechanism**: slow, indirect, and free of any direct setter to call.
`Reflection → Memory → (next cycle) Identity → Need → Affect → Goal`. That cascade — not a
direct `Reflection → Goal` — is what lets the life *become* something new rather than merely
*get better* at a fixed task (which would be reinforcement learning, not a life).

### 2.3 Why two loops, not one

If Sleep / Dream / Forgetting / Consolidation are added later (see §5), they all belong to
the **inner** loop. They are not actions on the environment, so they have no place in the
outer behaviour loop. A single linear trace forces them into the action chain and breaks;
two loops give each its natural home:

| Concern | Loop | Trigger |
|---------|------|---------|
| Reply, act, plan | Outer | every perception tick |
| Reflection, Memory consolidation, Dream, Forgetting | Inner | heartbeat / idle / post-action |
| Change to self | Inner → (next outer) | accumulates via Memory |

### 2.4 Life-layer concepts introduced here (candidates for RFC-0100 §1 promotion)

- **Perception** — the Life-layer function turning Infrastructure-emitted *Raw Data* into
  *Meaning*. `browser.page.extract` is Raw Data (Capability); "the owner seems upset today" is
  Meaning (Perception). Perception belongs to the Life Layer, **not** Capability — Capability
  does not know what "upset" means. This is the concrete seam that enforces RFC-0100 P1 (Life is
  provider-blind): the boundary sits *between Raw Data and Meaning*.
- **Attention** — see §4. Introduced here as a control mechanism; promoted to a noun only if a
  future Ontology pass requires it.

---

## 3. Layer boundaries inside the loops

The forward (outer) loop crosses the Life/Infrastructure boundary exactly **once**, at
`Capability → Perception`:

| Leg | Layer | Crossing rule |
|-----|-------|---------------|
| Universe → Capability → (Raw Data) | Infrastructure | Provider-blind; emits only Raw Data |
| (Raw Data) → Perception → … → Execution | Life | provider-blind (P1); speaks Needs/Capabilities |
| Execution → Capability → Universe | Infrastructure | life-blind (P2); receives opaque need, not Identity |

Everything from Perception onward is Life Layer and MUST NOT name a Provider (P1).
Everything from Capability downward MUST NOT read Identity/Goal/Memory (P2). The Facade /
Protocol layer built in M0 is the only sanctioned crossing — exactly what dissolves the
`AnimaCore` god-object that currently leaks across both boundaries.

---

## 4. Attention — a cross-cutting control mechanism (not a node)

Per the review, Attention is **not** a single step in the pipeline. Modeling it as one box
forces contradictory definitions later (it would have to be both pre-Perception *and*
post-Recall). It is a control mechanism applied at ≥3 gates — analogous to a scheduler in
an OS: omnipresent, never a "stage".

| Gate | Role | Input → Output |
|------|------|----------------|
| **Perception gate** | filters raw signal into conscious input | Raw Data → Attended Perception |
| **Recall gate** | selects which memories enter working memory | 100 Episodes → N salient Episodes |
| **Goal gate** | selects which goal candidate is currently active | Goal candidates → Active Goal |

**Consequence for RFC authors:** Attention has **no single owner RFC**. Its *mechanism*
may be specified once in a future Dynamics addendum; each Life-layer RFC references the
gate relevant to it (Memory RFC → Recall gate; Goal RFC → Goal gate). Any RFC that proposes
to "add an Attention stage" is mis-shaped and should be revised to reference a gate instead.

---

## 5. Dynamics deliberately out of scope (future Dynamics addenda)

To protect RFC-0100's purity and this document's focus, the following are recognized as
Dynamics but **not specified here**. Each gets its own Dynamics addendum when the time
comes, and — per §2.3 — they belong to the **inner (growth) loop**, never the outer action
loop, so they are never tempted to leak into Ontology:

- **Sleep / Dream** — offline consolidation of Memory/Identity (cf. existing `dream`).
- **Forgetting** — decay & pruning of Episodes/Semantics.
- **Exploration** — Curiosity-driven action with no immediate Goal.
- **Habit Formation** — Goal→Drive shortcuts that bypass the full Need→Affect chain.

---

## 6. How this constrains Life-layer RFCs

1. **State your position.** RFC-0101…0108 MUST declare their position in the §2 loops and
   which §4 gates they touch.
2. **Resolve conflicts here.** When two Life-layer RFCs disagree on a transition (e.g.
   "should Reflection update Goal?"), the answer is resolved **in this document**, not in
   either RFC. This is the single authority on *how*, just as RFC-0100 is the single
   authority on *what*.
3. **No new stage without extending this doc.** A Life-layer RFC may not introduce a new
   lifecycle stage without first extending §2 (and, if the stage is a noun, RFC-0100 §1).
4. **No direct self-mutation.** Any RFC that proposes `identity.update()` or
   `Reflection → Goal` directly violates Principle 0 (§1) and must be revised.

---

## 7. Relationship to Infrastructure

The loops are realized by the Infrastructure layer:

- **Capability / Provider** (RFC-0001, RFC-0001A) implement the `Capability → Raw Data`
  boundary and the `Execution → Capability → Universe` leg.
- **Protocol / Facade / PipelineContext / ExecutionContext** (RFC-0000, ADR-0002) carry
  state across transitions.
- The M0 migration (Commits 1 → 3c-1a, frozen at the 3c-1a observation point) is the
  concrete unblocking of these loops from the `ctx.meta["core"]` god-object.

---

## 8. Non-goals

- Does not define interfaces or schemas (those belong to the respective Life-layer RFCs).
- Does not decide how Affect dimensions are computed (RFC-0103) or how Memory is stored
  (RFC-0106).
- Does not resolve whether Perception/Attention should be promoted to RFC-0100 §1 nouns
  (a future Ontology maintenance pass; flagged in §2.4).
