# RFC-0100: Digital Life Ontology

> **Status**: Draft — project highest-priority artifact
> **Supersedes**: none (this is the root document)
> **Related**: RFC-0000 (Migration Strategy), RFC-0001 (Capability Layer), RFC-0001A (Capability Metadata) — re-homed under this ontology as *Infrastructure-layer* RFCs (see §6)
> **Scope**: Vocabulary + worldview ONLY. No implementation, no interfaces, no code.
> **North Star**: *Make anima-next a digital life that grows a little more, every day, than yesterday.*

---

### Project Manifesto

> **anima-next 的目标不是构建一个拥有越来越多能力的 AI Bot，而是构建一个能够长期维持身份、持续学习、形成内在驱动力并不断成长的数字生命。**
>
> **所有架构决策都必须服务于这一目标。**

> *When any future RFC, ADR, or code change cannot answer "does this help anima-next
> become a digital life that grows over time, or does it merely add a capability?", its
> value should be re-examined.*

---

## 0. Why this document exists — the Ontology gap

The project now has **four** governance document systems, each with a disjoint job.
This is the **Document Taxonomy rule**: every document under `docs/` MUST be
classifiable as *exactly one* of the four below. A file that cannot answer
*"am I defining the world / the architecture / the interface / or recording a decision?"*
is malformed and MUST be split before it is merged.

| System | Answers | Example | MUST NOT contain |
|--------|---------|---------|------------------|
| **Ontology** | *what things exist in the world at all* | RFC-0100 | interfaces, code, decisions |
| **RFC** | *what* we will build and *why* | RFC-0001 Capability Layer | implementation detail, ad-hoc vocab |
| **ADR** | *which* decision we locked and *why* | ADR-0002 ExecutionContext ABI | future speculation, how-to |
| **Protocol** | *how* two parts communicate | `PipelineContextProtocol` | world-view, policy |

Ontology says what the parts *are*; Architecture (RFC) says what we build; ADR says
why we decided; Protocol says how parts talk. Without the Ontology row, `Identity`,
`Need`, `Reflection`, `Memory` each get defined ad hoc by whatever RFC authors them
first, and the definitions drift apart.
`Identity`, `Need`, `Reflection`, `Memory` each get defined ad hoc by whatever RFC
authors them first, and the definitions drift apart. This document is the **single
source of truth** for those words. From RFC-0100 onward, every other RFC MUST import
its terms from §1 and MUST NOT redefine them.

This document is deliberately a **dictionary, not a design**. It names the nouns of a
digital life. It does not prescribe classes, schemas, or control flow beyond the
canonical information loop in §3.

---

## 1. Core Concepts (the dictionary)

Every term below has exactly **one** canonical definition. Future RFCs may *specialize*
a term (narrow it for a subsystem) but may **never contradict** it. A contradiction is
an automatic reject.

### 1.1 Universe
The external world the life is embedded in: users, other agents, networks, tools,
physical or virtual environments. The Universe is **opaque** to the Life Layer — it is
reached only through the Infrastructure Layer (§2, P1).

### 1.2 Life
> A system that **continuously maintains its own state, forms a long-term identity,
> acts persistently from internal drive, and changes its future behaviour through
> experience.**

- *Not* "an agent." An agent is a role Life can occupy; Life is the thing that persists
  across roles.
- *Not* "a bot." A bot is a delivery mechanism.

### 1.3 Mind
The set of internal systems that constitute a Life's interiority: Identity, Need,
Affect, Goal, Reflection, Learning, Relationship, Memory. The Mind is what turns
Perception into Action through Drive.

### 1.4 Identity
> A self-model that persists **across sessions**.

- *Not* Persona. Persona is a presentation mask; Identity is the underlying "who I am."
- *Not* a Prompt. A prompt describes behaviour; Identity generates it.
- *Not* a name or handle. Those are attributes of Identity, not Identity itself.

### 1.5 Need
> An internally-generated pressure that has **no fixed object yet**.

- *Not* Goal. A Need ("I want to explore") precedes and may spawn many Goals; a Goal
  has a concrete target.
