# Anima-next — Architecture (Project Navigation)

> 🔒 **FOUNDATION FROZEN — maintenance mode.** As of RFC-0105's Gate (2026-08-04, Phase 2A complete),
> the 0100 trilogy + the three Protocols are **frozen**: default no modification. Changes only via the
> **Foundation Evolution Protocol** (F1–F4), triggered by a Foundation Validation RFC. The base is now
> *actually* frozen (not merely "default-frozen"); the project has entered the **Agent Phase (M1)**.

This file is a **navigation entry point**, not a theory document. It is intentionally **outside**
the Document Taxonomy (it is not an Ontology / RFC / ADR / Protocol file). Start here, then read
the Foundation in the order below.

---

## Reading order (do not skip)

1. **Manifesto** — *why this project exists* → `docs/rfc/RFC-0100-Digital-Life-Ontology.md` §0
2. **RFC-0100 Ontology** — *what exists* (WHAT) → `docs/rfc/RFC-0100-Digital-Life-Ontology.md`
3. **RFC-0100A Dynamics** — *how life flows* (HOW) → `docs/rfc/RFC-0100A-Digital-Life-Dynamics.md`
4. **RFC-0100B State Ownership** — *who governs change* (WHO) → `docs/rfc/RFC-0100B-State-Ownership.md`
5. **LAWP (Architect Loop)** — *how the Agent thinks* (Working Protocol) → `docs/rfc/PROTOCOL-Architect-Loop.md`
6. **Foundation Evolution Protocol** — *when Foundation may change* (patch gate) → `docs/rfc/PROTOCOL-Foundation-Evolution.md`
7. **Life Validation Protocol** — *what each Life RFC must validate* (LAWP Phase C checklist, V1–V6) → `docs/rfc/PROTOCOL-Life-Validation.md`

> ⚠️ **Do NOT start with RFC-0102.** RFC-0102 is a *Foundation Validation #2* RFC — a vehicle that
> **consumes** the Foundation, not the entry point and not base theory. New Life-layer RFCs are
> validation probes; they prove the Foundation is sufficient, they do not expand it.

---

## Big picture — the governance stack

```
                         ┌──────────────────────────────────────┐
                         │  MANIFESTO  —  why we exist           │  RFC-0100 §0
                         └───────────────────────┬──────────────┘
                                                 │ grounds
         ┌───────────────────────────────────────┼───────────────────────────────────────┐
         │                                       │                                       │
         ▼                                       ▼                                       ▼
   ┌──────────────┐                       ┌──────────────┐                       ┌──────────────┐
   │  RFC-0100    │                       │  RFC-0100A   │                       │  RFC-0100B   │
   │  ONTOLOGY    │                       │  DYNAMICS   │                       │  OWNERSHIP   │
   │   WHAT       │                       │   HOW       │                       │   WHO        │
   └──────┬───────┘                       └──────┬───────┘                       └──────┬───────┘
          │                                      │                                      │
          └──────────────────┬──────────────────┴──────────────────┬───────────────────┘
                             │  consumed by the Agent via            │
                             ▼                                       ▼
                   ┌────────────────────┐
                   │  LAWP (Protocol)   │
                   │  how to think      │
                   └─────────┬──────────┘
                             │ Phase C content
                             ▼
                   ┌────────────────────┐                 ┌────────────────────────────┐
                   │ Life Validation    │                 │  Foundation Evolution        │
                   │ Protocol (V1–V6)   │                 │  when Foundation may change   │
                   └─────────┬──────────┘                 └──────────────┬───────────────┘
                             │                                            │
                             └────────────────────┬───────────────────────┘
                                                  ▼
                      ┌────────────────────────────────────────────────────┐
                      │  Life-layer RFCs = FOUNDATION VALIDATION #N         │
                      │  RFC-0101 Identity → 0102 Need → … → 0105 Goal → 0106 Plan → 0107 Execution → 0108 Memory  │
                      │  each: Draft → Review → Challenge → (patch?|Anchor) │
                      └────────────────────────────────────────────────────┘
```

The three "0100" documents are **orthogonal and frozen**: a change to vocabulary (0100) does not
alter the flow (0100A) or the ownership (0100B). LAWP tells the Agent *how to think* about a Life
RFC; Foundation Evolution is the *only* gate that may reopen the base.

