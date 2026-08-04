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
                      │  RFC-0101 Identity → 0102 Need → … → 0105 Goal → 0106 Plan     │
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
| RFC-0107 | Life-7 | Execution | Foundation Validation #7 | ⏳ pending (Plan→Execution boundary) |
| RFC-0108+ | Life-8+ | Memory / Persona / Reflection / Relationship | Foundation Validation #8+ | ⏳ pending (reordered after Plan/Execution) |
| RFC-0100C | — | State Permissions (Transactions) | **Deferred** | ⏸ opened only if a Life RFC proves a need |

> The first *real* candidate for RFC-0100C is **Memory** (consolidation / sleep rollback) or an
> **Execution** transaction need. Identity/Need/Affect/Drive/Goal/Plan (0101–0106) validate the deferral
> **six times**; Goal's lifecycle (Reading a, 0105) proves the owner is already named and Plan is Derived
> with no transaction, so 0100C stays deferred. It remains the *next* Foundation document, activated by a
> concrete Execution/Memory transaction need — **not** Plan.

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

The engines are **pure-function contracts** (`f/g/h/i`) — no setters, no in-place mutation — exactly as the
Foundation specifies. Each engine ships with a smoke test that *is* the validation: it proves the code
honors the frozen contract. Foundation changes only if *implementation* surfaces a contradiction no
Validation RFC foresaw; then via Foundation Evolution F1–F4 (Refinement, not Invention).

> **M1.1–M1.6 done (2026-08-04):** the full *derived* chain `Memory → Identity → Need → Affect → Drive`
> plus the **Planning Authority** (`derive_plan` + `Planner`) are implemented and smoke-tested in
> `anima/constitution/`. `invariants.py` (M1.5) mechanically guards the frozen contract (no setter / no
> storage authority / no upstream mutation) — including a dedicated check that `derive_plan` mutates
> *neither* Goal *nor* Drive, closing the Authority-leak risk the user named at the Goal→Plan seam. M1.6
> was unlocked only after RFC-0106 anchored (Plan classified Transient-Derived, 0100C deferred a 6th
> time), so it could not reopen RFC-0100C by building Plan as Owned.

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