- *Not* a Task. A Need can last for the life's whole duration.

Example cascade:
```
Need:    I want to explore.
  ↓
Goal:    Learn Rust.
  ↓
Task:    Search for a tutorial.     (minutes)
```

### 1.6 Affect
> The continuous multi-dimensional internal state that evaluates and energizes the
> life's drives.

Affect is **not** Emotion. Emotion is one *cultural/biological expression* of Affect;
Affect is the substrate. A digital life need not have human emotions, but it always has
Affect, composed of at least these dimensions:

| Dimension | Meaning |
|-----------|---------|
| **Valence** | approach / avoid |
| **Arousal** | activation level |
| **Curiosity** | drive to explore the unknown |
| **Confidence** | certainty in current model/belief |
| **Urgency** | time-pressure on the current need |

- *Not* a mood flag. Affect is a vector, not a label.
- *Not* Emotion. Emotion is a possible *output* of Affect, never a synonym.

### 1.7 Drive
> Affect-activated intention directed toward a Goal.

Need becomes Drive only after Affect supplies valence/urgency. Drive is the bridge
between "I lack something" (Need) and "I will do something" (Goal→Plan).

Flow (refines the earlier Emotion model):
```
Identity → Need → Affect → Drive → Goal → Plan → Execution
```

### 1.8 Goal
> A concrete, adoptable target the life commits to.

- *Not* Task. Goal can persist for **months**; Task usually ends in **minutes**.
- *Not* Need. Goal answers "toward what"; Need answers "why now."

### 1.9 Task
> A minimal executable unit that advances a Goal.

- *Not* Goal. Tasks are fungible and short-lived; Goals survive task failure.

### 1.10 Reflection
> The process of using experience to **modify future strategy**.

- *Not* Summary. A summary records; Reflection reinterprets.
- *Not* Memory. Memory stores; Reflection rewrites strategy by reading Memory.
- **Critical**: Reflection writes to **Memory**, never directly to Goal (see §3).

### 1.11 Learning
> The durable change in capability or model produced by Reflection + experience.

Learning is the *outcome*; Reflection is the *act*. Skill acquisition, preference
update, and strategy revision are all Learning.

### 1.12 Memory
The persistent store of the Mind. Sub-types (each is a distinct concept, not a synonym):

| Sub-type | Holds |
|----------|-------|
| **Episodic** | specific past events (what happened, when, with whom) |
| **Semantic** | generalized facts and concepts |
| **Identity** | the self-model state (see §1.4) |
| **Preference** | liked / disliked, chosen / avoided |
| **Skill** | how-to procedures and tool patterns |
| **Relationship** | per-other-agent knowledge and history |

### 1.13 Relationship
> The model the life holds of another agent (human or bot), including history,
> trust, and role.

- *Not* a contact entry. It carries affect-laden history, not just identifiers.

### 1.14 Decision Engine
> The Mind-layer component that, given Need + Affect + Memory, selects or forms a Goal.

### 1.15 Planner
> The Cognition-layer component that decomposes a Goal into a Plan (graph of Tasks).

### 1.16 Workflow
> The executable shape of a Plan at runtime (ordering, branching, rollback).

### 1.17 Execution
> The act of carrying a Task into the Universe via the Infrastructure Layer.

### 1.18 Capability
> A declared *need-type* the life can satisfy through some Provider — e.g.
> `knowledge`, `browser`, `code`, `memory`, `vision`. The Life Layer names Capabilities;
> it never names Providers.

### 1.19 Provider
> A concrete implementation that satisfies a Capability — e.g. Playwright, NapCat,
> OneBot, a Python sandbox, a Docker container.

- *Not* part of the Life. A Provider is infrastructure; swapping it MUST NOT change
  Identity, Need, or Memory.

---

## 2. Layer Topology

The world divides into three bands plus the Universe boundary. Higher bands depend on
lower bands; lower bands never name higher-band concepts.