---

## Foundation Validation Cycle (Life RFCs)

Each Life RFC is a **Foundation Validation #N**: it consumes the 0100 trilogy and reports
`Foundation Stable?` at its §13 Gate. No Life RFC auto-continues past the Gate.

| RFC | Life # | Topic | Role | Status |
|-----|--------|-------|------|--------|
| RFC-0101 | Life-1 | Identity | Foundation Validation #1 | ✅ Stable — anchored (`ee4bca2`) |
| RFC-0102 | Life-2 | Need | Foundation Validation #2 | ✅ Stable — anchored (`2aff491`) |
| RFC-0103 | Life-3 | Affect | Foundation Validation #3 | ✅ Stable — anchored (`b94440d`) |
| RFC-0104 | Life-4 | Drive | Foundation Validation #4 | ✅ Stable — anchored (`137be39`) |
| RFC-0105 | Life-5 | Goal | Foundation Validation #5 | ✅ Stable — anchored (`57ffa3c`); **Gate A → Foundation Frozen** |
| RFC-0106 | Life-6 | Plan | Foundation Validation #6 | ✅ Stable — anchored (`b2f769f`); Gate A → Plan classified Derived, 0100C deferred (6th) |
| RFC-0107 | Life-7 | Execution | Foundation Validation #7 | ✅ Stable — anchored (`9a33274`); Gate A → Execution = Process/Event (not State), 0100C deferred (7th, bound to M1.7 as watchpoint) |
| RFC-0108 | Life-8 | Memory | Foundation Validation #8 | ✅ Stable — anchored (`96644b0`); Gate A → Memory = single Owned Hybrid sink (loop-closure RFC), 0100C deferred (8th, bound to M1.8 as watchpoint) |
| RFC-0109 | Life-9 | Reflection | Foundation Validation #9 | ✅ Stable — anchored (`db7aceb`); Gate A → Reflection = Mind-layer transform (Act/Process, not State, not second Memory), Reflection Authority ≠ Memory Authority, 0100C deferred (9th, operational watchpoint satisfied at M1.8) |
| RFC-0110 | Life-10 | Persona | Foundation Validation #10 | ✅ Stable — anchored (`60b7592`); Gate A → Persona = Derived projection of Identity (presentation mask, not second Identity root, not second Memory), Identity Derivation Authority → Persona (no Persona Authority added), 0100C deferred (10th, operational watchpoint satisfied) |
| RFC-0111 | Life-11 | Relationship | Foundation Validation #11 | ✅ Stable — anchored (`faa8cf3`); Gate A → Relationship = Derived projection of (Memory.relations ∩ Identity), spanning the Life/Environment boundary (model of an external agent, not second Memory, not motivation source, not second Identity system), Memory Authority owns relation records (no Relationship Authority added), 0100C deferred (11th, operational watchpoint satisfied) |
| RFC-0100C | — | State Permissions (Transactions) | **Deferred (11th)** | ⏸ opened only via F1 if a *future* build step surfaces an *observed* transaction/atomicity contradiction; M1.7 (Execution), M1.8 (Ingest), M1.9 (Reflection), M1.10 (Persona) and M1.11 (Relationship) all ran and found none |