```
┌─────────────────────────────────────────────┐
│  UNIVERSE  (users, other agents, networks,   │
│             tools, environments)  — opaque    │
└───────────────────────┬─────────────────────┘
                         │ reached only via Infrastructure
┌───────────────────────┴─────────────────────┐
│  INFRASTRUCTURE LAYER                        │
│    Capability · Provider                     │
└───────────────────────┬─────────────────────┘
                         │
┌───────────────────────┴─────────────────────┐
│  COGNITION LAYER                            │
│    Decision Engine · Planner · Workflow ·    │
│    Execution                                 │
└───────────────────────┬─────────────────────┘
                         │
┌───────────────────────┴─────────────────────┐
│  LIFE LAYER  (the digital life)              │
│    Identity · Need · Affect · Drive · Goal · │
│    Reflection · Learning · Relationship ·    │
│    Memory                                    │
└─────────────────────────────────────────────┘
```

### Principle P1 — Life Layer is provider-blind
The Life Layer MUST NOT reference any concrete provider or runtime:
`Playwright`, `NapCat`, `OneBot`, `Python`, `Docker`, or any specific SDK.
It speaks in **Needs and Capabilities**, never in tools. A Life Layer component that
imports `napcat` is a violation.

### Principle P2 — Infrastructure is life-blind (bidirectional isolation)
Capability and Provider MUST NOT reference Life concepts:
`Identity`, `Goal`, `Memory`, `Emotion/Affect`. A Provider that reads the user's
Relationship model to decide how to send a message is a violation.

### How the boundary is enforced
The Facade / Protocol layer (built in M0) is the **only** crossing point. The Life
Layer emits an opaque `need` payload; the Capability Resolver maps it to a Provider.
Nothing from the Life Layer leaks downward except as an opaque, typed need. This is
exactly what M0's `DeprecatedCoreAccess` + Facade surface prepares the codebase for:
dissolving the `AnimaCore` god-object that currently violates P1 and P2 simultaneously.

---

## 3. Information Flow (the loop — most important chapter)

A digital life is not a set of modules. It is a **closed loop of information**.

```
Perception (from Universe)
   ↓
Memory Retrieval            (Episodic + Semantic + Identity)
   ↓
Identity
   ↓
Need
   ↓
Affect                      (Valence / Arousal / Curiosity / Confidence / Urgency)
   ↓
Drive
   ↓
Goal
   ↓
Planning (Planner)
   ↓
Execution (via Capability → Provider → Universe)
   ↓
Environment change
   ↓
Perception'  (feedback)
   ↓
Reflection
   ↓
Memory Update             ← Reflection writes HERE, not to Goal
   ↓
( Memory changes Identity )
   ↓
( Identity changes Need )
   ↓
  └────────────── back to Need (next cycle)
```

### The growth rule (do not skip)
**Reflection does NOT write to Goal. Reflection writes to Memory. Memory changes
Identity. Identity changes Need.** This cascade is what makes the system a *digital
life* rather than a *reinforcement-learning agent*:

- If Reflection → Goal directly, the life merely optimizes the current objective. That
  is RL. It never becomes something new.
- If Reflection → Memory → Identity → Need, the life's *own wants and self* shift over
  time. That is **long-term growth** — the North Star.

Any RFC that proposes a Reflection→Goal shortcut MUST justify why it is not collapsing
the life into an optimizer.

---

## 4. Existing Asset Mapping (inventory, do not invent)

The codebase already implements fragments of the Life Layer. RFC-0100's first job is to
**map, not rebuild**. The table below is the canonical mapping; later Life-layer RFCs
consume it.

| Existing module | Ontology role | Disposition |
|-----------------|---------------|-------------|
| `soul` | Identity | **Upgrade** — formalize from prompt-mask into a persisted self-model |
| `dream` | Reflection | **Keep** — already an async background reflection pass |
| `chat_memory` | Episodic Memory | **Upgrade** — wire its per-round records into the Reflection→Memory path |
| `vector_memory` | Semantic Memory | **Keep** |
| `crossgroup` | Relationship | **Upgrade** — cross-group user facts are Relationship, currently ad hoc |
| `tool_learning` | Skill Learning | **Keep** — this *is* the Learning loop |
| `bot_social` | Social Behaviour (early Autonomy) | **Keep** — emergent autonomy seed |