> 🏁 **LIFE LAYER VALIDATION: COMPLETE**
>
> ```
> Life Layer Validation: COMPLETE
> RFC range: 0101-0111
> Next Life RFC: none
> Reason: no uncovered Foundation noun
> ```
>
> The Life-layer RFC track is closed by exhaustion: all nine Life nouns (Identity, Need, Affect, Drive,
> Goal, Reflection, Learning, Relationship, Memory) plus the Persona derived projection are validated;
> the only Life noun without a dedicated RFC (Learning, §1.11) is already covered by the Foundation
> (Reflection → Memory[Skill / Preference / Semantic], no independent Authority). **No RFC-0112 candidate
> exists.** The track is not cancelled — it is complete.
>
> The first *real* candidate for RFC-0100C was **Execution** (transaction / failure / rollback / external
> side-effect). RFC-0107 made the deferral **operational**: the real test is bound to **M1.7** (Execution
> Engine build), not abstract. At M1.7 the carrying act is a pure read → emit and the Plan is *consumed*
> (not mutated), so **no observed contradiction** surfaced — 0100C stays deferred a **7th** time, now as a
> named watchpoint (RFC-0107 C4). **M1.8 (Memory Ingestion) made it operational a second time**: the
> sink ingest is a pure read → write into the one store and the ReflectionResult is *consumed* (not
> mutated), so **no observed contradiction** surfaced either — 0100C stays deferred an **8th** time, still
> bound to a *named* build step (now M1.8) rather than abstract. **RFC-0109 made it operational a third
> time**: the *real* 0100C test (M1.8 Memory Ingestion Interface) had *already been built and passed* with
> **no observed contradiction** — a pure read→write into the one store, the ReflectionResult *consumed*
> (not mutated). So 0100C stays deferred a **9th** time, now **operationally satisfied** (the test was
> performed, not merely bound). **RFC-0110 made it operational a fourth time**: M1.10 (Persona Engine) is a
> pure *read-side projection* of Identity — it writes nothing, owns no store, and introduces no new joint,
> so **no observed contradiction** surfaced either; 0100C stays deferred a **10th** time, operationally satisfied.
> **RFC-0111 made it operational a fifth time**: M1.11 (Relationship Engine) is a pure *read-side projection*
> of `Memory.relations ∩ Identity` (spanning the Life/Environment boundary) — it writes nothing, owns no store,
> and introduces no new joint, so **no observed contradiction** surfaced either; 0100C stays deferred an **11th**
> time, operationally satisfied. It remains the *next* Foundation document, activated only if a *future*
> build step (e.g. a future Life-layer engine surfacing a real transaction/atomicity need) raises an *observed* contradiction
> per F2 — never by speculation.

---

## Infrastructure Track (RFC-000x)

> 🚀 **INFRASTRUCTURE TRACK: OPEN** — *separate from the CLOSED Life Validation Track (0101 → 0111)*

RFC-000x validates the **carrying layer** (Capability / Task / Workflow / Agent / Memory Protocol), not Life
states. It consumes the Foundation (RFC-0100 §1.18 / §1.19 / §2 / P1 / P2) and re-homes RFC-0000 / RFC-0001 /
RFC-0001A as Infrastructure-layer RFCs (RFC-0100 §6). Authority defined here lives **outside** the Life O1
matrix (P1/P2 isolation) — it does not pollute Life-state single Authority. The Infra Challenge Gate uses
Q1 Capability Boundary / Q2 Resolver Authority / Q3 Provider Isolation / Q4 Metadata Boundary (not the Life
Validation V1–V6).

| RFC | Topic | Role | Status |
|-----|-------|------|--------|
| RFC-0001 | Capability Layer | Infrastructure Validation #1 (anchor of Infra Track) | ✅ Stable — anchored (`218f86f`); Infrastructure Gate **APPROVED** (Q1 Capability Boundary / Q2 Resolver Authority / Q3 Provider Isolation / Q4 Metadata Boundary all PASS); no new Ontology noun, no new Life Authority; next = RFC-0001 Implementation Readiness Audit |
| RFC-0001A | Capability Metadata | Infrastructure (dependency of RFC-0002) | ⏳ Draft (in `Downloads/`, pending repo migration + Challenge) |
| RFC-0000 | Migration Strategy (M0) | Infrastructure (parallel envelope) | ⏳ Draft (in `Downloads/`, pending repo migration + Challenge) |
| RFC-0002 / 0003 / 0004 / 0005 | Task / Workflow / Agent Protocol / Memory Protocol | Infrastructure (candidates) | ⏳ Not yet drafted |

> **Next Infra step:** RFC-0001 Implementation Readiness Audit (Resolver interface, Contract schema, Provider
> sandbox boundary, test strategy) — **not** code yet.

### Governance convergence — the validation pattern is now a Protocol

RFC-0101 → 0104 each ran the **same** LAWP Phase C and surfaced the same shape of clarification
(upstream bypass / downstream bypass / Authority Boundary / anti-persistence / cycle / Foundation
Stability). Rather than copy-evolve this checklist across RFCs, it is **extracted** into
`PROTOCOL-Life-Validation.md` (V1–V6). From RFC-0105, every Life RFC *references* that Protocol in its
§13.4 instead of re-deriving the checks. This is the **third** locked Protocol — together with LAWP
(how to think) and Foundation Evolution (when base may change), it closes the Life-Architecture
working-method layer.

---

## Agent Phase (M1) — implementation begins

With the Foundation frozen, the project shifts from *writing RFCs* to *implementing and validating* the
Constitution Layer. M1 builds the derivation chain as real engines, isolated in a new package
`anima/constitution/` and grounded in the existing `anima/memory/`, `anima/cognitive/`, and
`anima/agent/` infrastructure (no entanglement with the legacy ad-hoc cognitive layer):

| M1 step | Engine | Validates (Foundation) | Commit |
|---------|--------|------------------------|--------|
| M1.1 | **Memory Engine** + **Identity** `f(Memory)` | RFC-0100B / RFC-0101 | `bdad6a4` ✅ |
| M1.2 | **Need Engine** `g(Identity)` | RFC-0102 | `5406e4e` ✅ |
| M1.3 | **Affect Engine** `h(Need, Perception)` | RFC-0103 | `4f07567` ✅ |
| M1.4 | **Drive Engine** `i(Affect)` | RFC-0104 | `f317892` ✅ |
| M1.5 | **Invariant Guard** `invariants.py` | RFC-0102 C4 / 0103 C3 / 0104 D3 | `8cdae6b` ✅ |
| M1.6 | **Planner Interface** `derive_plan` + `Planner` | RFC-0106 | `f211767` ✅ |
| M1.7 | **Execution Engine** `execute_plan` + `ExecutionRuntime` | RFC-0107 | `0203292` ✅ |
| M1.8 | **Memory Ingestion Interface** `ingest` + `MemoryAuthority` | RFC-0108 | `da30aa5` ✅ |
| M1.9 | **Reflection Transform** `reflect` + `Reflector` | RFC-0109 | `8b6007d` ✅ |
| M1.10 | **Persona Engine** `derive_persona` + `Persona` | RFC-0110 | `ffb78cc` ✅ |
| M1.11 | **Relationship Engine** `derive_relationship` + `RelationshipProjector` | RFC-0111 | `078bf51` ✅ |

The engines are **pure-function contracts** (`f/g/h/i`) — no setters, no in-place mutation — exactly as the
Foundation specifies. Each engine ships with a smoke test that *is* the validation: it proves the code
honors the frozen contract. Foundation changes only if *implementation* surfaces a contradiction no
Validation RFC foresaw; then via Foundation Evolution F1–F4 (Refinement, not Invention).