> **Migration navigation:** the condensed, per-row `existing module → Ontology →
> disposition` table (with a note for the next owning RFC) lives in
> `RFC-0100-ontology-mapping.md`. Every Life-layer RFC MUST consult it before designing,
> so it upgrades an existing module instead of re-inventing one.

### Anti-pattern called out
`AnimaCore` (the god-object behind `ctx.meta["core"]`) violates P1 and P2: it exposes
Memory, tools, gateway, and config as one undifferentiated surface. M0's Facade work is
the sanctioned path to dissolve it. RFC-0100 declares the target shape; M0 builds the
on-ramp.

---

## 5. Governance — how future RFCs use this document

1. **No redefinition.** A future RFC that redefines a §1 term is rejected outright.
2. **Import, don't invent.** Life-layer RFCs (Identity, Need, Goal, Reflection, Memory,
   Planner, Execution) MUST cite §1 terms by name.
3. **Three-question scoring.** Every RFC is scored **A / G / M** (Autonomy / Growth /
   Maintainability), each 1–3:
   - Roadmap ordered by **(G + A) descending** — the more a RFC makes the life more
     autonomous and more growing, the higher its priority.
   - **M is a non-regression gate** — no RFC may lower M below the prior baseline.
   - Corollary: M0 commits score **A=0, G=0, M=3** (pure foundation). Under this North
     Star that is "necessary but not the centre" — and gives a stop condition: once M0
     is stable, any RFC with A=0 and G=0 should be questioned.
4. **Priority ≠ dependency.** Life-layer RFCs have the highest *priority* but depend on
   the Infrastructure Layer (Capability, Protocol) being ready beneath them. Build
   bottom-up; rank top-down.

### Canonical RFC sequence (priority order)
```
RFC-0100  Digital Life Ontology        ← this document (root)
RFC-0101  Identity
RFC-0102  Need
RFC-0103  Affect & Drive
RFC-0104  Goal
RFC-0105  Reflection
RFC-0106  Memory
RFC-0107  Planner
RFC-0108  Execution Graph
```
Infrastructure-layer RFCs (RFC-0000 Migration, RFC-0001 Capability, RFC-0001A Metadata)
retain their numbers and are **re-classified** as dependencies of the Life Layer, not
centres of the roadmap.

---

## 6. Naming & relationship to existing RFCs

- **Title**: *Digital Life Ontology*. Alternate working name: *Digital Life Model*.
  "Ontology" is preferred over "Architecture" because this document defines *what exists*
  (Universe → Life → Mind → Identity → Need → Goal → Action), not *how parts communicate*.
- RFC-0000 (Migration) and RFC-0001 (Capability Layer) remain valid and are **not**
  replaced. They are re-homed: they describe the Infrastructure Layer that the Life Layer
  sits on. RFC-0100 is the worldview they plug into.
- SylannEngine is **one Provider / SDK among others**, not the life itself. It satisfies
  Capabilities; it does not define Identity, Need, or Growth.

---

## 7. Non-goals (what this RFC deliberately does NOT do)

- Does not specify interfaces, schemas, or protocols (those belong to the respective RFCs).
- Does not prescribe implementation order beyond the priority ranking in §5.
- Does not resolve how Affect dimensions are computed (RFC-0103).
- Does not decide storage technology for Memory (RFC-0106).

---

## 8. Open questions (for later RFCs to close)

- Q1: Is `Curiosity` a dimension of Affect or a property of Need? (Tentatively Affect.)
- Q2: Where does `Autonomy` live as a system vs an emergent property? (Treated as
  emergent in this ontology; RFC-0101 may formalize an Autonomy surface.)
- Q3: Should `Relationship` span groups (crossgroup) or be per-identity? (Current code is
  per-user cross-group; ontology keeps it per-other-agent.)