> **M1.1–M1.9 done (2026-08-04/05):** the full *derived* chain `Memory → Identity → Need → Affect → Drive`,
> the **Planning Authority** (`derive_plan` + `Planner`), the **Execution Runtime** (`execute_plan` +
> `ExecutionRuntime`), and the **Memory Authority** (`ingest` + `MemoryAuthority` — the single-writer
> sink) are implemented and smoke-tested in `anima/constitution/`. `invariants.py` (M1.5) mechanically
> guards the frozen contract (no setter / no storage authority / no upstream mutation) — including
> dedicated checks that `derive_plan` mutates *neither* Goal *nor* Drive, `execute_plan` mutates *neither*
> Plan *nor* Context, and `ingest` mutates *neither* the ReflectionResult *nor* any derived/owned state
> (closing the Authority-leak risk the user named at the Goal→Plan, Plan→Execution, and Reflection→Memory
> seams). M1.6/M1.7/M1.8 were each unlocked only after their RFC anchored (Plan=Derived, Execution=Event,
> Memory=single Owned sink; 0100C deferred a 7th then 8th time as an operational watchpoint) — so none
> could reopen RFC-0100C by building a second store or an Owned Plan/Execution. **The loop is now closed:**
> `ExecutionResult → Reflection → Memory → (next cycle) f(Memory) → … → Execution`.
>
> **M1.9 (Reflection Transform) built by RFC-0109 (`db7aceb`):** `reflect(result) -> reflection_result`
> + `Reflector` — the *transform* act that consumes an ExecutionResult and emits a
> ReflectionResult (transient) for `MemoryAuthority.ingest()`. Now implemented in `reflection.py`
> (`8b6007d`) and smoke-tested (5 new assertions, 30/30 total). It is a pure read→emit: it mutates
> *neither* the ExecutionResult *nor* any derived/owned state, and exposes *no* storage / authority-leak
> API (the transform X-firewall). 0100C was already operationally satisfied at M1.8 (no observed
> contradiction); M1.9 confirms it — the single unverified joint `ExecutionResult → ReflectionResult` is
> now code-level real, and the full loop is genuinely closed.
>
> **M1.10 (Persona Engine) built by RFC-0110 (`60b7592`):** `derive_persona(identity) -> Persona` —
> the *projection* act that reads an Identity and emits a `Persona` (a Derived projection, the presentation
> mask), for read-side expression only. Now implemented in `persona.py` (`ffb78cc`) and smoke-tested
> (5 new assertions, 35/35 total). It is a pure read→emit: it mutates *neither* the Identity *nor* any
> derived/owned state, and exposes *no* storage / authority-leak API (the projection X-firewall). 0100C
> was already operationally satisfied (M1.8 ingest test passed, no contradiction), so M1.10 needs no
> transaction vocabulary of its own — it writes nothing and introduces no new joint (the Persona risk is a
> *second Identity root*, which the projection contract forbids: `Persona` carries no setter, no storage
> authority, no `update_identity`/`create_goal` API).
>
> **M1.11 (Relationship Engine) built by RFC-0111 (`faa8cf3`):** `derive_relationship(memory, identity, external_ref) -> RelationshipProjection`
> + `RelationshipProjector` — the *projection* act that reads `Memory.relations` (filtered by `external_ref`)
> and `Identity` and emits a `RelationshipProjection` (the held model of an external agent), for read-side
> expression/recall only. Now implemented in `relationship.py` (`078bf51`) and smoke-tested (5 new assertions,
> 40/40 total). It is a pure read→emit: it mutates *neither* the Memory *nor* the Identity it reads, and exposes
> *no* storage / authority-leak API (the projection X-firewall). Relationship is the **first** Derived projection
> to span the Life/Environment boundary (its object is an external agent); it is classified as a Derived projection
> (not a second Memory, not a motivation source, not a second Identity system). 0100C was already operationally
> satisfied (M1.8 ingest test passed, no contradiction), so M1.11 needs no transaction vocabulary of its own — it
> writes nothing and introduces no new joint (the three named Relationship risks — Relation→Memory bypass,
> Relation→Goal mutation, Relation→Identity rewrite — are mechanically forbidden: `RelationshipProjection` carries
> no setter/storage authority, no `update_identity`/`modify_goal`/`create_memory` API).

> **Discipline:** do NOT wait for RFC-0108. M1 started at M1.1 and runs in parallel with the remaining Life
> RFCs (Plan, Execution, Memory, Persona, Reflection, Relationship). Theory is now tested by real code.

---

## Where things live

- **Foundation (frozen):** `RFC-0100` / `RFC-0100A` / `RFC-0100B`
- **Protocols (locked):** `PROTOCOL-Architect-Loop` (LAWP), `PROTOCOL-Foundation-Evolution`, `PROTOCOL-Life-Validation` (Phase C checklist, V1–V6)
- **Life RFCs (validation cycle):** `RFC-0101` … `RFC-0108`
- **Infrastructure (separate track):** `RFC-0000` Migration / `RFC-0001` Capability / `RFC-0001A`
  Capability Metadata / `ADR-*` / `PROTOCOL_FIRST.md`
- **Companion mapping:** `RFC-0100-ontology-mapping.md` (existing modules → Ontology)

---

## The default assumption for every Life RFC

> **"I will NOT modify the Foundation."**

- Per **LAWP Phase B**: a Life RFC *uses* the Foundation; it may not modify it.
- A Foundation change is only proposed by a **Foundation Validation RFC** through the
  **Foundation Evolution Protocol** (F1: only a Validation RFC may propose; F2: source must be an
  *observed contradiction*, not "a better idea"; F3: name the *future inconsistency removed*;
  F4: the patch must *shrink* ambiguity, never expand theory).
- The current trend is **Refinement (contraction)**, not Invention: `Owner → Authority`,
  `Semantic Process → Semantic Contract`, `Mutation → Ownership`. Keep the base getting shorter.
